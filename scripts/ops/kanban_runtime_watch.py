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
import fcntl
import json
import os
import re
import sqlite3
import tempfile
import time
from collections import Counter, defaultdict
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterator
from urllib.parse import quote

DEFAULT_DB = Path("/opt/data/kanban.db")
DEFAULT_EVIDENCE = Path("/opt/data/cron/evidence/kanban-runtime-watch.jsonl")
DEFAULT_NOTIFICATION_STATE = Path(
    "/opt/data/cron/evidence/kanban-runtime-watch-notifications.json"
)
ACTIVE_STATUSES = ("running", "ready", "blocked", "triage", "todo")
STALE_SECONDS = 120
MAX_EVIDENCE_RECORD_BYTES = 1024 * 1024
EVIDENCE_LOCK_TIMEOUT_SECONDS = 0.5

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
    conn.execute("BEGIN")
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
            "WHERE kind IN ("
            "'created','promoted','unblocked','status','reclaimed','changes_requested'"
            ") "
            "ORDER BY created_at, id"
        ).fetchall():
            task_id = str(event["task_id"])
            if task_id not in selected:
                continue
            kind = str(event["kind"] or "")
            enters_ready = kind in {"promoted", "unblocked"}
            if kind in {"status", "changes_requested"} and "payload" in event_columns:
                try:
                    payload = json.loads(event["payload"] or "{}")
                except (TypeError, ValueError):
                    payload = {}
                enters_ready = (
                    isinstance(payload, dict) and payload.get("status") == "ready"
                )
            elif kind == "reclaimed" and "payload" in event_columns:
                try:
                    payload = json.loads(event["payload"] or "{}")
                except (TypeError, ValueError):
                    payload = {}
                enters_ready = (
                    isinstance(payload, dict)
                    and payload.get("retry_status") == "ready"
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

        raw_block_kind = _value(task, task_columns, "block_kind")
        block_kind = None if raw_block_kind is None else str(raw_block_kind)
        if status in {"blocked", "triage"} and block_kind in {
            None, "needs_input", "capability"
        }:
            detail = (
                "사람만 해소 가능한 legacy 미분류 막힘"
                if block_kind is None
                else f"사람만 해소 가능한 block_kind={block_kind}"
            )
            findings.append(_finding(
                "needs_input", task_id, human_only=True,
                detail=detail,
            ))
        elif status == "blocked" and block_kind not in {"dependency", "transient"}:
            findings.append(_finding(
                "invalid_block_kind", task_id,
                detail=f"알 수 없는 block_kind={block_kind}",
            ))

        if status in {"todo", "ready"} and not assignee and not parents_by_child.get(task_id):
            findings.append(_finding(
                "unassigned_followup", task_id,
                detail="독립 후속업무에 담당 프로필이 없음",
            ))

    pid_result = {
        "checked": 0, "alive": 0, "missing": 0,
        "duplicates": [], "results": [],
    }
    for pid, task_ids in sorted(pid_candidates.items()):
        pid_result["checked"] += 1
        alive = _pid_alive(pid)
        pid_result["results"].append({
            "pid": pid, "alive": alive, "task_ids": sorted(task_ids),
        })
        if alive:
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
            "checked": 0, "alive": 0, "missing": 0,
            "duplicates": [], "results": [],
        },
        "findings": [_finding("insufficient_evidence", "board", detail=detail)],
        "remediation_plan": [],
        "external_alerts": [{
            "cause": "감시기가 이번 점검을 완료하지 못했습니다.",
            "impact": "칸반 실행 상태를 정상으로 판정할 수 없습니다.",
            "minimum_action": "로컬 점검 기록에서 오류 원인을 확인해 주세요.",
            "follow_up": "원인 해소 후 감시기를 다시 실행해 비어 있지 않은 조회 결과를 확인합니다.",
        }],
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
    human_findings = [item for item in findings if bool(item.get("human_only"))]
    if not human_findings:
        return []
    causes = [
        f"{item['task_id']}: {item.get('detail', '사람 확인이 필요한 막힘')}"
        for item in human_findings
    ]
    first_task_id = str(human_findings[0]["task_id"])
    remaining = len(human_findings) - 1
    follow_up = "답변 후 공식 kanban unblock/재배정 경로로 다음 단계를 진행합니다."
    if remaining:
        follow_up += f" 재검사에서 나머지 {remaining}건의 해소 여부도 확인합니다."
    return [{
        "cause": "; ".join(causes),
        "impact": "담당 프로필의 후속 진행이 중단될 수 있습니다.",
        "minimum_action": f"칸반 카드 {first_task_id}의 요청에 답변해 주세요.",
        "follow_up": follow_up,
    }]


def append_evidence(path: Path, record: dict[str, object]) -> None:
    if path.is_symlink():
        raise ValueError("증거 경로는 심볼릭 링크일 수 없습니다")
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.parent.is_symlink():
        raise ValueError("증거 디렉터리는 심볼릭 링크일 수 없습니다")
    payload = json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    data = (payload + "\n").encode("utf-8")
    if len(data) > MAX_EVIDENCE_RECORD_BYTES:
        raise ValueError("증거 레코드가 크기 상한을 초과했습니다")
    flags = os.O_RDWR | os.O_APPEND | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags, 0o600)
    try:
        deadline = time.monotonic() + EVIDENCE_LOCK_TIMEOUT_SECONDS
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise TimeoutError("증거 파일 잠금 시간이 초과되었습니다")
                time.sleep(0.01)
        # Timestamp assignment belongs inside the append lock: concurrent
        # producers must leave independently identifiable evidence records.
        end = os.lseek(fd, 0, os.SEEK_END)
        position = end
        chunks: list[bytes] = []
        previous_line = b""
        while position > 0:
            size = min(8192, position)
            position -= size
            os.lseek(fd, position, os.SEEK_SET)
            chunks.insert(0, os.read(fd, size))
            tail = b"".join(chunks).rstrip(b"\n")
            if len(tail) > MAX_EVIDENCE_RECORD_BYTES:
                raise ValueError("이전 증거 레코드가 크기 상한을 초과했습니다")
            newline = tail.rfind(b"\n")
            if newline >= 0 or position == 0:
                previous_line = tail[newline + 1:]
                break
        if previous_line:
            try:
                previous = json.loads(previous_line)
                previous_at = datetime.fromisoformat(str(previous["timestamp"]))
                current_at = datetime.fromisoformat(str(record["timestamp"]))
                if current_at <= previous_at:
                    record["timestamp"] = (previous_at + timedelta(microseconds=1)).isoformat()
            except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                pass
        payload = json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        data = (payload + "\n").encode("utf-8")
        if len(data) > MAX_EVIDENCE_RECORD_BYTES:
            raise ValueError("증거 레코드가 크기 상한을 초과했습니다")
        written = os.write(fd, data)
        if written != len(data):
            raise OSError("증거 레코드를 단일 append로 완전히 기록하지 못했습니다")
        os.fsync(fd)
    finally:
        os.close(fd)


def format_human_notifications(record: dict[str, object]) -> str:
    """Render only actionable Korean fields; empty output suppresses delivery."""
    alerts = record.get("external_alerts") or []
    sections = []
    for alert in alerts:
        sections.append("\n".join((
            f"제목: {alert.get('title') or '칸반 감시기 점검'}",
            f"원인: {alert['cause']}",
            f"영향: {alert['impact']}",
            f"최소 조치: {alert['minimum_action']}",
            f"후속 확인: {alert['follow_up']}",
        )))
    return "\n\n".join(sections)


def _notification_candidates(
    conn: sqlite3.Connection, record: dict[str, object]
) -> list[dict[str, object]]:
    """Resolve public-delivery metadata without changing canonical evidence."""
    human_ids = {
        str(item["task_id"])
        for item in record.get("findings", [])  # type: ignore[union-attr]
        if bool(item.get("human_only"))
    }
    columns = _columns(conn, "tasks")
    event_columns = _columns(conn, "task_events")
    run_columns = _columns(conn, "task_runs")
    candidates: list[dict[str, object]] = []
    for task_id in human_ids:
        row = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        if row is None:
            continue
        title = str(_value(row, columns, "title") or "제목 없는 업무").strip()
        event_reason = ""
        event_at = 0
        if {"id", "task_id", "kind", "payload", "created_at"} <= event_columns:
            event = conn.execute(
                "SELECT * FROM task_events WHERE task_id=? "
                "AND kind IN ('blocked','block_loop_detected') "
                "ORDER BY created_at DESC, id DESC LIMIT 1",
                (task_id,),
            ).fetchone()
            if event is not None:
                try:
                    payload = json.loads(event["payload"] or "{}")
                except (TypeError, ValueError, json.JSONDecodeError):
                    payload = {}
                if isinstance(payload, dict) and isinstance(payload.get("reason"), str):
                    event_reason = payload["reason"].strip()
                    event_at = int(event["created_at"] or 0)
        run_summary = ""
        run_at = 0
        if {"task_id", "summary"} <= run_columns:
            run = conn.execute(
                "SELECT * FROM task_runs WHERE task_id=? AND summary IS NOT NULL "
                "AND trim(summary)<>'' "
                "ORDER BY COALESCE(ended_at, started_at) DESC, id DESC LIMIT 1",
                (task_id,),
            ).fetchone()
            if run is not None:
                run_summary = str(run["summary"]).strip()
                run_at = int(_value(run, run_columns, "ended_at") or
                             _value(run, run_columns, "started_at") or 0)
        kind = str(_value(row, columns, "block_kind") or "legacy")
        if run_summary and run_at > event_at:
            reason = run_summary
            reason_at = run_at
        elif event_reason:
            reason = event_reason
            reason_at = event_at
        elif run_summary:
            reason = run_summary
            reason_at = run_at
        else:
            reason = (
                "진행에 필요한 사람의 결정이나 정보가 아직 제공되지 않았습니다."
                if kind != "capability"
                else "진행에 필요한 접근 권한이나 외부 지원이 아직 제공되지 않았습니다."
            )
            reason_at = 0
        cause, action = _split_reason_action(reason, secrets=(task_id,))
        searchable = f"{title} {reason}".casefold().replace(" ", "")
        # These are observations or machine-owned waits, not unanswered human gates.
        excluded = ("기술검증", "검증완료", "확인완료", "이미조치", "조치완료",
                    "조치됨", "봇자동", "자동해결", "자동복구", "자동처리",
                    "의존성", "선행작업", "선행업무")
        if any(term in searchable for term in excluded):
            continue
        if kind in {"dependency", "transient", "technical_verification", "bot_resolving"}:
            continue
        if any(
            _value(row, columns, name)
            for name in ("actioned_at", "resolved_at", "verification_status",
                         "bot_resolving", "dependency_task_id")
        ):
            continue
        rank = tuple(
            int(_value(row, columns, name) or 0)
            for name in ("operational_impact", "priority", "urgency")
        )
        fingerprint = json.dumps(
            {"cause": cause, "minimum_action": action},
            ensure_ascii=False, sort_keys=True,
            separators=(",", ":"),
        )
        candidates.append({
            "key": task_id,
            "fingerprint": fingerprint,
            "title": title,
            "cause": cause,
            "action": action,
            "rank": (*rank, reason_at or int(_value(row, columns, "created_at") or 0)),
        })
    return candidates


_ACTION_PREFIX = re.compile(r"(?:최소\s*조치|필요한\s*조치|대안\s*1개)\s*[:：]\s*")
_INTERNAL_TOKEN = re.compile(
    r"(?:\b(?i:task|run|pid|commit|block(?:ed|er|_kind)?|needs_input|capability)"
    r"(?i:[-_:= ]?[a-z0-9]+)*\b|(?i:\b[0-9a-f]{7,40}\b)|"
    r"\b[A-Z][A-Z0-9]*(?:[-_][A-Z0-9]+)+\b)"
)


def _sanitize_public_text(value: str, *, secrets: tuple[str, ...] = ()) -> str:
    cleaned = value
    for secret in secrets:
        if secret:
            cleaned = cleaned.replace(secret, "")
    cleaned = _INTERNAL_TOKEN.sub("", cleaned)
    return re.sub(r"\s{2,}", " ", cleaned).strip(" ,;:-")


def _split_reason_action(
    reason: str, *, secrets: tuple[str, ...] = ()
) -> tuple[str, str]:
    match = _ACTION_PREFIX.search(reason)
    if match:
        cause = reason[:match.start()].strip()
        action = reason[match.end():].strip()
    else:
        sentences = re.split(r"(?<=[.!?。！？])\s+", reason.strip())
        action_index = next(
            (index for index, sentence in enumerate(sentences)
             if re.search(r"해\s*주세요[.!?。！？]?\s*$", sentence)),
            None,
        )
        if action_index is None:
            cause, action = reason.strip(), "필요한 결정이나 정보를 한 가지 알려 주세요."
        else:
            cause = " ".join(sentences[:action_index]).strip()
            action = sentences[action_index].strip()
    cause = _sanitize_public_text(cause, secrets=secrets) or "사람의 결정이나 정보가 필요합니다."
    action = _sanitize_public_text(action, secrets=secrets)
    if not action or "주세요" not in action.replace(" ", ""):
        action = "필요한 결정이나 정보를 한 가지 알려 주세요."
    return cause, action


def _public_notification(candidate: dict[str, object]) -> str:
    title = _sanitize_public_text(
        str(candidate["title"]), secrets=(str(candidate["key"]),)
    ) or "사람 확인이 필요한 업무"
    cause = str(candidate["cause"])
    action = str(candidate["action"])
    return "\n".join((
        f"제목: {title}",
        f"원인: {cause}",
        "영향: 이 업무와 연결된 다음 운영 단계가 시작되지 못합니다.",
        f"최소 조치: {action}",
        "후속 확인: 답변이 반영되면 다음 점검에서 진행 재개 여부를 확인합니다.",
    ))


def _candidate_rank(candidate: dict[str, object]) -> tuple[int, ...]:
    rank = candidate.get("rank")
    if not isinstance(rank, tuple):
        return ()
    return tuple(int(value) for value in rank)


def _paths_alias(left: Path, right: Path) -> bool:
    if left.resolve(strict=False) == right.resolve(strict=False):
        return True
    try:
        return os.path.samefile(left, right)
    except (FileNotFoundError, OSError):
        return False


@contextlib.contextmanager
def _notification_state_lock(path: Path) -> Iterator[None]:
    """Serialize the board snapshot and its notification-state transition."""
    if path.is_symlink():
        raise ValueError("알림 상태 경로는 심볼릭 링크일 수 없습니다")
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.parent.is_symlink():
        raise ValueError("알림 상태 디렉터리는 심볼릭 링크일 수 없습니다")
    lock_path = path.with_name(path.name + ".lock")
    lock_fd = os.open(
        lock_path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600
    )
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(lock_fd)


def _update_notification_state(
    path: Path, candidates: list[dict[str, object]], *, locked: bool = False
) -> str:
    """Atomically compare/update active fingerprints and return at most one alert."""
    if not locked:
        with _notification_state_lock(path):
            return _update_notification_state(path, candidates, locked=True)
    existed = path.is_file()
    valid_previous = not existed
    previous: dict[str, str] = {}
    if existed:
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if (isinstance(loaded, dict) and loaded.get("version") == 1
                    and isinstance(loaded.get("active"), dict)
                    and all(isinstance(k, str) and isinstance(v, str)
                            for k, v in loaded["active"].items())):
                previous = {str(k): str(v) for k, v in loaded["active"].items()}
                valid_previous = True
        except (OSError, ValueError, json.JSONDecodeError):
            pass
    current = {str(item["key"]): str(item["fingerprint"]) for item in candidates}
    changed = [
        item for item in candidates
        if existed and valid_previous
        and previous.get(str(item["key"])) != item["fingerprint"]
    ]
    selected = max(changed, key=_candidate_rank) if changed else None
    active: dict[str, str] = {}
    for item in candidates:
        key = str(item["key"])
        fingerprint = current[key]
        if selected is item or not (existed and valid_previous):
            active[key] = fingerprint
        elif previous.get(key) == fingerprint:
            active[key] = fingerprint
        elif key in previous:
            # Keep an unselected change pending for the next tick. New
            # unselected keys stay absent, which likewise keeps them pending.
            active[key] = previous[key]
    payload = json.dumps({"version": 1, "active": active}, ensure_ascii=False,
                         sort_keys=True, separators=(",", ":"))
    temp_fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(temp_fd, 0o600)
        data = payload.encode("utf-8")
        offset = 0
        while offset < len(data):
            written = os.write(temp_fd, data[offset:])
            if written <= 0:
                raise OSError("알림 상태를 완전히 기록하지 못했습니다")
            offset += written
        os.fsync(temp_fd)
        os.close(temp_fd)
        temp_fd = -1
        os.replace(temp_name, path)
        dir_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    finally:
        if temp_fd >= 0:
            os.close(temp_fd)
        if os.path.exists(temp_name):
            os.unlink(temp_name)
    if selected is None:
        return ""
    return _public_notification(selected)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--evidence", type=Path, default=DEFAULT_EVIDENCE)
    parser.add_argument("--state", type=Path)
    parser.add_argument("--now", type=int)
    parser.add_argument("--timeout", type=float, default=2.0)
    parser.add_argument(
        "--notification-mode", choices=("json", "human-only"), default="json"
    )
    args = parser.parse_args(argv)
    state_path = args.state
    if state_path is None:
        state_path = (
            DEFAULT_NOTIFICATION_STATE
            if args.evidence == DEFAULT_EVIDENCE
            else args.evidence.with_name(args.evidence.name + ".notifications.json")
        )

    now = int(time.time()) if args.now is None else args.now
    human_mode = args.notification_mode == "human-only"
    notification = ""
    try:
        if human_mode and any(
            _paths_alias(state_path, persistent)
            for persistent in (args.db, args.evidence)
        ):
            raise ValueError("알림 상태 경로는 데이터베이스나 증거 경로와 달라야 합니다")
        lock_context = (
            _notification_state_lock(state_path)
            if human_mode else contextlib.nullcontext()
        )
        with lock_context:
            with contextlib.closing(_read_db(args.db, args.timeout)) as conn:
                record = collect_evidence(conn, now=now)
                candidates = _notification_candidates(conn, record) if human_mode else []
            if args.now is None:
                record["timestamp"] = datetime.now(tz=timezone.utc).isoformat()
            append_evidence(args.evidence, record)
            if human_mode:
                notification = _update_notification_state(
                    state_path, candidates, locked=True
                )
    except Exception as exc:
        record = _error_record(now, 0, f"검사를 완료하지 못함: {type(exc).__name__}")
        try:
            append_evidence(args.evidence, record)
        except Exception:
            pass
        if human_mode:
            notification = format_human_notifications(record)
    if human_mode:
        if notification:
            print(notification)
        return 0
    print(json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    return 0 if record["status"] == "PASS" else (1 if record["status"] == "ERROR" else 2)


if __name__ == "__main__":
    raise SystemExit(main())
