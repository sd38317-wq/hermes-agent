#!/usr/bin/env python3
"""Update one existing Slack dashboard message, only when its content changes.

Examples::

    python scripts/ops/slack_dashboard_update.py
    python scripts/ops/slack_dashboard_update.py --smoke --db /opt/data/kanban.db

``--smoke`` is read-only: it neither writes state nor reads the Slack token nor
contacts Slack. Normal success has empty stdout; failure prints one safe Korean
line and exits nonzero.
"""
from __future__ import annotations

import argparse
import contextlib
import json
import os
import sqlite3
import stat
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable

try:
    from scripts.ops.kanban_exception_watch import (
        _columns, _read_db, _old_fingerprint, advisory_lock, fingerprint,
        save_state_atomic,
    )
except ImportError:  # direct execution outside the repository root
    from kanban_exception_watch import (  # type: ignore
        _columns, _read_db, _old_fingerprint, advisory_lock, fingerprint,
        save_state_atomic,
    )

DEFAULT_DB = Path("/opt/data/kanban.db")
DEFAULT_ENV_FILE = Path("/opt/data/.env")
DEFAULT_STATE = Path("/opt/data/cron/state/slack-dashboard-update.json")
DEFAULT_CHANNEL = "C0BPXD9TBB7"
DEFAULT_TS = "1786674259.552709"
PROFILES = ("dev", "productdev", "research", "plan", "design")


def _token_from_env_file(path: Path) -> str | None:
    """Read only SLACK_BOT_TOKEN from a securely owned dotenv-style file."""
    before = os.lstat(path)
    if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.geteuid()
            or stat.S_IMODE(before.st_mode) != 0o600):
        raise PermissionError("unsafe env file")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    try:
        after = os.fstat(fd)
        if ((before.st_dev, before.st_ino) != (after.st_dev, after.st_ino)
                or not stat.S_ISREG(after.st_mode) or after.st_uid != os.geteuid()
                or stat.S_IMODE(after.st_mode) != 0o600):
            raise PermissionError("unsafe env file")
        contents = os.read(fd, 1024 * 1024 + 1)
        if len(contents) > 1024 * 1024:
            raise ValueError("env file too large")
        text = contents.decode("utf-8")
    finally:
        os.close(fd)

    for line in text.splitlines():
        item = line.strip()
        if item.startswith("export "):
            item = item[7:].lstrip()
        key, separator, value = item.partition("=")
        if not separator or key.strip() != "SLACK_BOT_TOKEN":
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        return value or None
    return None


def _pid_alive(pid: object) -> bool:
    try:
        value = int(pid)  # type: ignore[arg-type]
        if value <= 0:
            return False
        os.kill(value, 0)
        return True
    except (TypeError, ValueError, ProcessLookupError):
        return False
    except PermissionError:
        return True


def _process_birth_identity(pid: object) -> str | None:
    """Return a stable Linux boot/process-start identity for a live PID."""
    try:
        value = int(pid)  # type: ignore[arg-type]
        if value <= 0:
            return None
        boot_id = Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip()
        stat = Path(f"/proc/{value}/stat").read_text(encoding="ascii")
        start_ticks = int(stat.rsplit(")", 1)[1].split()[19])
        return f"linux:{boot_id}:{start_ticks}"
    except (TypeError, ValueError, OSError, IndexError):
        return None


def _creation_matches(row: sqlite3.Row, columns: set[str]) -> bool:
    """Use Linux proc start identity when a compatible optional column exists."""
    pid = row["worker_pid"] if "worker_pid" in columns else None
    if "worker_birth_identity" in columns and row["worker_birth_identity"] is not None:
        return pid is not None and _process_birth_identity(pid) == str(row["worker_birth_identity"])
    expected_col = next((x for x in ("process_start_ticks", "worker_start_ticks") if x in columns), None)
    if not expected_col or row[expected_col] is None or pid is None:
        return True
    try:
        stat = Path(f"/proc/{int(pid)}/stat").read_text(encoding="ascii")
        return int(stat.rsplit(")", 1)[1].split()[19]) == int(row[expected_col])
    except (OSError, ValueError, IndexError):
        return False


def compute_dashboard(conn: sqlite3.Connection, now: int | None = None) -> dict[str, object]:
    now = int(time.time()) if now is None else now
    tc, rc = _columns(conn, "tasks"), _columns(conn, "task_runs")
    tasks = conn.execute("SELECT * FROM tasks ORDER BY id").fetchall() if {"id", "status"} <= tc else []
    runs: dict[str, list[sqlite3.Row]] = {}
    if {"task_id", "status"} <= rc:
        for row in conn.execute("SELECT * FROM task_runs WHERE status='running' ORDER BY id"):
            runs.setdefault(str(row["task_id"]), []).append(row)
    counts = {p: {"working": 0, "waiting": 0, "blocked": 0} for p in PROFILES}
    done = total = 0
    seen_tasks: set[str] = set()
    for task in tasks:
        task_id = str(task["id"])
        seen_tasks.add(task_id)
        active = runs.get(task_id, [])
        profile = str(task["assignee"] or "") if "assignee" in tc else ""
        if profile not in counts and active and "profile" in rc:
            profile = str(active[0]["profile"] or "")
        if profile not in counts:
            continue
        status = str(task["status"] or "")
        if status in {"done", "cancelled"}:
            done += status == "done"
            total += 1
            continue
        total += 1
        if status in {"blocked", "triage"}:
            bucket = "blocked"
        elif status == "running":
            run = active[0] if len(active) == 1 else None
            valid = run is not None
            if valid and "current_run_id" in tc and task["current_run_id"] is not None and "id" in rc:
                valid = int(task["current_run_id"]) == int(run["id"])
            if valid and "last_heartbeat_at" in rc and run["last_heartbeat_at"] is not None:
                valid = now - int(run["last_heartbeat_at"]) <= 3600
            pid_row, pid_cols = (run, rc) if run is not None and "worker_pid" in rc else (task, tc)
            if valid and "worker_pid" in pid_cols and pid_row["worker_pid"] is not None:
                valid = _pid_alive(pid_row["worker_pid"]) and _creation_matches(pid_row, pid_cols)
            bucket = "working" if valid else "blocked"
        elif active:
            bucket = "blocked"
        else:
            bucket = "waiting"
        counts[profile][bucket] += 1
    for task_id, orphan_runs in runs.items():
        if task_id in seen_tasks:
            continue
        for run in orphan_runs:
            profile = str(run["profile"] or "") if "profile" in rc else ""
            if profile in counts:
                counts[profile]["blocked"] += 1
                total += 1
    progress = 100 if total == 0 else (done * 100) // total
    return {"profiles": counts, "done": done, "total": total, "progress": progress}


def render(data: dict[str, object]) -> str:
    labels = {"working": "작업", "waiting": "대기", "blocked": "막힘"}
    profiles = data["profiles"]
    lines = [f"칸반 현황 · 전체 진행 {data['done']}/{data['total']} ({data['progress']}%)"]
    for profile in PROFILES:
        row = profiles[profile]  # type: ignore[index]
        lines.append(f"• {profile}: " + " · ".join(f"{labels[k]} {row[k]}" for k in labels))
    return "\n".join(lines)


def slack_sender(channel: str, ts: str, text: str, token: str, timeout: float) -> None:
    payload = json.dumps({"channel": channel, "ts": ts, "text": text}, separators=(",", ":")).encode()
    req = urllib.request.Request("https://slack.com/api/chat.update", data=payload,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json; charset=utf-8"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            result = json.loads(response.read(65536))
    except (OSError, ValueError, urllib.error.URLError):
        raise RuntimeError("Slack update failed") from None
    if not isinstance(result, dict) or result.get("ok") is not True:
        raise RuntimeError("Slack update rejected")


def main(argv: list[str] | None = None, *, sender: Callable = slack_sender,
         token_getter: Callable[[], str | None] = lambda: os.environ.get("SLACK_BOT_TOKEN")) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--db", type=Path, default=DEFAULT_DB)
    p.add_argument("--env-file", type=Path, default=DEFAULT_ENV_FILE)
    p.add_argument("--state", type=Path, default=DEFAULT_STATE)
    p.add_argument("--lock", type=Path)
    p.add_argument("--channel", default=DEFAULT_CHANNEL)
    p.add_argument("--ts", default=DEFAULT_TS)
    p.add_argument("--timeout", type=float, default=5.0)
    p.add_argument("--now", type=int)
    p.add_argument("--smoke", action="store_true")
    args = p.parse_args(argv)
    lock = args.lock or args.state.with_suffix(args.state.suffix + ".lock")
    try:
        if args.smoke:
            with contextlib.closing(_read_db(args.db, args.timeout)) as conn:
                data = compute_dashboard(conn, args.now)
            print(f"읽기 전용 점검 완료: 카드 {data['total']}건")
            return 0
        with advisory_lock(lock):
            with contextlib.closing(_read_db(args.db, args.timeout)) as conn:
                data = compute_dashboard(conn, args.now)
            text = render(data)
            current = fingerprint([{"dashboard": text}])  # type: ignore[list-item]
            if _old_fingerprint(args.state) == current:
                return 0
            token = token_getter() or _token_from_env_file(args.env_file)
            if not token:
                raise RuntimeError("missing token")
            sender(args.channel, args.ts, text, token, args.timeout)
            save_state_atomic(args.state, current)
            return 0
    except BlockingIOError:
        return 0
    except Exception:
        print("슬랙 대시보드를 업데이트하지 못했어요. 설정과 연결을 확인해 주세요.")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
