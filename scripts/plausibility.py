"""Правдоподобие свежего результата из LLM-шарда -- детерминированный фильтр после агента.

2026-11-22 агент матчей в 19:01 записал финал ATP Finals (начало 18:00, матч ещё шёл)
завершённым со счётом 6-4, 6-1 -- это счёт Alcaraz-Cobolli из группы двумя днями раньше.
Промпт выдумывать результат запрещает, но из проверок была только «начало не в будущем».

Неправдоподобный результат не роняет сборку, а возвращается в upcoming вместе с зеркальной
записью: следующий прогон агента посмотрит матч заново. Ошибка валидации здесь была бы хуже --
она останавливает пуш config.json целиком, а migrate_data.py заливает шарды и без него.

Проверяется только результат моложе FRESH_WINDOW от начала матча. В истории десятки неполных
счетов («1-5», «7-6, 4-6»): вернуть их в upcoming значило бы показать давно сыгранные матчи
предстоящими. Поэтому и проверять надо сразу после агента -- через FRESH_WINDOW любой
результат проходит.
"""

import datetime

from score import parse_score

FRESH_WINDOW = datetime.timedelta(hours=12)
# startAt -- время по расписанию («not before»): матч начинается позже, а финал -- ещё и после
# церемонии.
START_SLACK = datetime.timedelta(minutes=15)
# Вместе со сменами сторон гейм длится дольше трёх минут даже в очень быстром матче.
MIN_PER_GAME = datetime.timedelta(minutes=3)


def parse_start(value):
    if not value:
        return None
    try:
        return datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def complete_set(a, b):
    hi, lo = max(a, b), min(a, b)
    return (hi == 6 and lo <= 4) or (hi == 7 and lo in (5, 6))


def implausible(m, now):
    """Почему completed-запись нельзя принять сейчас, или None.

    now -- aware datetime. Запись без startAt или старше FRESH_WINDOW не проверяется."""
    start = parse_start(m.get("startAt"))
    if start is None or now - start >= FRESH_WINDOW:
        return None
    if start > now:
        return f"матч ещё не начался (startAt {m['startAt']})"

    warn = []
    sets, outcome = parse_score(m.get("score"), warn)
    if warn:
        return f"счёт не разобран: {m.get('score')!r}"
    if outcome == "walkover":
        return None
    if m.get("result") not in ("won", "lost"):
        return f"нет result ({m.get('result')!r})"
    if not sets:
        return "нет счёта"
    # при отказе последний сет не доигран -- его форму не проверяем
    played = sets if outcome == "normal" else sets[:-1]
    if not all(complete_set(a, b) for a, b, _ in played):
        return f"невозможный счёт сета: {m['score']!r}"
    if outcome == "normal":
        won_a = sum(1 for a, b, _ in sets if a > b)
        won_b = len(sets) - won_a
        hi, lo = max(won_a, won_b), min(won_a, won_b)
        if hi not in (2, 3) or lo >= hi:
            return f"матч не доигран по сетам: {m['score']!r}"
        last_a, last_b, _ = sets[-1]
        if (last_a > last_b) != (won_a > won_b):
            return f"последний сет взял проигравший: {m['score']!r}"

    games = sum(a + b for a, b, _ in sets)
    earliest = start + START_SLACK + games * MIN_PER_GAME
    if now < earliest:
        minutes = int((now - start).total_seconds() // 60)
        return (f"{games} геймов за {minutes} мин от startAt {m['startAt']} не сыграть: "
                f"раньше {earliest.isoformat(timespec='minutes')} матч не закончить")
    return None


def mirror_key(m):
    pair = frozenset(x for x in (m.get("playerId"), m.get("opponentId")) if x and x != "TBD")
    return (m.get("tournament"), m.get("stage"), m.get("startAt"), pair)


def back_to_upcoming(m):
    out = {k: v for k, v in m.items() if k not in ("status", "result", "score")}
    out.update(isLive=False, liveScore=None, status="upcoming")
    return out


def quarantine(upcoming, past, now):
    """Неправдоподобные свежие результаты из past -- обратно в upcoming, списки правятся на месте.

    Зеркальная запись (тот же матч глазами соперника) уходит вместе с забракованной: одна
    оставшаяся в past всё равно завершила бы матч при заливке. Возвращает [(id, причина)]."""
    reasons = {}
    for m in past:
        if m.get("status") == "completed":
            reason = implausible(m, now)
            if reason:
                reasons[m["id"]] = reason
    if not reasons:
        return []
    rejected = {mirror_key(m) for m in past if m["id"] in reasons}
    moved, keep = [], []
    for m in past:
        if m.get("status") == "completed" and mirror_key(m) in rejected:
            upcoming.append(back_to_upcoming(m))
            moved.append((m["id"], reasons.get(m["id"], "зеркальная запись забракованной")))
        else:
            keep.append(m)
    past[:] = keep
    return moved
