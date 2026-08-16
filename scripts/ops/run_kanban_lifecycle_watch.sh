#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
exec python3 "${script_dir}/kanban_lifecycle_watch.py" \
  --db /opt/data/kanban.db \
  --state /opt/data/cron/state/kanban-lifecycle-watch.json \
  --audit /opt/data/cron/evidence/kanban-lifecycle-watch.jsonl
