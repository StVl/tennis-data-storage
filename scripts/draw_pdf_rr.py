#!/usr/bin/env python3
"""Разбор PDF итогового турнира: групповой этап + полуфиналы и финал (Nitto ATP Finals,
protennislive.com/posting/<год>/605/mds.pdf). Вызывается из draw_pdf.parse_draw_pdf, когда
is_round_robin_page() узнала такой документ; разбор сеток на выбывание он не трогает.

Шаблон меняется из года в год (проверено на 2024, 2025 и 2026):
  - имена то «ALCARAZ, Carlos», то «Carlos Alcaraz»; страна то «(ESP)», то «[ESP]», то нет;
  - названия групп -- «ILIE NASTASE GROUP» / «John McEnroe Group»;
  - даты в шапке есть только с 2026 («15 November — 22 November 2026»).
Поэтому разбор держится не за координаты и формат имени, а за опорные слова, которые есть
во всех шаблонах:
  - «DAILY RESULTS <группа>» -- по одному на группу; группы стоят колонками слева направо,
    страница делится между ними поровну;
  - выше этой подписи -- таблица группы: строки «<посев> <имя> W L W ...»;
  - ниже -- результаты «<дата> <победитель> d. <проигравший> - <счёт>» до «<группа> - STANDINGS»;
  - внизу слева -- подписи «Semi-Final  Final  Champion»: в колонке полуфиналов четыре
    участника, в колонке финала -- победители полуфиналов со счётом под именем, в боксе
    чемпиона -- победитель финала со счётом (как колонки раундов обычной сетки).
Игрока в строке узнаём по фамилии участника группы внутри текста строки, так что порядок
«фамилия, имя» / «имя фамилия» и регистр значения не имеют.

Сетка в шарде:
  - entries -- 8 участников, позиции по группам (1-4 -- первая группа, 5-8 -- вторая);
  - groups -- [{name, positions}];
  - раунд RR -- все пары группы (6 на группу), сыгранные -- с победителем и счётом;
    pos = номер пары: группы по порядку, внутри -- пары позиций по возрастанию (стабилен
    и не зависит от того, в каком порядке PDF печатает результаты);
  - SF/F -- как у обычной сетки на 4: SF#1 = победитель 1-й группы против второго места
    2-й, SF#2 = победитель 2-й против второго места 1-й; top/bottom -- позиции участников.
"""

from __future__ import annotations

import itertools
import re

from draw_pdf import (MONTHS, PARTICLES, DrawParseError, _dedupe, _parse_dates, _rows,
                      _score_from_words, norm, proper_case)

DEFEATED = {"d.", "def.", "def", "bt.", "bt"}
SEPARATORS = {"-", "–", "—"}
COUNTRY_TAG_RE = re.compile(r"^[\[(]([A-Z]{3})[\])]$")
# слово имени: буквы, дефис, апостроф, точка инициала, запятая после фамилии; без цифр
NAME_WORD_RE = re.compile(r"^(?!.*\d)[^\W_][\w'’.\-]*,?$")
GRID_MARKS = {"W", "L"}  # колонки «Robin Round Record» в таблице группы
SCORE_WORDS = ("ret", "retired", "w/o", "walkover", "wo", "def", "default")
RELEASED_RE = re.compile(r"(\d{1,2}) ([A-Z][a-z]+),? (\d{4})(?: at)? (\d{1,2}):(\d{2})(?::(\d{2}))?")
# между колонками таблицы группы (имя -> W/L/цифры) разрыв больше этого
NAME_GAP = 30
# посев участника итогового турнира (8 + запасные); год «2024» и «15 November» шапки -- не посев
MAX_SEED = 12
# строка счёта -- не дальше этого под своим именем в колонке плей-офф
SCORE_BELOW = 16


def parse_round_robin_pdf(pdf) -> dict:
    page = pdf.pages[0]
    words = [w for w in _dedupe(page.extract_words(extra_attrs=["size", "fontname"]))
             if w.get("upright", True)]
    warnings = []

    dailies = sorted((w for w in words if w["text"] == "DAILY"), key=lambda w: w["x0"])
    if not dailies:
        raise DrawParseError("нет блоков DAILY RESULTS групп")
    band_w = float(page.width) / len(dailies)

    entries, groups = {}, []
    group_rows = []  # (позиции группы, строки результатов)
    for gi, daily in enumerate(dailies):
        bx0, bx1 = gi * band_w, (gi + 1) * band_w
        band = [w for w in words if bx0 <= w["x0"] < bx1]
        name = _group_name(band, daily)
        group_entries = _group_entries(band, daily, first_pos=len(entries) + 1)
        if not group_entries:
            warnings.append(f"группа «{name}»: не нашлись участники")
            continue
        for e in group_entries:
            entries[e["pos"]] = e
        positions = [e["pos"] for e in group_entries]
        groups.append({"name": name, "positions": positions})
        standings_y = min((w["top"] for w in band if w["text"] == "STANDINGS" and w["top"] > daily["top"] + 3),
                          default=float("inf"))
        result_rows = [ws for top, ws in _rows([w for w in band if daily["top"] + 3 < w["top"] < standings_y - 3])
                       if any(w["text"].lower() in DEFEATED for w in ws)]
        group_rows.append((positions, result_rows))

    if not entries:
        raise DrawParseError("в PDF нет участников групп")
    _assign_countries(words, entries)

    rounds = [{"code": "RR", "matches": _round_robin_matches(group_rows, entries, warnings)}]
    rounds += _knockout(words, page, entries, groups, warnings)

    return {
        **_header(words),
        "format": "round_robin",
        "groups": groups,
        "drawSize": len(entries),
        "slots": 4,
        "entries": [entries[p] for p in sorted(entries)],
        "rounds": rounds,
        "warnings": warnings,
    }


# ---------------------------------------------------------------------------
# Группы и участники
# ---------------------------------------------------------------------------

def _title_case(text):
    return " ".join(w.capitalize() for w in text.split()) if text.isupper() else text


def _group_name(band, daily):
    row = sorted((w for w in band if abs(w["top"] - daily["top"]) <= 3 and w["x0"] > daily["x0"]),
                 key=lambda w: w["x0"])
    return _title_case(" ".join(w["text"] for w in row if w["text"] != "RESULTS"))


def parse_name(text):
    """«ALCARAZ, Carlos» / «Carlos Alcaraz» / «Alex de Minaur» -> (first, last)."""
    text = text.strip()
    if "," in text:
        last_raw, _, first = text.partition(",")
        last_raw = last_raw.strip()
        return first.strip() or None, proper_case(last_raw) if last_raw.isupper() else last_raw
    parts = text.split()
    if text.isupper():
        parts = proper_case(text).split()
    if len(parts) == 1:
        return None, parts[0]
    cut = next((i for i in range(1, len(parts) - 1) if parts[i].lower() in PARTICLES), len(parts) - 1)
    return " ".join(parts[:cut]), " ".join(parts[cut:])


def _group_entries(band, daily, first_pos):
    """Таблица группы над «DAILY RESULTS»: «<посев> <имя> W L W 3 - 0 ...» -> entries."""
    out = []
    for top, ws in _rows([w for w in band if w["top"] < daily["top"] - 3]):
        if not ws[0]["text"].isdigit() or int(ws[0]["text"]) > MAX_SEED or len(ws) < 3:
            continue
        name_ws, prev, stop = [], ws[0], None
        for w in ws[1:]:
            if w["x0"] - prev["x1"] > NAME_GAP or w["text"] in GRID_MARKS or not NAME_WORD_RE.match(w["text"]):
                stop = w
                break
            name_ws.append(w)
            prev = w
        # строка таблицы: за именем -- W/L или цифры статистики (а не «— 22 November» шапки)
        if not name_ws or stop is None or not (stop["text"] in GRID_MARKS or stop["text"][:1].isdigit()):
            continue
        first, last = parse_name(" ".join(w["text"] for w in name_ws))
        out.append({
            "pos": first_pos + len(out),
            "last": last,
            "first": first,
            "truncated": False,
            "seed": int(ws[0]["text"]),
            "entry": None,
            "country": None,
        })
    return out


def match_entry(texts, entries):
    """Кто из entries назван в этих словах: фамилия участника -- подстрока текста."""
    key = norm(" ".join(texts))
    hits = [e for e in entries if norm(e["last"]) and norm(e["last"]) in key]
    if len(hits) > 1:
        longest = max(len(norm(e["last"])) for e in hits)
        hits = [e for e in hits if len(norm(e["last"])) == longest]
    if len(hits) > 1:
        hits = [e for e in hits if e.get("first") and norm(e["first"]) in key] or hits
    return hits[0] if len(hits) == 1 else None


def _assign_countries(words, entries):
    """Страна -- тег «[ESP]»/«(ESP)» сразу после имени, где бы он ни стоял (таблица
    standings, результаты, призовые). У нейтральных игроков тега нет."""
    pool = list(entries.values())
    for _, ws in _rows(words):
        buf = []
        for w in ws:
            tag = COUNTRY_TAG_RE.match(w["text"])
            if tag:
                e = match_entry([b["text"] for b in buf], pool)
                if e is not None and e["country"] is None:
                    e["country"] = tag.group(1)
                buf = []
            elif NAME_WORD_RE.match(w["text"]) and w["text"].lower() not in DEFEATED:
                buf.append(w)
            else:
                buf = []


# ---------------------------------------------------------------------------
# Групповой этап
# ---------------------------------------------------------------------------

def _is_score_word(text):
    return any(ch.isdigit() for ch in text) or text.lower().rstrip(".") in SCORE_WORDS


def _split_result(ws):
    """Строка «10 Nov FRITZ, Taylor (USA) d. MUSETTI, Lorenzo (ITA) - 63 64» ->
    (слова победителя, слова проигравшего, слова счёта)."""
    i = next(i for i, w in enumerate(ws) if w["text"].lower() in DEFEATED)
    winner, rest = ws[:i], ws[i + 1:]
    loser, score = [], []
    for j, w in enumerate(rest):
        if w["text"] in SEPARATORS or (loser and _is_score_word(w["text"]) and not COUNTRY_TAG_RE.match(w["text"])):
            score = [x for x in rest[j:] if x["text"] not in SEPARATORS]
            break
        loser.append(w)
    return winner, loser, score


def _round_robin_matches(group_rows, entries, warnings):
    matches, k = [], 0
    for positions, rows in group_rows:
        pool = [entries[p] for p in positions]
        results = {}
        for ws in rows:
            winner_ws, loser_ws, score_ws = _split_result(ws)
            w = match_entry([x["text"] for x in winner_ws], pool)
            lo = match_entry([x["text"] for x in loser_ws], pool)
            line = " ".join(x["text"] for x in ws)
            if w is None or lo is None or w is lo:
                warnings.append(f"RR: не разобрана строка результата «{line}»")
                continue
            pair = frozenset((w["pos"], lo["pos"]))
            if pair in results:
                warnings.append(f"RR: пара встречается дважды, оставлен первый результат: «{line}»")
                continue
            score, outcome = _score_from_words(score_ws)
            results[pair] = (w["pos"], score, outcome)
        for a, b in itertools.combinations(sorted(positions), 2):
            k += 1
            res = results.get(frozenset((a, b)))
            matches.append({
                "pos": k,
                "top": a,
                "bottom": b,
                "winner": None if res is None else ("top" if res[0] == a else "bottom"),
                "score": res[1] if res else None,
                "outcome": res[2] if res else None,
            })
    return matches


# ---------------------------------------------------------------------------
# Плей-офф
# ---------------------------------------------------------------------------

def _empty(pos):
    return {"pos": pos, "top": None, "bottom": None, "winner": None, "score": None, "outcome": None}


def _column_items(words, x0, x1, y0, y1, entries):
    """Колонка плей-офф: [(entry, слова счёта под именем)] сверху вниз."""
    items = []
    for top, ws in _rows([w for w in words if x0 <= w["x0"] < x1 and y0 < w["top"] < y1]):
        if _is_score_word(ws[0]["text"]):
            if items and not items[-1][2] and 0 < top - items[-1][1] <= SCORE_BELOW:
                items[-1][2].extend(ws)
            continue
        e = match_entry([w["text"] for w in ws], entries)
        if e is not None:
            items.append([e, top, []])
    return [(e, score) for e, _, score in items]


def _semifinal_slots(names, groups, warnings):
    """Участники полуфиналов сверху вниз -> [SF1 top, SF1 bottom, SF2 top, SF2 bottom].

    PDF печатает их в порядке «1-я группа #1, 2-я #2, 2-я #1, 1-я #2» (2024, 2025): игроки
    1-й группы сверху вниз -- слоты 0 и 3, 2-й -- слоты 1 и 2. Пока одна группа ещё играет,
    в колонке только двое из другой -- поэтому раскладываем по группам, а не по порядку."""
    slots = [None] * 4
    if len(groups) != 2:
        for i, e in enumerate(names[:4]):
            slots[i] = e["pos"]
        return slots
    for gi, (hi, lo) in enumerate(((0, 3), (1, 2))):
        mine = [e for e in names if e["pos"] in groups[gi]["positions"]]
        if len(mine) == 2:
            slots[hi], slots[lo] = mine[0]["pos"], mine[1]["pos"]
        elif mine:
            warnings.append(f"SF: из группы «{groups[gi]['name']}» в колонке один игрок, пропуск")
    return slots


def _knockout_match(pos, top, bottom, winners, warnings, code):
    m = {**_empty(pos), "top": top, "bottom": bottom}
    for e, score_ws in winners:
        if e["pos"] in (top, bottom) and None not in (top, bottom):
            score, outcome = _score_from_words(score_ws)
            m.update(winner="top" if e["pos"] == top else "bottom", score=score, outcome=outcome)
            break
    else:
        stray = [e for e, _ in winners if e["pos"] not in (top, bottom)]
        if top and bottom and stray and len(winners) == 1:
            warnings.append(f"{code} #{pos}: победитель {stray[0]['last']} не из пары {top}/{bottom}")
    return m


def _knockout(words, page, entries, groups, warnings):
    sf = next((w for w in words if w["text"] == "Semi-Final"), None)
    if sf is None:
        warnings.append("нет подписи Semi-Final: плей-офф не разобран")
        return [{"code": "SF", "matches": [_empty(1), _empty(2)]}, {"code": "F", "matches": [_empty(1)]}]
    row = [w for w in words if abs(w["top"] - sf["top"]) <= 3 and w["x0"] > sf["x0"]]
    fin = next((w for w in row if w["text"] == "Final"), None)
    champ = next((w for w in row if w["text"] == "Champion"), None)
    footer = min((w["top"] for w in words if w["text"] in ("Director", "Referee", "Supervisor")
                  and w["top"] > sf["top"]), default=float(page.height))
    # колонки -- от середины между центрами подписей; правее бокса чемпиона -- таблица призовых
    c = [(w["x0"] + w["x1"]) / 2 for w in (sf, fin, champ) if w is not None]
    bounds = [0.0] + [(a + b) / 2 for a, b in zip(c, c[1:])] + [c[-1] + (c[-1] - c[-2]) / 2 if len(c) > 1 else c[-1] + 60]
    y0, y1 = sf["top"] + 3, footer - 11
    pool = list(entries.values())
    cols = [_column_items(words, bounds[i], bounds[i + 1], y0, y1, pool) for i in range(len(c))]

    slots = _semifinal_slots([e for e, _ in cols[0]], groups, warnings)
    sf_winners = cols[1] if len(cols) > 1 else []
    champion = cols[2] if len(cols) > 2 else []
    semis = [_knockout_match(1, slots[0], slots[1], sf_winners, warnings, "SF"),
             _knockout_match(2, slots[2], slots[3], sf_winners, warnings, "SF")]

    def advanced(m):
        return None if m["winner"] is None else m[m["winner"]]

    final = _knockout_match(1, advanced(semis[0]), advanced(semis[1]), champion, warnings, "F")
    return [{"code": "SF", "matches": semis}, {"code": "F", "matches": [final]}]


# ---------------------------------------------------------------------------
# Шапка
# ---------------------------------------------------------------------------

def _header(words):
    rows = [" ".join(w["text"] for w in ws) for _, ws in _rows(words)]
    top_rows = [" ".join(w["text"] for w in ws) for _, ws in _rows([w for w in words if w["top"] < 75])]
    out = {"title": top_rows[0] if top_rows else None, "location": None,
           "startDate": None, "endDate": None, "points": None, "category": None, "released": None}
    for line in top_rows[1:]:
        dates = _parse_dates(line)
        if dates:
            out["startDate"], out["endDate"] = dates
        elif out["location"] is None and "," in line and not any(ch.isdigit() for ch in line):
            out["location"] = line
    for line in rows:
        m = RELEASED_RE.search(line)
        if m and m.group(2) in MONTHS:
            d, mon, y, hh, mm, ss = m.groups()
            out["released"] = f"{y}-{MONTHS[mon]:02d}-{int(d):02d}T{int(hh):02d}:{mm}:{ss or '00'}"
    return out
