# Задача: обновить ближайшие турниры (приоритет 2)

Ты обновляешь данные теннисного приложения. Работаешь ТОЛЬКО с файлами
`data/tournaments_upcoming.json` и `data/tournaments_past.json`. Остальное не трогаешь.

## Источник
ATP (atptour.com) — календарь турниров и составы участников.

## Что сделать
1. Прочитай `data/tournaments_upcoming.json` и `data/players.json`.
2. Для каждого турнира в `tournaments_upcoming`:
   - актуализируй `dates` (start/end), `courtType`, `players` (список playerId
     заявленных участников — только те id, что есть в `data/players.json`);
   - если турнир сейчас идёт — `status: "ongoing"`;
   - если турнир завершился — проставь `status: "completed"`, `winner`, `finalist`
     и ПЕРЕНЕСИ его в `tournaments_past.json`.
3. Добавь новые предстоящие турниры ближайших недель со `status: "upcoming"`
   (поле `tournament` — короткий слаг, `name` — полное название).

## Правила
- `players` содержит ТОЛЬКО playerId, существующие в `data/players.json`.
- Не выдумывай составы и даты — бери с ATP. Не уверен — оставь как было.
- JSON валидный, UTF-8, отступ 2 пробела.

## После правок
`python3 build_config.py --check` — и если ок, остановись.
