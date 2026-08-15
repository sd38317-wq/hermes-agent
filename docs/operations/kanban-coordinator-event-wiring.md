# Kanban coordinator event wiring runbook

Investigation timestamp: 2026-08-15T00:28:49Z

Scope: task `t_6c909683`; read-only inspection of the live default board and runtime.

This document records observed state and deployment/rollback instructions. The investigation did not change the live scheduler, configuration, database, scripts, or services.

## Executive result

The intended design is sound but is not currently connected end to end in production.

1. Cron job `4cc951cdcdd3` runs every minute in `no_agent=true` mode, so it cannot call a model. However, its configured script is `kanban_exception_watch.py`, which resolves to `/opt/data/scripts/kanban_exception_watch.py`. That underscore-named file is the old 249-line watcher. It only detects human/deadline/run/heartbeat exceptions, writes a single whole-set fingerprint, and never inserts an internal coordination event.
2. The current repository watcher and the separately deployed hyphen-named file `/opt/data/scripts/kanban-exception-watch.py` are byte-identical (`a687…dc1`). They contain the new safety net for five managed profiles and the `coordination_required` writer. The cron job does not execute that file. The actual cron target hash is `fda5…183a`.
3. `/opt/data/config.yaml` has `kanban.auto_subscribe_on_create=true`, `retry_model=gpt-5.4-mini`, `retry_provider=openai-codex`, and `retry_reasoning_effort=low`. Effective defaults make `dispatch_in_gateway=true` and `dispatch_interval_seconds=60`.
4. The live `/opt/data/kanban.db` contained zero rows in `kanban_notify_subs` at inspection time, including no subscriptions for `t_4df918f9`, `t_43bff155`, `t_6c909683`, or `t_80227d75`. `auto_subscribe_on_create` is prospective only; it does not backfill existing cards. Auto-decomposer-created children also have `session_id=NULL`, so they do not independently identify the coordinator session to wake.
5. The gateway process is PID 303190 under root-supervised `s6-supervise gateway-default` and imports `/opt/hermes`. Its three relevant modules differ from the verified repository and from the runtime-compatible overlay prepared under `/opt/data/tmp-t_4df918f9`. Therefore the live gateway does not yet contain the complete claimed/coordination wake and low-cost retry path.
6. Consequently, the observed production path is currently: deterministic one-minute old watcher -> state file update/local stdout -> no internal event -> no subscription -> no coordinator wake. Dispatcher claims still work independently every 60 seconds.

## Exact components and call graph

### One-minute no-agent safety net

- Scheduler store: `/opt/data/cron/jobs.json`
- Job: `4cc951cdcdd3` (`모델 없는 1분 오류 감시`)
- Schedule: `* * * * *`
- Mode: `no_agent=true`
- Delivery: `local`
- Script field: `kanban_exception_watch.py`
- Resolver: `cron/scheduler.py::_run_script`, around lines 2615-2671
  - Relative script paths resolve under `HERMES_HOME/scripts`.
  - The gateway environment has `HERMES_HOME=/opt/data`, so the exact target is `/opt/data/scripts/kanban_exception_watch.py`.
- No-agent branch: `cron/scheduler.py::run_job`, around lines 3449-3489. It executes the script directly and structurally bypasses `AIAgent`, provider selection, and all LLM calls. Empty stdout is a silent successful run; nonzero exit becomes an error result.

### Intended watcher decision and internal signal

Repository file: `scripts/ops/kanban_exception_watch.py`

- `collect_exceptions()` (lines 115-264) reads `/opt/data/kanban.db` in SQLite `mode=ro` and deterministically computes canonical `{kind, task}` records.
- Managed profiles are exactly `dev`, `productdev`, `research`, `plan`, and `design` (line 28).
- It detects existing human/deadline/run/heartbeat exceptions plus:
  - `ready_stale`: assigned ready card unclaimed for at least 120 seconds;
  - `promotion_drift`: todo card with no parents or all parents terminal;
  - `dependency_stall`: todo child whose unfinished parents are not actively running for at least 120 seconds;
  - `fleet_idle`: actionable managed work while no managed profile has a live running worker;
  - `orchestrator_report_missing`: a wake subscription has an unacknowledged claimed/spawned/completed/review/blocked/crashed/gave_up event.
- The watcher never claims, promotes, unlinks, assigns, or bypasses a human gate. Lines 166 and 227-230 explicitly retain signal-only behavior.
- `emit_coordination_events()` (lines 315-368) is the exact internal call interface:

  `emit_coordination_events(db_path: Path, items: list[dict[str, str]], *, now: int, timeout: float = 2.0) -> None`

- It groups exceptions by task and appends a `task_events` row in a `BEGIN IMMEDIATE` transaction. The exact event is:

  - `kind`: `coordination_required`
  - `task_id`: the affected existing task
  - `created_at`: integer Unix timestamp
  - `payload`: compact sorted JSON with this shape:

    `{ "kinds": ["dependency_stall", "fleet_idle", ...], "signal_id": "<sha256>" }`

  `signal_id` is SHA-256 over compact, sorted JSON of the task-local canonical records, for example `[{
  "kind":"fleet_idle","task":"t_example"}]` without whitespace in the actual hash input.

- This is an internal database event, not a Telegram or Slack send and not an RPC call. The watcher does not invoke a model or gateway adapter.

### Dispatcher claim path

- Gateway loop: `gateway/kanban_watchers.py::_kanban_dispatcher_watcher()` (lines 1214-1543).
- Config keys:
  - `kanban.dispatch_in_gateway` (default true; boot-time read)
  - `kanban.dispatch_interval_seconds` (default 60)
  - `kanban.failure_limit` (default 2)
  - `kanban.retry_model`, `retry_provider`, `retry_reasoning_effort`
  - `kanban.default_assignee`
  - `kanban.max_in_progress_per_profile`
- Single dispatcher lock: `<kanban root>/kanban/.dispatcher.lock`, held for the gateway process lifetime.
- Tick entry point: `hermes_cli/kanban_db.py::dispatch_once()` (lines 9509-9614), then `_dispatch_once_locked()`.
- Ready selection: lines 9720-9724. Assigned ready work is ordered by priority descending and creation time ascending.
- Atomic claim: `claim_task()` (lines 4604-4723) rechecks all parent gates, performs `ready -> running`, creates a `task_runs` row, and appends:

  `{ "kind":"claimed", "payload":{"lock":"<claimer>","expires":<unix>,"run_id":<id>} }`

- Spawn follows only after a successful claim (lines 9878-9954). A worker PID is persisted when available. The spawned lifecycle event is generated by the spawn path/observer after PID persistence.
- The safety-net watcher must not call `claim_task()` or mutate ready/todo status. A ready card remains the dispatcher's responsibility. A todo-only dependency stall remains the coordinator's decision; the watcher emits only `coordination_required`.

### Auto-subscription and coordinator wake

- Creation hook: `tools/kanban_tools.py::_handle_create()` calls `_maybe_auto_subscribe()` after successful card creation (line 1466).
- Gate and routing: `_maybe_auto_subscribe()` (lines 1484-1612).
- `kanban.auto_subscribe_on_create=true` enables the attempt, but a persistent session channel is still required.
- Gateway-created cards use the current platform/chat/thread/profile ContextVars and force `delivery_mode="wake"` (lines 1531-1604).
- TUI-created cards use `platform="tui"` and the inherited `HERMES_SESSION_KEY`.
- CLI, cron, tests, and unattached creators do not subscribe.
- Persistence API: `hermes_cli/kanban_db.py::add_notify_sub()` (lines 11040-11173). It is idempotent on `(task_id, platform, chat_id, thread_id)`, starts caught up at the task's current max event ID, and uses explicit last-write-wins mode changes.
- `auto_subscribe_on_create` does not scan or backfill old tasks. Turning the key on after card creation cannot wake the original coordinator unless a subscription row is subsequently created with the original destination.
- The repository CLI already exposes `hermes kanban notify-subscribe --delivery-mode wake` in `hermes_cli/kanban.py`. The currently packaged `/opt/hermes` CLI does not expose that option, which is another runtime/repository divergence. Until a runtime-compatible `hermes_cli/kanban.py` is deployed, the packaged CLI creates the normal visible-notification default and is not an exact replacement for the wake-only auto-subscription path.

### Internal wake versus user-visible delivery

The gateway notifier is `gateway/kanban_watchers.py::_kanban_notifier_watcher()`.

- Normal `notify` and `notify+wake` modes may call the platform adapter's visible `send()` path for Telegram, Slack, or another push platform.
- Auto-created gateway subscriptions use `delivery_mode="wake"`:
  - `send_passive` is false (lines 530-532), so no normal/log/progress text is posted to Telegram or Slack.
  - Wake-only subscriptions additionally consume `claimed`, `spawned`, and `coordination_required` (lines 435-452).
  - The synthetic coordinator turn includes task id, status, title, assignee, board, optional completion handoff, and guidance to inspect the board (lines 815-849).
  - `gateway.wake.deliver_wake()` injects that turn into the originating session. For push adapters it reconstructs `SessionSource` using platform/chat type/thread/user/profile/scope metadata (lines 897-949).
- Thus the required control plane is a wake-only subscription plus `coordination_required`; Telegram/Slack visible delivery is a separate mode and should remain disabled for routine coordination.

## Fingerprint, crash window, and retry semantics

### Watcher state

- State file: `/opt/data/cron/state/kanban-exception-watch.json`
- Lock file: `/opt/data/cron/state/kanban-exception-watch.json.lock`, mode 0600
- New state shape: `{ "fingerprints": ["<condition sha256>", ...] }`
- Legacy/current live shape at inspection: `{ "fingerprint":"0ca2…912fc" }`, proving the underscore-named old watcher is still the active writer.
- Each condition is independently hashed by `_exception_fingerprints()`. Unchanged conditions are suppressed across process restarts; resolved conditions leave the saved set and may generate a new event if they recur later.
- `advisory_lock()` uses nonblocking `flock`, so overlapping cron runs exit silently rather than racing.
- `save_state_atomic()` writes a temporary file in the same directory, flushes and `fsync()`s it, uses `os.replace()`, then `fsync()`s the parent directory. This protects restart durability and prevents partial JSON.

### Ordering and failure meaning

The main path is intentionally:

1. acquire lock;
2. read DB and calculate current conditions;
3. insert new internal coordination events and commit;
4. atomically replace the fingerprint state file.

Consequences:

- If event insertion fails, state is not saved and the next minute retries.
- If the DB commit succeeds but the process dies before state replacement, the next minute sees the old state. `emit_coordination_events()` suppresses the same task/payload while no later non-coordination event exists, so the common crash window does not duplicate the event.
- If state replacement fails after the event commit, the cron run exits nonzero and retries next minute. DB-level event dedupe again protects the common case.
- If event insertion and state replacement succeed but a **wake-only** gateway delivery fails, the watcher does not need to emit again. Wake-only notification delivery has its own durable claim/delivered cursors and retry path. This guarantee does not extend to push-mode `notify+wake`: after its visible send succeeds, the cursor advances and the additional wake injection is intentionally best-effort.
- Remaining edge risk: the `INSERT ... SELECT ... FROM tasks WHERE id=?` rowcount is not checked. A task deleted between collection and insertion could result in no event followed by a successful state save. Tasks are normally archived rather than deleted, but a follow-up hardening change should verify insertion count before acknowledging that condition.
- DB event dedupe deliberately treats a later non-`coordination_required` event as a state transition. If the watcher state file is still stale after such an event, an identical signal may be emitted again. This is at-least-once, state-transition-aware behavior rather than global exactly-once behavior.

### Gateway delivery cursors

- `claim_unseen_events_for_sub()` (lines 11423-11471) claims an unseen range by advancing `last_event_id` inside `BEGIN IMMEDIATE`; concurrent gateway watchers serialize on SQLite's writer lock.
- Wake-only delivery must succeed before acknowledgement. On failure, `_kanban_notifier_watcher()` calls `rewind_notify_cursor()` and retries on the next notifier tick.
- `advance_notify_cursor()` records range ACKs and advances `delivered_event_id` only after delivery.
- `rewind_notify_cursor()` uses compare-and-swap first and falls back to rewinding the suffix when a later range was claimed. This provides at-least-once delivery: later successful events may duplicate after a race, but the failed earlier event is not lost.
- After `MAX_SEND_FAILURES`, the gateway drops the subscription. This is a terminal delivery failure that requires operational visibility; it is not repaired by watcher fingerprint recurrence.

## Observed production evidence

At 2026-08-15T00:28:49Z:

- Cron job `4cc951cdcdd3`: enabled, scheduled, last status `ok`, next minute scheduled, `no_agent=true`, `deliver=local`.
- Exact hashes:
  - repository watcher: `a687b4319ccaf22b9b1010f5555bebe430d148c3c27f67abe0c5154f065f0dc1`
  - actual cron target `/opt/data/scripts/kanban_exception_watch.py`: `fda53f6664b43ac70c6b5b4a67e8164ca8454f0727a92026b3663f84194f183a`
  - unused deployed alias `/opt/data/scripts/kanban-exception-watch.py`: `a687b4319ccaf22b9b1010f5555bebe430d148c3c27f67abe0c5154f065f0dc1`
- Live default board: no `kanban_notify_subs` rows and no `coordination_required` events.
- Recent `claimed` and `spawned` events prove the independent 60-second dispatcher is operating.
- Gateway runtime PID 303190 started before the verified coordination commits and imports root-owned `/opt/hermes`.
- `/opt/hermes` is not a git checkout. Do not copy repository modules over it blindly: runtime file sizes show it is a divergent packaged tree. Use the runtime-compatible overlay in `/opt/data/tmp-t_4df918f9`, which preserves the installed runtime base and applies the reviewed changes.
- Candidate overlay files parse successfully with `ast.parse()`:
  - `/opt/data/tmp-t_4df918f9/gateway/kanban_watchers.py`
  - `/opt/data/tmp-t_4df918f9/hermes_cli/kanban_db.py`
  - `/opt/data/tmp-t_4df918f9/hermes_cli/config_defaults.py`
- Original runtime backups already exist under `/opt/data/deploy-backups/t_4df918f9`.
- Expected candidate hashes are:
  - `gateway/kanban_watchers.py`: `e1149fd714e10b1621ef7d395449fccae3ccd6f09e83f3e7bf7adc399ffc71c7`
  - `hermes_cli/kanban_db.py`: `921431805933995a5b495bc80fdd00b2e05576c17815325a162d7dda5caed2da`
  - `hermes_cli/config_defaults.py`: `52d27860b4e103e80eb5ab3a745a14eae1e0755a7540908f79fb31f95275e04f`

## Safe deployment procedure for the implementation task

These commands are intentionally not executed by this investigation task. Run them as the authorized operator, from the verified repository checkout.

### 1. Stop the filename split and verify no-agent behavior

Preferred minimal correction: deploy the verified watcher to the exact filename already referenced by the cron job.

```bash
set -euo pipefail
repo=/opt/data/kanban/workspaces/t_3e737cce/repo
stamp=$(date -u +%Y%m%dT%H%M%SZ)
backup=/opt/data/deploy-backups/kanban-coordinator-$stamp
install -d -m 0700 "$backup"
install -m 0755 /opt/data/scripts/kanban_exception_watch.py \
  "$backup/kanban_exception_watch.py.before"
install -m 0600 /opt/data/cron/jobs.json "$backup/jobs.json.before"
install -m 0600 /opt/data/cron/state/kanban-exception-watch.json \
  "$backup/kanban-exception-watch.json.before"
install -m 0755 "$repo/scripts/ops/kanban_exception_watch.py" \
  /opt/data/scripts/kanban_exception_watch.py
sha256sum "$repo/scripts/ops/kanban_exception_watch.py" \
  /opt/data/scripts/kanban_exception_watch.py
```

Expected: both hashes are `a687b4319ccaf22b9b1010f5555bebe430d148c3c27f67abe0c5154f065f0dc1`.

An alternative is to edit the cron job to use the hyphen name, but changing the exact deployed target is lower risk because it avoids a scheduler metadata write:

```bash
HERMES_HOME=/opt/data HOME=/opt/data /opt/hermes/.venv/bin/hermes \
  cron edit 4cc951cdcdd3 --script kanban-exception-watch.py --no-agent
```

Do one approach, not both. Keep `no_agent=true` and `deliver=local`.

### 2. Install the runtime-compatible gateway overlay

The gateway tree is root-owned and not a git checkout. Use the prebuilt overlay rather than repository files. The current `hermes` user has no `sudo` command, so this entire block must run in an authorized root shell. Hash the mutable staging files before installation; syntax checks alone do not establish provenance.

```bash
set -euo pipefail
test "$(id -u)" -eq 0 || { echo "Run this block as root" >&2; exit 1; }
candidate=/opt/data/tmp-t_4df918f9
backup=/opt/data/deploy-backups/t_4df918f9
printf '%s  %s\n' \
  e1149fd714e10b1621ef7d395449fccae3ccd6f09e83f3e7bf7adc399ffc71c7 "$candidate/gateway/kanban_watchers.py" \
  921431805933995a5b495bc80fdd00b2e05576c17815325a162d7dda5caed2da "$candidate/hermes_cli/kanban_db.py" \
  52d27860b4e103e80eb5ab3a745a14eae1e0755a7540908f79fb31f95275e04f "$candidate/hermes_cli/config_defaults.py" \
  | sha256sum -c -
test -f "$backup/gateway/kanban_watchers.py"
test -f "$backup/hermes_cli/kanban_db.py"
test -f "$backup/hermes_cli/config_defaults.py"
install -m 0644 "$candidate/gateway/kanban_watchers.py" \
  /opt/hermes/gateway/kanban_watchers.py
install -m 0644 "$candidate/hermes_cli/kanban_db.py" \
  /opt/hermes/hermes_cli/kanban_db.py
install -m 0644 "$candidate/hermes_cli/config_defaults.py" \
  /opt/hermes/hermes_cli/config_defaults.py
/opt/hermes/.venv/bin/python -m py_compile \
  /opt/hermes/gateway/kanban_watchers.py \
  /opt/hermes/hermes_cli/kanban_db.py \
  /opt/hermes/hermes_cli/config_defaults.py
(cd /opt/hermes && /opt/hermes/.venv/bin/python -c \
  'import gateway.kanban_watchers, hermes_cli.kanban_db, hermes_cli.config_defaults; print("runtime imports ok")')
/package/admin/s6-2.15.0.0/command/s6-svc -r \
  /run/service/gateway-default
```

Record the old and new gateway PIDs and confirm the new process imports `/opt/hermes` with `HERMES_HOME=/opt/data`.

### 3. Establish coordinator wake ownership

Do not rely on `auto_subscribe_on_create=true` to backfill old cards. The safe long-term contract is:

- root/coordinator task is created from the coordinator's gateway session;
- creation inherits a wake-only subscription to the root and relevant descendants;
- worker children inherit coordinator ownership through the existing task-graph/subscription mechanism;
- decomposer-created `session_id=NULL` children do not invent a Telegram/Slack destination;
- any existing task backfill must use the coordinator's real platform/chat/thread/profile metadata and `delivery_mode="wake"` through `add_notify_sub()`, never guessed identifiers.

Before backfilling, deploy and test a runtime-compatible `hermes_cli/kanban.py` that carries the repository's existing `--delivery-mode wake` support, or perform the operation from the live coordinator session so `_maybe_auto_subscribe()` supplies the exact routing metadata. The currently packaged `notify-subscribe` CLI defaults to visible `notify` mode and is not acceptable for silent control-plane backfill. The three-file overlay above does not include `hermes_cli/kanban.py`, so CLI backfill remains unavailable after that minimal deployment.

### 4. Verification gates

```bash
# Read-only watcher smoke; no state/event writes.
/opt/data/scripts/kanban_exception_watch.py --smoke --db /opt/data/kanban.db

# Show scheduler mode and recent runs.
HERMES_HOME=/opt/data HOME=/opt/data /opt/hermes/.venv/bin/hermes \
  cron status
HERMES_HOME=/opt/data HOME=/opt/data /opt/hermes/.venv/bin/hermes \
  cron runs 4cc951cdcdd3

# Verify effective config from the same home used by gateway.
HERMES_HOME=/opt/data HOME=/opt/data /opt/hermes/.venv/bin/hermes \
  config get kanban.auto_subscribe_on_create
HERMES_HOME=/opt/data HOME=/opt/data /opt/hermes/.venv/bin/hermes \
  config get kanban.dispatch_in_gateway
HERMES_HOME=/opt/data HOME=/opt/data /opt/hermes/.venv/bin/hermes \
  config get kanban.dispatch_interval_seconds
```

Then use an isolated fixture board/session, not production task statuses, to prove:

1. assigned ready remains untouched by watcher and is claimed by dispatcher;
2. todo dependency stall remains todo and receives one `coordination_required` event;
3. repeating the same watcher state adds no event;
4. changing the condition adds one new event;
5. process restart preserves suppression through the fingerprint state file;
6. failed wake rewinds the subscription cursor and succeeds once on the next notifier tick;
7. wake-only Telegram and Slack paths invoke `deliver_wake()` but never the visible adapter `send()` path;
8. a completed/blocked/crashed/gave_up lifecycle event wakes the coordinator once;
9. one minute of no-agent scheduler execution creates no model/provider run.

Do not declare production verification complete until the gateway PID changes, the runtime hashes match the candidate overlay, a wake-only subscription exists for the fixture coordinator, and both `coordination_required` creation and cursor acknowledgement are observed.

## Rollback

### Watcher/scheduler rollback

If the exact-target deployment was used:

```bash
set -euo pipefail
backup=/opt/data/deploy-backups/kanban-coordinator-<timestamp>
install -m 0755 "$backup/kanban_exception_watch.py.before" \
  /opt/data/scripts/kanban_exception_watch.py
install -m 0600 "$backup/kanban-exception-watch.json.before" \
  /opt/data/cron/state/kanban-exception-watch.json
```

If the cron metadata was edited instead, restore the old script field:

```bash
HERMES_HOME=/opt/data HOME=/opt/data /opt/hermes/.venv/bin/hermes \
  cron edit 4cc951cdcdd3 --script kanban_exception_watch.py --no-agent
```

### Gateway rollback

```bash
set -euo pipefail
test "$(id -u)" -eq 0 || { echo "Run this block as root" >&2; exit 1; }
backup=/opt/data/deploy-backups/t_4df918f9
install -m 0644 "$backup/gateway/kanban_watchers.py" \
  /opt/hermes/gateway/kanban_watchers.py
install -m 0644 "$backup/hermes_cli/kanban_db.py" \
  /opt/hermes/hermes_cli/kanban_db.py
install -m 0644 "$backup/hermes_cli/config_defaults.py" \
  /opt/hermes/hermes_cli/config_defaults.py
/opt/hermes/.venv/bin/python -m py_compile \
  /opt/hermes/gateway/kanban_watchers.py \
  /opt/hermes/hermes_cli/kanban_db.py \
  /opt/hermes/hermes_cli/config_defaults.py
(cd /opt/hermes && /opt/hermes/.venv/bin/python -c \
  'import gateway.kanban_watchers, hermes_cli.kanban_db, hermes_cli.config_defaults; print("runtime imports ok")')
/package/admin/s6-2.15.0.0/command/s6-svc -r \
  /run/service/gateway-default
```

Do not roll back `/opt/data/config.yaml` wholesale. It may contain unrelated live settings. If an individual key must be reverted, use `hermes config set` with `HERMES_HOME=/opt/data` and the recorded previous value.

## Tool log

- Claude Code was invoked first in read-only print mode with only `Read` and `Bash(git *)`; it returned HTTP 429: `You've hit your weekly limit · resets Aug 16, 1pm (UTC)`. No files or runtime state were changed by Claude Code.
- Direct repository inspection used `read_file`, `search_files`, SQLite read-only URIs, hash comparisons, git history/diff, process inspection, effective config reads, and cron CLI help.
- After Claude Code's verified limit error, Codex CLI 0.147.0 was used in read-only review mode. It found and prompted corrections for packaged-versus-repository CLI divergence, unavailable `sudo`, candidate hash verification, and wake-only retry wording. No production code was modified.
