#!/usr/bin/env python3
"""Разбор официального PDF сетки ATP (protennislive.com/posting/<год>/<id>/mds.pdf)
и WTA (wtafiles.wtatennis.com/pdf/draws/<год>/<id>/MDS.pdf).

Чистая функция без сети и БД: байты PDF -> словарь сетки. Импорт и запись в шард --
в import_draws.py, заливка в БД -- в migrate_data.py.

Как устроен PDF ATP (проверено на 28/32/48/96-сетках 2026 года):
  - слева список позиций: «номер | посев/статус (WC, Q, LL, SE, PR, WC7 = WC + 7-й посев) |
    ФАМИЛИЯ, Имя | страна»; пустая позиция -- «Bye»;
  - справа колонки раундов, подписанные «Round of 32 / Quarterfinals / ... / Winner».
    В колонке j стоят ПОБЕДИТЕЛИ раунда j («A. Zverev1» -- посев приклеен к фамилии),
    под именем -- счёт с точки зрения победителя: «76» + надстрочное «1» = 7-6(1);
  - сетки на 96/128 занимают несколько страниц (половины/четверти); финал у них --
    отдельный бокс «Champion» поверх колонок.

PDF WTA -- та же таблица (проверено на Washington 28, Antalya 32, Indian Wells 96, 2026),
отличия описаны в LAYOUTS:
  - подписи раундов -- ВНИЗУ, под сеткой; под ними призовые («500 pt»), список посевов,
    блок Retirements с диагнозами («R. Sramkova Lower Back Injury») и «RELEASED 3 Aug
    2026 1:54 PM». Номер даты там же, у левого края, -- поэтому позиции берутся только
    выше подписей, а результаты -- только в полосе позиций;
  - номера позиций 1-9 стоят правее (x~32), чем двузначные;
  - колонка результатов начинается левее, чем у ATP (подпись «Round of 16» широкая), и
    шаг до неё гуляет по турнирам -- границу списка берём по самим результатам;
  - тай-брейк пишется в строку: «76(7)»; инициал бывает из трёх букв («Xin.»);
  - длинное имя переносится на вторую строку («MARISTANY ZULETA DE REALES,» / «Guiomar»);
  - у нейтральных игроков страны нет;
  - чемпион -- бокс «CHAMPION» с «Aryna SABALENKA»; у 96-сетки он на каждой странице;
  - шапка: «July 27 - August 2, 2026» / «March 4-15 2026»; очки -- «pt».
Сетки шлемов на wtafiles нет (как и на protennislive), у WTA Finals -- круговой этап.

Позиции в списке и есть дерево: матч k первого раунда -- позиции 2k-1 и 2k,
матч k раунда r кормит матч ceil(k/2) раунда r+1. Поэтому bracket_pos берётся отсюда,
а не восстанавливается по победителям.
"""

from __future__ import annotations

import io
import math
import re
import unicodedata

ROUND_LABELS = ("Round", "Quarterfinals", "Semifinals", "Final", "Winner")
# Колонка начинается чуть левее своей подписи: подпись центрирована в ячейке,
# а имена победителей прижаты к её левому краю.
COLUMN_LEFT_PAD = 15
TAG_RE = re.compile(r"^(WC|Q|LL|SE|PR|ALT|SR|ITF|JR)?(\d+)?$")
COUNTRY_RE = re.compile(r"^[A-Z]{3}$")
INITIAL_RE = re.compile(r"^(?:[A-Z][a-z]{0,2}\.)(?:-?[A-Z]\.)*$")  # «A.», «J.M.», «J.-L.», «Th.», «Xin.»
DATES_RE = re.compile(r"(\d{1,2}) (\w+) — (\d{1,2}) (\w+) (\d{4})")  # ATP: «30 September — 6 October 2026»
# WTA: «July 27 - August 2, 2026», «March 4-15 2026»
DATES_US_RE = re.compile(r"([A-Z][a-z]+) (\d{1,2}) ?- ?(?:([A-Z][a-z]+) )?(\d{1,2}),? (\d{4})")
TIEBREAK_INLINE_RE = re.compile(r"^(\d)(\d)\((\d+)\)$")  # WTA: «76(7)»
PARTICLES = {"de", "del", "della", "der", "van", "von", "da", "di", "du", "la", "le", "dos"}

# Чем PDF туров отличаются друг от друга. Очки чемпиона -> категория; None -- турнир
# распознан, но не ведётся (WTA 125).
#   position_max_x  -- правый край номеров позиций;
#   labels_below    -- подписи раундов под сеткой, а не над ней;
#   name_col_pad    -- отступ границы списка от подписи 2-й колонки; None -- граница по
#                      левому краю результатов первого раунда;
#   champion_pad    -- насколько имя чемпиона левее слова «Champion».
LAYOUTS = {
    "atp": {
        "categories": {250: "ATP 250", 500: "ATP 500", 1000: "ATP 1000", 2000: "Grand Slam"},
        "position_max_x": 30, "labels_below": False, "name_col_pad": COLUMN_LEFT_PAD, "champion_pad": 5,
    },
    "wta": {
        "categories": {125: None, 250: "WTA 250", 500: "WTA 500", 1000: "WTA 1000", 2000: "Grand Slam"},
        "position_max_x": 34, "labels_below": True, "name_col_pad": None, "champion_pad": 30,
    },
}
# граница списка у WTA, пока результатов первого раунда ещё нет
WTA_NAME_COL_FALLBACK_PAD = 18


def category_for_points(source: str, points: int):
    return LAYOUTS[source]["categories"].get(points)
MONTHS = {m: i for i, m in enumerate(
    ["January", "February", "March", "April", "May", "June", "July", "August",
     "September", "October", "November", "December"], start=1)}


class DrawParseError(ValueError):
    pass


def norm(name: str) -> str:
    """Ключ сравнения фамилий: без регистра, диакритики, пробелов, дефисов и «…»."""
    s = unicodedata.normalize("NFKD", name)
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    return re.sub(r"[^a-z]", "", s.lower())


def proper_case(upper: str) -> str:
    """«DE MINAUR» -> «de Minaur», «AUGER-ALIASSIME» -> «Auger-Aliassime».
    Только запасной путь: нормальное написание берётся из колонок результатов."""
    out = []
    for i, word in enumerate(upper.split()):
        low = word.lower()
        if i < len(upper.split()) - 1 and low in PARTICLES:
            out.append(low)
        else:
            out.append("-".join(p[:1].upper() + p[1:].lower() for p in low.split("-")))
    return " ".join(out)


# ---------------------------------------------------------------------------
# Строки страницы
# ---------------------------------------------------------------------------

def _rows(words, tol=3.0):
    """Слова -> строки (по top с допуском), каждая строка отсортирована по x."""
    rows = []
    for w in sorted(words, key=lambda w: (w["top"], w["x0"])):
        if rows and abs(rows[-1][0] - w["top"]) <= tol:
            rows[-1][1].append(w)
        else:
            rows.append([w["top"], [w]])
    return [(top, sorted(ws, key=lambda w: w["x0"])) for top, ws in rows]


def _dedupe(words):
    """В некоторых PDF текст продублирован вторым слоем (бокс чемпиона) -- одно слово дважды."""
    seen, out = set(), []
    for w in words:
        key = (w["text"], round(w["x0"]), round(w["top"]))
        if key not in seen:
            seen.add(key)
            out.append(w)
    return out


def _is_bold(w):
    return "Bold" in w.get("fontname", "")


def _label_columns(words):
    """Подписи колонок раундов: [(x0, текст)], слева направо. Первая -- список позиций."""
    rows = _rows([w for w in words if w["text"] in ROUND_LABELS or w["text"] in ("of",)
                  or re.fullmatch(r"\d+", w["text"])])
    best = None
    for top, ws in rows:
        starts = [w for w in ws if w["text"] in ROUND_LABELS]
        if len(starts) >= 3 and (best is None or len(starts) > len(best[1])):
            best = (top, starts)
    if not best:
        raise DrawParseError("не нашлась строка подписей раундов")
    return best[0], [(w["x0"], w["text"]) for w in best[1]]


def _untruncate(s):
    """Без многоточия обрезки: «…» у ATP, «...» у WTA."""
    return re.sub(r"\s*(…|\.\.\.)$", "", s.strip()).strip()


def _in_draw_band(top, labels_y, layout):
    """Строка сетки, а не подписи раундов и не то, что под ними (WTA: посевы, Retirements)."""
    if layout["labels_below"]:
        return top < labels_y - 4
    return abs(top - labels_y) > 4


def _entries(words, name_col_end, labels_y, layout):
    """Строки списка позиций -> {pos: entry}, плюс y каждой позиции.

    Строку собираем вокруг номера позиции, а не общей группировкой по строкам: номер стоит
    на 1-2pt выше имени, и соседний текст справа (счёт, бокс чемпиона) сдвигал границу строки."""
    entries, ys = {}, {}
    numbers = [w for w in words if w["x0"] <= layout["position_max_x"] and w["text"].isdigit()
               and _in_draw_band(w["top"], labels_y, layout)]
    for first in sorted(numbers, key=lambda w: w["top"]):
        top = first["top"]
        rest = sorted((w for w in words if w is not first and 30 < w["x1"] and w["x0"] < name_col_end
                       and abs(w["top"] - top) <= 3), key=lambda w: w["x0"])
        if not rest:
            continue
        pos = int(first["text"])
        # повтор номера (например, номер 1 в таблице посевов) -- не строка сетки
        if pos in entries:
            continue
        tag_words = [w for w in rest if w["x0"] < 53 and TAG_RE.match(w["text"]) and _is_bold(w)]
        body = [w for w in rest if w not in tag_words]
        seed = entry = None
        for w in tag_words:
            m = TAG_RE.match(w["text"])
            entry = entry or m.group(1)
            seed = seed or (int(m.group(2)) if m.group(2) else None)
        country = None
        if body and COUNTRY_RE.match(body[-1]["text"]) and body[-1]["x0"] > 120:
            country = body.pop()["text"]
        lines = _rows(body, tol=1.5)
        if len(lines) > 1 and lines[0][1][-1]["text"].endswith(","):
            # имя перенесено на следующую строку (WTA: «MARISTANY ZULETA DE REALES,» / «Guiomar»),
            # и по x «Guiomar» встаёт рядом с «MARISTANY» -- склеиваем построчно
            body = [w for _, ws in lines for w in ws]
        text = " ".join(w["text"] for w in body).strip()
        if not text:
            continue
        ys[pos] = top
        if text.lower() == "bye":
            entries[pos] = {"pos": pos, "bye": True}
            continue
        last_raw, _, first_raw = text.partition(",")
        entries[pos] = {
            "pos": pos,
            "last": proper_case(_untruncate(last_raw)),
            "first": _untruncate(first_raw) or None,
            "truncated": "…" in text or "..." in text,
            "seed": seed,
            "entry": entry,
            "country": country,
        }
    return entries, ys


# ---------------------------------------------------------------------------
# Счёт
# ---------------------------------------------------------------------------

def _score_from_words(ws):
    """Слова строки счёта -> («7-6(1), 6-4», outcome).

    Надстрочное число -- тай-брейк: оно мельче соседнего гейма. Сравниваем именно с соседом,
    а не с «основным» кеглем страницы: в 96-сетках имена 6.0, счёт 6.7, надстрочные 5.6."""
    sets, outcome = [], "normal"
    prev_size = None
    for w in ws:
        t = w["text"].strip()
        low = t.lower().rstrip(".")
        if low in ("ret", "retired"):
            outcome = "retirement"
            continue
        if low in ("walkover", "w/o", "wo"):
            outcome = "walkover"
            continue
        if low in ("def", "default"):
            outcome = "default"
            continue
        tb = TIEBREAK_INLINE_RE.match(t)
        if tb:
            sets.append((int(tb.group(1)), int(tb.group(2)), int(tb.group(3))))
            prev_size = w["size"]
            continue
        if not t.isdigit():
            continue
        if prev_size is not None and w["size"] < prev_size - 0.5 and sets:
            sets[-1] = (sets[-1][0], sets[-1][1], int(t))
            continue
        prev_size = w["size"]
        if len(t) == 2:
            sets.append((int(t[0]), int(t[1]), None))
        elif len(t) == 3:
            # 10-8 / 8-10: одна из сторон -- двузначная
            a, b = (int(t[:2]), int(t[2])) if t.startswith("1") else (int(t[0]), int(t[1:]))
            sets.append((a, b, None))
        elif len(t) == 4:
            sets.append((int(t[:2]), int(t[2:]), None))
    text = ", ".join(f"{a}-{b}" + (f"({tb})" if tb is not None else "") for a, b, tb in sets)
    return text or None, outcome


# ---------------------------------------------------------------------------
# Результаты
# ---------------------------------------------------------------------------

def _result_lines(rows, x_min, exclude):
    """Пары (имя победителя, строка счёта) правее списка позиций.

    Возвращает [(x0, y, initial, surname, score_words)]."""
    out = []
    pending = []  # имена, ждущие строку счёта, по колонкам
    for _, ws in rows:
        ws = [w for w in ws if w["x0"] >= x_min and not exclude(w)]
        if not ws:
            continue
        # в одной строке может быть несколько колонок: режем по разрывам > 20pt
        groups, cur = [], [ws[0]]
        for w in ws[1:]:
            if w["x0"] - cur[-1]["x1"] > 20:
                groups.append(cur)
                cur = [w]
            else:
                cur.append(w)
        groups.append(cur)
        for g in groups:
            # y -- свой у группы, а не у строки: строку мог открыть соседний текст выше
            # (бокс чемпиона в 96-сетке WTA), и имя «съезжало» к матчу выше
            top = min(w["top"] for w in g)
            if INITIAL_RE.match(g[0]["text"]) and len(g) >= 2:
                surname = " ".join(w["text"] for w in g[1:])
                surname = re.sub(r"\d+$", "", surname).strip()
                item = [g[0]["x0"], top, g[0]["text"], surname, []]
                out.append(item)
                pending.append(item)
            elif any(ch.isdigit() for ch in g[0]["text"]) or g[0]["text"].lower().rstrip(".") in (
                    "ret", "walkover", "w/o", "def"):
                # строка счёта относится к ближайшему имени выше в той же колонке
                owner = None
                for item in reversed(pending):
                    if abs(item[0] - g[0]["x0"]) < 25 and 0 < top - item[1] <= 16:
                        owner = item
                        break
                if owner is not None and not owner[4]:
                    owner[4] = g
    return out


def _match_candidate(initial, surname, candidates, entries):
    """Кто из двух участников матча -- этот победитель. None, если не понять."""
    key = norm(surname)
    hits = []
    for pos in candidates:
        e = entries.get(pos)
        if not e or e.get("bye"):
            continue
        last = norm(e["last"])
        if key and (last.startswith(key) or key.startswith(last)):
            hits.append(pos)
    if len(hits) > 1:
        ini = initial[0].upper()
        hits = [p for p in hits if (entries[p].get("first") or "")[:1].upper() == ini] or hits
    return hits[0] if len(hits) == 1 else None


def parse_draw_pdf(data: bytes, source: str = "atp") -> dict:
    """source -- тур документа: "atp" (protennislive) или "wta" (wtafiles), см. LAYOUTS."""
    import pdfplumber  # импорт здесь: зависимость нужна только импорту сеток

    try:
        pdf = pdfplumber.open(io.BytesIO(data))
    except Exception as exc:  # noqa: BLE001 -- битый/HTML вместо PDF
        raise DrawParseError(f"не PDF: {exc}") from exc

    layout = LAYOUTS[source]
    entries, warnings = {}, []
    # результаты: round_index (1 = первый раунд) -> {match_k: (winner_pos|None, sets, outcome)}
    raw_results: dict[int, dict[int, dict]] = {}
    header = {}
    proper_names = {}  # norm(фамилия) -> написание из колонок результатов
    champion = None

    for page_no, page in enumerate(pdf.pages):
        words = _dedupe(page.extract_words(extra_attrs=["size", "fontname"]))
        if not words:
            continue
        if not header:
            header = _parse_header(words, layout)
        try:
            labels_y, labels = _label_columns(words)
        except DrawParseError:
            continue
        rows = _rows(words)
        name_col_end = _name_col_end(words, labels, labels_y, layout)
        page_entries, ys = _entries(words, name_col_end, labels_y, layout)
        if not page_entries:
            continue
        entries.update(page_entries)
        y_min, y_max = min(ys.values()) - 12, max(ys.values()) + 16
        if layout["labels_below"]:
            y_max = min(y_max, labels_y - 2)

        champ_word = next((w for w in words if w["text"] in ("Champion", "CHAMPION")), None)
        champ_pad = layout["champion_pad"]

        def exclude(w, cw=champ_word):
            if not (y_min <= w["top"] <= y_max):
                return True
            return bool(cw and w["x0"] >= cw["x0"] - champ_pad and cw["top"] - 2 <= w["top"] <= cw["top"] + 60)

        page_positions = sorted(ys)

        for x0, y, initial, surname, score_words in _result_lines(rows, name_col_end, exclude):
            # Подпись центрирована в ячейке, имена прижаты к её левому краю, и сдвиг между
            # ними гуляет от 1 до ~27pt. Поэтому колонка -- первая подпись не левее имени.
            col = next((i for i, (x, _) in enumerate(labels) if x >= x0 - 3), None)
            if not col:
                continue
            rnd = col  # колонка j -- победители раунда j
            block = 2 ** rnd
            # матч, чей блок позиций ближе всего по вертикали
            best_k, best_d = None, None
            for k in sorted({(p - 1) // block + 1 for p in page_positions}):
                span = [ys[p] for p in range((k - 1) * block + 1, k * block + 1) if p in ys]
                if not span:
                    continue
                lo, hi = min(span) - 6, max(span) + 6
                d = 0 if lo <= y <= hi else min(abs(y - lo), abs(y - hi))
                if best_d is None or d < best_d:
                    best_k, best_d = k, d
            if best_k is None or best_d > 10:
                warnings.append(f"стр.{page_no + 1}: не привязался победитель {initial} {surname}")
                continue
            score, outcome = _score_from_words(score_words)
            raw_results.setdefault(rnd, {})[best_k] = {
                "initial": initial, "surname": surname, "score": score, "outcome": outcome,
            }
            proper_names.setdefault(norm(surname), surname)

        if champ_word and champion is None:
            champion = _parse_champion(words, champ_word, champ_pad)

    if not entries:
        raise DrawParseError("в PDF нет списка позиций")

    slots = 2 ** math.ceil(math.log2(max(entries)))
    n_rounds = int(math.log2(slots))

    # фамилии в нормальном регистре -- из колонок результатов, где они есть
    for e in entries.values():
        if not e.get("bye"):
            e["last"] = proper_names.get(norm(e["last"]), e["last"])

    if champion and n_rounds not in raw_results:
        raw_results[n_rounds] = {1: champion}

    rounds = _build_rounds(entries, raw_results, n_rounds, warnings)
    return {
        **header,
        "drawSize": sum(1 for e in entries.values() if not e.get("bye")),
        "slots": slots,
        "entries": [entries[p] for p in sorted(entries)],
        "rounds": rounds,
        "warnings": warnings,
    }


def _name_col_end(words, labels, labels_y, layout):
    """Правая граница списка позиций: всё левее -- позиция, правее -- результаты."""
    if layout["name_col_pad"] is not None:
        return labels[1][0] - layout["name_col_pad"]
    # WTA: результаты первого раунда (инициал победителя, счёт) прижаты к левому краю своей
    # колонки, страна в списке всегда левее. Ищем этот край между списком и 2-й подписью.
    col = [w["x0"] for w in words if 120 < w["x0"] < labels[1][0] and _in_draw_band(w["top"], labels_y, layout)
           and (INITIAL_RE.match(w["text"]) or w["text"][:1].isdigit())]
    return min(col) - 1 if col else labels[1][0] - WTA_NAME_COL_FALLBACK_PAD


def _parse_dates(line):
    """(start, end) в ISO или None. ATP: «30 September — 6 October 2026», WTA: «July 27 -
    August 2, 2026» / «March 4-15 2026»."""
    m = DATES_RE.search(line)
    if m:
        d1, m1, d2, m2, year = m.groups()
    else:
        m = DATES_US_RE.search(line)
        if not m:
            return None
        m1, d1, m2, d2, year = m.groups()
        if m2 is None and m1 in MONTHS and int(d2) < int(d1):
            # «October 26-1 2026»: WTA не пишет второй месяц, когда неделя переходит через границу
            # месяца. Взять месяц начала значит получить конец раньше начала, и проверка дат в
            # import_draws отбросит сетку как «PDF о другом турнире».
            m2 = list(MONTHS)[MONTHS[m1] % 12]
        m2 = m2 or m1
    if m1 not in MONTHS or m2 not in MONTHS:
        return None
    y2 = int(year)
    y1 = y2 - 1 if MONTHS[m1] > MONTHS[m2] else y2
    return f"{y1:04d}-{MONTHS[m1]:02d}-{int(d1):02d}", f"{y2:04d}-{MONTHS[m2]:02d}-{int(d2):02d}"


def _parse_released(words):
    """Версия документа: шард меняется только вместе с ней, а не на каждом прогоне.
    ATP: «Released» / «10/02/2026 14:25:46» (MM/DD/YYYY); WTA: «RELEASED» / «3 Aug 2026 1:54 PM»."""
    rel = next((w for w in words if w["text"] in ("Released", "RELEASED")), None)
    if not rel:
        return None
    below = sorted((w for w in words if 0 < w["top"] - rel["top"] < 12 and abs(w["x0"] - rel["x0"]) < 60),
                   key=lambda w: w["x0"])
    m = re.match(r"(\d{2})/(\d{2})/(\d{4})", below[0]["text"]) if below else None
    if m:
        time_part = below[1]["text"] if len(below) > 1 else "00:00:00"
        return f"{m.group(3)}-{m.group(1)}-{m.group(2)}T{time_part}"
    m = re.match(r"(\d{1,2}) ([A-Z][a-z]{2}) (\d{4}) (\d{1,2}):(\d{2}) ?(AM|PM)",
                 " ".join(w["text"] for w in below))
    if m:
        month = next((i for name, i in MONTHS.items() if name.startswith(m.group(2))), None)
        if month:
            hour = int(m.group(4)) % 12 + (12 if m.group(6) == "PM" else 0)
            return f"{m.group(3)}-{month:02d}-{int(m.group(1)):02d}T{hour:02d}:{m.group(5)}:00"
    return None


def _location_case(line):
    """WTA пишет место капсом: «WASHINGTON DC, USA» -> «Washington DC, USA». Слова до трёх
    букв (штат, код страны) оставляем как есть."""
    if not line.isupper():
        return line
    return " ".join(w if len(w.strip(",")) <= 3 else w.capitalize() for w in line.split())


def _parse_header(words, layout):
    top = [w for w in words if w["top"] < 75]
    lines = [" ".join(w["text"] for w in ws) for _, ws in _rows(top)]
    out = {"title": lines[0] if lines else None, "location": None,
           "startDate": None, "endDate": None, "points": None, "category": None}
    for line in lines[1:]:
        dates = _parse_dates(line)
        if dates:
            out["startDate"], out["endDate"] = dates
        elif out["location"] is None and "," in line and "Draw" not in line:
            out["location"] = _location_case(line)
    # очки чемпиона: последнее «NNN pts» (WTA: «NNN pt») в строке призовых, либо «Points NNN»
    # в боксе чемпиона
    pts = [int(w["text"]) for w in words if w["text"].isdigit()
           and any(o["text"] in ("pts", "pt", "Points") and abs(o["top"] - w["top"]) < 12
                   and 0 < o["x0"] - w["x0"] < 30 or (o["text"] == "Points" and 0 < w["top"] - o["top"] < 14
                                                      and abs(o["x0"] - w["x0"]) < 15)
                   for o in words)]
    pts = [p for p in pts if p in layout["categories"]]
    if pts:
        out["points"] = max(pts)
        out["category"] = layout["categories"][out["points"]]
    out["released"] = _parse_released(words)
    return out


def _parse_champion(words, cw, pad):
    box = [w for w in words if w["x0"] >= cw["x0"] - pad and cw["top"] < w["top"] <= cw["top"] + 35]
    rows = _rows(box)
    if not rows:
        return None
    name_ws = rows[0][1]
    if len(name_ws) < 2:
        return None
    # «Arthur Fils» / «Aryna SABALENKA»: инициал -- первая буква имени, фамилия -- остальное
    first, surname = name_ws[0]["text"], " ".join(w["text"] for w in name_ws[1:])
    score, outcome = (None, "normal")
    if len(rows) > 1:
        score, outcome = _score_from_words(rows[1][1])
    return {"initial": first[:1] + ".", "surname": surname, "score": score, "outcome": outcome}


def round_code(n_rounds: int, rnd: int) -> str:
    """Код раунда по расстоянию до финала: F, SF, QF, R16, R32, R64, R128."""
    left = n_rounds - rnd  # 0 = финал
    return {0: "F", 1: "SF", 2: "QF"}.get(left, f"R{2 ** (left + 1)}")


def _build_rounds(entries, raw_results, n_rounds, warnings):
    """Дерево: участники каждого матча -- позиции из списка (или None = TBD)."""
    # кто прошёл из матча k раунда r: позиция или None
    advanced = {0: {p: (p if p in entries and not entries[p].get("bye") else None)
                    for p in range(1, 2 ** n_rounds + 1)}}
    rounds = []
    for rnd in range(1, n_rounds + 1):
        prev = advanced[rnd - 1]
        cur, matches = {}, []
        for k in range(1, 2 ** (n_rounds - rnd) + 1):
            top, bottom = prev.get(2 * k - 1), prev.get(2 * k)
            res = raw_results.get(rnd, {}).get(k)
            winner = None
            if res:
                winner = _match_candidate(res["initial"], res["surname"],
                                          [x for x in (top, bottom) if x], entries)
                if winner is None:
                    warnings.append(f"{round_code(n_rounds, rnd)} #{k}: победитель "
                                    f"{res['initial']} {res['surname']} не из пары {top}/{bottom}")
            if rnd == 1 and (top is None) != (bottom is None):
                # bye: проходит единственный участник, матча нет
                cur[k] = top or bottom
                continue
            cur[k] = winner
            if top is None and bottom is None and res is None:
                matches.append({"pos": k, "top": None, "bottom": None, "winner": None,
                                "score": None, "outcome": None})
                continue
            matches.append({
                "pos": k,
                "top": top,
                "bottom": bottom,
                "winner": None if winner is None else ("top" if winner == top else "bottom"),
                "score": res["score"] if res and winner is not None else None,
                "outcome": res["outcome"] if res and winner is not None else None,
            })
        advanced[rnd] = cur
        rounds.append({"code": round_code(n_rounds, rnd), "matches": matches})
    return rounds
