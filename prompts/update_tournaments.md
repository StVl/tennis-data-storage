# Задача: обновить ближайшие турниры (приоритет 2)

Ты обновляешь данные теннисного приложения. Работаешь ТОЛЬКО с файлами
`data/tournaments_upcoming.json` и `data/tournaments_past.json`. Остальное не трогаешь.

## Источник
ATP (atptour.com) — календарь турниров и составы участников.

## Что сделать
1. Прочитай `data/tournaments_upcoming.json` и `data/players.json`.
2. **Сначала** возьми календарь ATP на ближайшие 4 недели от сегодняшней даты
   (atptour.com/en/tournaments). Для КАЖДОЙ недели должны быть учтены ВСЕ
   ATP-турниры этой недели — включая параллельные 250-е (например, на грассе
   одновременно идут Stuttgart и s-Hertogenbosch; Eastbourne и Mallorca и т.п.).
   Если какого-то турнира нет в шарде — добавь его.
3. Для каждого турнира в `tournaments_upcoming`:
   - актуализируй `name` (бывают смены title sponsor — напр. cinch → HSBC),
     `dates` (start/end), `courtType`;
   - `players` — заяви ОФИЦИАЛЬНЫЙ entry list. Источник в порядке предпочтения:
     (а) официальный сайт турнира, (б) ATP entry list, (в) LTA/национальная
     федерация. НЕ берёшь "headliners" из новостей — там цитируют 4-5 имён;
     нужен полный список. Только id, существующие в `data/players.json`;
     остальных просто пропускаешь.
   - если турнир сейчас идёт — `status: "ongoing"`;
   - если турнир завершился — `status: "completed"`, `winner`, `finalist`,
     и ПЕРЕНЕСИ запись в `tournaments_past.json`.
4. Поле `tournament` — короткий снейк-кейс-слаг (`queens_club`, `s_hertogenbosch`).
   `name` — полное официальное название.

## Правила
- `players` содержит ТОЛЬКО playerId, существующие в `data/players.json`.
- Не выдумывай составы и даты — бери с ATP. Не уверен — оставь как было.
- JSON валидный, UTF-8, отступ 2 пробела.

## После правок
`python3 build_config.py --check` — и если ок, остановись.
