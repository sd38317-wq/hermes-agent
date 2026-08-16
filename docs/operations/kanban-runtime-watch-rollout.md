# Deterministic kanban runtime watchdog rollout

This procedure replaces the paused cron job `d1b35bd84781`. Do not resume that
job: its LLM-generated self-report is not evidence.

## Safety boundary

`scripts/ops/kanban_runtime_watch.py` opens the kanban database in SQLite
`mode=ro` with `PRAGMA query_only=ON`. It does not claim, assign, unblock,
reclaim, signal, or terminate anything. Suggested remediation is emitted only
as structured `kanban_comment` tool arguments; a coordinator
must execute an applicable step through the official tool after inspecting the
finding. Never translate the plan into direct SQLite writes or a `hermes
kanban` shell command.

## Preflight and dry run

Use the pushed commit, then run:

```bash
python3 scripts/ops/kanban_runtime_watch.py \
  --db /opt/data/kanban.db \
  --evidence /opt/data/cron/evidence/kanban-runtime-watch.jsonl
```

The detector's default mode prints exactly one JSON object. Reject the rollout unless all of
these fields exist:

- `query_count > 0`
- `input_row_count > 0`
- `finding_count == len(findings)`
- `action_count == 0` during detection
- `pid_reconciliation.checked`, `alive`, `missing`, `duplicates`, and per-PID
  `results`
- an ISO-8601 UTC `timestamp`

`PASS` is valid only when the input and query counts are non-zero and no
finding exists. `FAIL` means the evidence is valid and exceptions were found;
`ERROR` means there was not enough evidence to decide. Both non-PASS states
must remain non-zero process exits.

## Install the no-agent script

Cron scripts must live under the target profile's `HERMES_HOME/scripts`
directory. Copy both reviewed files from the pushed commit to that directory:

- `scripts/ops/kanban_runtime_watch.py`
- `scripts/ops/run_kanban_runtime_watch.sh`

Preserve executable mode `0700` or `0755` and compare SHA-256 hashes before
scheduling `run_kanban_runtime_watch.sh`. The supported production alerting
cron must run as a `no_agent=True` script job every minute with
`deliver=origin`. The checked-in wrapper selects
`--notification-mode human-only`: every full JSON record remains in the local
evidence file, while stdout is empty for healthy and non-human-only findings.
Only a genuine `human_only=true` blocker reaches the external delivery
connection. This avoids both an LLM call and scheduler failure/noise for
ordinary findings.

An existing ten-minute job may remain only as a local, evidence-only job. It
is not the supported production notification path and must not deliver normal
or internal output externally.

The script arguments for the installed copy must remain:

```text
--db /opt/data/kanban.db --evidence /opt/data/cron/evidence/kanban-runtime-watch.jsonl --notification-mode human-only
```

The checked-in wrapper supplies those fixed arguments. Do not add SQL or task
mutations to it.

## Three-run acceptance gate

Before creating the one-minute production job, execute the exact installed command three
times sequentially. Keep all three JSONL lines. Each
line must be complete, independently JSON-parseable, have a distinct timestamp,
and contain non-zero input/query counts, finding/action counts, and PID
reconciliation. A zero-query or zero-input `PASS` fails acceptance.

Also run the focused regression suite:

```bash
scripts/run_tests.sh tests/scripts/test_ops_kanban_runtime_watch.py
```

The focused suite separately launches three concurrent detector processes
against one evidence file to prove append locking and complete JSONL records.

The fixtures named `t_adf495b7`, `t_a40a0e65`, and `t_b965de12` cover the live
research PID/heartbeat shape and the dependency-gated restore/three-hour
follow-ups without mutating the live board.

## Alert and remediation policy

- Healthy runs produce zero external alerts.
- Internal lifecycle starts/completions produce no external alerts.
- Only findings marked `human_only=true` may become external notifications.
- A human notification must carry exactly the emitted title, Korean `cause`,
  `impact`, single `minimum_action`, and `follow_up` fields.
- Non-human exceptions stay in local evidence and the remediation plan. A
  coordinator may apply a plan step only via the named official `kanban_*`
  tool and must record the resulting tool response.

## Internal lifecycle coordinator feed

This is a separate job from the human-only runtime blocker watch above. Do not
alter, replace, resume, or remove the human-blocker no-agent job when installing
the lifecycle feed.

`scripts/ops/kanban_lifecycle_watch.py` reads `task_events` through a read-only
SQLite connection. It keeps an atomic cursor, appends a sanitized audit record
to `/opt/data/cron/evidence/kanban-lifecycle-watch.jsonl`, and prints at most one
bounded JSON batch. The batch contains coordinator lifecycle metadata only; it
never contains card bodies, full event payloads, credentials, PIDs, claim-lock
values, or other internal identifiers. Heartbeats and classified internal
events are aggregated by count in the local audit without representative text
and advance the validated cursor without producing coordinator input. An
unknown event kind fails closed with `health_error` and does not advance the
cursor.

The audit proves only that the watcher read, sanitized, and durably recorded a
batch locally. It is not proof that the cron runner invoked an agent or that an
origin received a notification. Operators must monitor the lifecycle cron
entry in `/opt/data/cron/jobs.json`: a non-`ok` `last_status` or a non-empty
`last_delivery_error` is a delivery-path incident. Hermes does not provide a
downstream recipient acknowledgement here, so rollout and ongoing checks must
not describe the audit record as end-to-end delivery evidence.

The first production run establishes a silent baseline. Use `--from-start`
only with an isolated fixture database during acceptance. A malformed cursor,
missing task, incomplete schema, cursor rollback, or event-ID gap is a
`health_error`; treat that as loss of audit coverage and investigate it rather
than advancing or recreating the state file.

Install these reviewed files under the coordinator profile's
`HERMES_HOME/scripts` and preserve executable mode on the wrapper:

- `scripts/ops/kanban_lifecycle_watch.py`
- `scripts/ops/run_kanban_lifecycle_watch.sh`

Create a distinct, LLM-driven one-minute coordinator job (do not pass
`--no-agent`):

```bash
hermes cron create "every 1m" \
  "아래 내부 칸반 이벤트를 모두 점검하세요. routine start/progress/completion만 있으면 정확히 [SILENT]를 반환하세요. 중요한 blocker 또는 scope risk만 대표자에게 쉬운 한국어로 짧게 보고하세요. 원문 카드 본문이나 내부 payload를 재현하지 마세요." \
  --script run_kanban_lifecycle_watch.sh \
  --deliver origin \
  --name "kanban-lifecycle-coordinator"
```

The prompt contract is strict: inspect every emitted event internally; return
exactly `[SILENT]` for routine starts, progress, and completions; notify the
representative only about important blockers or scope risks, in easy Korean.
The cron interval must remain one minute so relevant lifecycle events are
collected within one minute.

Before rollout, run both watcher modules:

```bash
scripts/run_tests.sh tests/scripts/test_ops_kanban_lifecycle_watch.py
scripts/run_tests.sh tests/scripts/test_ops_kanban_runtime_watch.py
```

## Cutover

1. Keep `d1b35bd84781` paused.
2. Complete the dry run, focused tests, hash check, and three-run gate.
3. Create a new one-minute no-agent job whose delivery resolves to the
   conversation origin; do not reuse the old prompt-based job. Use:

   ```bash
   hermes cron create "every 1m" \
     --no-agent \
     --script run_kanban_runtime_watch.sh \
     --deliver origin \
     --name "kanban-runtime-watch"
   ```

   `--deliver local` is not acceptance for the production alerting job: it
   proves storage, not origin delivery. A separate ten-minute evidence-only
   job may use local delivery. Record the production job ID and resolved origin
   target in the private rollout log; never put credentials in this repository.
4. Exercise all three paths through that connection: healthy and non-human-only
   runs must store evidence without a delivery; the human-only fixture must
   deliver exactly the five fields `제목`, `원인`, `영향`, one `최소 조치`, and
   `후속 확인`. Verify all three stored records against the evidence file.
5. Observe at least one scheduled tick before removing the old paused job.
6. Roll back by pausing the new job only. The detector has no board mutation to
   undo. Keep legacy job `d1b35bd84781` paused throughout; do not resume or
   delete it during this rollout.
