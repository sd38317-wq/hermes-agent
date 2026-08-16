#!/usr/bin/env python3
"""Read-only watchdog for the Kanban lifecycle coordinator cron job."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import tempfile
import sys
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_JOBS = Path("/opt/data/cron/jobs.json")
DEFAULT_WATCH_STATE = Path("/opt/data/cron/state/kanban-lifecycle-watch.json")
DEFAULT_AUDIT = Path("/opt/data/cron/evidence/kanban-lifecycle-watch.jsonl")
DEFAULT_GUARD_STATE = Path("/opt/data/cron/state/kanban-lifecycle-guard.json")
DEFAULT_DB = Path("/opt/data/kanban.db")
DEFAULT_FALLBACK = Path("/opt/data/cron/evidence/kanban-tool-ledger-incidents.jsonl")
STALE_SECONDS = 150
TOOL_EVENT_TOTAL_LIMIT = 1_000_000
TOOL_EVENT_HOURLY_LIMIT = 100_000
FALLBACK_MAX_BYTES = 1_048_576
DEFAULT_COORDINATOR_NAME = "칸반 상태 총괄 집계"


def _timestamp(value: object) -> float | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _read_json(path: Path) -> object:
    if path.is_symlink() or not path.is_file():
        raise ValueError("unsafe or missing input")
    return json.loads(path.read_text(encoding="utf-8"))


def _jobs(value: object) -> list[dict]:
    rows = value.get("jobs", []) if isinstance(value, dict) else value
    return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []


def incidents(jobs_path: Path, watch_state: Path, audit: Path, *, now: float,
              coordinator_name: str = DEFAULT_COORDINATOR_NAME,
              coordinator_id: str | None = None, fallback: Path = DEFAULT_FALLBACK,
              db: Path = DEFAULT_DB) -> tuple[str, ...]:
    found: set[str] = set()
    try:
        jobs = _jobs(_read_json(jobs_path))
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
        return ("watchdog_config",)
    coordinators = [
        job for job in jobs
        if (job.get("id") == coordinator_id if coordinator_id else job.get("name") == coordinator_name)
    ]
    if len(coordinators) != 1 or not coordinators[0].get("enabled", True):
        found.add("cron_missing")
    else:
        job = coordinators[0]
        last = _timestamp(job.get("last_run_at"))
        if last is None or now - last > STALE_SECONDS:
            found.add("cron_stale")
        if job.get("last_status") not in {None, "ok", "success"}:
            found.add("cron_failed")
        if job.get("last_delivery_error"):
            found.add("delivery_failed")
    try:
        state = _read_json(watch_state)
        cursor = state.get("cursor") if isinstance(state, dict) else None
        if isinstance(cursor, bool) or not isinstance(cursor, int) or cursor < 0:
            found.add("watch_state")
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
        found.add("watch_state")
    try:
        if audit.is_symlink() or not audit.is_file() or now - audit.stat().st_mtime > STALE_SECONDS:
            found.add("audit_stale")
        else:
            last_line = audit.read_text(encoding="utf-8").splitlines()[-1]
            record = json.loads(last_line)
            if record.get("status") not in {"ok", "baseline"}:
                found.add("audit_error")
    except (OSError, UnicodeError, json.JSONDecodeError, IndexError):
        found.add("audit_error")
    try:
        if fallback.is_symlink():
            found.add("fallback_incident")
        elif fallback.is_file():
            stat = fallback.stat()
            if stat.st_size >= FALLBACK_MAX_BYTES:
                found.add("fallback_capacity")
            if now - stat.st_mtime > STALE_SECONDS:
                raise FileNotFoundError("no recent fallback incident")
            record = json.loads(fallback.read_text(encoding="utf-8").splitlines()[-1])
            created_at = record.get("created_at")
            if (
                record.get("kind") == "tool_ledger_write_failure"
                and isinstance(created_at, int) and not isinstance(created_at, bool)
                and 0 <= now - created_at <= STALE_SECONDS
            ):
                found.add("fallback_incident")
    except FileNotFoundError:
        pass
    except (OSError, UnicodeError, json.JSONDecodeError, IndexError):
        found.add("fallback_incident")
    try:
        if not db.exists():
            return tuple(sorted(found))
        if db.is_symlink() or not db.is_file():
            raise OSError("unsafe database")
        with closing(sqlite3.connect(f"file:{db.resolve()}?mode=ro", uri=True, timeout=2)) as conn:
            exists = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='task_events'"
            ).fetchone()
            if exists:
                total = int(conn.execute(
                    "SELECT COUNT(*) FROM task_events WHERE kind IN ('tool_started','tool_completed')"
                ).fetchone()[0])
                recent = int(conn.execute(
                    "SELECT COUNT(*) FROM task_events WHERE kind IN ('tool_started','tool_completed') AND created_at>=?",
                    (int(now) - 3600,),
                ).fetchone()[0])
                if total > TOOL_EVENT_TOTAL_LIMIT or recent > TOOL_EVENT_HOURLY_LIMIT:
                    found.add("tool_event_growth")
    except (OSError, sqlite3.Error):
        found.add("tool_event_measurement")
    return tuple(sorted(found))


def _save(path: Path, fingerprint: str) -> None:
    absolute = path.absolute()
    current = Path(absolute.parts[0])
    for part in absolute.parts[1:-1]:
        current /= part
        if current.is_symlink():
            raise OSError("unsafe state")
    if path.is_symlink():
        raise OSError("unsafe state")
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.parent.is_symlink():
        raise OSError("unsafe state")
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        data = (json.dumps({"version": 1, "incident": fingerprint}) + "\n").encode()
        offset = 0
        while offset < len(data):
            written = os.write(fd, data[offset:])
            if written <= 0:
                raise OSError("incomplete write")
            offset += written
        os.fsync(fd)
        os.fchmod(fd, 0o600)
        os.close(fd)
        fd = -1
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if fd >= 0:
            os.close(fd)
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def run(jobs: Path, watch_state: Path, audit: Path, guard_state: Path, *, now: float,
        coordinator_name: str = DEFAULT_COORDINATOR_NAME,
        coordinator_id: str | None = None, fallback: Path = DEFAULT_FALLBACK,
        db: Path = DEFAULT_DB, emit=None) -> str | None:
    current = incidents(
        jobs, watch_state, audit, now=now,
        coordinator_name=coordinator_name, coordinator_id=coordinator_id,
        fallback=fallback, db=db,
    )
    fingerprint = hashlib.sha256("\n".join(current).encode()).hexdigest()[:24] if current else ""
    previous = ""
    try:
        saved = _read_json(guard_state)
        if isinstance(saved, dict) and isinstance(saved.get("incident"), str):
            previous = saved["incident"]
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
        pass
    message = None
    if current and fingerprint != previous:
        message = "칸반 활동 감시가 정상적으로 확인되지 않습니다. 운영자가 감시 작업과 전달 상태를 확인해 주세요."
        if emit is not None:
            emit(message)
    _save(guard_state, fingerprint)
    return message


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--jobs", type=Path, default=DEFAULT_JOBS)
    parser.add_argument("--watch-state", type=Path, default=DEFAULT_WATCH_STATE)
    parser.add_argument("--audit", type=Path, default=DEFAULT_AUDIT)
    parser.add_argument("--state", type=Path, default=DEFAULT_GUARD_STATE)
    parser.add_argument("--coordinator-name", default=DEFAULT_COORDINATOR_NAME)
    parser.add_argument("--coordinator-id")
    parser.add_argument("--fallback", type=Path, default=DEFAULT_FALLBACK)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    args = parser.parse_args(argv)
    def emit(message: str) -> None:
        sys.stdout.write(message + "\n")
        sys.stdout.flush()
    message = run(args.jobs, args.watch_state, args.audit, args.state,
                  now=datetime.now(timezone.utc).timestamp(),
                  coordinator_name=args.coordinator_name,
                  coordinator_id=args.coordinator_id, fallback=args.fallback,
                  db=args.db, emit=emit)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
