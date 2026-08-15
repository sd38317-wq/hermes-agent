#!/usr/bin/env python3
"""Deterministic, read-only kanban runtime/queue watchdog.

The detector never mutates the kanban database.  It emits one canonical JSON
record per invocation and appends the same record to a local JSONL evidence
file.  Any remediation is an explicit plan of public ``kanban_*`` tool calls
for a coordinator to execute after review.
"""
from __future__ import annotations

import argparse
import contextlib
import json
import os
import sqlite3
import time
from collections import Counter, defaultdict
from contextvars import ContextVar
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterator
from urllib.parse import quote

DEFAULT_DB = Path("/opt/data/kanban.db")
DEFAULT_EVIDENCE = Path("/opt/data/cron/evidence/kanban-runtime-watch.jsonl")
ACTIVE_STATUSES = ("running", "ready", "blocked", "todo")
STALE_SECONDS = 120

_PID_PROBE: ContextVar[Callable[[int], bool] | None] = ContextVar(
    "kanban_runtime_watch_pid_probe", default=None
)


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    if not conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone():
        return set()
    return {str(row[1]) for row in conn.execute(f'PRAGMA table_info("{table}")')}


def _read_db(path: Path, timeout: float) -> sqlite3.Connection:
    if path.is_symlink():
        raise ValueError("DB 경로는 심볼릭 링크일 수 없습니다")
    if not path.is_file():
        raise ValueError("DB 파일을 찾을 수 없습니다")
    uri = f"file:{quote(str(path.resolve()))}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=timeout)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    conn.execute(f"PRAGMA busy_timeout={max(1, int(timeout * 1000))}")
    return conn


def _proc_alive(pid: int) -> bool:
    return Path(f"/proc/{pid}").is_dir()


def _pid_alive(pid: object) -> bool:
    try:
        value = int(pid)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return False
    if value <= 0:
        return False
    probe = _PID_PROBE.get() or _proc_alive
    return bool(probe(value))


@contextlib.contextmanager
def pid_probe(probe: Callable[[int], bool]) -> Iterator[None]:
    """Override the proc probe for deterministic fixture tests."""
    token = _PID_PROBE.set(probe)
    try:
        yield
    finally:
        _PID_PROBE.reset(token)


def _value(row: sqlite3.Row, columns: set[str], name: str):
    return row[name] if name in columns else None


def _finding(
    kind: str,
    task_id: str,
    *,
    human_only: bool = False,
    detail: str,
) -> dict[str, object]:
    return {
        "kind": kind,
        "task_id": task_id,
        "human_only": human_only,
        "detail": detail,
    }


def collect_evidence(
    conn: sqlite3.Connection,
    *,
    now: int | None = None,
) -> dict[str, object]:
    """Query canonical state and return a deterministic evidence record."""
    now = int(time.time()) if now is None else int(now)
    query_count = 0
    task_columns = _columns(conn, "tasks")
    run_columns = _columns(conn, "task_runs")
    link_columns = _columns(conn, "task_links")

    required = {"id", "status"}
    if not required <= task_columns:
        return _error_record(now, query_count, "tasks 스키마에 id/status가 없습니다")

    placeholders = ",".join("?" for _ in ACTIVE_STATUSES)
    tasks = conn.execute(
        f"SELECT * FROM tasks WHERE status IN ({placeholders}) ORDER BY id",
        ACTIVE_STATUSES,
    ).fetchall()
    query_count += 1

    runs: list[sqlite3.Row] = []
    if {"id", "task_id"} <= run_columns:
        where = "status='running'" if "status" in run_columns else "ended_at IS NULL"
        runs = conn.execute(
            f"SELECT * FROM task_runs WHERE {where} ORDER BY id"
        ).fetchall()
        query_count += 1

    links: list[sqlite3.Row] = []
    if {"parent_id", "child_id"} <= link_columns:
        links = conn.execute(
            "SELECT parent_id, child_id FROM task_links ORDER BY parent_id, child_id"
        ).fetchall()
        query_count += 1

    ready_since: dict[str, int] = {}
    event_columns = _columns(conn, "task_events")
    if {"task_id", "kind", "created_at"} <= event_columns:
        selected = {str(row["id"]) for row in tasks}
        for event in conn.execute(
            "SELECT * FROM task_events "
            "WHERE kind IN ('created','promoted','unblocked','status') "
            "ORDER BY created_at, id"
        ).fetchall():
            task_id = str(event["task_id"])
            if task_id not in selected:
                continue
            kind = str(event["kind"] or "")
            enters_ready = kind in {"promoted", "unblocked"}
            if kind == "status" and "payload" in event_columns:
                try:
                    payload = json.loads(event["payload"] or "{}")
                except (TypeError, ValueError):
                    payload = {}
                enters_ready = (
                    isinstance(payload, dict) and payload.get("status") == "ready"
                )
            if enters_ready:
                ready_since[task_id] = int(event["created_at"])
        query_count += 1

    if not tasks:
        return _error_record(now, query_count, "검사 대상 행이 0건이라 정상 판정할 수 없습니다")

    task_by_id = {str(row["id"]): row for row in tasks}
    runs_by_task: dict[str, list[sqlite3.Row]] = defaultdict(list)
    for run in runs:
        runs_by_task[str(run["task_id"])].append(run)
    parents_by_child: dict[str, set[str]] = defaultdict(set)
    children_by_parent: dict[str, set[str]] = defaultdict(set)
    for link in links:
        parent, child = str(link["parent_id"]), str(link["child_id"])
        parents_by_child[child].add(parent)
        children_by_parent[parent].add(child)

    findings: list[dict[str, object]] = []
    pid_candidates: dict[int, set[str]] = defaultdict(set)

    for run in runs:
        task_id = str(run["task_id"])
        if task_id not in task_by_id:
            findings.append(_finding(
                "orphan_run", task_id,
                detail=f"활성 run {run['id']}의 카드가 활성 상태가 아님",
            ))
            run_pid = _value(run, run_columns, "worker_pid")
            if run_pid is not None:
                pid_candidates[int(run_pid)].add(task_id)

    current_run_refs: dict[int, set[str]] = defaultdict(set)
    if "current_run_id" in task_columns:
        for task_id, task in task_by_id.items():
            current_run_id = task["current_run_id"]
            if current_run_id is not None:
                current_run_refs[int(current_run_id)].add(task_id)
    for run_id, task_ids in sorted(current_run_refs.items()):
        if len(task_ids) > 1:
            for task_id in sorted(task_ids):
                findings.append(_finding(
                    "duplicate_current_run", task_id,
                    detail=f"current_run_id {run_id}를 {len(task_ids)}개 카드가 공유함",
                ))

    for task_id, task in task_by_id.items():
        status = str(task["status"] or "")
        created_at = int(_value(task, task_columns, "created_at") or now)
        assignee = str(_value(task, task_columns, "assignee") or "")
        active_runs = runs_by_task.get(task_id, [])

        effective_ready_since = ready_since.get(task_id, created_at)
        if status == "ready" and now - effective_ready_since > STALE_SECONDS:
            findings.append(_finding(
                "ready_stale", task_id,
                detail=f"ready 상태가 {now - effective_ready_since}초 지속됨",
            ))

        if status == "running":
            task_pid = _value(task, task_columns, "worker_pid")
            if task_pid is None:
                findings.append(_finding(
                    "running_without_pid", task_id,
                    detail="running 카드에 worker_pid가 없음",
                ))
            else:
                pid_candidates[int(task_pid)].add(task_id)

            if len(active_runs) != 1:
                findings.append(_finding(
                    "duplicate_task_run" if len(active_runs) > 1 else "missing_run",
                    task_id,
                    detail=f"활성 run 수가 {len(active_runs)}건임",
                ))

            current_run_id = _value(task, task_columns, "current_run_id")
            for run in active_runs:
                run_id = int(run["id"])
                if current_run_id is None or int(current_run_id) != run_id:
                    findings.append(_finding(
                        "current_run_mismatch", task_id,
                        detail=f"current_run_id={current_run_id}, active_run_id={run_id}",
                    ))
                run_pid = _value(run, run_columns, "worker_pid")
                if run_pid is not None:
                    pid_candidates[int(run_pid)].add(task_id)
                    if task_pid is not None and int(task_pid) != int(run_pid):
                        findings.append(_finding(
                            "worker_pid_mismatch", task_id,
                            detail=f"task PID={task_pid}, run PID={run_pid}",
                        ))
                profile = str(_value(run, run_columns, "profile") or "")
                if assignee and profile and assignee != profile:
                    findings.append(_finding(
                        "role_mismatch", task_id,
                        detail=f"assignee={assignee}, run profile={profile}",
                    ))

                task_heartbeat = _value(task, task_columns, "last_heartbeat_at")
                run_heartbeat = _value(run, run_columns, "last_heartbeat_at")
                if (
                    task_heartbeat is not None
                    and run_heartbeat is not None
                    and int(task_heartbeat) != int(run_heartbeat)
                ):
                    findings.append(_finding(
                        "heartbeat_mismatch", task_id,
                        detail=(f"task heartbeat={task_heartbeat}, "
                                f"run heartbeat={run_heartbeat}"),
                    ))

            runtime_limit = _value(task, task_columns, "max_runtime_seconds")
            started_at = _value(task, task_columns, "started_at")
            if runtime_limit is None and active_runs:
                runtime_limit = _value(
                    active_runs[0], run_columns, "max_runtime_seconds"
                )
            if started_at is None and active_runs:
                started_at = _value(active_runs[0], run_columns, "started_at")
            if (
                runtime_limit is not None
                and started_at is not None
                and now - int(started_at) > int(runtime_limit)
            ):
                findings.append(_finding(
                    "runtime_exceeded", task_id,
                    detail=(f"실행 {now - int(started_at)}초가 "
                            f"상한 {int(runtime_limit)}초를 초과함"),
                ))

            heartbeats = [
                _value(task, task_columns, "last_heartbeat_at"),
                *[_value(run, run_columns, "last_heartbeat_at") for run in active_runs],
            ]
            present_heartbeats = [int(value) for value in heartbeats if value is not None]
            if not present_heartbeats or now - max(present_heartbeats) > STALE_SECONDS:
                age = "없음" if not present_heartbeats else f"{now - max(present_heartbeats)}초"
                findings.append(_finding(
                    "heartbeat_stale", task_id,
                    detail=f"최신 heartbeat가 {age}",
                ))

        block_kind = str(_value(task, task_columns, "block_kind") or "")
        if status == "blocked" and block_kind not in {"", "dependency", "transient"}:
            findings.append(_finding(
                "needs_input", task_id, human_only=True,
                detail=f"사람만 해소 가능한 block_kind={block_kind}",
            ))

        if status in {"todo", "ready"} and not assignee and not parents_by_child.get(task_id):
            findings.append(_finding(
                "unassigned_followup", task_id,
                detail="독립 후속업무에 담당 프로필이 없음",
            ))

    pid_result = {"checked": 0, "alive": 0, "missing": 0, "duplicates": []}
    for pid, task_ids in sorted(pid_candidates.items()):
        pid_result["checked"] += 1
        if _pid_alive(pid):
            pid_result["alive"] += 1
        else:
            pid_result["missing"] += 1
            for task_id in sorted(task_ids):
                findings.append(_finding(
                    "pid_missing", task_id,
                    detail=f"/proc/{pid}가 존재하지 않음",
                ))
        if len(task_ids) > 1:
            duplicate = {"pid": pid, "task_ids": sorted(task_ids)}
            pid_result["duplicates"].append(duplicate)
            for task_id in sorted(task_ids):
                findings.append(_finding(
                    "duplicate_pid", task_id,
                    detail=f"PID {pid}를 {len(task_ids)}개 카드가 공유함",
                ))

    run_ids = [int(run["id"]) for run in runs]
    duplicate_run_ids = {run_id for run_id, count in Counter(run_ids).items() if count > 1}
    for run_id in sorted(duplicate_run_ids):
        for run in runs:
            if int(run["id"]) == run_id:
                findings.append(_finding(
                    "duplicate_run", str(run["task_id"]),
                    detail=f"run id {run_id}가 중복됨",
                ))

    profiles = {
        str(_value(task, task_columns, "assignee") or "")
        for task in tasks
        if _value(task, task_columns, "assignee")
    }
    for profile in sorted(profiles):
        profile_tasks = [
            task for task in tasks
            if str(_value(task, task_columns, "assignee") or "") == profile
        ]
        if any(str(task["status"]) in {"running", "ready"} for task in profile_tasks):
            continue
        waiting_ids = {
            str(task["id"]) for task in profile_tasks if str(task["status"]) == "todo"
        }
        blocked_parents_by_waiting = {
            task_id: {
                parent_id
                for parent_id in parents_by_child.get(task_id, set())
                if parent_id in task_by_id
                and str(task_by_id[parent_id]["status"]) == "blocked"
            }
            for task_id in waiting_ids
        }
        if waiting_ids and all(blocked_parents_by_waiting.values()):
            blocker = min(set().union(*blocked_parents_by_waiting.values()))
            findings.append(_finding(
                "profile_blocked", blocker,
                detail=f"{profile} 프로필의 대기 카드 전체가 blocked 카드에 의존함",
            ))

    findings = sorted(
        {json.dumps(item, ensure_ascii=False, sort_keys=True): item for item in findings}.values(),
        key=lambda item: (str(item["kind"]), str(item["task_id"]), str(item["detail"])),
    )
    alerts = _human_alerts(findings)
    return {
        "timestamp": datetime.fromtimestamp(now, tz=timezone.utc).isoformat(),
        "status": "PASS" if not findings else "FAIL",
        "query_count": query_count,
        "input_row_count": len(tasks),
        "finding_count": len(findings),
        "action_count": 0,
        "pid_reconciliation": pid_result,
        "findings": findings,
        "remediation_plan": build_remediation_plan(findings),
        "external_alerts": alerts,
    }


def _error_record(now: int, query_count: int, detail: str) -> dict[str, object]:
    return {
        "timestamp": datetime.fromtimestamp(now, tz=timezone.utc).isoformat(),
        "status": "ERROR",
        "query_count": query_count,
        "input_row_count": 0,
        "finding_count": 1,
        "action_count": 0,
        "pid_reconciliation": {
            "checked": 0, "alive": 0, "missing": 0, "duplicates": [],
        },
        "findings": [_finding("insufficient_evidence", "board", detail=detail)],
        "remediation_plan": [],
        "external_alerts": [],
    }


def build_remediation_plan(
    findings: list[dict[str, object]],
) -> list[dict[str, object]]:
    """Return official tool calls; this read-only detector never executes them."""
    plan: list[dict[str, object]] = []
    for finding in findings:
        task_id = str(finding["task_id"])
        kind = str(finding["kind"])
        if kind != "insufficient_evidence":
            plan.append({
                "tool": "kanban_comment",
                "arguments": {
                    "task_id": task_id,
                    "body": f"runtime-watch: {kind} — {finding.get('detail', '')}",
                },
            })
    return plan


def _human_alerts(findings: list[dict[str, object]]) -> list[dict[str, str]]:
    alerts: list[dict[str, str]] = []
    seen: set[str] = set()
    for finding in findings:
        if not bool(finding.get("human_only")):
            continue
        task_id = str(finding["task_id"])
        if task_id in seen:
            continue
        seen.add(task_id)
        alerts.append({
            "cause": f"{task_id}: {finding.get('detail', '사람 확인이 필요한 막힘')}",
            "impact": "담당 프로필의 후속 진행이 중단될 수 있습니다.",
            "minimum_action": f"칸반 카드 {task_id}의 요청에 답변해 주세요.",
            "follow_up": "답변 후 공식 kanban unblock/재배정 경로로 다음 단계를 진행합니다.",
        })
    return alerts


def append_evidence(path: Path, record: dict[str, object]) -> None:
    if path.is_symlink():
        raise ValueError("증거 경로는 심볼릭 링크일 수 없습니다")
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.parent.is_symlink():
        raise ValueError("증거 디렉터리는 심볼릭 링크일 수 없습니다")
    flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags, 0o600)
    try:
        payload = json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        os.write(fd, (payload + "\n").encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--evidence", type=Path, default=DEFAULT_EVIDENCE)
    parser.add_argument("--now", type=int)
    parser.add_argument("--timeout", type=float, default=2.0)
    args = parser.parse_args(argv)

    now = int(time.time()) if args.now is None else args.now
    try:
        with contextlib.closing(_read_db(args.db, args.timeout)) as conn:
            record = collect_evidence(conn, now=now)
        append_evidence(args.evidence, record)
    except Exception as exc:
        record = _error_record(now, 0, f"검사를 완료하지 못함: {type(exc).__name__}")
        try:
            append_evidence(args.evidence, record)
        except Exception:
            pass
    print(json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    return 0 if record["status"] == "PASS" else (1 if record["status"] == "ERROR" else 2)


if __name__ == "__main__":
    raise SystemExit(main())
