"""Best-effort, metadata-only tool lifecycle ledger for Kanban workers."""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sqlite3
import threading
import time
import fcntl
from contextlib import closing
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_SAFE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,95}\Z")
_MAX_DURATION_MS = 86_400_000
_MAX_FALLBACK_BYTES = 1_048_576
_FALLBACK_NAME = "kanban-tool-ledger-incidents.jsonl"
_failures = 0
_failure_lock = threading.Lock()


def enabled() -> bool:
    task_id = (os.environ.get("HERMES_KANBAN_TASK") or "").strip()
    if not task_id:
        return False
    try:
        from agent.delegation_context import is_dispatcher_owned_worker_context

        return is_dispatcher_owned_worker_context()
    except Exception:
        return False


def opaque_call_id(value: object, *, tool_name: str = "tool") -> str:
    raw = value if isinstance(value, str) and value else f"{tool_name}:{time.monotonic_ns()}"
    return hashlib.sha256(raw.encode("utf-8", "replace")).hexdigest()[:24]


def _identifier(value: object, limit: int = 96) -> str | None:
    if not isinstance(value, str) or not _SAFE_NAME.fullmatch(value):
        return None
    return value[:limit]


def _db_path() -> Path | None:
    value = os.environ.get("HERMES_KANBAN_DB")
    if not value:
        return None
    path = Path(value)
    if not path.is_absolute() or path.is_symlink():
        return None
    return path


def _payload(tool_name: str, call_id: str, *, status: str | None,
             duration_ms: int | float | None) -> dict[str, object]:
    payload: dict[str, object] = {
        "tool": _identifier(tool_name) or "unknown",
        "call_id": opaque_call_id(call_id, tool_name=tool_name),
    }
    if status is not None:
        payload["status"] = status if status in {"success", "error", "cancelled"} else "error"
    if duration_ms is not None:
        try:
            bounded = max(0, min(_MAX_DURATION_MS, int(duration_ms)))
        except (TypeError, ValueError, OverflowError):
            bounded = 0
        payload["duration_ms"] = bounded
    return payload


def _fallback_incident(db_path: Path | None = None) -> None:
    """Durably expose lost coverage without copying any observed tool data."""
    try:
        configured_root = (os.environ.get("HERMES_KANBAN_ROOT") or "").strip()
        if configured_root:
            root = Path(configured_root)
        elif db_path is not None:
            root = db_path.parent
        else:
            return
        if not root.is_absolute() or root.is_symlink():
            return
        evidence = root / "cron" / "evidence"
        evidence.mkdir(parents=True, exist_ok=True)
        if evidence.is_symlink():
            return
        path = evidence / _FALLBACK_NAME
        if path.is_symlink():
            return
        data = (json.dumps({
            "version": 1, "kind": "tool_ledger_write_failure", "created_at": int(time.time()),
        }, separators=(",", ":")) + "\n").encode("ascii")
        existed = path.exists()
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            if os.fstat(fd).st_size + len(data) > _MAX_FALLBACK_BYTES:
                return
            offset = 0
            while offset < len(data):
                written = os.write(fd, data[offset:])
                if written <= 0:
                    return
                offset += written
            os.fsync(fd)
        finally:
            os.close(fd)
        if not existed:
            directory_fd = os.open(evidence, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    except Exception:
        # This is the terminal, non-recursive failure path: never call record()
        # or attempt another database write from here.
        return


def record(kind: str, tool_name: str, call_id: str, *, status: str | None = None,
           duration_ms: int | float | None = None) -> None:
    """Append a tool event without ever affecting the tool being observed."""
    global _failures
    if kind not in {"tool_started", "tool_completed"} or not enabled():
        return
    task_id = _identifier((os.environ.get("HERMES_KANBAN_TASK") or "").strip(), 128)
    run_id = _identifier((os.environ.get("HERMES_KANBAN_RUN_ID") or "").strip(), 128)
    db_path = _db_path()
    if task_id is None or db_path is None:
        return
    try:
        with closing(sqlite3.connect(db_path, timeout=2)) as conn:
            columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(task_events)")}
            payload = json.dumps(
                _payload(tool_name, call_id, status=status, duration_ms=duration_ms),
                separators=(",", ":"),
            )
            now = int(time.time())
            if "run_id" in columns:
                conn.execute(
                    "INSERT INTO task_events (task_id,run_id,kind,payload,created_at) VALUES (?,?,?,?,?)",
                    (task_id, run_id, kind, payload, now),
                )
            else:
                conn.execute(
                    "INSERT INTO task_events (task_id,kind,payload,created_at) VALUES (?,?,?,?)",
                    (task_id, kind, payload, now),
                )
            conn.commit()
        with _failure_lock:
            _failures = 0
    except Exception:
        with _failure_lock:
            _failures += 1
            failures = _failures
        logger.warning("kanban tool lifecycle coverage write failed (count=%d)", failures)
        _fallback_incident(db_path)
        if failures % 3 == 0:
            # If the original failure was transient or kind-specific, make the
            # loss visible in the same append-only protocol. This attempt is
            # deliberately independent and just as best-effort.
            try:
                with closing(sqlite3.connect(db_path, timeout=1)) as conn:
                    columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(task_events)")}
                    values = (task_id, run_id, "coverage_gap", '{"source":"tool_ledger"}', int(time.time()))
                    if "run_id" in columns:
                        conn.execute(
                            "INSERT INTO task_events (task_id,run_id,kind,payload,created_at) VALUES (?,?,?,?,?)",
                            values,
                        )
                    else:
                        conn.execute(
                            "INSERT INTO task_events (task_id,kind,payload,created_at) VALUES (?,?,?,?)",
                            (task_id, "coverage_gap", values[3], values[4]),
                        )
                    conn.commit()
            except Exception:
                logger.warning("kanban tool lifecycle coverage event unavailable")


def result_status(result: Any) -> str:
    if isinstance(result, BaseException):
        return "cancelled" if result.__class__.__name__ in {"CancelledError", "KeyboardInterrupt"} else "error"
    value = result
    if isinstance(result, str):
        try:
            value = json.loads(result)
        except json.JSONDecodeError:
            lowered = result.lower()
            if "cancelled" in lowered or "canceled" in lowered:
                return "cancelled"
            if "error executing tool" in lowered:
                return "error"
            return "success"
    if isinstance(value, dict):
        status = str(value.get("status") or "").strip().lower()
        if status in {"cancelled", "canceled", "interrupted"}:
            return "cancelled"
        if status in {"failed", "failure", "error"}:
            return "error"
        if value.get("error") or value.get("last_delivery_error"):
            return "error"
        exit_code = value.get("exit_code", value.get("exitCode"))
        if exit_code is not None:
            try:
                if int(exit_code) != 0:
                    return "error"
            except (TypeError, ValueError, OverflowError):
                pass
    return "success"
