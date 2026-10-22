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
  2. игроки ростера (is_tracked=true; gender из шарда, по умолчанию 'm') + соперники
     вне ростера (is_tracked=false; пол -- как у игрока ростера, с которым играют)
  3. tournaments + tournament_editions (2026) из data/tournaments_*.json; запись с
     "tour": "wta" -- женский розыгрыш <brand>_wta_2026 (см. edition_slug);
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

from migrate_draws import load_draws, migrate_draws, shard_record
from seed_tournament_card_meta import apply as apply_tournament_card_meta

# прогресс должен быть виден в логе сразу, даже при выводе в файл
sys.stdout.reconfigure(line_buffering=True)

# База за интернет-прокси (Railway): TCP keepalive против молчаливых разрывов
CONNECT_OPTS = dict(
    connect_timeout=15,
    keepalives=1, keepalives_idle=30, keepalives_interval=10, keepalives_count=3,
)

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
    # North-American hard swing: Sofascore names ≠ tournament shard slugs.
    "Western & Southern Open 2026": "cincinnati",
    "Cincinnati Open 2026": "cincinnati",
    "National Bank Open Montreal 2026": "canada",
    "National Bank Open Toronto 2026": "canada",
    "National Bank Open 2026": "canada",
    "Winston-Salem Open 2026": "winston_salem",
    "US Open 2026": "us_open",
    # Asian swing.
    "Chengdu Open 2026": "chengdu",
    "Hangzhou Open 2026": "hangzhou",
    "China Open 2026": "beijing",
    "Japan Open 2026": "japan_open",
    "Kinoshita Group Japan Open 2026": "japan_open",
    "Rolex Shanghai Masters 2026": "shanghai",
    "Shanghai Masters 2026": "shanghai",
    "Rolex Paris Masters 2026": "paris_masters",
    "Paris Masters 2026": "paris_masters",
    "Nitto ATP Finals 2026": "atp_finals",
    "ATP Finals 2026": "atp_finals",
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


SEASON = 2026


def tour_for_gender(gender):
    """'m'/None -> 'atp', 'f' -> 'wta'. В базе то же делает tour_code_for(gender_t)."""
    return "wta" if gender == "f" else "atp"


def edition_slug(brand, tour, year=SEASON):
    """Слаг розыгрыша: мужской <brand>_<year>, женский <brand>_wta_<year>.

    У совмещённого турнира (шлемы, Мадрид...) один бренд и два розыгрыша; у чисто
    женского бренда шаблон тот же, чтобы слаг всегда выводился из (бренд, тур, год)."""
    return f"{brand}_wta_{year}" if tour == "wta" else f"{brand}_{year}"


def player_genders(players_shard):
    """slug -> 'm'/'f' для ростера. Поле gender в шарде необязательно: нет -- мужчина."""
    return {p["id"]: p.get("gender", "m") for p in players_shard["players"]}


def outsider_genders(all_matches, genders):
    """Соперник вне ростера играет в туре того игрока ростера, против которого вышел."""
    out = {}
    for m in all_matches:
        opp = m.get("opponentId")
        if opp and opp != "TBD" and opp not in genders and m.get("playerId") in genders:
            out.setdefault(opp, genders[m["playerId"]])
    return out


def match_edition_slug(m, genders):
    """Розыгрыш матча: бренд -- по названию турнира, тур -- по полу владельца записи."""
    brand = TOURNAMENT_ALIASES.get(m["tournament"])
    if not brand:
        return None
    return edition_slug(brand, tour_for_gender(genders.get(m["playerId"])))


def brand_tours(tournaments):
    """Тур бренда: 'wta' только у чисто женских брендов. У совмещённого -- 'atp', а тур
    розыгрыша задаёт tournament_editions.gender."""
    tours = defaultdict(set)
    for t in tournaments:
        tours[t["tournament"]].add(t.get("tour", "atp"))
    return {brand: ("atp" if "atp" in ts else "wta") for brand, ts in tours.items()}


def ranking_tour(r):
    return r.get("tour", "atp")


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
            """insert into players (slug, first_name, last_name, display_name, gender,
                                    photo_url, play_style_id, traits, links, is_tracked)
               values (%s, %s, %s, %s, %s, %s, %s, %s, %s, true)
               on conflict (slug) do update set
                 first_name = excluded.first_name,
                 gender = excluded.gender,
                 last_name = excluded.last_name,
                 display_name = excluded.display_name,
                 photo_url = coalesce(excluded.photo_url, players.photo_url),
                 play_style_id = excluded.play_style_id,
                 traits = excluded.traits,
                 links = excluded.links,
                 is_tracked = true""",
            (p["id"], first, last, p["name"], p.get("gender", "m"), photo,
             style_ids.get(p.get("playStyleId")),
             Jsonb({"ru": p.get("temperament", [])}),
             Jsonb(p.get("links", {}))),
        )

    outsiders = outsider_genders(all_matches, player_genders(players_shard))
    for slug, gender in sorted(outsiders.items()):
        first, last = split_name(humanize(slug))
        cur.execute(
            """insert into players (slug, first_name, last_name, display_name, gender, is_tracked)
               values (%s, %s, %s, %s, %s, false)
               on conflict (slug) do nothing""",
            (slug, first, last, humanize(slug), gender),
        )
    print(f"[ok] players: {len(roster)} ростер + {len(outsiders)} соперников вне ростера")
    return {slug: pid for slug, pid in cur.execute("select slug, id from players")}


def upsert_tournaments(cur, tournaments, all_matches, player_ids):
    tours_by_brand = brand_tours(tournaments)
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
        tour = t.get("tour", "atp")
        edition = edition_slug(t["tournament"], tour)
        cur.execute(
            """insert into tournaments (slug, name, tour_code, default_surface, conditions)
               values (%s, %s, %s, %s, %s)
               on conflict (slug) do update
                 set name = excluded.name,
                     tour_code = excluded.tour_code,
                     default_surface = excluded.default_surface,
                     conditions = excluded.conditions""",
            (t["tournament"], t["name"], tours_by_brand[t["tournament"]], surface,
             Jsonb({"indoor": True} if indoor else {})),
        )
        cur.execute(
            """insert into tournament_editions
                 (tournament_id, year, slug, start_date, end_date, surface, gender,
                  champion_id, runner_up_id)
               values ((select id from tournaments where slug = %s),
                       2026, %s, %s, %s, %s, %s, %s, %s)
               on conflict (slug) do update set
                 start_date = excluded.start_date,
                 end_date = excluded.end_date,
                 surface = excluded.surface,
                 champion_id = excluded.champion_id,
                 runner_up_id = excluded.runner_up_id""",
            (t["tournament"], edition,
             t["dates"]["start"], t["dates"]["end"], surface,
             "f" if tour == "wta" else "m",
             player_ids.get(t.get("winner")), player_ids.get(t.get("finalist"))),
        )
        for i, slug in enumerate(t.get("players", []), start=1):
            if slug not in player_ids:
                continue
            cur.execute(
                """insert into tournament_entries (edition_id, player_id)
                   values ((select id from tournament_editions where slug = %s), %s)
                   on conflict do nothing""",
                (edition, player_ids[slug]),
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


def migrate_matches(cur, up, past, player_ids, genders, warn, draw_editions=frozenset()):
    """Дедупликация зеркальных записей и заливка matches/participants/sets.

    Розыгрыши из draw_editions (есть полная сетка в data/draws/) здесь не пишутся:
    их записи возвращаются как обогащение для migrate_draws."""
    edition_ids = dict(cur.execute("select slug, id from tournament_editions").fetchall())

    groups = defaultdict(list)  # канонический ключ -> [raw записи]
    skipped = []
    enrich = defaultdict(list)
    for m in up + past:
        edition = match_edition_slug(m, genders)
        if not edition or edition not in edition_ids:
            skipped.append(m["id"])
            continue
        if edition in draw_editions:
            enrich[edition].append(shard_record(m, player_ids, parse_score, warn))
            continue
        pair = frozenset(
            x for x in (m["playerId"], m.get("opponentId")) if x and x != "TBD"
        )
        groups[(edition, m["stage"], m.get("startAt"), pair)].append(m)

    if skipped:
        warn.append(f"матчи без турнира (пропущены): {skipped}")

    n_pairs = n_singles = 0
    for (edition, stage, start_at, pair), recs in sorted(
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

        # без `returning id`: все запросы адресуют матч по import_key, чтобы
        # они могли уехать одним pipeline-батчем без round-trip'а на каждый матч
        import_key = min(r["id"] for r in recs)

        # В исходных данных счёт иногда записан с точки зрения победителя,
        # а не владельца записи (LLM-шум). Инвариант: у нормально завершённого
        # матча победитель выигрывает больше сетов — иначе счёт перевёрнут.
        if status == "completed" and outcome == "normal" and winner_side and sets:
            s1 = sum(1 for a, b, _ in sets if a > b)
            s2 = sum(1 for a, b, _ in sets if b > a)
            loser_won_more = (s2 > s1) if winner_side == 1 else (s1 > s2)
            if loser_won_more:
                sets = [(b, a, tb) for a, b, tb in sets]
                warn.append(f"счёт был ориентирован на победителя, перевёрнут: {import_key}")
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
                 live_state = excluded.live_state""",
            (edition_ids[edition], stage, start_at, status,
             winner_side, outcome if status == "completed" else None,
             Jsonb(live_state), import_key),
        )
        cur.execute(
            "delete from match_participants where match_id = (select id from matches where import_key = %s)",
            (import_key,),
        )
        cur.execute(
            "delete from match_sets where match_id = (select id from matches where import_key = %s)",
            (import_key,),
        )
        cur.execute(
            """insert into match_participants (match_id, side, slot, player_id)
               select id, 1, 1, %s from matches where import_key = %s""",
            (player_ids[side1_slug], import_key),
        )
        if side2_slug:
            cur.execute(
                """insert into match_participants (match_id, side, slot, player_id)
                   select id, 2, 1, %s from matches where import_key = %s""",
                (player_ids[side2_slug], import_key),
            )
        for i, (g1, g2, tb) in enumerate(sets, start=1):
            cur.execute(
                """insert into match_sets
                     (match_id, set_no, side1_games, side2_games, tiebreak_loser_points)
                   select id, %s, %s, %s, %s from matches where import_key = %s""",
                (i, g1, g2, tb, import_key),
            )

    total = n_pairs + n_singles
    print(f"[ok] matches: {total} физических ({n_pairs} из зеркальных пар + {n_singles} одиночных)")
    return enrich


def migrate_rankings(cur, rankings, player_ids, warn):
    today = datetime.date.today()
    for r in rankings["rankings"]:
        pid = player_ids.get(r["playerId"])
        if not pid:
            warn.append(f"rankings: неизвестный игрок {r['playerId']}")
            continue
        tour = ranking_tour(r)
        cur.execute(
            """insert into ranking_snapshots
                 (player_id, tour_code, snapshot_date, rank, points, race_points)
               values (%s, %s, %s, %s, %s, %s)
               on conflict (player_id, tour_code, snapshot_date) do update set
                 rank = excluded.rank, points = excluded.points,
                 race_points = excluded.race_points""",
            (pid, tour, today, r["current"], r["ytdPoints"], r.get("seasonPoints")),
        )
        # Снапшот на начало сезона -- только если изменение известно (у WTA-сида его нет;
        # 0 вместо пропуска показал бы «без изменений», а это неправда).
        if "seasonDelta" not in r:
            continue
        start_rank = max(1, r["current"] + r.get("seasonDelta", 0))
        cur.execute(
            """insert into ranking_snapshots
                 (player_id, tour_code, snapshot_date, rank, race_points)
               values (%s, %s, %s, %s, 0)
               on conflict (player_id, tour_code, snapshot_date) do update set
                 rank = excluded.rank""",
            (pid, tour, SEASON_START, start_rank),
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
    ap.add_argument("--draws-only", action="store_true",
                    help="только сетки из data/draws/ (для частой задачи run_draws.sh)")
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
    draws = load_draws(DATA)
    genders = player_genders(players_shard)

    warn = []
    with psycopg.connect(args.db_url, **CONNECT_OPTS) as conn:
        # pipeline: тысячи INSERT'ов уезжают батчами, а не по одному
        # round-trip'у на запрос (критично для базы за интернет-прокси)
        with conn.pipeline(), conn.cursor() as cur:
            if args.draws_only:
                # Игроки, турниры и LLM-матчи -- забота почасовой задачи. Здесь только сетки;
                # записи шардов нужны им как обогащение (время начала).
                player_ids = dict(cur.execute("select slug, id from players").fetchall())
                enrich = defaultdict(list)
                for m in all_matches:
                    edition = match_edition_slug(m, genders)
                    if edition in draws:
                        enrich[edition].append(shard_record(m, player_ids, parse_score, warn))
                migrate_draws(cur, draws, enrich, warn)
        if args.draws_only:
            if args.dry_run:
                conn.rollback()
            else:
                conn.commit()
                print("[ok] сетки закоммичены")
            for w in warn:
                print("  -", w)
            return
        with conn.pipeline(), conn.cursor() as cur:
            upsert_play_styles(cur, reference)
            player_ids = upsert_players(cur, players_shard, all_matches)
            upsert_tournaments(cur, tournaments, all_matches, player_ids)
            apply_tournament_card_meta(cur)
            enrich = migrate_matches(cur, up, past, player_ids, genders, warn, frozenset(draws))
            migrate_draws(cur, draws, enrich, warn)
            migrate_rankings(cur, rankings, player_ids, warn)
        with conn.cursor() as cur:
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
