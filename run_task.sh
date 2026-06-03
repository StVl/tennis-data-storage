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

echo "[run_task] $(date '+%Y-%m-%d %H:%M:%S')  старт: $PROMPT_FILE"

# Путь к claude. Если cron не видит claude — впиши сюда абсолютный путь
# (узнать: which claude), напр. CLAUDE_BIN="/Users/ты/.local/bin/claude"
CLAUDE_BIN="${CLAUDE_BIN:-claude}"

# "Голова": Claude Code ходит в веб и правит ОДИН шард по инструкции из промпта.
"$CLAUDE_BIN" -p "$(cat "$PROMPT_FILE")" --permission-mode acceptEdits

# "Сантехника": детерминированная сборка + пуш, если есть изменения.
python3 build_config.py --push

echo "[run_task] $(date '+%Y-%m-%d %H:%M:%S')  готово"
