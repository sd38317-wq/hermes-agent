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

CATEGORY_KINDS = {
    "start": ("claimed", "spawned"),
    "progress": ("attached", "commented", "claim_extended", "retry_model_selected"),
    "scope_change": (
        "assigned", "attachment_removed", "changes_requested", "created", "decomposed",
        "descendant_invalidated", "edited", "linked", "model_override_set",
        "priority_lock_designated", "priority_lock_replaced", "priority_lock_released",
        "reasoning_effort_set", "reprioritized", "specified", "status", "unlinked",
    ),
    "blocked": (
        "blocked", "block_loop_detected", "claim_rejected",
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


def _load_cursor(path: Path) -> int | None:
    _reject_symlinks(path)
    if not path.exists():
        return None
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise HealthError("malformed cursor state") from exc
    if not isinstance(state, dict) or state.get("version") != 1:
        raise HealthError("malformed cursor state")
    cursor = state.get("cursor")
    if isinstance(cursor, bool) or not isinstance(cursor, int) or cursor < 0:
        raise HealthError("malformed cursor state")
    return cursor


def _atomic_cursor(path: Path, cursor: int) -> None:
    _safe_parent(path)
    _reject_symlinks(path)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        data = json.dumps({"version": 1, "cursor": cursor}, separators=(",", ":"))
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


def _emit(record: dict[str, object]) -> None:
    sys.stdout.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def run(db: Path, state: Path, audit: Path, *, from_start: bool) -> dict[str, object] | None:
    cursor = _load_cursor(state)
    with closing(_connect(db)) as conn:
        if not {"id", "task_id", "kind", "payload", "created_at"} <= _columns(conn, "task_events"):
            raise HealthError("task_events schema is incomplete")
        if not {"id", "title", "assignee"} <= _columns(conn, "tasks"):
            raise HealthError("tasks schema is incomplete")
        maximum = _validate_history(conn, cursor or 0)
        if cursor is None and not from_start:
            _append_audit(audit, {
                "timestamp": _utc_now(), "status": "baseline", "cursor": maximum,
                "emitted": 0,
            })
            _atomic_cursor(state, maximum)
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
            if kind in ROUTINE_KINDS:
                aggregated["heartbeat"] += 1
            elif KIND_TO_CATEGORY[kind] == "internal":
                aggregated["internal"] += 1
            else:
                events.append(_event(row))
        audit_record = {
            "timestamp": _utc_now(), "status": "ok", "cursor_before": cursor,
            "cursor_after": end, "scanned": max(0, end - cursor), "emitted": len(events),
            "aggregated": aggregated, "events": events,
        }
        batch = {"status": "events", "count": len(events), "events": events} if events else None
        if batch is not None:
            _emit(batch)
        _append_audit(audit, audit_record)
        _atomic_cursor(state, end)
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
        result = {"status": "health_error", "error": str(exc), "timestamp": _utc_now()}
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
