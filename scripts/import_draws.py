#!/usr/bin/env python3
"""Импорт полных сеток турниров из официальных PDF ATP в шарды data/draws/<edition>.json.

Использование:
  python3 scripts/import_draws.py                       # текущие и ближайшие розыгрыши
  python3 scripts/import_draws.py --edition beijing_2026
  python3 scripts/import_draws.py --edition beijing_2026 --pdf /path/mds.pdf   # без сети

Детерминированный скрипт без LLM, в отличие от prompts/update_matches.md: сетка -- это
32-128 матчей с позициями, и агент, читающий их со страницы, ошибается (в БД нашлись
перевёрнутые победители и пара Molcan–Rinderknech, хотя Rinderknech снялся).

Источник -- protennislive.com/posting/<год>/<atp_id>/mds.pdf: официальный документ
ATP, перевыпускаемый по ходу турнира (метка «Released»). Sofascore и atptour.com
закрыты антибот-защитой; её не обходим.

Шард перезаписывается, только если сетка изменилась, поэтому почасовой запуск не
плодит пустых коммитов. Запись в БД -- в migrate_data.py (migrate_draws).
"""

from __future__ import annotations

import argparse
import datetime
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from draw_pdf import DrawParseError, parse_draw_pdf

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
DRAWS = DATA / "draws"

PDF_URL = "https://www.protennislive.com/posting/{year}/{atp_id}/mds.pdf"
USER_AGENT = "tennis-data-storage draw import (+https://github.com/StVl/tennis-data-storage)"
# Сервер отвечает 429 на частые запросы. Пауза между PDF и не больше нескольких
# розыгрышей за прогон; на 429 прогон прекращается до следующего часа.
REQUEST_PAUSE_SEC = 5
# Окно, в котором сетку стоит тянуть: жеребьёвка за 1-3 дня до старта, последние
# исправления -- в день после финала.
WINDOW_BEFORE_DAYS = 4
WINDOW_AFTER_DAYS = 1

# Слаг турнира -> id турнира ATP (часть URL PDF). Ошибка в id не опасна: импорт сверяет
# даты из PDF с датами розыгрыша и отбрасывает чужой документ (id 316 в 2026 -- M15 Bastad).
# Сетки Grand Slam турниры публикуют у себя, не на protennislive -- их здесь нет.
ATP_IDS = {
    "almaty": 9900,
    "atp_finals": 605,
    "barcelona": 425,
    "basel": 328,
    "beijing": 747,
    "canada": 421,
    "chengdu": 7581,
    "cincinnati": 422,
    "eastbourne": 741,
    "estoril": 7290,
    "european_open": 7485,
    "gstaad": 314,
    "hangzhou": 7350,
    "japan_open": 329,
    "kitzbuhel": 319,
    "libema": 440,
    "los_cabos": 7480,
    "lyon": 7694,
    "madrid": 1536,
    "mallorca": 8994,
    "monte_carlo": 410,
    "munich": 308,
    "newport": 315,
    "paris_masters": 352,
    "rome": 416,
    "shanghai": 5014,
    "stuttgart": 321,
    "umag": 439,
    "vienna": 337,
    "washington": 418,
    "winston_salem": 6242,
}


class RateLimited(RuntimeError):
    pass


def load_editions():
    """Розыгрыши из шардов турниров: slug -> (start, end)."""
    out = {}
    for name in ("tournaments_upcoming", "tournaments_past"):
        for t in json.loads((DATA / f"{name}.json").read_text(encoding="utf-8"))["tournaments"]:
            start = datetime.date.fromisoformat(t["dates"]["start"])
            end = datetime.date.fromisoformat(t["dates"]["end"])
            out[f"{t['tournament']}_{start.year}"] = (t["tournament"], start, end)
    return out


def due_editions(editions, today):
    """Розыгрыши, чья сетка может меняться сейчас."""
    due = []
    for slug, (brand, start, end) in sorted(editions.items(), key=lambda kv: kv[1][1]):
        if brand not in ATP_IDS:
            continue
        if start - datetime.timedelta(days=WINDOW_BEFORE_DAYS) <= today <= end + datetime.timedelta(days=WINDOW_AFTER_DAYS):
            due.append(slug)
    return due


def fetch_pdf(url):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.read()
    except urllib.error.HTTPError as exc:
        if exc.code == 429:
            raise RateLimited(url) from exc
        if exc.code == 404:
            return None  # сетка ещё не опубликована
        raise


def dates_match(draw, start, end):
    """PDF относится к этому розыгрышу, если совпали даты (±1 день на часовые пояса)."""
    if not draw.get("startDate") or not draw.get("endDate"):
        return False
    ds = datetime.date.fromisoformat(draw["startDate"])
    de = datetime.date.fromisoformat(draw["endDate"])
    return abs((ds - start).days) <= 1 and abs((de - end).days) <= 1


def to_shard(edition, atp_id, url, draw):
    """Только то, что нужно БД, в стабильном порядке: diff шарда = diff сетки."""
    return {
        "edition": edition,
        "atpId": atp_id,
        "source": url,
        "released": draw.get("released"),
        "title": draw.get("title"),
        "location": draw.get("location"),
        "category": draw.get("category"),
        "drawSize": draw["drawSize"],
        "slots": draw["slots"],
        "entries": draw["entries"],
        "rounds": draw["rounds"],
    }


def write_if_changed(path, shard):
    text = json.dumps(shard, ensure_ascii=False, indent=1) + "\n"
    if path.exists() and path.read_text(encoding="utf-8") == text:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return True


def import_edition(edition, editions, pdf_path=None):
    brand, start, end = editions[edition]
    atp_id = ATP_IDS.get(brand)
    if atp_id is None:
        print(f"[skip] {edition}: нет id ATP для «{brand}»")
        return False
    url = PDF_URL.format(year=start.year, atp_id=atp_id)
    data = Path(pdf_path).read_bytes() if pdf_path else fetch_pdf(url)
    if data is None:
        print(f"[skip] {edition}: сетка ещё не опубликована ({url})")
        return False
    try:
        draw = parse_draw_pdf(data)
    except DrawParseError as exc:
        print(f"[skip] {edition}: PDF не разобран: {exc}")
        return False
    if not dates_match(draw, start, end):
        print(f"[skip] {edition}: PDF о другом турнире ({draw.get('title')} "
              f"{draw.get('startDate')}–{draw.get('endDate')}), проверьте ATP_IDS")
        return False
    for w in draw["warnings"]:
        print(f"[warn] {edition}: {w}")
    changed = write_if_changed(DRAWS / f"{edition}.json", to_shard(edition, atp_id, url, draw))
    played = sum(1 for r in draw["rounds"] for m in r["matches"] if m["winner"])
    print(f"[ok] {edition}: {draw['drawSize']} игроков, сыграно {played}"
          f"{', шард обновлён' if changed else ', без изменений'}")
    return changed


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--edition", action="append", help="слаг розыгрыша; можно несколько")
    ap.add_argument("--pdf", help="локальный PDF вместо скачивания (только с одним --edition)")
    ap.add_argument("--today", help="дата для выбора розыгрышей, YYYY-MM-DD")
    args = ap.parse_args()

    editions = load_editions()
    today = datetime.date.fromisoformat(args.today) if args.today else datetime.date.today()
    targets = args.edition or due_editions(editions, today)
    if args.pdf and len(targets) != 1:
        sys.exit("--pdf работает только с одним --edition")
    unknown = [e for e in targets if e not in editions]
    if unknown:
        sys.exit(f"нет таких розыгрышей в шардах турниров: {unknown}")

    print(f"[import_draws] {today}: {', '.join(targets) or 'нечего импортировать'}")
    for i, edition in enumerate(targets):
        if i and not args.pdf:
            time.sleep(REQUEST_PAUSE_SEC)
        try:
            import_edition(edition, editions, args.pdf)
        except RateLimited as exc:
            print(f"[stop] 429 от {exc}: остальные розыгрыши -- в следующий прогон")
            break
        except (urllib.error.URLError, TimeoutError) as exc:
            print(f"[warn] {edition}: сеть: {exc}")


if __name__ == "__main__":
    main()
