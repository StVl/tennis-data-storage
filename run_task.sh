#!/usr/bin/env bash
# Запускает одну задачу-обновление через Claude Code (headless), затем собирает
# config.json из шардов и пушит, если что-то изменилось.
#
# Использование:
#   ./run_task.sh prompts/update_matches.md
#
# Вызывается из cron. Все пути абсолютные относительно расположения скрипта,
# т.к. cron стартует с произвольным cwd.

set -euo pipefail

# каталог, где лежит сам скрипт (и весь проект)
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$DIR"

PROMPT_FILE="${1:?укажи файл промпта, напр. prompts/update_matches.md}"

if [[ ! -f "$PROMPT_FILE" ]]; then
  echo "[run_task] нет файла промпта: $PROMPT_FILE" >&2
  exit 1
fi

# Троттл по state-файлу. Полезно, когда launchd срабатывает ежедневно, но
# фактическая частота нужна реже (раз в N дней). Считаем только успешные
# запуски — state пишем в конце, после пуша.
MIN_INTERVAL_HOURS="${MIN_INTERVAL_HOURS:-0}"
STATE_DIR="$DIR/.state"
STATE_FILE="$STATE_DIR/last_run_$(basename "$PROMPT_FILE" .md)"

if [[ "$MIN_INTERVAL_HOURS" -gt 0 && -f "$STATE_FILE" ]]; then
  LAST_TS="$(cat "$STATE_FILE")"
  NOW_TS="$(date +%s)"
  DELTA_H=$(( (NOW_TS - LAST_TS) / 3600 ))
  if [[ "$DELTA_H" -lt "$MIN_INTERVAL_HOURS" ]]; then
    echo "[run_task] $(date '+%Y-%m-%d %H:%M:%S')  пропуск: последний успех $DELTA_H ч назад, минимум $MIN_INTERVAL_HOURS"
    exit 0
  fi
fi

echo "[run_task] $(date '+%Y-%m-%d %H:%M:%S')  старт: $PROMPT_FILE"

# Путь к claude. Cron не наследует PATH из shell — нужен абсолютный.
CLAUDE_BIN="${CLAUDE_BIN:-/opt/homebrew/bin/claude}"

# Форсим Sonnet — задача не требует Opus, цена предсказуема.
CLAUDE_MODEL="${CLAUDE_MODEL:-claude-sonnet-4-6}"

# В env может быть невалидный GITHUB_TOKEN (мешает git push через gh-helper).
unset GITHUB_TOKEN GITHUB_PERSONAL_ACCESS_TOKEN

# "Голова": Claude Code ходит в веб и правит ОДИН шард по инструкции из промпта.
"$CLAUDE_BIN" -p "$(cat "$PROMPT_FILE")" --model "$CLAUDE_MODEL" --permission-mode acceptEdits

# "Сантехника": детерминированная сборка + пуш, если есть изменения.
python3 build_config.py --push

# Зафиксировать успешный запуск (для троттла на следующий раз).
if [[ "$MIN_INTERVAL_HOURS" -gt 0 ]]; then
  mkdir -p "$STATE_DIR"
  date +%s > "$STATE_FILE"
fi

echo "[run_task] $(date '+%Y-%m-%d %H:%M:%S')  готово"
