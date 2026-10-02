#!/usr/bin/env bash
# Частая задача (launchd, каждые 30 минут): свежие сетки идущих турниров -> шард -> БД.
#
# Без LLM и без git: PDF ATP разбирается детерминированно, а шарды data/draws/
# закоммитит ближайший прогон run_task.sh (там `git add -A`). Почасовая задача матчей
# тоже импортирует сетки, но только после Claude, то есть через 5-55 минут после
# начала часа; эта -- чтобы результаты в сетке отставали от PDF не больше чем на полчаса.
#
# С run_task.sh пересекается только по записи в БД. Обе задачи пишут одной транзакцией
# и идемпотентно: при одновременном прогоне одна ждёт блокировок другой, а в худшем
# случае Postgres снимет её как deadlock -- тогда сетка доедет следующим прогоном.

set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$DIR"

if [[ -f "$DIR/.env" ]]; then
  set -a; source "$DIR/.env"; set +a
fi

echo "[run_draws] $(date '+%Y-%m-%d %H:%M:%S')  старт"
python3 scripts/import_draws.py

if [[ -n "${DATABASE_URL:-}" ]]; then
  python3 scripts/migrate_data.py --draws-only || echo "[run_draws] предупреждение: запись сеток в БД не удалась" >&2
else
  echo "[run_draws] предупреждение: нет .env с DATABASE_URL" >&2
fi
