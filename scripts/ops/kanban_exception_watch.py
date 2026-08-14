#!/usr/bin/env python3
"""Quiet, read-only kanban exception watcher.

Examples::

    python scripts/ops/kanban_exception_watch.py
    python scripts/ops/kanban_exception_watch.py --smoke --db /opt/data/kanban.db

Normal success is deliberately silent. ``--smoke`` inspects read-only and
prints one Korean summary without acquiring a lock or writing state.
"""
from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import json
import os
import sqlite3
import tempfile
import time
from pathlib import Path
from urllib.parse import quote

DEFAULT_DB = Path("/opt/data/kanban.db")
DEFAULT_STATE = Path("/opt/data/cron/state/kanban-exception-watch.json")


def _reject_symlink(path: Path, label: str) -> None:
    if path.is_symlink():
        raise ValueError(f"{label} 경로는 심볼릭 링크일 수 없습니다")


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
        return set()
    return {str(row[1]) for row in conn.execute(f'PRAGMA table_info("{table}")')}


def _read_db(path: Path, timeout: float = 2.0) -> sqlite3.Connection:
    _reject_symlink(path, "DB")
    if not path.is_file():
        raise ValueError("DB 파일을 찾을 수 없습니다")
    uri = f"file:{quote(str(path.resolve()))}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=timeout)
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout={max(1, int(timeout * 1000))}")
    return conn


def collect_exceptions(conn: sqlite3.Connection, now: int | None = None) -> list[dict[str, str]]:
    """Return sorted, content-free canonical exception records."""
    now = int(time.time()) if now is None else now
    tc = _columns(conn, "tasks")
    if not {"id", "status"} <= tc:
        return []
    task_rows = conn.execute("SELECT * FROM tasks ORDER BY id").fetchall()
    tasks = {str(r["id"]): r for r in task_rows}
    found: set[tuple[str, str]] = set()
    for task_id, row in tasks.items():
        status = str(row["status"] or "")
        if status in {"blocked", "triage"}:
            kind = str(row["block_kind"] or "") if "block_kind" in tc else ""
            if kind != "dependency":
                found.add(("human", task_id))
        for due_col in ("due_at", "deadline_at", "scheduled_at", "scheduled_for",
                        "execute_at", "run_at", "promised_at", "promised_execution_at"):
            if due_col in tc and row[due_col] is not None and int(row[due_col]) < now and status not in {"done", "cancelled"}:
                found.add(("overdue", task_id))
                break

    rc = _columns(conn, "task_runs")
    active: dict[str, list[sqlite3.Row]] = {}
    if {"id", "task_id", "status"} <= rc:
        for run in conn.execute("SELECT * FROM task_runs WHERE status='running' ORDER BY id"):
            active.setdefault(str(run["task_id"]), []).append(run)
            task = tasks.get(str(run["task_id"]))
            if task is None or str(task["status"] or "") != "running":
                found.add(("run_card", str(run["task_id"])))
            elif "current_run_id" in tc and task["current_run_id"] is not None and int(task["current_run_id"]) != int(run["id"]):
                found.add(("run_card", str(run["task_id"])))
            elif "worker_pid" in tc and "worker_pid" in rc and task["worker_pid"] is not None \
                    and run["worker_pid"] is not None and int(task["worker_pid"]) != int(run["worker_pid"]):
                found.add(("run_card", str(run["task_id"])))
            hb = run["last_heartbeat_at"] if "last_heartbeat_at" in rc else None
            if hb is not None and now - int(hb) > 3600:
                found.add(("heartbeat", str(run["task_id"])))
        for task_id, task in tasks.items():
            if str(task["status"] or "") == "running" and len(active.get(task_id, [])) != 1:
                found.add(("card_run", task_id))
            if active.get(task_id) and "last_heartbeat_at" in tc and "last_heartbeat_at" in rc:
                thb, rhb = task["last_heartbeat_at"], active[task_id][0]["last_heartbeat_at"]
                if thb is not None and rhb is not None and int(thb) != int(rhb):
                    found.add(("heartbeat", task_id))
    return [{"kind": kind, "task": task} for kind, task in sorted(found)]


def fingerprint(exceptions: list[dict[str, str]]) -> str:
    raw = json.dumps(exceptions, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()


def save_state_atomic(path: Path, value: str) -> None:
    _reject_symlink(path, "상태")
    path.parent.mkdir(parents=True, exist_ok=True)
    _reject_symlink(path.parent, "상태 디렉터리")
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump({"fingerprint": value}, fh, sort_keys=True, separators=(",", ":"))
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(name, path)
        dfd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(name)


def _old_fingerprint(path: Path) -> str | None:
    _reject_symlink(path, "상태")
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data.get("fingerprint") if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


@contextlib.contextmanager
def advisory_lock(path: Path):
    _reject_symlink(path, "잠금")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    fh = os.fdopen(fd, "a+")
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        fh.close()


def _message(items: list[dict[str, str]]) -> str:
    kinds = {"human": "사람 확인", "overdue": "기한 초과", "run_card": "실행 불일치", "card_run": "실행 누락", "heartbeat": "하트비트 이상"}
    parts = [f"{kinds[k]} {sum(x['kind'] == k for x in items)}건" for k in kinds if any(x["kind"] == k for x in items)]
    ids = ", ".join("".join(c if c.isalnum() or c in "-_." else "?" for c in x["task"])[:32] for x in items[:5])
    return f"칸반 확인이 필요해요: {', '.join(parts)} (카드: {ids})"


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--db", type=Path, default=DEFAULT_DB)
    p.add_argument("--state", type=Path, default=DEFAULT_STATE)
    p.add_argument("--lock", type=Path)
    p.add_argument("--timeout", type=float, default=2.0)
    p.add_argument("--now", type=int)
    p.add_argument("--smoke", action="store_true")
    args = p.parse_args(argv)
    lock = args.lock or args.state.with_suffix(args.state.suffix + ".lock")
    try:
        if args.smoke:
            with contextlib.closing(_read_db(args.db, args.timeout)) as conn:
                items = collect_exceptions(conn, args.now)
            print(f"읽기 전용 점검 완료: 예외 {len(items)}건")
            return 0
        with advisory_lock(lock):
            with contextlib.closing(_read_db(args.db, args.timeout)) as conn:
                items = collect_exceptions(conn, args.now)
            current = fingerprint(items)
            old = _old_fingerprint(args.state)
            if old == current:
                return 0
            save_state_atomic(args.state, current)
            if items:
                print(_message(items))
            return 0
    except BlockingIOError:
        return 0
    except Exception:
        print("칸반 점검을 완료하지 못했어요. 경로와 권한을 확인해 주세요.")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
