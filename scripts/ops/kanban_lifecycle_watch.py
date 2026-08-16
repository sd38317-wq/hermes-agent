#!/usr/bin/env python3
"""Read-only, cursor-based kanban lifecycle feed for a coordinator agent."""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import sqlite3
import sys
import tempfile
import hashlib
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

DEFAULT_DB = Path("/opt/data/kanban.db")
DEFAULT_STATE = Path("/opt/data/cron/state/kanban-lifecycle-watch.json")
DEFAULT_AUDIT = Path("/opt/data/cron/evidence/kanban-lifecycle-watch.jsonl")
MAX_ROWS = 500
MAX_EVENTS = 100
MAX_TEXT = 240
MAX_IDENTIFIER = 128
MAX_RECONCILE_ROWS = 500
MAX_MANIFEST_FILES = 2000
UNMATCHED_TOOL_SECONDS = 300

CATEGORY_KINDS = {
    "start": ("claimed", "spawned"),
    "progress": (
        "attached", "commented", "claim_extended", "retry_model_selected",
        "tool_started", "tool_completed",
    ),
    "scope_change": (
        "assigned", "attachment_removed", "changes_requested", "created", "decomposed",
        "descendant_invalidated", "edited", "linked", "model_override_set",
        "priority_lock_designated", "priority_lock_replaced", "priority_lock_released",
        "reasoning_effort_set", "reprioritized", "specified", "status", "unlinked",
    ),
    "blocked": (
        "blocked", "block_loop_detected", "claim_rejected",
        "coverage_gap",
        "completion_blocked_hallucination", "coordination_required", "crashed",
        "dependency_wait", "gave_up", "protocol_violation", "rate_limited",
        "reclaim_deferred", "respawn_guarded", "spawn_failed",
        "suspected_hallucinated_references", "timed_out",
    ),
    "resumed": ("promoted", "promoted_manual", "reclaimed", "unblocked"),
    "completed": ("completed", "review_requested"),
    "internal": (
        "archived", "heartbeat", "reconciled", "review_reopened", "scheduled", "stale",
        "tip_scratch_workspace",
    ),
}
ROUTINE_KINDS = frozenset({"heartbeat"})
KIND_TO_CATEGORY = {
    kind: category for category, kinds in CATEGORY_KINDS.items() for kind in kinds
}
KNOWN_KINDS = frozenset(KIND_TO_CATEGORY)
IDENTIFIER_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:@-]{0,127}\Z")


class HealthError(Exception):
    pass


class CoverageError(HealthError):
    """A detected audit omission whose cursor/fingerprint must not advance."""

    pass


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _reject_symlinks(path: Path, *, include_leaf: bool = True) -> None:
    absolute = path.absolute()
    parts = absolute.parts
    current = Path(parts[0])
    stop = len(parts) if include_leaf else len(parts) - 1
    for part in parts[1:stop]:
        current /= part
        if current.is_symlink():
            raise HealthError("unsafe symlink path")
    if include_leaf and absolute.is_symlink():
        raise HealthError("unsafe symlink path")


def _safe_parent(path: Path) -> None:
    _reject_symlinks(path.parent)
    path.parent.mkdir(parents=True, exist_ok=True)
    _reject_symlinks(path.parent)


def _fsync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_all(fd: int, data: bytes) -> None:
    offset = 0
    while offset < len(data):
        written = os.write(fd, data[offset:])
        if written <= 0:
            raise OSError("incomplete write")
        offset += written


def _connect(path: Path) -> sqlite3.Connection:
    if path.is_symlink() or not path.is_file():
        raise HealthError("database path is missing or unsafe")
    conn = sqlite3.connect(
        f"file:{quote(str(path.resolve()))}?mode=ro", uri=True, timeout=5
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    conn.execute("BEGIN")
    return conn


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    if not exists:
        return set()
    return {str(row[1]) for row in conn.execute(f'PRAGMA table_info("{table}")')}


def _load_state(path: Path) -> dict[str, object] | None:
    _reject_symlinks(path)
    if not path.exists():
        return None
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise HealthError("malformed cursor state") from exc
    if not isinstance(state, dict) or state.get("version") not in {1, 2}:
        raise HealthError("malformed cursor state")
    cursor = state.get("cursor")
    if isinstance(cursor, bool) or not isinstance(cursor, int) or cursor < 0:
        raise HealthError("malformed cursor state")
    if state.get("version") == 1:
        return {"version": 2, "cursor": cursor, "manifests": {}, "mutation_event": {}}
    manifests = state.get("manifests", {})
    mutations = state.get("mutation_event", {})
    if not isinstance(manifests, dict) or not isinstance(mutations, dict):
        raise HealthError("malformed cursor state")
    if len(manifests) > MAX_RECONCILE_ROWS or len(mutations) > MAX_RECONCILE_ROWS:
        raise HealthError("cursor state is unbounded")
    return {"version": 2, "cursor": cursor, "manifests": manifests, "mutation_event": mutations}


def _atomic_state(path: Path, state: dict[str, object]) -> None:
    _safe_parent(path)
    _reject_symlinks(path)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        data = json.dumps(state, separators=(",", ":"), sort_keys=True)
        _write_all(fd, (data + "\n").encode("utf-8"))
        os.fsync(fd)
        os.fchmod(fd, 0o600)
        os.close(fd)
        fd = -1
        _reject_symlinks(path)
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        if fd >= 0:
            os.close(fd)
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def _append_audit(path: Path, record: dict[str, object]) -> None:
    _safe_parent(path)
    _reject_symlinks(path)
    data = (json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n").encode()
    existed = path.exists()
    flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        _write_all(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)
    if not existed:
        _fsync_directory(path.parent)


def _identifier(value: object) -> str | None:
    if not isinstance(value, str) or not IDENTIFIER_RE.fullmatch(value):
        return None
    return value[:MAX_IDENTIFIER]


def _sanitize(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    text = re.sub(r"[\x00-\x20\x7f]+", " ", value).strip()
    text = re.sub(r"(?i)\b(?:bearer|basic)\s+[A-Za-z0-9._~+/=-]+", "[redacted]", text)
    text = re.sub(
        r"(?i)\b(?:sk|pk|rk|gh[pousr]|xox[baprs]|hf_|npm_|AIza|AKIA)[-A-Za-z0-9_]{6,}",
        "[redacted]", text,
    )
    text = re.sub(r"(?i)\b(?:https?|ftp)://\S+|\bwww\.\S+", "[redacted]", text)
    text = re.sub(r"(?<![A-Za-z0-9.])/(?:[^\s/:]+/)*[^\s/:]*|\b[A-Za-z]:\\\S+", "[redacted]", text)
    # Redact both bare labels (token=...) and prefixed environment-style
    # labels (OPENAI_API_KEY=..., AWS_SECRET_ACCESS_KEY=...).
    sensitive_label = (
        r"[A-Za-z0-9_]*(?:token|secret|password|passwd|api[_-]?key|access[_-]?key|"
        r"credentials?|authorization|claim[_-]?lock|lock)"
    )
    text = re.sub(
        rf"(?i)\b(?:{sensitive_label})\b\s*[:=]\s*(?:[^,;|]+)",
        "[redacted]", text,
    )
    text = re.sub(r"(?i)\b(?:reason|summary|body|payload|title|assignee)\s*:", "[redacted] ", text)
    text = re.sub(r"(?i)\bpid\s*[:=#]?\s*\d+\b", "[redacted]", text)
    text = re.sub(r"\b\d{4,}\b", "[redacted]", text)
    return text[:MAX_TEXT] if text else None


def _event(row: sqlite3.Row) -> dict[str, object]:
    try:
        payload = json.loads(row["payload"]) if row["payload"] else {}
    except (TypeError, json.JSONDecodeError):
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    task_id = _identifier(row["task_id"])
    if task_id is None:
        raise HealthError("event has unsafe task identifier")
    created_at = row["created_at"]
    if isinstance(created_at, bool) or not isinstance(created_at, int) or created_at <= 0:
        raise HealthError("event has unsafe timestamp")
    result: dict[str, object] = {
        "task_id": task_id,
        "title": _sanitize(row["title"]) or "(untitled)",
        "assignee": _identifier(row["assignee"]) if row["assignee"] else None,
        "category": KIND_TO_CATEGORY[str(row["kind"])],
        "kind": str(row["kind"]),
        "time": created_at,
    }
    if str(row["kind"]) in {"tool_started", "tool_completed"}:
        result.pop("title", None)
        result.pop("assignee", None)
        for key in ("tool", "call_id", "status", "duration_ms"):
            value = payload.get(key)
            if key == "duration_ms" and isinstance(value, int) and 0 <= value <= 86_400_000:
                result[key] = value
            elif key != "duration_ms" and _identifier(value) is not None:
                result[key] = _identifier(value)
    for key in ("reason", "summary"):
        value = _sanitize(payload.get(key))
        if value:
            result[key] = value
            break
    return result


def _validate_history(conn: sqlite3.Connection, cursor: int) -> int:
    stats = conn.execute(
        "SELECT COUNT(*) AS n, MIN(id) AS lo, MAX(id) AS hi FROM task_events"
    ).fetchone()
    count, lo, hi = int(stats["n"]), stats["lo"], stats["hi"]
    maximum = int(hi) if hi is not None else 0
    if count and (int(lo) != 1 or count != maximum):
        raise HealthError("event-id gap detected")
    if cursor > maximum:
        raise HealthError("cursor is ahead of event log")
    return maximum


def _payload_dict(raw: object) -> dict[str, object]:
    try:
        value = json.loads(raw) if raw else {}
    except (TypeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _manifest(path: Path) -> tuple[str | None, str]:
    """Return (fingerprint, audit outcome) without following symlinks."""
    try:
        _reject_symlinks(path)
        resolved = path.resolve(strict=True)
    except (HealthError, OSError):
        return None, "unsafe"
    text = str(resolved)
    managed_markers = ("/kanban/workspaces/", "/.worktrees/")
    if not any(marker in text for marker in managed_markers):
        return None, "unmanaged"
    digest = hashlib.sha256()
    count = 0
    try:
        for root, dirs, files in os.walk(resolved, topdown=True, followlinks=False):
            dirs[:] = sorted(d for d in dirs if not (Path(root) / d).is_symlink())
            for name in sorted(files):
                candidate = Path(root) / name
                if candidate.is_symlink():
                    continue
                stat = candidate.stat(follow_symlinks=False)
                relative = candidate.relative_to(resolved).as_posix()
                digest.update(relative.encode("utf-8", "surrogateescape"))
                digest.update(f":{stat.st_size}:{stat.st_mtime_ns}\n".encode())
                count += 1
                if count >= MAX_MANIFEST_FILES:
                    return None, "bounded"
    except OSError:
        return None, "unsafe"
    return digest.hexdigest()[:32], "scanned"


def _reconcile(conn: sqlite3.Connection, prior: dict[str, object], *, enforce: bool = True) -> tuple[dict[str, str], dict[str, int], dict[str, int]]:
    """Check durable rows against lifecycle events and bounded worktree manifests."""
    issues: list[str] = []
    counts = {"worktrees_scanned": 0, "worktrees_unmanaged": 0,
              "worktrees_unsafe": 0, "worktrees_bounded": 0}
    task_cols = _columns(conn, "tasks")
    select = [c for c in ("id", "status", "workspace_kind", "workspace_path") if c in task_cols]
    task_count = int(conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0])
    if task_count > MAX_RECONCILE_ROWS:
        raise CoverageError("task reconciliation capacity exceeded")
    tasks = conn.execute(
        f"SELECT {','.join(select)} FROM tasks ORDER BY id LIMIT ?", (MAX_RECONCILE_ROWS,)
    ).fetchall()
    manifests: dict[str, str] = {}
    mutations: dict[str, int] = {}
    prior_manifests = prior.get("manifests", {}) if isinstance(prior.get("manifests"), dict) else {}
    prior_mutations = prior.get("mutation_event", {}) if isinstance(prior.get("mutation_event"), dict) else {}
    mutation_tools = {
        "patch", "write_file", "terminal", "execute_code", "computer_use",
        "apply_patch", "exec_command",
    }
    for task in tasks:
        tid = str(task["id"])
        status = str(task["status"]) if "status" in task.keys() else ""
        # A brand-new empty board is a valid silent baseline. Once a running
        # task has any durable activity, however, worker ownership must also be
        # represented by a claim/spawn/heartbeat event.
        has_activity = conn.execute(
            "SELECT 1 FROM task_events WHERE task_id=? LIMIT 1", (tid,)
        ).fetchone() is not None
        if status == "running" and not has_activity:
            issues.append("running task lacks lifecycle activity")
        if status in {"done", "completed"} and conn.execute(
            "SELECT 1 FROM task_events WHERE task_id=? AND kind='completed' LIMIT 1", (tid,)
        ).fetchone() is None:
            issues.append("completed task lacks completed event")
        placeholders = ",".join("?" for _ in mutation_tools)
        mutation_row = conn.execute(
            "SELECT MAX(id) FROM task_events WHERE task_id=? AND kind='tool_completed' "
            "AND json_extract(payload,'$.status')='success' "
            f"AND json_extract(payload,'$.tool') IN ({placeholders})",
            (tid, *sorted(mutation_tools)),
        ).fetchone()
        successful_mutation = int(mutation_row[0] or 0)
        mutations[tid] = successful_mutation
        if {"workspace_kind", "workspace_path"} <= task_cols and task["workspace_kind"] == "worktree" and task["workspace_path"]:
            fingerprint, outcome = _manifest(Path(str(task["workspace_path"])))
            counts[f"worktrees_{outcome}"] = counts.get(f"worktrees_{outcome}", 0) + 1
            if fingerprint is not None:
                manifests[tid] = fingerprint
                before = prior_manifests.get(tid)
                if before is not None and before != fingerprint and successful_mutation <= int(prior_mutations.get(tid, 0) or 0):
                    issues.append("managed worktree changed without successful mutation tool")

    now = int(datetime.now(timezone.utc).timestamp())
    stale_start = conn.execute(
        "SELECT 1 FROM task_events s WHERE s.kind='tool_started' AND s.created_at<? "
        "AND json_type(s.payload,'$.call_id')='text' AND NOT EXISTS ("
        "SELECT 1 FROM task_events c WHERE c.task_id=s.task_id AND c.kind='tool_completed' "
        "AND json_extract(c.payload,'$.call_id')=json_extract(s.payload,'$.call_id')) LIMIT 1",
        (now - UNMATCHED_TOOL_SECONDS,),
    ).fetchone()
    if stale_start is not None:
        issues.append("unmatched tool start is stale")

    if _columns(conn, "task_runs"):
        run_cols = _columns(conn, "task_runs")
        if {"id", "task_id", "status"} <= run_cols:
            run_count = int(conn.execute(
                "SELECT COUNT(*) FROM task_runs WHERE status='running'"
            ).fetchone()[0])
            if run_count > MAX_RECONCILE_ROWS:
                raise CoverageError("running-run reconciliation capacity exceeded")
            rows = conn.execute(
                "SELECT id,task_id FROM task_runs WHERE status='running' ORDER BY id LIMIT ?",
                (MAX_RECONCILE_ROWS,),
            ).fetchall()
            event_has_run_id = "run_id" in _columns(conn, "task_events")
            for row in rows:
                if event_has_run_id:
                    found = conn.execute(
                        "SELECT 1 FROM task_events WHERE task_id=? AND run_id=? "
                        "AND kind IN ('claimed','spawned','heartbeat','reclaimed') LIMIT 1",
                        (str(row["task_id"]), str(row["id"])),
                    ).fetchone()
                else:
                    found = conn.execute(
                        "SELECT 1 FROM task_events WHERE task_id=? "
                        "AND kind IN ('claimed','spawned','heartbeat','reclaimed') LIMIT 1",
                        (str(row["task_id"]),),
                    ).fetchone()
                if found is None:
                    issues.append("running run lacks lifecycle activity")
    if _columns(conn, "task_attachments"):
        attachment_cols = _columns(conn, "task_attachments")
        if {"task_id", "filename", "size"} <= attachment_cols:
            attachment_count = int(conn.execute(
                "SELECT COUNT(*) FROM task_attachments"
            ).fetchone()[0])
            if attachment_count > MAX_RECONCILE_ROWS:
                raise CoverageError("attachment reconciliation capacity exceeded")
            rows = conn.execute(
                "SELECT task_id,filename,size FROM task_attachments LIMIT ?", (MAX_RECONCILE_ROWS,)
            ).fetchall()
            for attachment in rows:
                found = conn.execute(
                    "SELECT 1 FROM task_events WHERE task_id=? AND kind='attached' "
                    "AND json_extract(payload,'$.filename')=? "
                    "AND json_extract(payload,'$.size')=? LIMIT 1",
                    (str(attachment["task_id"]), str(attachment["filename"]), int(attachment["size"])),
                ).fetchone()
                if found is None:
                    issues.append("attachment lacks attached event")
    if issues and enforce:
        raise CoverageError("; ".join(sorted(set(issues))))
    return manifests, mutations, counts


def _emit(record: dict[str, object]) -> None:
    sys.stdout.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def run(db: Path, state: Path, audit: Path, *, from_start: bool) -> dict[str, object] | None:
    loaded = _load_state(state)
    current_state = loaded or {"version": 2, "cursor": 0, "manifests": {}, "mutation_event": {}}
    cursor = int(current_state["cursor"]) if loaded is not None else None
    with closing(_connect(db)) as conn:
        if not {"id", "task_id", "kind", "payload", "created_at"} <= _columns(conn, "task_events"):
            raise HealthError("task_events schema is incomplete")
        if not {"id", "title", "assignee"} <= _columns(conn, "tasks"):
            raise HealthError("tasks schema is incomplete")
        maximum = _validate_history(conn, cursor or 0)
        unknown = conn.execute(
            "SELECT kind FROM task_events WHERE kind NOT IN (%s) LIMIT 1"
            % ",".join("?" for _ in KNOWN_KINDS), tuple(sorted(KNOWN_KINDS)),
        ).fetchone()
        if unknown is not None:
            raise HealthError(f"unknown event kind: {_sanitize(str(unknown['kind'])) or '(invalid)'}")
        manifests, mutations, reconcile_counts = _reconcile(
            conn, current_state, enforce=loaded is not None
        )
        if cursor is None and not from_start:
            _append_audit(audit, {
                "timestamp": _utc_now(), "status": "baseline", "cursor": maximum,
                "emitted": 0, "reconciliation": reconcile_counts,
            })
            _atomic_state(state, {"version": 2, "cursor": maximum, "manifests": manifests,
                                  "mutation_event": mutations})
            return None
        cursor = 0 if from_start or cursor is None else cursor
        rows = conn.execute(
            "SELECT e.id,e.task_id,e.kind,e.payload,e.created_at,t.title,t.assignee "
            "FROM task_events e LEFT JOIN tasks t ON t.id=e.task_id "
            "WHERE e.id>? ORDER BY e.id LIMIT ?", (cursor, MAX_ROWS),
        ).fetchall()
        expected, end = cursor + 1, cursor
        events: list[dict[str, object]] = []
        aggregated = {"heartbeat": 0, "internal": 0}
        category_counts = {category: 0 for category in CATEGORY_KINDS}
        for row in rows:
            if int(row["id"]) != expected:
                raise HealthError("event-id gap detected")
            if row["title"] is None:
                raise HealthError("event references missing task")
            kind = str(row["kind"])
            if kind not in KNOWN_KINDS:
                raise HealthError(f"unknown event kind: {_sanitize(kind) or '(invalid)'}")
            if kind not in ROUTINE_KINDS and len(events) >= MAX_EVENTS:
                break
            expected += 1
            end = int(row["id"])
            category = KIND_TO_CATEGORY[kind]
            category_counts[category] += 1
            if kind == "heartbeat":
                aggregated["heartbeat"] += 1
            elif category == "internal":
                aggregated["internal"] += 1
            if kind in ROUTINE_KINDS or category == "internal":
                pass
            else:
                events.append(_event(row))
        audit_record = {
            "timestamp": _utc_now(), "status": "ok", "cursor_before": cursor,
            "cursor_after": end, "scanned": max(0, end - cursor), "emitted": len(events),
            "aggregated": aggregated, "category_counts": category_counts,
            "events": events, "reconciliation": reconcile_counts,
        }
        batch = {"status": "events", "count": len(events), "events": events} if events else None
        if batch is not None:
            _emit(batch)
        _append_audit(audit, audit_record)
        _atomic_state(state, {"version": 2, "cursor": end, "manifests": manifests,
                              "mutation_event": mutations})
        return batch


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--state", type=Path, default=DEFAULT_STATE)
    parser.add_argument("--audit", type=Path, default=DEFAULT_AUDIT)
    parser.add_argument("--from-start", action="store_true")
    args = parser.parse_args(argv)
    try:
        _safe_parent(args.state)
        lock_path = Path(str(args.state) + ".lock")
        _reject_symlinks(lock_path)
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
        lock_fd = os.open(lock_path, flags, 0o600)
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            run(args.db, args.state, args.audit, from_start=args.from_start)
        finally:
            os.close(lock_fd)
    except (HealthError, sqlite3.Error, OSError) as exc:
        status = "coverage_error" if isinstance(exc, CoverageError) else "health_error"
        result = {"status": status, "error": str(exc), "timestamp": _utc_now()}
        try:
            _append_audit(args.audit, result)
        except (HealthError, OSError):
            pass
        try:
            _emit(result)
        except OSError:
            pass
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
