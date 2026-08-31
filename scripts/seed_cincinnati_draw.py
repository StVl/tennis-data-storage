#!/usr/bin/env python3
"""Заполняет карточку сетки только для Cincinnati 2026.

Ставит draw_date / draw_status / draw_size на розыгрыш и проставляет
matches.bracket_pos, восстанавливая дерево от финала по победителям.

Не выдумывает матчи и соперников: трёх отсутствующих R1 в источнике нет —
их не создаём.

  DATABASE_URL=postgres://... python3 scripts/seed_cincinnati_draw.py
"""

from __future__ import annotations

import argparse
import os
import sys
from collections import defaultdict

import psycopg

EDITION_SLUG = "cincinnati_2026"
DRAW_DATE = "2026-08-10"  # за 3 дня до start_date 13 Aug
DRAW_SIZE = 96

ROUND_ORDER = ["R1", "R2", "R3", "R4", "QF", "SF", "F"]
PREV = {b: a for a, b in zip(ROUND_ORDER, ROUND_ORDER[1:])}
EXPECTED = {"R1": 32, "R2": 32, "R3": 16, "R4": 8, "QF": 4, "SF": 2, "F": 1}


def load_matches(conn, edition_id: int) -> list[dict]:
    rows = conn.execute(
        """
        select m.id, m.round_code, m.winner_side,
               max(p.slug) filter (where mp.side = 1) as p1,
               max(p.slug) filter (where mp.side = 2) as p2
        from matches m
        left join match_participants mp on mp.match_id = m.id
        left join players p on p.id = mp.player_id
        where m.edition_id = %s
        group by m.id
        """,
        (edition_id,),
    ).fetchall()
    out = []
    for id_, rnd, wside, p1, p2 in rows:
        winner = p1 if wside == 1 else p2 if wside == 2 else None
        out.append(
            {
                "id": id_,
                "round": rnd,
                "p1": p1,
                "p2": p2,
                "winner": winner,
                "pos": None,
            }
        )
    return out


def assign_positions(matches: list[dict]) -> None:
    by_round: dict[str, list[dict]] = defaultdict(list)
    for m in matches:
        by_round[m["round"]].append(m)

    used: set[int] = set()

    def feeder(player: str | None, prev_round: str) -> dict | None:
        if not player:
            return None
        for m in by_round[prev_round]:
            if m["winner"] == player and m["id"] not in used:
                return m
        return None

    def walk(match: dict, pos: int) -> None:
        match["pos"] = pos
        used.add(match["id"])
        prev = PREV.get(match["round"])
        if not prev:
            return
        # 96-сетка: R1 — плей-ин в конкретную вилку R2, pos совпадает с R2.
        if match["round"] == "R2":
            child = feeder(match["p1"], prev) or feeder(match["p2"], prev)
            if child:
                walk(child, pos)
            return
        left = feeder(match["p1"], prev)
        right = feeder(match["p2"], prev)
        if left:
            walk(left, 2 * pos - 1)
        if right:
            walk(right, 2 * pos)

    finals = by_round.get("F") or []
    if len(finals) != 1:
        sys.exit(f"ожидал 1 финал, нашёл {len(finals)}")
    walk(finals[0], 1)

    for rnd, expected in EXPECTED.items():
        taken = {m["pos"] for m in by_round[rnd] if m["pos"] is not None}
        free = [i for i in range(1, expected + 1) if i not in taken]
        for m in by_round[rnd]:
            if m["pos"] is None:
                if not free:
                    sys.exit(f"нет свободного bracket_pos в {rnd} для match {m['id']}")
                m["pos"] = free.pop(0)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db-url", default=os.environ.get("DATABASE_URL"))
    args = ap.parse_args()
    if not args.db_url:
        sys.exit("нужен DATABASE_URL или --db-url")

    with psycopg.connect(args.db_url) as conn:
        row = conn.execute(
            "select id from tournament_editions where slug = %s",
            (EDITION_SLUG,),
        ).fetchone()
        if not row:
            sys.exit(f"нет розыгрыша {EDITION_SLUG}")
        edition_id = row[0]

        matches = load_matches(conn, edition_id)
        assign_positions(matches)

        conn.execute(
            """
            update tournament_editions
               set draw_date = %s,
                   draw_status = 'drawn',
                   draw_size = %s
             where id = %s
            """,
            (DRAW_DATE, DRAW_SIZE, edition_id),
        )
        for m in matches:
            conn.execute(
                "update matches set bracket_pos = %s where id = %s",
                (m["pos"], m["id"]),
            )
        conn.commit()

        by_round: dict[str, list[dict]] = defaultdict(list)
        for m in matches:
            by_round[m["round"]].append(m)

        print(f"[ok] {EDITION_SLUG}: draw_status=drawn draw_date={DRAW_DATE} draw_size={DRAW_SIZE}")
        for rnd in ROUND_ORDER:
            ms = sorted(by_round[rnd], key=lambda x: x["pos"] or 0)
            n = len(ms)
            exp = EXPECTED[rnd]
            gap = "" if n == exp else f"  (ожидали {exp}, не хватает {exp - n})"
            print(f"  {rnd:3} {n:2} матчей{gap}")
            if rnd in ("SF", "F"):
                for m in ms:
                    print(f"      pos={m['pos']} {m['p1']} vs {m['p2']}  winner={m['winner']}")


if __name__ == "__main__":
    main()
