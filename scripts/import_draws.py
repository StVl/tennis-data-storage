#!/usr/bin/env python3
"""Импорт полных сеток турниров из официальных PDF ATP и WTA в шарды data/draws/<edition>.json.

Использование:
  python3 scripts/import_draws.py                       # текущие и ближайшие розыгрыши
  python3 scripts/import_draws.py --edition beijing_2026
  python3 scripts/import_draws.py --edition beijing_2026 --pdf /path/mds.pdf   # без сети
  python3 scripts/import_draws.py --edition washington_wta_2026                # женская сетка

Детерминированный скрипт без LLM, в отличие от prompts/update_matches.md: сетка -- это
32-128 матчей с позициями, и агент, читающий их со страницы, ошибается (в БД нашлись
перевёрнутые победители и пара Molcan–Rinderknech, хотя Rinderknech снялся).

Источники (SOURCES) -- официальные документы туров, перевыпускаемые по ходу турнира
(метка «Released»): ATP -- protennislive.com/posting/<год>/<id>/mds.pdf, WTA --
wtafiles.wtatennis.com/pdf/draws/<год>/<id>/MDS.pdf. Sofascore и atptour.com закрыты
антибот-защитой; её не обходим. Тур розыгрыша -- поле "tour" записи в шарде турниров,
женский розыгрыш -- <brand>_wta_<year> (edition_slug). Розыгрыш WTA 125 (очки чемпиона 125)
распознаётся и пропускается: такие турниры не ведём.

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
from migrate_data import edition_slug

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
DRAWS = DATA / "draws"

ATP_PDF_URL = "https://www.protennislive.com/posting/{year}/{id}/mds.pdf"
WTA_PDF_URL = "https://wtafiles.wtatennis.com/pdf/draws/{year}/{id}/MDS.pdf"
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
    "almaty": 9410,
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

# Слаг бренда -> id турнира WTA (tournamentGroup.id в api.wtatennis.com/tennis/tournaments,
# он же в ссылках wtatennis.com/tournaments/<id>/...; сверено с календарём 2026 на 2026-10-22).
# У совмещённого турнира слаг -- как у ATP-записи, у чисто женского -- город (как в
# prompts/update_tournaments.md). Шлемов нет: wtafiles их не публикует (404), как и
# protennislive; WTA Finals -- круговой этап; WTA 125 не ведём.
WTA_IDS = {
    "abu_dhabi": 2088,
    "adelaide": 2014,
    "athens": 1175,
    "auckland": 1049,
    "austin": 2082,
    "bad_homburg": 2017,
    "beijing": 1020,
    "berlin": 2012,
    "bogota": 894,
    "brisbane": 800,
    "canada": 806,
    "charleston": 804,
    "chennai": 1148,
    "cincinnati": 1017,
    "cluj_napoca": 2050,
    "doha": 1003,
    "dubai": 718,
    "eastbourne": 710,
    "guadalajara": 2075,
    "guangzhou": 1023,
    "hamburg": 2042,
    "hobart": 1050,
    "hong_kong": 1074,
    "iasi": 2063,
    "indian_wells": 609,
    # Osaka: «Kinoshita Group Japan Open», то же имя, что у ATP Tokyo (алиас Sofascore -> japan_open)
    "japan_open": 405,
    # тот же турнир под слагом, который даёт ему агент турниров (по названию, а не по городу)
    "japan_womens_open": 405,
    "libema": 822,
    "linz": 528,
    "madrid": 1038,
    "memphis": 1167,
    "merida": 2085,
    "miami": 902,
    "monterrey": 1039,
    "ningbo": 2092,
    "nottingham": 1080,
    "ostrava": 1154,
    "prague": 1082,
    "queens_club": 1111,
    "rabat": 1005,
    "rome": 709,
    "rouen": 2066,
    "sao_paulo": 1139,
    "seoul": 1024,
    "singapore": 1152,
    # Porsche Tennis Grand Prix (апрель, грунт) -- не BOSS Open, которого ATP-бренд stuttgart
    "stuttgart": 1051,
    "strasbourg": 406,
    "tokyo": 1056,
    # Toray Pan Pacific Open -- слаг агента турниров для того же Токио
    "pan_pacific_open": 1056,
    "washington": 1045,
    "wuhan": 1075,
}

# Тур -> откуда брать PDF. Разбор параметризуется тем же ключом (draw_pdf.LAYOUTS).
SOURCES = {
    "atp": {"url": ATP_PDF_URL, "ids": ATP_IDS},
    "wta": {"url": WTA_PDF_URL, "ids": WTA_IDS},
}


class RateLimited(RuntimeError):
    pass


def editions_from(tournaments):
    """Записи шардов турниров -> {slug розыгрыша: (бренд, тур, start, end)}."""
    out = {}
    for t in tournaments:
        start = datetime.date.fromisoformat(t["dates"]["start"])
        end = datetime.date.fromisoformat(t["dates"]["end"])
        tour = t.get("tour", "atp")
        out[edition_slug(t["tournament"], tour, start.year)] = (t["tournament"], tour, start, end)
    return out


def load_editions():
    """Розыгрыши из шардов турниров."""
    tournaments = []
    for name in ("tournaments_upcoming", "tournaments_past"):
        tournaments += json.loads((DATA / f"{name}.json").read_text(encoding="utf-8"))["tournaments"]
    return editions_from(tournaments)


def shard_path(edition):
    return DRAWS / f"{edition}.json"


def source_url(tour, year, source_id):
    return SOURCES[tour]["url"].format(year=year, id=source_id)


def due_editions(editions, today):
    """Розыгрыши, чья сетка может меняться сейчас."""
    due = []
    for slug, (brand, tour, start, end) in sorted(editions.items(), key=lambda kv: (kv[1][2], kv[0])):
        if brand not in SOURCES[tour]["ids"]:
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


def to_shard(edition, tour, source_id, url, draw):
    """Только то, что нужно БД, в стабильном порядке: diff шарда = diff сетки.
    sourceId -- id турнира у тура (раньше поле называлось atpId; migrate_draws читает оба)."""
    return {
        "edition": edition,
        "tour": tour,
        "sourceId": source_id,
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
    brand, tour, start, end = editions[edition]
    source_id = SOURCES[tour]["ids"].get(brand)
    if source_id is None:
        print(f"[skip] {edition}: нет id {tour.upper()} для «{brand}»")
        return False
    url = source_url(tour, start.year, source_id)
    data = Path(pdf_path).read_bytes() if pdf_path else fetch_pdf(url)
    if data is None:
        print(f"[skip] {edition}: сетка ещё не опубликована ({url})")
        return False
    try:
        draw = parse_draw_pdf(data, source=tour)
    except DrawParseError as exc:
        print(f"[skip] {edition}: PDF не разобран: {exc}")
        return False
    if not dates_match(draw, start, end):
        print(f"[skip] {edition}: PDF о другом турнире ({draw.get('title')} "
              f"{draw.get('startDate')}–{draw.get('endDate')}), проверьте {tour.upper()}_IDS")
        return False
    if draw.get("points") is not None and draw.get("category") is None:
        print(f"[skip] {edition}: турнир вне ведения ({draw['points']} очков чемпиону, WTA 125)")
        return False
    for w in draw["warnings"]:
        print(f"[warn] {edition}: {w}")
    changed = write_if_changed(shard_path(edition), to_shard(edition, tour, source_id, url, draw))
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
