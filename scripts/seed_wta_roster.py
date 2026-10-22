#!/usr/bin/env python3
"""Доливает WTA-ростер в шарды data/players.json и data/rankings.json из снимка рейтинга.

Использование:
  python3 scripts/seed_wta_roster.py data/seeds/wta_singles_2026-10-22.json
  python3 build_config.py --check

Снимок -- топ-50 одиночного рейтинга WTA, снятый с wtatennis.com/rankings/singles (rank, имя,
страна, очки, движение за неделю, id игрока WTA). Шарды правит почасовой пайплайн, поэтому
WTA-игроки вносятся не правкой шардов в ветке, а этим скриптом -- уже на main.

Скрипт ИДЕМПОТЕНТЕН: игрок узнаётся по wtaId; повторный запуск обновляет только рейтинг.
Чего в снимке нет, то не заполняется: ни стиля игры, ни фото, ни очков Race и изменения с
начала сезона (их нет на странице рейтинга -- вписывать догадки нельзя)."""

import json
import re
import sys
import unicodedata
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"


def ascii_slug(text):
    folded = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "_", folded.lower()).strip("_")


def player_slug(name, taken):
    """Фамилия, как у ATP-ростера; занятая (Zheng -- соперник из ATP) -- имя_фамилия."""
    first, _, last = name.rpartition(" ")
    slug = ascii_slug(last or name)
    if slug in taken:
        slug = ascii_slug(name)
    return slug


def trend(move):
    """Движение за неделю со страницы рейтинга: "-" -- на месте, "3" -- вверх, "-2" -- вниз."""
    move = move.strip()
    if move in ("", "-"):
        return "stable"
    return "down" if move.startswith("-") else "up"


def apply(shards, snapshot):
    players = shards["players"]["players"]
    rankings = shards["rankings"]["rankings"]
    by_wta_id = {p["wtaId"]: p for p in players if "wtaId" in p}
    taken = {p["id"] for p in players}
    for m in shards["matches_upcoming"]["matches"] + shards["matches_past"]["matches"]:
        taken.add(m.get("opponentId"))

    for row in snapshot["rows"]:
        player = by_wta_id.get(row["wtaId"])
        if player is None:
            slug = player_slug(row["name"], taken)
            taken.add(slug)
            player = {
                "id": slug,
                "name": row["name"],
                "gender": "f",
                "wtaId": row["wtaId"],
                "links": {"profile": f"https://www.wtatennis.com/players/{row['wtaId']}/"
                                     f"{ascii_slug(row['name']).replace('_', '-')}"},
            }
            players.append(player)
            by_wta_id[row["wtaId"]] = player

        ranking = {"playerId": player["id"], "tour": "wta", "current": row["rank"],
                   "trend": trend(row["move"]), "ytdPoints": row["points"]}
        for i, r in enumerate(rankings):
            if r["playerId"] == player["id"]:
                rankings[i] = {**r, **ranking}
                break
        else:
            rankings.append(ranking)
    return shards


def main():
    if len(sys.argv) != 2:
        sys.exit("использование: seed_wta_roster.py <снимок.json>")
    snapshot = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    shards = {name: json.loads((DATA / f"{name}.json").read_text(encoding="utf-8"))
              for name in ("players", "rankings", "matches_upcoming", "matches_past")}
    apply(shards, snapshot)
    for name in ("players", "rankings"):
        (DATA / f"{name}.json").write_text(
            json.dumps(shards[name], ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    women = sum(1 for p in shards["players"]["players"] if p.get("gender") == "f")
    print(f"[ok] WTA-игроков в ростере: {women}")


if __name__ == "__main__":
    main()
