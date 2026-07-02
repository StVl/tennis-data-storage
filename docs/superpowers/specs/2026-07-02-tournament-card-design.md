# Дизайн: карточка турнира (tournament card) с сеткой

Дата: 2026-07-02
Репозиторий: `StVl/tennis-data-storage`

## Цель

Дать приложению новую сущность — **карточку турнира** с полной сеткой (bracket):
раунды → матчи → слоты участников. На старте — только **Wimbledon 2026,
Gentlemen's Singles**. Модель проектируется расширяемой на другие турниры и
разряды в будущем, но сейчас наполняется данными только для одного турнира.

## Решения (согласовано в брейншторминге)

1. **Охват:** сейчас только Wimbledon (Gentlemen's Singles). Другие турниры не
   заводим и не отображаем. Модель обобщённая — любой турнир может иметь сетку.
2. **Интеграция:** новая top-level секция `tournamentCards[]` в `config.json`,
   **отдельно** от существующего `tournaments[]`. Данные частично дублируются
   (name/dates/surface) — это осознанно, чтобы не ломать уже установленные
   версии приложения. Оптимизацию (слияние моделей) откладываем.
3. **Участники в слотах:** строки (`name`/`flag`/`seed`) + **опциональный
   `playerId`**. Если `playerId` есть в `players.json`, отображаемое `name`
   берётся из ростера (детерминированно в `build_config.py`). Флаг хранится в
   слоте — в `players.json` страны/флага нет.
4. **Источник:** официальный сайт Wimbledon —
   `https://www.wimbledon.com/en_GB/draws/gentlemens-singles`.
5. **Частота обновления:** раз в день (launchd-агент + `run_task.sh`).
6. **Засев:** начальная сетка засевается вручную сейчас.
7. **Контракт апдейта — только дописывание:** ежедневный промпт НЕ переписывает
   зафиксированный скелет сетки. Он только (а) проставляет `winner` у
   завершённых матчей, (б) раскрывает `tbd`-слоты в реальных игроков по мере
   прохождения раундов, (в) добавляет отсутствующие раунды/матчи. Уже
   заполненные корректные слоты не трогает.
8. `schemaVersion` не меняем — добавление ключа аддитивно, старые клиенты
   игнорируют незнакомый `tournamentCards`.

## Модель данных

Новый ключ в `config.json`: `"tournamentCards": TournamentCard[]`.

```jsonc
TournamentCard {
  "id": "wimbledon-2026",          // уникальный слаг
  "name": "The Championships, Wimbledon",
  "category": "Grand Slam",
  "event": "Gentlemen's Singles",  // опционально
  "monogram": "W",                 // 1 буква для лого-плитки
  "surface": "grass",              // enum: grass|clay|hard; fallback grass
  "description": "…",              // 1–2 предложения
  "dateRange": "29 Jun – 12 Jul",
  "location": "London · GB",       // city · country code
  "drawSize": 128,
  "format": "Best of 5",
  "drawDate": "26 Jun 2026",
  "state": "drawn",                // enum: awaiting_draw|drawn
  "rounds": [ Round ]              // присутствует только при state=drawn
}

Round {
  "code": "R1",                    // R1|R2|R3|R4|QF|SF|F
  "title": "First Round",
  "matches": [ Match ]
}

Match {
  "id": "m-r1-01",
  "top": Slot,
  "bottom": Slot
}

Slot {
  "name": "C. Alcaraz",            // "To be determined" когда слот пуст
  "flag": "🇪🇸",                    // хранится в слоте; может быть null
  "seed": 1,                       // или null
  "winner": true,                  // true у победителя матча
  "tbd": false,                    // true когда участник ещё не известен
  "playerId": "alcaraz"            // опционально; если есть → name из ростера
}
```

Канонические раунды 128-сетки (code / title / число матчей):
R1 First Round 64 · R2 Second Round 32 · R3 Third Round 16 ·
R4 Fourth Round 8 · QF Quarter-final 4 · SF Semi-final 2 · F Final 1.
`rounds` — упорядоченный массив от первого раунда к финалу; рендерятся все
матчи, которые вернул бэкенд.

## Хранение (шард)

Новый шард `data/tournament_cards.json`:

```jsonc
{ "tournamentCards": [ TournamentCard, … ] }
```

Источник правды, редактируется ежедневным промптом. Один шард (турнир пока
один и обновляется ежедневно — дробить статику/сетку преждевременно, YAGNI).

## Изменения в `build_config.py`

1. Добавить `"tournament_cards"` в список `SHARDS`.
2. **Валидация** новой секции (в `validate`):
   - `id` уникален среди карточек;
   - `surface` ∈ {grass, clay, hard};
   - `state` ∈ {awaiting_draw, drawn};
   - при `state == "drawn"`: `rounds` непусты; каждый `code` ∈
     {R1,R2,R3,R4,QF,SF,F}; у каждого матча есть `top` и `bottom`;
   - не более одного `winner: true` на матч;
   - если у слота есть `playerId` — он обязан существовать в `players.json`
     (как `opponentId` в матчах — мягкая ссылка, но здесь проверяем строго,
     т.к. `playerId` проставляется осознанно только для игроков ростера).
3. **Детерминированное обогащение** (новая функция, вызывается в `build`):
   для каждого слота с `playerId`, найденным в `players.json`, перезаписать
   `name` форматом «И. Фамилия» из полного имени ростера
   (напр. «Jannik Sinner» → «J. Sinner»). Остальные поля слота не трогаем.
   Слоты без `playerId` остаются как в шарде.
4. В `build` добавить в итоговый `config` ключ
   `"tournamentCards": shards["tournament_cards"]["tournamentCards"]`
   (после обогащения).

## Логика обновления (раз в день)

### Промпт `prompts/update_tournament_card.md`
- Работает ТОЛЬКО с `data/tournament_cards.json` (+ читает `players.json`).
- Источник: `https://www.wimbledon.com/en_GB/draws/gentlemens-singles`.
- Только Wimbledon (Gentlemen's Singles).
- **Только дописывание** (см. решение 7): не переписывать зафиксированный
  скелет; проставлять `winner`, раскрывать `tbd`→игрок, добавлять недостающие
  раунды/матчи. Не выдумывать счёт/составы — не уверен, оставить как было.
- Проставлять `playerId`, если игрок есть в `players.json` (сопоставление по
  имени), иначе оставлять строковое `name` + `flag`.
- В конце: `python3 build_config.py --check`.

### Планировщик
launchd-агент `com.stvl.tennis-data.tournament-card.plist` — запуск раз в день
через `./run_task.sh prompts/update_tournament_card.md` (рядом с существующими
`com.stvl.tennis-data.{matches,tournaments,rankings}.plist`). Для cron —
строка в стиле остальных задач в `CLAUDE.md`.

## Засев начальной сетки (сейчас)

Стянуть текущую сетку Wimbledon 2026 (Gentlemen's Singles) с офсайта и записать
в `data/tournament_cards.json`: статические поля карточки + `state` + `rounds`.
Игрокам, присутствующим в `players.json`, проставить `playerId`.

> Замечание по данным: репозиторий содержит проектные/синтетические данные
> сезона 2026. При засеве используем реальную сетку с офсайта; там, где офсайт
> недоступен для программного чтения, засеваем скелет, согласованный с уже
> существующими матчами Wimbledon 2026 в `data/matches_*.json`, и помечаем
> нераскрытые слоты как `tbd`. Ежедневный апдейт затем дописывает результаты.

## Обновление документации
- `CLAUDE.md`: добавить строку про шард `tournament_cards`, промпт
  `update_tournament_card.md` и cron/launchd-строку.

## Вне скоупа (на будущее)
- Другие турниры и разряды (дамы, парные).
- Слияние `tournaments[]` и `tournamentCards[]` в одну модель.
- Связь слотов сетки с профилями игроков в UI (данные готовы — `playerId`).
