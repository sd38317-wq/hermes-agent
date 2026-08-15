#!/usr/bin/env bash
set -euo pipefail

script_dir="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
exec python3 "${script_dir}/kanban_runtime_watch.py" \
  --db /opt/data/kanban.db \
  --evidence /opt/data/cron/evidence/kanban-runtime-watch.jsonl \
  --notification-mode human-only
