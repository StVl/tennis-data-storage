"""Счёт в записи LLM-шарда: «6-4, 7-6(5), 1-3 ret.» <-> [(геймы_a, геймы_b, тай-брейк)].

Без psycopg: модуль нужен и build_config.py (проверка правдоподобия), и migrate_data.py."""

import re

SET_RE = re.compile(r"^(\d+)-(\d+)(?:\((\d+|\?)\))?$")
RET_MARKERS = ("ret.", "(retired)", "(opponent retired)", "retired")
WO_MARKERS = ("w/o", "(walkover)", "walkover")


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


def format_score(sets, outcome="normal"):
    """Обратная parse_score: [(a, b, tb)] глазами владельца записи -> «6-4, 7-6(5)»."""
    if outcome == "walkover":
        return "w/o"
    text = ", ".join(f"{a}-{b}" + (f"({tb})" if tb is not None else "") for a, b, tb in sets)
    return f"{text} ret." if outcome == "retirement" else text
