#!/usr/bin/env python3
"""
build_config.py — детерминированная сборка config.json из шардов.

Что делает (без LLM, бесплатно):
  1. читает все шарды из data/
  2. валидирует их (схема + ссылочная целостность)
  3. пересчитывает playerCards из матчей (nextMatchId, lastMatchIds)
  4. склеивает всё в один config.json
  5. (опционально) git add/commit/push, если данные изменились

Запуск:
  python3 build_config.py                 # собрать и записать config.json
  python3 build_config.py --check         # только валидация, ничего не писать
  python3 build_config.py --push          # собрать и запушить, если изменилось

Шарды — источник правды. config.json — собранный артефакт, который читает приложение.
Руками config.json не правим: правим шард и пересобираем.
"""

import json
import sys
import subprocess
import datetime
from pathlib import Path

ROOT = Path(__file__).parent
DATA = ROOT / "data"
CONFIG_PATH = ROOT / "config.json"

SHARDS = [
    "reference",
    "players",
    "rankings",
    "matches_upcoming",
    "matches_past",
    "tournaments_upcoming",
    "tournaments_past",
]

VALID_TRENDS = {"up", "down", "stable"}
VALID_MATCH_STATUS = {"upcoming", "completed"}


def load(name):
    path = DATA / f"{name}.json"
    if not path.exists():
        raise SystemExit(f"[ERROR] нет шарда: {path}")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise SystemExit(f"[ERROR] битый JSON в {name}.json: {e}")


def validate(shards):
    """Проверяет схему и ссылочную целостность. Возвращает список ошибок."""
    errors = []

    players = {p["id"] for p in shards["players"]["players"]}
    styles = {s["id"] for s in shards["reference"]["playStyles"]}

    # players ссылаются на существующий playStyle
    for p in shards["players"]["players"]:
        if p.get("playStyleId") not in styles:
            errors.append(f"player {p['id']}: неизвестный playStyleId {p.get('playStyleId')}")

    # rankings: по одной записи на игрока, без сирот, валидный trend
    ranked = set()
    for r in shards["rankings"]["rankings"]:
        pid = r.get("playerId")
        if pid not in players:
            errors.append(f"rankings: неизвестный playerId {pid}")
        if pid in ranked:
            errors.append(f"rankings: дубль для {pid}")
        ranked.add(pid)
        if r.get("trend") not in VALID_TRENDS:
            errors.append(f"rankings {pid}: некорректный trend {r.get('trend')}")
    missing = players - ranked
    if missing:
        errors.append(f"rankings: нет записей для игроков: {sorted(missing)}")

    # матчи: статус и ссылки на игроков
    all_matches = (
        shards["matches_upcoming"]["matches"] + shards["matches_past"]["matches"]
    )
    ids = set()
    for m in all_matches:
        if m["id"] in ids:
            errors.append(f"матч-дубль: {m['id']}")
        ids.add(m["id"])
        if m.get("status") not in VALID_MATCH_STATUS:
            errors.append(f"матч {m['id']}: статус {m.get('status')}")
        # playerId (чья карточка) обязан существовать; opponentId может быть
        # "TBD" или игроком вне ростера — это нормально, приложение это рисует.
        if m.get("playerId") not in players:
            errors.append(f"матч {m['id']}: неизвестный playerId={m.get('playerId')}")
    # upcoming не должен содержать completed и наоборот
    for m in shards["matches_upcoming"]["matches"]:
        if m.get("status") != "upcoming":
            errors.append(f"матч {m['id']} в upcoming, но статус {m.get('status')}")
    for m in shards["matches_past"]["matches"]:
        if m.get("status") != "completed":
            errors.append(f"матч {m['id']} в past, но статус {m.get('status')}")

    # completed-матч не может стартовать в будущем (защита от галлюцинаций LLM)
    now = datetime.datetime.now(datetime.timezone.utc)
    for m in shards["matches_past"]["matches"]:
        s = m.get("startAt")
        if not s:
            continue
        try:
            dt = datetime.datetime.fromisoformat(s.replace("Z", "+00:00"))
        except ValueError:
            errors.append(f"матч {m['id']}: некорректный startAt={s}")
            continue
        if dt > now:
            errors.append(
                f"матч {m['id']}: status=completed, но startAt={s} в будущем "
                f"(возможна галлюцинация — не выдумывай счёт будущих матчей)"
            )

    return errors


def recompute_player_cards(shards):
    """nextMatchId = ближайший upcoming-матч игрока;
    lastMatchIds = до 3 последних completed по startAt (новые первыми)."""
    players = [p["id"] for p in shards["players"]["players"]]
    up = shards["matches_upcoming"]["matches"]
    past = shards["matches_past"]["matches"]

    cards = []
    for pid in players:
        # учитываем только матчи, где игрок — основной playerId (как в исходных данных)
        nexts = sorted(
            [m for m in up if m["playerId"] == pid],
            key=lambda m: m.get("startAt", ""),
        )
        lasts = sorted(
            [m for m in past if m["playerId"] == pid],
            key=lambda m: m.get("startAt", ""),
            reverse=True,
        )
        cards.append({
            "playerId": pid,
            "nextMatchId": nexts[0]["id"] if nexts else None,
            "lastMatchIds": [m["id"] for m in lasts[:3]],
        })
    return cards


def build(shards):
    """Собирает финальный config.json (форма совместима с исходным файлом):
    рейтинг и очки вклеиваются обратно внутрь каждого игрока."""
    rank_by_id = {r["playerId"]: r for r in shards["rankings"]["rankings"]}

    players = []
    for p in shards["players"]["players"]:
        r = rank_by_id[p["id"]]
        merged = dict(p)
        merged["ranking"] = {
            "current": r["current"],
            "seasonDelta": r["seasonDelta"],
            "trend": r["trend"],
        }
        merged["ytdPoints"] = r["ytdPoints"]
        merged["seasonPoints"] = r["seasonPoints"]
        players.append(merged)

    config = {
        "schemaVersion": 1,
        "updatedAt": datetime.datetime.now(datetime.timezone.utc)
        .strftime("%Y-%m-%dT%H:%M:%SZ"),
        "playStyles": shards["reference"]["playStyles"],
        "players": players,
        "matches": shards["matches_upcoming"]["matches"]
        + shards["matches_past"]["matches"],
        "playerCards": recompute_player_cards(shards),
        "tournaments": shards["tournaments_upcoming"]["tournaments"]
        + shards["tournaments_past"]["tournaments"],
    }
    return config


def git_push():
    """Коммитит и пушит, только если в репо есть изменения."""
    status = subprocess.run(
        ["git", "status", "--porcelain"], cwd=ROOT, capture_output=True, text=True
    ).stdout.strip()
    if not status:
        print("[git] изменений нет, пуш не нужен")
        return
    stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    subprocess.run(["git", "add", "-A"], cwd=ROOT, check=True)
    subprocess.run(["git", "commit", "-m", f"data update {stamp}"], cwd=ROOT, check=True)
    subprocess.run(["git", "push"], cwd=ROOT, check=True)
    print("[git] запушено")


def main():
    args = set(sys.argv[1:])
    shards = {name: load(name) for name in SHARDS}

    errors = validate(shards)
    if errors:
        print("[VALIDATION FAILED]")
        for e in errors:
            print("  -", e)
        sys.exit(1)
    print(f"[ok] валидация пройдена ({len(SHARDS)} шардов)")

    if "--check" in args:
        return

    config = build(shards)
    CONFIG_PATH.write_text(
        json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"[ok] собран {CONFIG_PATH.name}: "
          f"{len(config['players'])} игроков, "
          f"{len(config['matches'])} матчей, "
          f"{len(config['tournaments'])} турниров")

    if "--push" in args:
        git_push()


if __name__ == "__main__":
    main()
