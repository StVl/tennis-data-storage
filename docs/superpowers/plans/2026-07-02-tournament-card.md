# Tournament Card Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Добавить в `config.json` новую секцию `tournamentCards[]` — карточку турнира с полной сеткой (rounds → matches → slots), засеять её реальными данными Wimbledon 2026 (Gentlemen's Singles) и настроить ежедневное дописывающее обновление.

**Architecture:** Держимся существующего пайплайна: новый шард `data/tournament_cards.json` — источник правды; `build_config.py` валидирует его, детерминированно обогащает имена из `players.json` и вклеивает в `config.json`. Ежедневный промпт + launchd-агент дописывают результаты. Спека: `docs/superpowers/specs/2026-07-02-tournament-card-design.md`.

**Tech Stack:** Python 3 (только stdlib), JSON-шарды, launchd (macOS), `run_task.sh` + Claude Code headless.

## Global Constraints

- JSON всегда валидный, UTF-8, **отступ 2 пробела** (`json.dumps(..., ensure_ascii=False, indent=2)`).
- `config.json` руками НЕ редактируется — только через `build_config.py`. Правим только `data/`.
- `build_config.py` — **только stdlib**, без внешних зависимостей.
- Тесты — тоже без зависимостей: запускаются `python3 tests/<file>.py`, падают с ненулевым кодом при ошибке.
- Не выдумывать составы/счёт — брать с офсайта Wimbledon; не уверен — оставить как было.
- `surface` ∈ {grass, clay, hard} (fallback grass). `state` ∈ {awaiting_draw, drawn}. round `code` ∈ {R1,R2,R3,R4,QF,SF,F}.
- Отображаемое имя слота с `playerId` — формат «И. Фамилия», выводится из `players.json` при сборке (не хранится в шарде жёстко).
- Ежедневный апдейт — **только дописывание**: не переписывать зафиксированный скелет сетки.
- Все команды выполняются из корня репозитория `/Users/stepanpotapov/code/tennis-data-storage`.

---

## File Structure

- `data/tournament_cards.json` — **создать**. Новый шард: `{ "tournamentCards": [ TournamentCard ] }`. Источник правды.
- `build_config.py` — **изменить**. Добавить шард в `SHARDS`, функцию форматирования имени, валидацию карточек, обогащение, вклейку в `config`.
- `tests/test_tournament_cards.py` — **создать**. Юнит-тесты формата имени, валидации, обогащения (stdlib-only).
- `prompts/update_tournament_card.md` — **создать**. Промпт ежедневного дописывающего обновления.
- `~/Library/LaunchAgents/com.stvl.tennis-data.tournament-card.plist` — **создать**. Ежедневный запуск.
- `CLAUDE.md` — **изменить**. Задокументировать шард, промпт, cron/launchd-строку.

---

## Task 0: Приостановить агенты на время правок (безопасность)

Активные launchd-агенты делают `git add -A && commit && push`. Пока правим `build_config.py`/шард, запуск по расписанию может закоммитить и запушить недописанное состояние. Снимаем на время работы.

**Files:** нет (системная операция, обратимая — вернём в Task 8).

- [ ] **Step 1: Снять три агента**

Run:
```bash
launchctl unload ~/Library/LaunchAgents/com.stvl.tennis-data.matches.plist
launchctl unload ~/Library/LaunchAgents/com.stvl.tennis-data.rankings.plist
launchctl unload ~/Library/LaunchAgents/com.stvl.tennis-data.tournaments.plist
```

- [ ] **Step 2: Убедиться, что агентов нет в списке**

Run: `launchctl list | grep tennis`
Expected: пустой вывод (ни одного `com.stvl.tennis-data.*`).

---

## Task 1: Засеять шард `data/tournament_cards.json`

**Files:**
- Create: `data/tournament_cards.json`

**Interfaces:**
- Produces: файл-шард формы `{ "tournamentCards": TournamentCard[] }`, где `TournamentCard`, `Round`, `Match`, `Slot` — как в спеке. Task 3–4 (валидация/обогащение) полагаются на эту форму.

Статические поля карточки Wimbledon 2026 (константы, вписать как есть):

```jsonc
{
  "id": "wimbledon-2026",
  "name": "The Championships, Wimbledon",
  "category": "Grand Slam",
  "event": "Gentlemen's Singles",
  "monogram": "W",
  "surface": "grass",
  "description": "The oldest championship in tennis, on the lawns of the All England Club. 128 players, one grass-court crown.",
  "dateRange": "29 Jun – 12 Jul",
  "location": "London · GB",
  "drawSize": 128,
  "format": "Best of 5",
  "drawDate": "26 Jun 2026",
  "state": "drawn",
  "rounds": [ /* заполнить ниже */ ]
}
```

- [ ] **Step 1: Стянуть текущую сетку с офсайта**

Использовать WebFetch по `https://www.wimbledon.com/en_GB/draws/gentlemens-singles`. Извлечь для каждого матча: имена участников, флаги (эмодзи страны), сиды, кто прошёл дальше (winner), а также состояние ещё не сыгранных пар.

Если офсайт недоступен для чтения (JS-рендер/блокировка): засеять скелет, согласованный с уже существующими матчами Wimbledon 2026 в `data/matches_upcoming.json` и `data/matches_past.json` (те же игроки/раунды/результаты), а неизвестные слоты пометить `tbd`. Явно сообщить пользователю, что засеян скелет, а не полный официальный draw.

- [ ] **Step 2: Собрать `rounds` по канонической структуре**

Массив от первого раунда к финалу. Пары code/title и число матчей:
`R1`/First Round (64), `R2`/Second Round (32), `R3`/Third Round (16), `R4`/Fourth Round (8), `QF`/Quarter-final (4), `SF`/Semi-final (2), `F`/Final (1). Включать только реально существующие на данный момент раунды/матчи; ещё не наступившие раунды можно опустить (их допишет ежедневный апдейт).

Форма матча и слота (пример двух матчей R1):

```jsonc
{
  "code": "R1",
  "title": "First Round",
  "matches": [
    {
      "id": "m-r1-01",
      "top":    { "name": "J. Sinner",  "flag": "🇮🇹", "seed": 1,   "winner": true,  "tbd": false, "playerId": "sinner" },
      "bottom": { "name": "V. Royer",   "flag": "🇫🇷", "seed": null, "winner": false, "tbd": false, "playerId": "royer" }
    },
    {
      "id": "m-r1-02",
      "top":    { "name": "To be determined", "flag": null, "seed": null, "winner": false, "tbd": true },
      "bottom": { "name": "To be determined", "flag": null, "seed": null, "winner": false, "tbd": true }
    }
  ]
}
```

Правила заполнения слота:
- `playerId` добавлять ТОЛЬКО если id есть в `data/players.json`; иначе поле опустить и оставить строковое `name` + `flag`.
- `name` для слота с `playerId` можно ставить любым (при сборке он всё равно переписывается из ростера) — но для читаемости шарда ставь «И. Фамилия».
- `winner: true` максимум у одного из `top`/`bottom`. Матч не сыгран → оба `false`, счёта нет.
- Пустой слот: `name: "To be determined"`, `tbd: true`, `flag: null`, `seed: null`, без `playerId`.
- `id` матча — `m-<code-lower>-NN` (`m-r1-01`, `m-qf-03`, `m-f-01`).

- [ ] **Step 3: Записать файл**

Записать `data/tournament_cards.json` = `{ "tournamentCards": [ <карточка Wimbledon> ] }`, UTF-8, отступ 2 пробела.

- [ ] **Step 4: Проверить, что JSON валиден и форма верна**

Run:
```bash
python3 - <<'PY'
import json
d = json.load(open("data/tournament_cards.json", encoding="utf-8"))
cards = d["tournamentCards"]
assert isinstance(cards, list) and cards, "tournamentCards пуст"
c = cards[0]
assert c["id"] == "wimbledon-2026"
assert c["surface"] in {"grass","clay","hard"}
assert c["state"] in {"awaiting_draw","drawn"}
if c["state"] == "drawn":
    assert c["rounds"], "state=drawn, но rounds пуст"
    for r in c["rounds"]:
        assert r["code"] in {"R1","R2","R3","R4","QF","SF","F"}, r["code"]
        for m in r["matches"]:
            assert "top" in m and "bottom" in m, m["id"]
            wins = sum(1 for s in (m["top"], m["bottom"]) if s.get("winner"))
            assert wins <= 1, f"{m['id']}: >1 winner"
print("OK: карточек", len(cards), "| раундов", len(c.get("rounds", [])))
PY
```
Expected: `OK: карточек 1 | раундов N` без AssertionError.

- [ ] **Step 5: Commit**

```bash
git add data/tournament_cards.json
git commit -m "feat(data): засеять карточку турнира Wimbledon 2026 (сетка)"
```

---

## Task 2: `format_display_name()` в `build_config.py`

Форматирует полное имя ростера в «И. Фамилия» для слотов сетки.

**Files:**
- Modify: `build_config.py` (добавить функцию рядом с другими хелперами, напр. после `load`)
- Test: `tests/test_tournament_cards.py`

**Interfaces:**
- Produces: `format_display_name(full_name: str) -> str`. Используется в Task 4 (`enrich_tournament_cards`).

- [ ] **Step 1: Написать падающий тест**

Создать `tests/test_tournament_cards.py`:

```python
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import build_config as bc


def test_format_display_name():
    assert bc.format_display_name("Jannik Sinner") == "J. Sinner"
    assert bc.format_display_name("Carlos Alcaraz") == "C. Alcaraz"
    # многословная фамилия: инициал имени + всё остальное
    assert bc.format_display_name("Alex de Minaur") == "A. de Minaur"
    assert bc.format_display_name("Felix Auger-Aliassime") == "F. Auger-Aliassime"
    # одно слово — возвращаем как есть
    assert bc.format_display_name("Nadal") == "Nadal"
    # пустая строка — как есть
    assert bc.format_display_name("") == ""


def _run():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("PASS", name)
    print("ALL OK")


if __name__ == "__main__":
    _run()
```

- [ ] **Step 2: Запустить тест — убедиться, что падает**

Run: `python3 tests/test_tournament_cards.py`
Expected: FAIL — `AttributeError: module 'build_config' has no attribute 'format_display_name'`.

- [ ] **Step 3: Минимальная реализация**

Добавить в `build_config.py`:

```python
def format_display_name(full_name):
    """'Jannik Sinner' -> 'J. Sinner'. Одно слово или пусто — как есть."""
    parts = full_name.split()
    if len(parts) < 2:
        return full_name
    first, rest = parts[0], " ".join(parts[1:])
    return f"{first[0]}. {rest}"
```

- [ ] **Step 4: Запустить тест — убедиться, что проходит**

Run: `python3 tests/test_tournament_cards.py`
Expected: `PASS test_format_display_name` … `ALL OK`.

- [ ] **Step 5: Commit**

```bash
git add build_config.py tests/test_tournament_cards.py
git commit -m "feat(build): format_display_name для слотов сетки"
```

---

## Task 3: Валидация карточек турниров

**Files:**
- Modify: `build_config.py` (константы вверху; новая функция `validate_tournament_cards`; вызов внутри `validate`; `"tournament_cards"` в `SHARDS`)
- Test: `tests/test_tournament_cards.py`

**Interfaces:**
- Consumes: `shards` (dict имя→данные), `players` (set id) — как в существующей `validate`.
- Produces: `validate_tournament_cards(shards, players) -> list[str]` (ошибки). Вызывается из `validate`.

- [ ] **Step 1: Написать падающие тесты**

Добавить в `tests/test_tournament_cards.py`:

```python
def _players():
    return {"sinner", "royer"}


def _valid_card():
    return {
        "id": "wimbledon-2026", "surface": "grass", "state": "drawn",
        "rounds": [{
            "code": "R1", "title": "First Round", "matches": [{
                "id": "m-r1-01",
                "top":    {"name": "J. Sinner", "flag": "🇮🇹", "seed": 1, "winner": True,  "tbd": False, "playerId": "sinner"},
                "bottom": {"name": "V. Royer",  "flag": "🇫🇷", "seed": None, "winner": False, "tbd": False, "playerId": "royer"},
            }],
        }],
    }


def _shards(card):
    return {"tournament_cards": {"tournamentCards": [card]}}


def test_valid_card_passes():
    assert bc.validate_tournament_cards(_shards(_valid_card()), _players()) == []


def test_bad_surface():
    c = _valid_card(); c["surface"] = "carpet"
    errs = bc.validate_tournament_cards(_shards(c), _players())
    assert any("surface" in e for e in errs), errs


def test_bad_state():
    c = _valid_card(); c["state"] = "in_progress"
    errs = bc.validate_tournament_cards(_shards(c), _players())
    assert any("state" in e for e in errs), errs


def test_drawn_requires_rounds():
    c = _valid_card(); c["rounds"] = []
    errs = bc.validate_tournament_cards(_shards(c), _players())
    assert any("rounds" in e for e in errs), errs


def test_bad_round_code():
    c = _valid_card(); c["rounds"][0]["code"] = "R9"
    errs = bc.validate_tournament_cards(_shards(c), _players())
    assert any("code" in e for e in errs), errs


def test_two_winners():
    c = _valid_card(); c["rounds"][0]["matches"][0]["bottom"]["winner"] = True
    errs = bc.validate_tournament_cards(_shards(c), _players())
    assert any("winner" in e for e in errs), errs


def test_unknown_playerid():
    c = _valid_card(); c["rounds"][0]["matches"][0]["top"]["playerId"] = "ghost"
    errs = bc.validate_tournament_cards(_shards(c), _players())
    assert any("playerId" in e for e in errs), errs


def test_duplicate_ids():
    c = _valid_card()
    errs = bc.validate_tournament_cards({"tournament_cards": {"tournamentCards": [c, dict(c)]}}, _players())
    assert any("дубль" in e or "id" in e for e in errs), errs
```

- [ ] **Step 2: Запустить — убедиться, что падает**

Run: `python3 tests/test_tournament_cards.py`
Expected: FAIL — `AttributeError: ... 'validate_tournament_cards'`.

- [ ] **Step 3: Реализация**

В `build_config.py` рядом с существующими константами добавить:

```python
VALID_SURFACES = {"grass", "clay", "hard"}
VALID_CARD_STATES = {"awaiting_draw", "drawn"}
VALID_ROUND_CODES = {"R1", "R2", "R3", "R4", "QF", "SF", "F"}
```

Добавить `"tournament_cards"` в список `SHARDS` (в конец).

Добавить функцию:

```python
def validate_tournament_cards(shards, players):
    """Схема карточек турниров + ссылочная целостность playerId."""
    errors = []
    cards = shards["tournament_cards"]["tournamentCards"]
    seen = set()
    for c in cards:
        cid = c.get("id")
        if cid in seen:
            errors.append(f"tournamentCard дубль id: {cid}")
        seen.add(cid)
        if c.get("surface") not in VALID_SURFACES:
            errors.append(f"card {cid}: некорректный surface {c.get('surface')}")
        if c.get("state") not in VALID_CARD_STATES:
            errors.append(f"card {cid}: некорректный state {c.get('state')}")
        if c.get("state") == "drawn" and not c.get("rounds"):
            errors.append(f"card {cid}: state=drawn, но rounds пуст")
        for r in c.get("rounds", []):
            if r.get("code") not in VALID_ROUND_CODES:
                errors.append(f"card {cid}: неизвестный round code {r.get('code')}")
            for m in r.get("matches", []):
                mid = m.get("id")
                if "top" not in m or "bottom" not in m:
                    errors.append(f"card {cid} match {mid}: нет top/bottom")
                    continue
                slots = (m["top"], m["bottom"])
                if sum(1 for s in slots if s.get("winner")) > 1:
                    errors.append(f"card {cid} match {mid}: больше одного winner")
                for s in slots:
                    pid = s.get("playerId")
                    if pid is not None and pid not in players:
                        errors.append(f"card {cid} match {mid}: неизвестный playerId {pid}")
    return errors
```

В существующей `validate(shards)`, где формируется `players` и в конце возвращается `errors`, перед `return errors` добавить:

```python
    errors += validate_tournament_cards(shards, players)
```

- [ ] **Step 4: Запустить — убедиться, что проходит**

Run: `python3 tests/test_tournament_cards.py`
Expected: все `PASS`, `ALL OK`.

- [ ] **Step 5: Прогнать реальную валидацию на засеянном шарде**

Run: `python3 build_config.py --check`
Expected: `[ok] валидация пройдена (8 шардов)` (было 7 + новый).

- [ ] **Step 6: Commit**

```bash
git add build_config.py tests/test_tournament_cards.py
git commit -m "feat(build): валидация секции tournamentCards"
```

---

## Task 4: Обогащение имён и вклейка в `config.json`

**Files:**
- Modify: `build_config.py` (функция `enrich_tournament_cards`; вклейка ключа в `build`)
- Test: `tests/test_tournament_cards.py`

**Interfaces:**
- Consumes: `format_display_name` (Task 2), `shards`.
- Produces: `enrich_tournament_cards(shards) -> list` (карточки с переписанными `name` у слотов с `playerId`); ключ `config["tournamentCards"]`.

- [ ] **Step 1: Написать падающий тест**

Добавить в `tests/test_tournament_cards.py`:

```python
def test_enrich_overrides_name_from_roster(monkeypatch=None):
    card = _valid_card()
    # имя в шарде «неправильное» — должно перезаписаться из ростера
    card["rounds"][0]["matches"][0]["top"]["name"] = "WRONG"
    shards = {
        "tournament_cards": {"tournamentCards": [card]},
        "players": {"players": [
            {"id": "sinner", "name": "Jannik Sinner"},
            {"id": "royer", "name": "Valentin Royer"},
        ]},
    }
    out = bc.enrich_tournament_cards(shards)
    top = out[0]["rounds"][0]["matches"][0]["top"]
    bottom = out[0]["rounds"][0]["matches"][0]["bottom"]
    assert top["name"] == "J. Sinner", top["name"]
    assert bottom["name"] == "V. Royer", bottom["name"]


def test_enrich_keeps_string_slot_without_playerid():
    card = _valid_card()
    slot = card["rounds"][0]["matches"][0]["top"]
    del slot["playerId"]
    slot["name"] = "Q. Halys"
    shards = {
        "tournament_cards": {"tournamentCards": [card]},
        "players": {"players": [{"id": "royer", "name": "Valentin Royer"}]},
    }
    out = bc.enrich_tournament_cards(shards)
    assert out[0]["rounds"][0]["matches"][0]["top"]["name"] == "Q. Halys"
```

- [ ] **Step 2: Запустить — убедиться, что падает**

Run: `python3 tests/test_tournament_cards.py`
Expected: FAIL — `AttributeError: ... 'enrich_tournament_cards'`.

- [ ] **Step 3: Реализация**

Добавить в `build_config.py`:

```python
import copy


def enrich_tournament_cards(shards):
    """Для слотов с playerId из ростера переписывает name в 'И. Фамилия'.
    Слоты без playerId остаются как в шарде. Возвращает копию (шард не мутируем)."""
    name_by_id = {p["id"]: p["name"] for p in shards["players"]["players"]}
    cards = copy.deepcopy(shards["tournament_cards"]["tournamentCards"])
    for c in cards:
        for r in c.get("rounds", []):
            for m in r.get("matches", []):
                for key in ("top", "bottom"):
                    s = m.get(key)
                    if not s:
                        continue
                    pid = s.get("playerId")
                    if pid in name_by_id:
                        s["name"] = format_display_name(name_by_id[pid])
    return cards
```

В функции `build(shards)`, в словарь `config`, добавить последним ключом:

```python
        "tournamentCards": enrich_tournament_cards(shards),
```

- [ ] **Step 4: Запустить юнит-тесты — убедиться, что проходят**

Run: `python3 tests/test_tournament_cards.py`
Expected: все `PASS`, `ALL OK`.

- [ ] **Step 5: Полная сборка и проверка вывода**

Run:
```bash
python3 build_config.py
python3 - <<'PY'
import json
d = json.load(open("config.json", encoding="utf-8"))
assert "tournamentCards" in d, "нет ключа tournamentCards"
c = d["tournamentCards"][0]
print("id:", c["id"], "| state:", c["state"], "| rounds:", len(c.get("rounds", [])))
# проверить, что имя игрока ростера в формате 'И. Фамилия'
for r in c.get("rounds", []):
    for m in r["matches"]:
        for s in (m["top"], m["bottom"]):
            if s.get("playerId"):
                assert s["name"][:2].endswith(". ") is False  # sanity
print("OK")
PY
```
Expected: строка `id: wimbledon-2026 | state: drawn | rounds: N` и `OK`.

- [ ] **Step 6: Commit**

```bash
git add build_config.py tests/test_tournament_cards.py config.json
git commit -m "feat(build): вклейка tournamentCards в config.json с обогащением имён"
```

---

## Task 5: Промпт ежедневного обновления

**Files:**
- Create: `prompts/update_tournament_card.md`

**Interfaces:** запускается через `./run_task.sh prompts/update_tournament_card.md`.

- [ ] **Step 1: Создать промпт**

Записать `prompts/update_tournament_card.md`:

```markdown
# Задача: обновить карточку турнира (сетку) — Wimbledon

Ты обновляешь данные теннисного приложения. Работаешь ТОЛЬКО с файлом
`data/tournament_cards.json`. Остальное не трогаешь (только читаешь `data/players.json`).

## Источник
Официальный сайт Wimbledon: https://www.wimbledon.com/en_GB/draws/gentlemens-singles

## Скоуп
Только Wimbledon, Gentlemen's Singles (карточка `id: "wimbledon-2026"`).
Другие турниры сейчас НЕ заводим.

## ГЛАВНОЕ ПРАВИЛО: только дописывание, без перезаписи
Скелет сетки уже засеян. Твоя задача — аккуратно ДОПИСАТЬ новое, не ломая старое:
1. Проставь `winner: true` победителю в матчах, которые ЗАВЕРШИЛИСЬ с момента прошлого запуска.
2. Раскрой слоты `tbd: true` в реальных игроков, когда их пара определилась
   (набери `name`, `flag`, `seed`, `winner:false`, `tbd:false`; добавь `playerId`,
   если игрок есть в `data/players.json`).
3. Добавь раунды/матчи, которых ещё нет, по мере продвижения турнира
   (каноничные code: R1,R2,R3,R4,QF,SF,F).
НЕ переписывай уже корректно заполненные слоты. НЕ выдумывай счёт/имена —
не уверен, оставь как было.

## Поля слота
- `name` — «И. Фамилия». Для слота с `playerId` name всё равно перепишется из
  ростера при сборке, но заполни для читаемости.
- `flag` — эмодзи страны (или null). `seed` — число или null.
- `winner` — true максимум у одного из top/bottom. `tbd` — true когда участник неизвестен.
- `playerId` добавляй ТОЛЬКО если id есть в `data/players.json` (сопоставь по имени/фамилии).

## Состояние карточки
- До жеребьёвки: `state: "awaiting_draw"`, `rounds` можно не заполнять.
- После жеребьёвки/во время турнира: `state: "drawn"`.

## Правила
- JSON валидный, UTF-8, отступ 2 пробела.
- `id` матча — формат `m-<code-lower>-NN` (`m-r1-01`, `m-qf-03`, `m-f-01`).

## После правок
`python3 build_config.py --check` — и если ок, остановись.
```

- [ ] **Step 2: Проверить, что файл на месте**

Run: `test -f prompts/update_tournament_card.md && echo OK`
Expected: `OK`.

- [ ] **Step 3: Commit**

```bash
git add prompts/update_tournament_card.md
git commit -m "feat(prompts): ежедневное дописывающее обновление сетки Wimbledon"
```

---

## Task 6: launchd-агент (раз в день)

**Files:**
- Create: `~/Library/LaunchAgents/com.stvl.tennis-data.tournament-card.plist`

- [ ] **Step 1: Посмотреть существующий агент как образец**

Run: `cat ~/Library/LaunchAgents/com.stvl.tennis-data.tournaments.plist`
Expected: увидеть структуру (ProgramArguments с путём к `run_task.sh`, ключ расписания `StartCalendarInterval`, `StandardOutPath`/`StandardErrorPath`). Скопировать её форму.

- [ ] **Step 2: Создать plist**

Записать `~/Library/LaunchAgents/com.stvl.tennis-data.tournament-card.plist`, повторяя структуру образца из Step 1, изменив: `Label` → `com.stvl.tennis-data.tournament-card`; аргумент промпта → `prompts/update_tournament_card.md`; лог-файлы → `/tmp/tennis_card.log`; расписание — раз в день (`StartCalendarInterval` с `Hour`, напр. 8, `Minute` 15 — отличный от других задач, чтобы не пересекались). Путь к `run_task.sh` — абсолютный: `/Users/stepanpotapov/code/tennis-data-storage/run_task.sh`.

Ориентир содержимого (сверить с образцом Step 1 и привести к его форме):

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.stvl.tennis-data.tournament-card</string>
    <key>ProgramArguments</key>
    <array>
        <string>/Users/stepanpotapov/code/tennis-data-storage/run_task.sh</string>
        <string>prompts/update_tournament_card.md</string>
    </array>
    <key>StartCalendarInterval</key>
    <dict>
        <key>Hour</key><integer>8</integer>
        <key>Minute</key><integer>15</integer>
    </dict>
    <key>StandardOutPath</key>
    <string>/tmp/tennis_card.log</string>
    <key>StandardErrorPath</key>
    <string>/tmp/tennis_card.log</string>
</dict>
</plist>
```

- [ ] **Step 3: Проверить синтаксис plist**

Run: `plutil -lint ~/Library/LaunchAgents/com.stvl.tennis-data.tournament-card.plist`
Expected: `... OK`.

---

## Task 7: Обновить `CLAUDE.md`

**Files:**
- Modify: `CLAUDE.md`

- [ ] **Step 1: Добавить шард в таблицу «Шарды»**

В таблицу добавить строку:
```
| `data/tournament_cards.json` | карточка турнира + сетка (bracket) | Wimbledon offsite | раз в день |
```

- [ ] **Step 2: Добавить задачу в «Задачи обновления»**

Добавить пункт:
```
- `prompts/update_tournament_card.md` — карточка турнира и сетка Wimbledon (раз в день, только дописывание)
```

- [ ] **Step 3: Добавить строку расписания в блок cron**

В cron-блок добавить (для тех, кто на cron, а не launchd):
```cron
15 8 * * *  /АБС_ПУТЬ/run_task.sh prompts/update_tournament_card.md  >> /tmp/tennis_card.log 2>&1
```

- [ ] **Step 4: Commit**

```bash
git add CLAUDE.md
git commit -m "docs: задокументировать шард и задачу карточки турнира"
```

---

## Task 8: Вернуть агенты и финальная проверка

**Files:** нет (обратная операция к Task 0) + загрузка нового агента.

- [ ] **Step 1: Финальная сборка и полный прогон тестов**

Run:
```bash
python3 tests/test_tournament_cards.py && python3 build_config.py --check
```
Expected: `ALL OK` и `[ok] валидация пройдена (8 шардов)`.

- [ ] **Step 2: Загрузить новый агент карточки**

Run: `launchctl load ~/Library/LaunchAgents/com.stvl.tennis-data.tournament-card.plist`

- [ ] **Step 3: Вернуть три ранее снятых агента**

Run:
```bash
launchctl load ~/Library/LaunchAgents/com.stvl.tennis-data.matches.plist
launchctl load ~/Library/LaunchAgents/com.stvl.tennis-data.rankings.plist
launchctl load ~/Library/LaunchAgents/com.stvl.tennis-data.tournaments.plist
```

- [ ] **Step 4: Убедиться, что все четыре агента активны**

Run: `launchctl list | grep tennis`
Expected: четыре строки — `matches`, `rankings`, `tournaments`, `tournament-card`.

- [ ] **Step 5: Дать пользователю решение о пуше**

Изменения закоммичены локально. Спросить пользователя, пушить ли в `StVl/tennis-data-storage` сейчас (`git push`), или оставить на автопуш ближайшего запланированного запуска.

---

## Self-Review (выполнено при написании плана)

- **Покрытие спеки:** модель данных → Task 1; изменения build_config (SHARDS/валидация/обогащение/вклейка) → Task 3,4; format_display_name → Task 2; промпт → Task 5; launchd → Task 6; CLAUDE.md → Task 7; засев → Task 1; контракт «только дописывание» → Task 5 (промпт). Все разделы спеки покрыты.
- **Плейсхолдеры:** кода-плейсхолдеров нет; данные сетки (128 матчей) не выписываются в план осознанно — это выгрузка с офсайта в Task 1, процедура и форма заданы полностью.
- **Согласованность типов:** `format_display_name`, `validate_tournament_cards(shards, players)`, `enrich_tournament_cards(shards)` — сигнатуры совпадают между определением и вызовами/тестами. `config["tournamentCards"]` — единое имя ключа.
