#!/usr/bin/env python3
"""Create or update one Slack dashboard message when its content changes.

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
import inspect
import json
import os
import sqlite3
import stat
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable

try:
    from scripts.ops.kanban_exception_watch import (
        _columns, _read_db, _old_fingerprint, advisory_lock, fingerprint,
    )
except ImportError:  # direct execution outside the repository root
    from kanban_exception_watch import (  # type: ignore
        _columns, _read_db, _old_fingerprint, advisory_lock, fingerprint,
    )

DEFAULT_DB = Path("/opt/data/kanban.db")
DEFAULT_ENV_FILE = Path("/opt/data/.env")
DEFAULT_STATE = Path("/opt/data/cron/state/slack-dashboard-update.json")
DEFAULT_CHANNEL = "C0BPXD9TBB7"
PROFILE_ROWS = (
    ("default", "Hermes", "🔵"),
    ("dev", "개발", "🟢"),
    ("research", "조사", "🟣"),
    ("plan", "기획문서", "🟠"),
    ("design", "디자인", "🔴"),
)
EXCLUDED_STATUSES = {"archived", "cancelled", "done", "completed", "triage", "internal"}
STATE_LABELS = {
    "running": "진행", "blocked": "막힘", "ready": "대기", "todo": "대기",
    "scheduled": "예약", "review": "검토",
}


def _public_title(task: sqlite3.Row, columns: set[str]) -> str | None:
    """Return a display-safe title; identifiers are never title fallbacks."""
    if "title" not in columns or not isinstance(task["title"], str):
        return None
    title = " ".join(task["title"].split()).strip()
    normalized = title.casefold().replace("-", " ").replace("_", " ")
    if not title or "자기개발" in title or "self development" in normalized:
        return None
    return title


def _evidence_percent(*, has_attachment: bool, metadata_fields: set[str]) -> int:
    """Award 25 points per explicit output fact; remaining work stays below 100."""
    score = 25 * (int(has_attachment) + len(metadata_fields))
    return min(score, 95)


def _structured_evidence(raw: object) -> set[str]:
    if not isinstance(raw, str) or not raw:
        return set()
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return set()
    if not isinstance(value, dict):
        return set()
    return {key for key in ("output", "verification", "delivery") if value.get(key)}


def _token_from_env_file(path: Path) -> str | None:
    """Read only SLACK_BOT_TOKEN from a securely owned dotenv-style file."""
    before = os.lstat(path)
    if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.geteuid()  # windows-footgun: ok - POSIX-only
            or stat.S_IMODE(before.st_mode) != 0o600):
        raise PermissionError("unsafe env file")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    try:
        after = os.fstat(fd)
        if ((before.st_dev, before.st_ino) != (after.st_dev, after.st_ino)
                or not stat.S_ISREG(after.st_mode) or after.st_uid != os.geteuid()  # windows-footgun: ok - POSIX-only
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
        os.kill(value, 0)  # windows-footgun: ok - POSIX-only script
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
    run_facts: dict[str, set[str]] = {}
    if {"task_id", "status"} <= rc:
        for row in conn.execute("SELECT * FROM task_runs"):
            task_id = str(row["task_id"])
            fields = _structured_evidence(row["metadata"]) if "metadata" in rc else set()
            run_facts[task_id] = run_facts.get(task_id, set()) | fields
    attachment_tasks: set[str] = set()
    ac = _columns(conn, "task_attachments")
    if "task_id" in ac:
        attachment_tasks = {str(row[0]) for row in
                            conn.execute("SELECT DISTINCT task_id FROM task_attachments")}
    lifecycle: dict[str, tuple[str, object]] = {}
    ec = _columns(conn, "task_events")
    if {"id", "task_id", "kind"} <= ec:
        payload_sql = "payload" if "payload" in ec else "NULL AS payload"
        for event in conn.execute(
            f"SELECT task_id, kind, {payload_sql} FROM task_events "
            "WHERE kind IN ("
            "'crashed','gave_up','coordination_required',"
            "'claimed','spawned','completed','unblocked','status'"
            ") "
            "ORDER BY id"
        ):
            lifecycle[str(event["task_id"])] = (str(event["kind"]), event["payload"])
    focus: dict[str, str] | None = None
    focus_key: tuple[int, int, str] | None = None
    profile_focus: dict[str, tuple[tuple[int, int, str], dict[str, str]]] = {}
    for task in tasks:
        task_id = str(task["id"])
        active = runs.get(task_id, [])
        status = str(task["status"] or "")
        title = _public_title(task, tc)
        if status in EXCLUDED_STATUSES or title is None:
            continue
        valid_running = False
        if status == "running":
            run = active[0] if len(active) == 1 else None
            valid_running = run is not None
            if valid_running and "current_run_id" in tc and task["current_run_id"] is not None and "id" in rc:
                valid_running = int(task["current_run_id"]) == int(run["id"])
            if valid_running and "last_heartbeat_at" in rc and run["last_heartbeat_at"] is not None:
                valid_running = now - int(run["last_heartbeat_at"]) <= 3600
            pid_row, pid_cols = (run, rc) if run is not None and "worker_pid" in rc else (task, tc)
            if valid_running and "worker_pid" in pid_cols and pid_row["worker_pid"] is not None:
                valid_running = _pid_alive(pid_row["worker_pid"]) and _creation_matches(pid_row, pid_cols)
        rank = 99
        label = ""
        if valid_running:
            rank, label = 0, "진행 중"
        elif status == "blocked":
            rank, label = 1, "막힘"
        elif status == "review":
            rank, label = 2, "검토"
        elif status in {"ready", "todo"}:
            rank, label = 3, "대기"
        try:
            priority = int(task["priority"] or 0) if "priority" in tc else 0
        except (TypeError, ValueError):
            priority = 0
        candidate_key = (rank, -priority, task_id)
        if rank < 99 and (focus_key is None or candidate_key < focus_key):
            focus_key = candidate_key
            focus = {"id": task_id, "title": title, "label": label}
        profile = str(task["assignee"] or "default") if "assignee" in tc else "default"
        if profile == "hermes":
            profile = "default"
        if rank < 99 and (profile not in profile_focus or candidate_key < profile_focus[profile][0]):
            profile_focus[profile] = (candidate_key, {
                "title": title,
                "state": (
                    "current" if valid_running else "blocked" if rank == 1
                    else "review" if status == "review" else "waiting"
                ),
            })
    rows = []
    for key, name, emoji in PROFILE_ROWS:
        item = profile_focus.get(key)
        rows.append({"name": name, "emoji": emoji,
                     "work": item[1] if item is not None else None})
    remaining = []
    for task in tasks:
        status = str(task["status"] or "")
        title = _public_title(task, tc)
        if status in EXCLUDED_STATUSES or title is None:
            continue
        task_id = str(task["id"])
        metadata_fields = run_facts.get(task_id, set())
        percent = _evidence_percent(
            has_attachment=task_id in attachment_tasks,
            metadata_fields=metadata_fields,
        )
        try:
            priority = int(task["priority"] or 0) if "priority" in tc else 0
        except (TypeError, ValueError):
            priority = 0
        remaining.append({
            "id": task_id,
            "title": title,
            "percent": percent,
            "priority": priority,
            "state_label": STATE_LABELS.get(status, "대기"),
        })
    remaining.sort(key=lambda item: (-int(item["priority"]), str(item["id"])))
    warnings: list[str] = []
    public_titles = {
        str(task["id"]): title
        for task in tasks
        if str(task["status"] or "") not in EXCLUDED_STATUSES
        if (title := _public_title(task, tc)) is not None
    }
    for task_id, (kind, raw_payload) in sorted(lifecycle.items()):
        title = public_titles.get(task_id)
        if title is None:
            continue
        if kind in {"crashed", "gave_up"}:
            warnings.append(f"⚠ 자동복구 확인: {title}")
        elif kind == "coordination_required":
            try:
                payload = json.loads(raw_payload) if isinstance(raw_payload, str) else {}
            except ValueError:
                payload = {}
            if not isinstance(payload, dict):
                payload = {}
            if "dependency_stall" in payload.get("kinds", []):
                warnings.append(f"⚠ 의존성 정체: {title}")
    profile_names = {key: name for key, name, _ in PROFILE_ROWS}
    queue_counts = {key: 0 for key in profile_names}
    for task in tasks:
        profile = str(task["assignee"] or "default") if "assignee" in tc else "default"
        if profile == "hermes":
            profile = "default"
        if profile in queue_counts and str(task["status"] or "") in {"ready", "todo", "review"}:
            queue_counts[profile] += 1
    busiest = max(queue_counts, key=lambda key: (queue_counts[key], key))
    if queue_counts[busiest] >= 3 and queue_counts[busiest] - min(queue_counts.values()) >= 2:
        warnings.append(
            f"⚠ 대기열 불균형: {profile_names[busiest]} {queue_counts[busiest]}건"
        )
    return {"focus": focus, "profiles": rows, "warnings": warnings,
            "remaining": remaining[:4],
            "total": len(tasks)}


def render(data: dict[str, object]) -> str:
    remaining = data.get("remaining")
    if not isinstance(remaining, list) or not remaining:
        return "현재 진행 중인 작업이 없어요."
    return "\n".join(_dashboard_lines(data))


def _profile_line(row: dict[str, object]) -> str:
    work = row.get("work")
    wording = "대기 중"
    if isinstance(work, dict):
        if work.get("state") == "current":
            wording = f"현재 작업: {work['title']}"
        elif work.get("state") == "blocked":
            wording = f"막힘: {work['title']}"
        elif work.get("state") == "review":
            wording = f"검토: {work['title']}"
        else:
            wording = f"대기 작업: {work['title']}"
    return f"{row['emoji']} {row['name']} · {wording}"


def _dashboard_lines(data: dict[str, object]) -> list[str]:
    lines = [_profile_line(row) for row in data.get("profiles", [])
             if isinstance(row, dict)]
    lines.extend(str(warning) for warning in data.get("warnings", []))
    lines.extend(
        f"{number}. [{item['state_label']}] {item['title']} · {item['percent']}%"
        for number, item in enumerate(data.get("remaining", []), 1)
        if isinstance(item, dict)
    )
    return lines


def render_blocks(text: str, data: dict[str, object] | None = None) -> list[dict[str, object]]:
    if data is not None and isinstance(data.get("profiles"), list):
        blocks: list[dict[str, object]] = [
            {"type": "header", "text": {"type": "plain_text", "text": "작업 현황"}},
        ]
        for row in data["profiles"]:
            if not isinstance(row, dict):
                continue
            blocks.append({"type": "section", "text": {"type": "mrkdwn",
                           "text": _profile_line(row)}})
        for warning in data.get("warnings", []):
            blocks.append({"type": "section", "text": {"type": "mrkdwn",
                           "text": str(warning)}})
        remaining = data.get("remaining")
        if isinstance(remaining, list):
            blocks.append({"type": "divider"})
            for number, item in enumerate(remaining, 1):
                if isinstance(item, dict):
                    blocks.append({"type": "section", "text": {"type": "mrkdwn",
                                   "text": f"{number}. [{item['state_label']}] "
                                           f"{item['title']} · {item['percent']}%"}})
        return blocks
    return [{"type": "section", "text": {"type": "mrkdwn", "text": text}}]


def slack_sender(channel: str, ts: str, text: str, token: str, timeout: float, *,
                 blocks: list[dict[str, object]] | None = None) -> None:
    payload = json.dumps({"channel": channel, "ts": ts, "text": text,
                          "blocks": blocks or render_blocks(text)}, separators=(",", ":")).encode()
    req = urllib.request.Request("https://slack.com/api/chat.update", data=payload,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json; charset=utf-8"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            result = json.loads(response.read(65536))
    except (OSError, ValueError, urllib.error.URLError):
        raise RuntimeError("Slack update failed") from None
    if not isinstance(result, dict) or result.get("ok") is not True:
        raise RuntimeError("Slack update rejected")


def _slack_call(method: str, payload: dict[str, object], token: str, timeout: float) -> dict[str, object]:
    body = json.dumps(payload, separators=(",", ":")).encode()
    req = urllib.request.Request(f"https://slack.com/api/{method}", data=body,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json; charset=utf-8"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            result = json.loads(response.read(65536))
    except (OSError, ValueError, urllib.error.URLError):
        raise RuntimeError("Slack request failed") from None
    if not isinstance(result, dict) or result.get("ok") is not True:
        raise RuntimeError("Slack request rejected")
    return result


def slack_message_exists(channel: str, ts: str, token: str, timeout: float) -> bool:
    result = _slack_call("conversations.history", {
        "channel": channel, "oldest": ts, "latest": ts, "inclusive": True, "limit": 1,
    }, token, timeout)
    messages = result.get("messages")
    return isinstance(messages, list) and any(
        isinstance(message, dict) and str(message.get("ts")) == ts for message in messages)


def slack_poster(channel: str, text: str, token: str, timeout: float, *,
                 blocks: list[dict[str, object]] | None = None) -> str:
    result = _slack_call("chat.postMessage", {"channel": channel, "text": text,
                         "blocks": blocks or render_blocks(text)}, token, timeout)
    ts = result.get("ts")
    if not isinstance(ts, str) or not ts:
        raise RuntimeError("Slack post omitted timestamp")
    return ts


def _load_state(path: Path) -> dict[str, str]:
    _old_fingerprint(path)  # preserves the shared symlink rejection contract
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return {key: str(value[key]) for key in ("fingerprint", "ts")
                if isinstance(value, dict) and isinstance(value.get(key), str)}
    except (OSError, ValueError):
        return {}


def _save_state(path: Path, current: str, ts: str) -> None:
    _old_fingerprint(path)  # rejects a symlink target
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.parent.is_symlink():
        raise ValueError("unsafe state directory")
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump({"fingerprint": current, "ts": ts}, fh, sort_keys=True, separators=(",", ":"))
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


def _send_with_optional_blocks(call: Callable, args: tuple[object, ...],
                               blocks: list[dict[str, object]]):
    """Pass blocks when supported while preserving legacy injected callables."""
    try:
        inspect.signature(call).bind(*args, blocks=blocks)
    except (TypeError, ValueError):
        return call(*args)
    return call(*args, blocks=blocks)


def main(argv: list[str] | None = None, *, sender: Callable = slack_sender,
         verifier: Callable | None = None, poster: Callable | None = None,
         token_getter: Callable[[], str | None] = lambda: os.environ.get("SLACK_BOT_TOKEN")) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--db", type=Path, default=DEFAULT_DB)
    p.add_argument("--env-file", type=Path, default=DEFAULT_ENV_FILE)
    p.add_argument("--state", type=Path, default=DEFAULT_STATE)
    p.add_argument("--lock", type=Path)
    p.add_argument("--channel", default=DEFAULT_CHANNEL)
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
            blocks = render_blocks(text, data)
            current = fingerprint([{"dashboard": text, "blocks": blocks}])  # type: ignore[list-item]
            token = token_getter() or _token_from_env_file(args.env_file)
            if not token:
                raise RuntimeError("missing token")
            state = _load_state(args.state)
            check = verifier or (slack_message_exists if sender is slack_sender else lambda *unused: True)
            post = poster or slack_poster
            ts = state.get("ts")
            if ts is None:
                ts = _send_with_optional_blocks(
                    post, (args.channel, text, token, args.timeout), blocks)
                _save_state(args.state, current, ts)
                return 0
            if not check(args.channel, ts, token, args.timeout):
                ts = _send_with_optional_blocks(
                    post, (args.channel, text, token, args.timeout), blocks)
                _save_state(args.state, current, ts)
                return 0
            if state.get("fingerprint") == current:
                return 0
            _send_with_optional_blocks(
                sender, (args.channel, ts, text, token, args.timeout), blocks)
            _save_state(args.state, current, ts)
            return 0
    except BlockingIOError:
        return 0
    except Exception:
        print("슬랙 대시보드를 업데이트하지 못했어요. 설정과 연결을 확인해 주세요.")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
