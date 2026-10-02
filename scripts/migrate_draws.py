"""Заливка полных сеток (data/draws/*.json) в Postgres. Вызывается из migrate_data.py.

Сетка из PDF ATP -- источник правды для розыгрыша: участники, bracket_pos, результаты.
Для её розыгрышей migrate_matches не пишет матчи из LLM-шардов сам, а отдаёт их сюда
как «обогащение»: шард знает время начала, которого в PDF нет, и бывает свежее по
результату, пока PDF не перевыпущен.

Устройство -- планировщик и исполнитель. plan_edition() -- чистая функция над словарями
(тестируется без БД), apply_plan() -- только SQL.

Правила плана:
  1. Строку матча из сетки ищем среди уже существующих строк розыгрыша: та же глубина
     раунда и bracket_pos, иначе та же пара игроков, иначе заглушка «игрок + TBD» на той
     же глубине. Найденную правим на месте -- её id держат live_flags и
     live_activity_sessions; не нашли -- вставляем с import_key «draw:<edition>:<раунд>:<pos>».
  2. Коды раундов в БД разнобойные (R1/R2 у Уимблдона, R128/R64 у RG при той же сетке),
     поэтому сравнение -- по глубине: сколько раундов до финала.
  3. Результат -- из PDF; нет в PDF, но есть в шарде для той же пары -- из шарда (временно).
     Статус live не трогаем: им владеет live-ingest бэкенда.
  4. Строки розыгрыша, которым в сетке места не нашлось (заглушки TBD, пары, которых не
     было: Molcan–Rinderknech при снявшемся Rinderknech), -> cancelled. Не удаляем: на них
     могут ссылаться live-таблицы.
  5. Матч, у которого не известен ни один участник, не создаём: дерево позиций и так
     восстанавливается из bracket_pos (k -> ceil(k/2)).
"""

from __future__ import annotations

import json
import math
import re
import unicodedata
from pathlib import Path

from psycopg.types.json import Jsonb

SIZE_CODES = {"F": 0, "SF": 1, "QF": 2, "R16": 3, "R32": 4, "R64": 5, "R128": 6}
R_NUM = re.compile(r"^R(\d)$")


def norm(name):
    s = unicodedata.normalize("NFKD", name or "")
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    return re.sub(r"[^a-z]", "", s.lower())


def depth(code, n_rounds):
    """Раундов до финала: F=0, SF=1 ... R1 в сетке из 5 раундов = 4. None для квалификации."""
    if code in SIZE_CODES:
        return SIZE_CODES[code]
    m = R_NUM.match(code or "")
    if m:
        return n_rounds - int(m.group(1))
    return None


def load_draws(data_dir: Path):
    out = {}
    for path in sorted((data_dir / "draws").glob("*.json")):
        draw = json.loads(path.read_text(encoding="utf-8"))
        out[draw["edition"]] = draw
    return out


# ---------------------------------------------------------------------------
# Игроки
# ---------------------------------------------------------------------------

def slugify(text):
    s = unicodedata.normalize("NFKD", text)
    s = "".join(ch for ch in s if not unicodedata.combining(ch)).lower()
    return re.sub(r"[^a-z0-9]+", "_", s).strip("_")


def humanize(slug):
    """Копия migrate_data.humanize: так строилось имя игроку вне ростера, пришедшему из шарда."""
    return " ".join(p.upper() + "." if len(p) == 1 else p.capitalize() for p in slug.split("_"))


def real_first_name(p):
    return bool((p["first"] or "").strip()) and p["display"] != humanize(p["slug"])


def resolve_player(entry, players):
    """Позиция сетки -> существующий игрок или None.

    players: [{id, slug, first, last, display, tracked}]. Фамилии вне ростера в БД часто
    восстановлены из слага («Carabelli» вместо «Ugo Carabelli», «Fokina» вместо
    «Davidovich Fokina»), поэтому сравниваем и по окончанию фамилии."""
    last = norm(entry["last"])
    initial = (entry.get("first") or "")[:1].lower()
    cands = []
    for p in players:
        plast, pdisp = norm(p["last"]), norm(p["display"])
        exact = plast == last
        suffix = len(plast) >= 4 and (last.endswith(plast) or plast.endswith(last))
        if exact or suffix or (len(last) >= 4 and pdisp.endswith(last)):
            cands.append((0 if exact else 1, p))
    # Имя в БД известно и ни одно слово имени из PDF не начинается с той же буквы -- другой
    # человек с той же фамилией (Roman и Andres Burruchaga). «Agustin» для «Thiago Agustin»
    # Tirante -- тот же: второе имя.
    # Проверяем только настоящее имя: у игроков вне ростера его часто сгенерировал
    # migrate_data.humanize() из слага («van_assche» -> имя «Van»), и такое ничего не доказывает.
    pdf_initials = {w[:1].lower() for w in re.split(r"[\s.\-]+", entry.get("first") or "") if w}
    if pdf_initials:
        cands = [(r, p) for r, p in cands
                 if not real_first_name(p) or p["first"].strip()[:1].lower() in pdf_initials]
    if not cands:
        return None
    if len(cands) > 1 and initial:
        with_initial = [(r, p) for r, p in cands if (p["first"] or "")[:1].lower() == initial]
        cands = with_initial or cands
    best_rank = min(r for r, _ in cands)
    cands = [p for r, p in cands if r == best_rank]
    if len(cands) > 1:
        tracked = [p for p in cands if p["tracked"]]
        cands = tracked if len(tracked) == 1 else cands
    return cands[0] if len(cands) == 1 else None


def new_player(entry, taken_slugs):
    base = slugify(entry["last"])
    slug = base
    if slug in taken_slugs:
        slug = f"{slugify((entry.get('first') or 'x')[:1])}_{base}"
    n = 2
    while slug in taken_slugs:
        slug = f"{base}_{n}"
        n += 1
    first = entry.get("first")
    return {
        "slug": slug,
        "first": first,
        "last": entry["last"],
        "display": f"{first} {entry['last']}" if first else entry["last"],
    }


# ---------------------------------------------------------------------------
# Счёт
# ---------------------------------------------------------------------------

SET_RE = re.compile(r"^(\d+)-(\d+)(?:\((\d+)\))?$")


def sets_for_sides(score, winner):
    """«7-6(1), 6-4» глазами победителя -> [(side1, side2, tb)] глазами top/bottom."""
    out = []
    for tok in (score or "").split(","):
        m = SET_RE.match(tok.strip())
        if not m:
            continue
        a, b = int(m.group(1)), int(m.group(2))
        tb = int(m.group(3)) if m.group(3) else None
        out.append((a, b, tb) if winner == "top" else (b, a, tb))
    return out


# ---------------------------------------------------------------------------
# План
# ---------------------------------------------------------------------------

def plan_edition(draw, rows, shard_recs, pos_player):
    """Что сделать с матчами одного розыгрыша.

    draw       -- шард сетки;
    rows       -- существующие строки: {id, round, bracket_pos, status, import_key,
                  scheduled_at, players: {side: player_id}};
    shard_recs -- записи LLM-шарда этого розыгрыша, уже с id игроков (shard_record()):
                  {round, start_at, player_ids: (a, b|None), status, winner_id, outcome,
                   sets_by_player: [({player_id: games}, tb)]};
    pos_player -- позиция сетки -> player_id.

    Возвращает {"upserts": [...], "cancel": [row_id], "warnings": [...]}.
    """
    n_rounds = int(math.log2(draw["slots"]))
    warnings, upserts = [], []
    free = {r["id"]: r for r in rows if depth(r["round"], n_rounds) is not None}

    def take(pred):
        for rid, r in list(free.items()):
            if pred(r):
                del free[rid]
                return r
        return None

    for rnd in draw["rounds"]:
        d = depth(rnd["code"], n_rounds)
        for m in rnd["matches"]:
            top = pos_player.get(m["top"]) if m["top"] else None
            bottom = pos_player.get(m["bottom"]) if m["bottom"] else None
            if top is None and bottom is None:
                continue
            pair = {p for p in (top, bottom) if p}

            def same_depth(r, d=d):
                return depth(r["round"], n_rounds) == d

            row = (take(lambda r: same_depth(r) and r["bracket_pos"] == m["pos"]
                        and set(r["players"].values()) <= pair | {None})
                   or take(lambda r: len(pair) == 2 and set(r["players"].values()) == pair)
                   or take(lambda r: same_depth(r) and len(r["players"]) == 1
                           and set(r["players"].values()) <= pair))

            # обогащение из шарда: та же пара (или тот же игрок при TBD) на той же глубине
            rec = next((s for s in shard_recs
                        if depth(s["round"], n_rounds) == d
                        and {p for p in s["player_ids"] if p} == pair), None)

            winner_side = sets = outcome = None
            status = None
            if m["winner"]:
                winner_side = 1 if m["winner"] == "top" else 2
                sets = sets_for_sides(m["score"], m["winner"])
                outcome = m["outcome"] or "normal"
                status = "completed"
            elif rec and rec["status"] == "completed" and rec.get("winner_id") in pair and len(pair) == 2:
                winner_side = 1 if rec["winner_id"] == top else 2
                # [({player_id: games, ...}, tb)] -- без привязки к сторонам, ориентируем здесь
                sets = [(g[top], g[bottom], tb) for g, tb in rec.get("sets_by_player") or []]
                outcome = rec.get("outcome") or "normal"
                status = "completed"

            upserts.append({
                "row_id": row["id"] if row else None,
                "import_key": None if row else f"draw:{draw['edition']}:{rnd['code']}:{m['pos']}",
                "round": rnd["code"],
                "bracket_pos": m["pos"],
                "side1": top,
                "side2": bottom,
                "status": status,          # None = не трогать (scheduled/live/cancelled как есть)
                "winner_side": winner_side,
                "outcome": outcome,
                "sets": sets,              # None = не трогать
                "scheduled_at": rec["start_at"] if rec and rec.get("start_at") else None,
                "was_status": row["status"] if row else None,
            })

    cancel = [rid for rid, r in free.items() if r["status"] != "cancelled"]
    for rid in cancel:
        r = free[rid]
        warnings.append(f"{draw['edition']}: строка {rid} ({r['round']}, {r['import_key']}) "
                        f"не нашла места в сетке -> cancelled")
    return {"upserts": upserts, "cancel": cancel, "warnings": warnings}


# ---------------------------------------------------------------------------
# Исполнение
# ---------------------------------------------------------------------------

def _players_index(cur):
    return [
        {"id": pid, "slug": slug, "first": first, "last": last or "", "display": disp or "", "tracked": tracked}
        for pid, slug, first, last, disp, tracked in cur.execute(
            "select id, slug, first_name, last_name, display_name, is_tracked from players").fetchall()
    ]


def _resolve_positions(cur, draw, players, warn):
    taken = {p["slug"] for p in players}
    pos_player = {}
    for e in draw["entries"]:
        if e.get("bye"):
            continue
        p = resolve_player(e, players)
        if p is None:
            info = new_player(e, taken)
            taken.add(info["slug"])
            pid = cur.execute(
                """insert into players (slug, first_name, last_name, display_name, is_tracked)
                   values (%s, %s, %s, %s, false)
                   on conflict (slug) do update set slug = excluded.slug
                   returning id""",
                (info["slug"], info["first"], info["last"], info["display"]),
            ).fetchone()[0]
            p = {"id": pid, "slug": info["slug"], "first": info["first"], "last": info["last"],
                 "display": info["display"], "tracked": False}
            players.append(p)
        elif not p["tracked"] and e.get("first") and not e.get("truncated"):
            # имя вне ростера было восстановлено из слага -- ставим настоящее
            display = f"{e['first']} {e['last']}"
            if display != p["display"]:
                cur.execute(
                    "update players set first_name = %s, last_name = %s, display_name = %s where id = %s",
                    (e["first"], e["last"], display, p["id"]),
                )
                p.update(first=e["first"], last=e["last"], display=display)
        pos_player[e["pos"]] = p["id"]
    return pos_player


def _edition_rows(cur, edition_id):
    rows = {}
    for mid, rc, pos, status, ik, sched in cur.execute(
            """select id, round_code, bracket_pos, status::text, import_key, scheduled_at
               from matches where edition_id = %s and round_code !~* '^q'""", (edition_id,)):
        rows[mid] = {"id": mid, "round": rc, "bracket_pos": pos, "status": status,
                     "import_key": ik, "scheduled_at": sched, "players": {}}
    if rows:
        for mid, side, pid in cur.execute(
                """select match_id, side, player_id from match_participants
                   where match_id = any(%s) and slot = 1""", (list(rows),)):
            rows[mid]["players"][side] = pid
    return list(rows.values())


def apply_plan(cur, edition_id, plan):
    for op in plan["upserts"]:
        if op["row_id"] is None:
            op["row_id"] = cur.execute(
                """insert into matches (edition_id, round_code, bracket_pos, status, discipline,
                                        scheduled_at, import_key)
                   values (%s, %s, %s, 'scheduled', 'singles', %s, %s)
                   on conflict (import_key) do update set round_code = excluded.round_code
                   returning id""",
                (edition_id, op["round"], op["bracket_pos"], op["scheduled_at"], op["import_key"]),
            ).fetchone()[0]
        mid = op["row_id"]
        # bracket_pos уникален в (edition, round): освобождаем место у чужой строки, если оно занято
        cur.execute(
            """update matches set bracket_pos = null
               where edition_id = %s and round_code = %s and bracket_pos = %s and id <> %s""",
            (edition_id, op["round"], op["bracket_pos"], mid),
        )
        cur.execute(
            """update matches set round_code = %s, bracket_pos = %s,
                 scheduled_at = coalesce(%s, scheduled_at)
               where id = %s""",
            (op["round"], op["bracket_pos"], op["scheduled_at"], mid),
        )
        if op["status"] == "completed":
            cur.execute(
                """update matches set status = 'completed', winner_side = %s, outcome = %s
                   where id = %s""",
                (op["winner_side"], op["outcome"], mid),
            )
        elif op["was_status"] == "completed":
            # строка была завершена по шарду, а пара в PDF ещё не сыграна или другая --
            # результат больше не подтверждён
            cur.execute(
                "update matches set status = 'scheduled', winner_side = null, outcome = null where id = %s",
                (mid,),
            )
        cur.execute("delete from match_participants where match_id = %s", (mid,))
        for side, pid in ((1, op["side1"]), (2, op["side2"])):
            if pid:
                cur.execute(
                    "insert into match_participants (match_id, side, slot, player_id) values (%s, %s, 1, %s)",
                    (mid, side, pid),
                )
        if op["sets"] is not None and (op["status"] == "completed" or op["was_status"] == "completed"):
            cur.execute("delete from match_sets where match_id = %s", (mid,))
            for i, (a, b, tb) in enumerate(op["sets"], start=1):
                cur.execute(
                    """insert into match_sets (match_id, set_no, side1_games, side2_games, tiebreak_loser_points)
                       values (%s, %s, %s, %s, %s)""",
                    (mid, i, a, b, tb),
                )
    if plan["cancel"]:
        cur.execute(
            "update matches set status = 'cancelled', bracket_pos = null where id = any(%s)",
            (plan["cancel"],),
        )


def apply_edition_meta(cur, draw):
    """Размер сетки, статус жеребьёвки и категория -- на розыгрыш."""
    cur.execute(
        """update tournament_editions
           set draw_size = %s, draw_status = 'drawn',
               metadata = metadata || %s
           where slug = %s""",
        (draw["drawSize"],
         Jsonb({k: v for k, v in {"category": draw.get("category"), "atp_id": draw.get("atpId"),
                                   "draw_released": draw.get("released"),
                                   # «Beijing, China»: у части брендов tournaments.location пуст
                                   "location": draw.get("location")}.items() if v is not None}),
         draw["edition"]),
    )


def shard_record(m, player_ids, parse_score, warn):
    """Запись data/matches_*.json -> обогащение для plan_edition. Счёт в записи -- глазами
    её владельца (playerId); переводим в «игрок -> геймы», стороны назначит план."""
    a = player_ids.get(m["playerId"])
    b = player_ids.get(m.get("opponentId")) if m.get("opponentId") not in (None, "TBD") else None
    sets, outcome = parse_score(m.get("score"), warn)
    winner = None
    if m.get("result") == "won":
        winner = a
    elif m.get("result") == "lost":
        winner = b
    status = "completed" if m.get("status") == "completed" else ("live" if m.get("isLive") else "scheduled")
    return {
        "round": m["stage"],
        "start_at": m.get("startAt"),
        "player_ids": (a, b),
        "status": status,
        "winner_id": winner,
        "outcome": outcome,
        "sets_by_player": [({a: x, b: y}, tb) for x, y, tb in sets] if a and b else [],
    }


def migrate_draws(cur, draws, shard_by_edition, warn):
    """draws: edition -> шард сетки; shard_by_edition: edition -> [shard_record()]."""
    players = _players_index(cur)
    edition_ids = dict(cur.execute("select slug, id from tournament_editions").fetchall())
    total_up = total_cancel = 0
    for edition, draw in sorted(draws.items()):
        if edition not in edition_ids:
            warn.append(f"сетка {edition}: розыгрыша нет в БД, пропуск")
            continue
        eid = edition_ids[edition]
        pos_player = _resolve_positions(cur, draw, players, warn)
        plan = plan_edition(draw, _edition_rows(cur, eid), shard_by_edition.get(edition, []), pos_player)
        apply_edition_meta(cur, draw)
        apply_plan(cur, eid, plan)
        warn.extend(plan["warnings"])
        total_up += len(plan["upserts"])
        total_cancel += len(plan["cancel"])
    print(f"[ok] draws: {len(draws)} сеток, {total_up} матчей, {total_cancel} строк -> cancelled")
