# tennis-data

Данные теннисного приложения, разбитые на шарды по частоте обновления.

## Принцип

- **`data/*.json`** — источник правды. Маленькие файлы, каждый влезает в контекст LLM
  и обновляется на своей частоте.
- **`config.json`** — собранный артефакт, который читает приложение (один файл, как раньше).
  Руками НЕ правится — собирается из шардов.
- **`build_config.py`** — детерминированная сборка (без LLM): валидация → пересчёт
  `playerCards` → склейка `config.json` → git push.

```
Claude+веб  →  правит ОДИН шард (data/rankings.json и т.п.)
build_config.py →  валидирует + собирает config.json + git push   (без LLM)
приложение  →  читает config.json с GitHub Pages (ничего не обновляет само)
```

Приложение остаётся простым: один GET на `config.json`, отрисовка. Вся «свежесть»
обеспечивается тем, что скрипт регулярно пересобирает и пушит этот файл.

## Шарды и расписание

| Файл | Что | Источник | Частота |
|---|---|---|---|
| `data/matches_upcoming.json` | ближайшие матчи + `isLive`/`liveScore` | Sofascore | каждый час |
| `data/matches_past.json` | завершённые матчи (append) | Sofascore | по факту завершения |
| `data/tournaments_upcoming.json` | ближайшие/идущие турниры | ATP | раз в день |
| `data/tournaments_past.json` | завершённые турниры | ATP | редко |
| `data/rankings.json` | рейтинг, очки, тренд | ATP / Tennis Abstract | раз в неделю (пн) |
| `data/players.json` | профили игроков (без рейтинга) | ATP | раз в неделю |
| `data/reference.json` | стили игры | — | почти никогда |

`playerCards` не хранится в шардах — пересчитывается скриптом из матчей.

## Сборка вручную

```bash
python3 build_config.py            # собрать config.json
python3 build_config.py --check    # только валидация
python3 build_config.py --push     # собрать и запушить, если изменилось
```

## Автообновление через Claude Code (headless) + cron

«Голову» (поход в веб и правку шарда) выполняет Claude Code в headless-режиме,
«сантехнику» (сборку и пуш) — `build_config.py`. Промпты лежат в `prompts/`.

Пример обёртки `run_task.sh`:

```bash
#!/usr/bin/env bash
set -e
cd "$(dirname "$0")"
PROMPT_FILE="$1"                       # напр. prompts/update_matches.md
claude -p "$(cat "$PROMPT_FILE")" --permission-mode acceptEdits
python3 build_config.py --push
```

crontab (`crontab -e`):

```cron
# P1: ближайшие матчи + live-счёт — каждый час
0 * * * *   /path/tennis-data/run_task.sh prompts/update_matches.md      >> /tmp/tennis_matches.log 2>&1
# P2: ближайшие турниры — каждый день в 07:30
30 7 * * *  /path/tennis-data/run_task.sh prompts/update_tournaments.md  >> /tmp/tennis_tourn.log   2>&1
# P3: рейтинг и очки — по понедельникам в 08:00
0 8 * * 1   /path/tennis-data/run_task.sh prompts/update_rankings.md     >> /tmp/tennis_rank.log    2>&1
```

> На macOS вместо cron можно использовать launchd, если ноут часто спит —
> launchd доганяет пропущенные запуски.

## Зачем шарды, если приложение читает один файл

Чтобы при обновлении (особенно через LLM) работать с маленьким файлом: он влезает
в контекст, дешевле по токенам и не даёт случайно переписать лишнее. Рейтинг вынесен
из профилей игроков именно поэтому — его правят часто, а профили почти не меняются.

## Переносимость

Вся логика живёт здесь, в репозитории (шарды, скрипт, промпты). Триггер (cron + Claude
Code, либо планировщик Cowork) — тонкая внешняя обёртка. Сменил аккаунт/инструмент —
пересоздал только расписание, указал на те же файлы.
