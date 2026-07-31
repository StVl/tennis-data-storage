#!/usr/bin/env python3
"""Миграция данных из JSON-шардов (data/*.json) в Postgres.

Использование:
  DATABASE_URL=postgres://... python3 scripts/migrate_data.py            # залить
  DATABASE_URL=postgres://... python3 scripts/migrate_data.py --dry-run  # прогон без commit

Схема должна быть уже применена (scripts/apply_schema.py).
Скрипт ИДЕМПОТЕНТЕН: игроки/турниры/матчи апсертятся по slug / import_key,
повторный запуск не создаёт дублей.

Что делает:
  1. play_styles из data/reference.json (тексты под ключом "en")
  2. 102 игрока ростера (is_tracked=true) + соперники вне ростера (is_tracked=false)
  3. tournaments + tournament_editions (2026) из data/tournaments_*.json;
     досоздаёт Stuttgart и Libema, которых нет в шардах (даты из матчей)
  4. tournament_entries из массивов players[]
  5. Матчи: дедупликация зеркальных записей (два JSON-объекта на один матч),
     парсинг счёта в match_sets, ret./W.O. -> outcome
  6. ranking_snapshots: текущий снапшот + синтетический на начало сезона
     (rank = current + seasonDelta; старое поле trend не переносится --
     оно противоречит seasonDelta и вычисляется заново из снапшотов)
  7. Валидация: счётчики и сверка зеркальности
"""

import argparse
import datetime
import json
import os
import re
import sys
from collections import defaultdict
from pathlib import Path

import psycopg
from psycopg.types.json import Jsonb

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"

SEASON_START = datetime.date(2026, 1, 1)

# Строка турнира в матчах -> slug турнира. Названия в матчах расходятся
# с шардом турниров (French Open == Roland Garros и т.п.) -- ручная таблица
# надёжнее автоматики по подстрокам.
TOURNAMENT_ALIASES = {
    "Wimbledon 2026": "wimbledon",
    "Roland Garros 2026": "roland_garros",
    "French Open 2026": "roland_garros",
    "Internazionali BNL d'Italia 2026": "rome",
    "Rome Masters 2026": "rome",
    "Mallorca Championships 2026": "mallorca",
    "Libema Open 2026": "libema",
    "BOSS Open Stuttgart 2026": "stuttgart",
    "Lexus Eastbourne Open 2026": "eastbourne",
    "Generali Open Kitzbuhel 2026": "kitzbuhel",
    "Swiss Open Gstaad 2026": "gstaad",
    "Mubadala Citi DC Open 2026": "washington",
    "Monte-Carlo Masters 2026": "monte_carlo",
    "Swedish Open Bastad 2026": "bastad",
    "Nordea Open Bastad 2026": "bastad",
    "Croatia Open Umag 2026": "umag",
    "BMW Open Munich 2026": "munich",
    "Mutua Madrid Open 2026": "madrid",
    "Barcelona Open Banc Sabadell 2026": "barcelona",
    "Mifel Open Los Cabos 2026": "los_cabos",
}

# Турниры, у которых есть матчи, но нет записи в tournaments_*.json.
# Даты возьмём из min/max startAt их матчей.
MISSING_TOURNAMENTS = {
    "stuttgart": {"name": "BOSS Open", "surface": "grass", "location": "Stuttgart, Germany"},
    "libema": {"name": "Libema Open", "surface": "grass", "location": "'s-Hertogenbosch, Netherlands"},
}

SET_RE = re.compile(r"^(\d+)-(\d+)(?:\((\d+|\?)\))?$")
RET_MARKERS = ("ret.", "(retired)", "(opponent retired)", "retired")
WO_MARKERS = ("w/o", "(walkover)", "walkover")


def load(name):
    return json.loads((DATA / f"{name}.json").read_text(encoding="utf-8"))


def normalize_surface(court_type):
    """'indoor_hard' -- это hard в зале: покрытие отдельно, зал -- в conditions."""
    if court_type == "indoor_hard":
        return "hard", True
    return court_type, False


def humanize(slug):
    parts = slug.split("_")
    return " ".join(p.upper() + "." if len(p) == 1 else p.capitalize() for p in parts)


def split_name(display_name):
    if " " in display_name:
        first, last = display_name.rsplit(" ", 1)
        return first, last
    return None, display_name


def parse_score(score, warn):
    """'6-4, 7-6(5), 1-3 ret.' -> (sets, outcome).
    sets: [(games_a, games_b, tiebreak|None)] с точки зрения записи-источника."""
    s = (score or "").strip()
    if not s:
        return [], "normal"
    low = s.lower()
    if any(m in low for m in WO_MARKERS):
        return [], "walkover"

    outcome = "normal"
    sets = []
    for raw_tok in s.split(","):
        tok = raw_tok.strip()
        if not tok:
            continue
        low_tok = tok.lower()
        for m in RET_MARKERS:
            if low_tok.endswith(m):
                outcome = "retirement"
                tok = tok[: len(tok) - len(m)].strip()
                break
        if not tok:
            continue
        m = SET_RE.match(tok)
        if not m:
            warn.append(f"не распарсился фрагмент счёта: '{raw_tok.strip()}' в '{score}'")
            continue
        a, b = int(m.group(1)), int(m.group(2))
        tb = m.group(3)
        tb = None if tb in (None, "?") else int(tb)
        sets.append((a, b, tb))
    return sets, outcome


# ---------------------------------------------------------------------------
# Шаги миграции
# ---------------------------------------------------------------------------

def upsert_play_styles(cur, reference):
    for st in reference["playStyles"]:
        cur.execute(
            """insert into play_styles (slug, name, description)
               values (%s, %s, %s)
               on conflict (slug) do update
                 set name = excluded.name, description = excluded.description""",
            (st["id"], Jsonb({"en": st["name"]}), Jsonb({"en": st["description"]})),
        )
    print(f"[ok] play_styles: {len(reference['playStyles'])}")


def upsert_players(cur, players_shard, all_matches):
    style_ids = dict(cur.execute("select slug, id from play_styles").fetchall())

    roster = players_shard["players"]
    for p in roster:
        first, last = split_name(p["name"])
        photo = p.get("avatar_url") or p.get("avata_url")  # avata_url -- опечатка в данных
        cur.execute(
            """insert into players (slug, first_name, last_name, display_name,
                                    photo_url, play_style_id, traits, links, is_tracked)
               values (%s, %s, %s, %s, %s, %s, %s, %s, true)
               on conflict (slug) do update set
                 first_name = excluded.first_name,
                 last_name = excluded.last_name,
                 display_name = excluded.display_name,
                 photo_url = coalesce(excluded.photo_url, players.photo_url),
                 play_style_id = excluded.play_style_id,
                 traits = excluded.traits,
                 links = excluded.links,
                 is_tracked = true""",
            (p["id"], first, last, p["name"], photo,
             style_ids.get(p["playStyleId"]),
             Jsonb({"ru": p.get("temperament", [])}),
             Jsonb(p.get("links", {}))),
        )

    roster_slugs = {p["id"] for p in roster}
    outsiders = sorted(
        {m["opponentId"] for m in all_matches
         if m.get("opponentId") and m["opponentId"] != "TBD"} - roster_slugs
    )
    for slug in outsiders:
        first, last = split_name(humanize(slug))
        cur.execute(
            """insert into players (slug, first_name, last_name, display_name, is_tracked)
               values (%s, %s, %s, %s, false)
               on conflict (slug) do nothing""",
            (slug, first, last, humanize(slug)),
        )
    print(f"[ok] players: {len(roster)} ростер + {len(outsiders)} соперников вне ростера")
    return {slug: pid for slug, pid in cur.execute("select slug, id from players")}


def upsert_tournaments(cur, tournaments, all_matches, player_ids):
    # даты матчей по slug'у турнира -- для досоздания отсутствующих
    match_dates = defaultdict(list)
    for m in all_matches:
        slug = TOURNAMENT_ALIASES.get(m["tournament"])
        if slug and m.get("startAt"):
            match_dates[slug].append(m["startAt"][:10])

    known = []
    for t in tournaments:
        known.append(t["tournament"])
        surface, indoor = normalize_surface(t["courtType"])
        cur.execute(
            """insert into tournaments (slug, name, default_surface, conditions)
               values (%s, %s, %s, %s)
               on conflict (slug) do update
                 set name = excluded.name,
                     default_surface = excluded.default_surface,
                     conditions = excluded.conditions""",
            (t["tournament"], t["name"], surface,
             Jsonb({"indoor": True} if indoor else {})),
        )
        cur.execute(
            """insert into tournament_editions
                 (tournament_id, year, slug, start_date, end_date, surface,
                  champion_id, runner_up_id)
               values ((select id from tournaments where slug = %s),
                       2026, %s, %s, %s, %s, %s, %s)
               on conflict (slug) do update set
                 start_date = excluded.start_date,
                 end_date = excluded.end_date,
                 surface = excluded.surface,
                 champion_id = excluded.champion_id,
                 runner_up_id = excluded.runner_up_id""",
            (t["tournament"], f"{t['tournament']}_2026",
             t["dates"]["start"], t["dates"]["end"], surface,
             player_ids.get(t.get("winner")), player_ids.get(t.get("finalist"))),
        )
        for i, slug in enumerate(t.get("players", []), start=1):
            if slug not in player_ids:
                continue
            cur.execute(
                """insert into tournament_entries (edition_id, player_id)
                   values ((select id from tournament_editions where slug = %s), %s)
                   on conflict do nothing""",
                (f"{t['tournament']}_2026", player_ids[slug]),
            )

    for slug, info in MISSING_TOURNAMENTS.items():
        dates = sorted(match_dates.get(slug, []))
        if not dates:
            print(f"[warn] нет матчей для досоздаваемого турнира {slug}, пропуск")
            continue
        cur.execute(
            """insert into tournaments (slug, name, location, default_surface)
               values (%s, %s, %s, %s) on conflict (slug) do nothing""",
            (slug, info["name"], info["location"], info["surface"]),
        )
        cur.execute(
            """insert into tournament_editions
                 (tournament_id, year, slug, start_date, end_date, surface)
               values ((select id from tournaments where slug = %s),
                       2026, %s, %s, %s, %s)
               on conflict (slug) do update
                 set start_date = excluded.start_date, end_date = excluded.end_date""",
            (slug, f"{slug}_2026", dates[0], dates[-1], info["surface"]),
        )
    print(f"[ok] tournaments: {len(known)} из шардов + {len(MISSING_TOURNAMENTS)} досозданы")


def migrate_matches(cur, up, past, player_ids, warn):
    """Дедупликация зеркальных записей и заливка matches/participants/sets."""
    edition_ids = dict(cur.execute("select slug, id from tournament_editions").fetchall())

    groups = defaultdict(list)  # канонический ключ -> [raw записи]
    skipped = []
    for m in up + past:
        slug = TOURNAMENT_ALIASES.get(m["tournament"])
        if not slug or f"{slug}_2026" not in edition_ids:
            skipped.append(m["id"])
            continue
        pair = frozenset(
            x for x in (m["playerId"], m.get("opponentId")) if x and x != "TBD"
        )
        groups[(slug, m["stage"], m.get("startAt"), pair)].append(m)

    if skipped:
        warn.append(f"матчи без турнира (пропущены): {skipped}")

    n_pairs = n_singles = 0
    for (slug, stage, start_at, pair), recs in sorted(
        groups.items(), key=lambda kv: str(kv[0])
    ):
        if len(recs) > 2:
            warn.append(f"группа >2 записей, пропуск: {[r['id'] for r in recs]}")
            continue
        if len(recs) == 2:
            n_pairs += 1
            a, b = recs
            ra, rb = a.get("result", ""), b.get("result", "")
            if ra and rb and {ra, rb} != {"won", "lost"}:
                warn.append(f"зеркальные записи не инвертированы: {a['id']} / {b['id']}")
        else:
            n_singles += 1

        # side1 = лексикографически меньший слаг из известных участников
        known = sorted(pair)
        side1_slug = known[0] if known else None
        side2_slug = known[1] if len(known) > 1 else None
        if side1_slug is None:
            warn.append(f"матч без единого известного участника: {[r['id'] for r in recs]}")
            continue

        # перспектива side1: запись, где playerId == side1
        persp = next((r for r in recs if r["playerId"] == side1_slug), recs[0])
        persp_is_side1 = persp["playerId"] == side1_slug

        raw_status = persp.get("status")
        if raw_status == "completed":
            status = "completed"
        elif persp.get("isLive"):
            status = "live"
        else:
            status = "scheduled"

        sets, outcome = parse_score(persp.get("score"), warn)
        if not persp_is_side1:  # счёт задан с точки зрения другой стороны
            sets = [(b_, a_, tb) for a_, b_, tb in sets]

        winner_side = None
        res = persp.get("result", "")
        if res in ("won", "lost"):
            won = res == "won"
            if not persp_is_side1:
                won = not won
            winner_side = 1 if won else 2
        if status == "completed" and winner_side is None and outcome == "normal":
            # победитель неизвестен, счёта нет -- в данных это walkover'ы
            outcome = "walkover"

        live_state = {}
        if status == "live" and persp.get("liveScore"):
            live_state = {"score": persp["liveScore"]}

        import_key = min(r["id"] for r in recs)
        cur.execute(
            """insert into matches (edition_id, round_code, scheduled_at, status,
                                    winner_side, outcome, live_state, import_key)
               values (%s, %s, %s, %s, %s, %s, %s, %s)
               on conflict (import_key) do update set
                 edition_id = excluded.edition_id,
                 round_code = excluded.round_code,
                 scheduled_at = excluded.scheduled_at,
                 status = excluded.status,
                 winner_side = excluded.winner_side,
                 outcome = excluded.outcome,
                 live_state = excluded.live_state
               returning id""",
            (edition_ids[f"{slug}_2026"], stage, start_at, status,
             winner_side, outcome if status == "completed" else None,
             Jsonb(live_state), import_key),
        )
        match_id = cur.fetchone()[0]

        cur.execute("delete from match_participants where match_id = %s", (match_id,))
        cur.execute("delete from match_sets where match_id = %s", (match_id,))
        cur.execute(
            "insert into match_participants (match_id, side, slot, player_id) values (%s, 1, 1, %s)",
            (match_id, player_ids[side1_slug]),
        )
        if side2_slug:
            cur.execute(
                "insert into match_participants (match_id, side, slot, player_id) values (%s, 2, 1, %s)",
                (match_id, player_ids[side2_slug]),
            )
        for i, (g1, g2, tb) in enumerate(sets, start=1):
            cur.execute(
                """insert into match_sets
                     (match_id, set_no, side1_games, side2_games, tiebreak_loser_points)
                   values (%s, %s, %s, %s, %s)""",
                (match_id, i, g1, g2, tb),
            )

    total = n_pairs + n_singles
    print(f"[ok] matches: {total} физических ({n_pairs} из зеркальных пар + {n_singles} одиночных)")


def migrate_rankings(cur, rankings, player_ids, warn):
    today = datetime.date.today()
    for r in rankings["rankings"]:
        pid = player_ids.get(r["playerId"])
        if not pid:
            warn.append(f"rankings: неизвестный игрок {r['playerId']}")
            continue
        cur.execute(
            """insert into ranking_snapshots
                 (player_id, tour_code, snapshot_date, rank, points, race_points)
               values (%s, 'atp', %s, %s, %s, %s)
               on conflict (player_id, tour_code, snapshot_date) do update set
                 rank = excluded.rank, points = excluded.points,
                 race_points = excluded.race_points""",
            (pid, today, r["current"], r["ytdPoints"], r["seasonPoints"]),
        )
        start_rank = max(1, r["current"] + r.get("seasonDelta", 0))
        cur.execute(
            """insert into ranking_snapshots
                 (player_id, tour_code, snapshot_date, rank, race_points)
               values (%s, 'atp', %s, %s, 0)
               on conflict (player_id, tour_code, snapshot_date) do update set
                 rank = excluded.rank""",
            (pid, SEASON_START, start_rank),
        )
    print(f"[ok] ranking_snapshots: {len(rankings['rankings'])} x 2 снапшота")


def validate(cur):
    print("\n--- Валидация ---")
    for q, label in [
        ("select count(*) from players where is_tracked", "игроков в ростере"),
        ("select count(*) from players where not is_tracked", "игроков вне ростера"),
        ("select count(*) from tournaments", "турниров"),
        ("select count(*) from tournament_editions", "розыгрышей"),
        ("select count(*) from tournament_entries", "заявок"),
        ("select count(*) from matches", "матчей"),
        ("select count(*) from matches where status = 'live'", "live-матчей"),
        ("select count(*) from match_sets", "сетов"),
        ("select count(*) from ranking_snapshots", "снапшотов рейтинга"),
        ("select count(*) from matches m where not exists (select 1 from match_participants mp where mp.match_id = m.id)", "матчей без участников (должно быть 0)"),
        ("select count(*) from matches where status = 'completed' and winner_side is null and outcome = 'normal'", "completed без победителя (должно быть 0)"),
    ]:
        print(f"  {cur.execute(q).fetchone()[0]:5d}  {label}")

    print("\n  Пример v_player_matches (Синнер, последние 3):")
    rows = cur.execute(
        """select round_code, result, coalesce(score_text, '—'), status
           from v_player_matches
           where player_id = (select id from players where slug = 'sinner')
           order by scheduled_at desc nulls last limit 3"""
    ).fetchall()
    for r in rows:
        print(f"    {r[0]:5s} {str(r[1]):5s} {r[2]:30s} {r[3]}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db-url", default=os.environ.get("DATABASE_URL"))
    ap.add_argument("--dry-run", action="store_true", help="прогон без commit")
    args = ap.parse_args()
    if not args.db_url:
        sys.exit("нужен DATABASE_URL (env) или --db-url")

    reference = load("reference")
    players_shard = load("players")
    rankings = load("rankings")
    up = load("matches_upcoming")["matches"]
    past = load("matches_past")["matches"]
    tournaments = (load("tournaments_upcoming")["tournaments"]
                   + load("tournaments_past")["tournaments"])
    all_matches = up + past

    warn = []
    with psycopg.connect(args.db_url) as conn:
        with conn.cursor() as cur:
            upsert_play_styles(cur, reference)
            player_ids = upsert_players(cur, players_shard, all_matches)
            upsert_tournaments(cur, tournaments, all_matches, player_ids)
            migrate_matches(cur, up, past, player_ids, warn)
            migrate_rankings(cur, rankings, player_ids, warn)
            validate(cur)
        if args.dry_run:
            conn.rollback()
            print("\n[dry-run] изменения откачены")
        else:
            conn.commit()
            print("\n[ok] закоммичено")

    if warn:
        print(f"\n--- Предупреждения ({len(warn)}) ---")
        for w in warn:
            print("  -", w)


if __name__ == "__main__":
    main()
