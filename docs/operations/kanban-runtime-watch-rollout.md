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

The command prints exactly one JSON object. Reject the rollout unless all of
these fields exist:

- `query_count > 0`
- `input_row_count > 0`
- `finding_count == len(findings)`
- `action_count == 0` during detection
- `pid_reconciliation.checked`, `alive`, `missing`, and `duplicates`
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
scheduling `run_kanban_runtime_watch.sh`. The cron must run as a
`no_agent=True` script job every 10 minutes and write its delivery locally;
stdout is evidence, not a chat notification. Do not configure Telegram, Slack,
or origin delivery for routine output.

The script arguments for the installed copy must remain:

```text
--db /opt/data/kanban.db --evidence /opt/data/cron/evidence/kanban-runtime-watch.jsonl
```

The checked-in wrapper supplies those fixed arguments. Do not add SQL or task
mutations to it.

## Three-run acceptance gate

Before creating the 10-minute job, execute the exact installed command three
times. Keep all three JSONL lines. Each line must independently contain its
own timestamp, non-zero input/query counts, finding/action counts, and PID
reconciliation. A zero-query or zero-input `PASS` fails acceptance.

Also run the focused regression suite:

```bash
scripts/run_tests.sh tests/scripts/test_ops_kanban_runtime_watch.py
```

The fixtures named `t_adf495b7`, `t_a40a0e65`, and `t_b965de12` cover the live
research PID/heartbeat shape and the dependency-gated restore/three-hour
follow-ups without mutating the live board.

## Alert and remediation policy

- Healthy runs produce zero external alerts.
- Internal lifecycle starts/completions produce no external alerts.
- Only findings marked `human_only=true` may become external notifications.
- A human notification must carry the emitted Korean `cause`, `impact`, single
  `minimum_action`, and `follow_up` fields.
- Non-human exceptions stay in local evidence and the remediation plan. A
  coordinator may apply a plan step only via the named official `kanban_*`
  tool and must record the resulting tool response.

## Cutover

1. Keep `d1b35bd84781` paused.
2. Complete the dry run, focused tests, hash check, and three-run gate.
3. Create a new 10-minute no-agent/local-delivery cron job for the installed
   script; do not reuse the old prompt-based job.
4. Run the new job once manually and verify its stored output against the
   evidence file.
5. Observe at least one scheduled tick before removing the old paused job.
6. Roll back by pausing the new job only. The detector has no board mutation to
   undo.
