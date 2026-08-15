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
MANAGED_PROFILES = frozenset({"dev", "productdev", "research", "plan", "design"})
READY_STALE_SECONDS = 120
ORCHESTRATION_EVENT_KINDS = frozenset(
    {
        "claimed", "spawned", "completed", "review_requested", "blocked",
        "crashed", "gave_up",
    }
)


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


def _process_start_ticks(pid: object) -> int | None:
    try:
        value = int(pid)  # type: ignore[arg-type]
        stat = Path(f"/proc/{value}/stat").read_text(encoding="ascii")
        return int(stat.rsplit(")", 1)[1].split()[19])
    except (TypeError, ValueError, OSError, IndexError):
        return None


def _process_birth_identity(pid: object) -> str | None:
    """Return the Linux boot/process-start identity used by current schemas."""
    try:
        boot_id = Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip()
        start_ticks = _process_start_ticks(pid)
        return f"linux:{boot_id}:{start_ticks}" if start_ticks is not None else None
    except OSError:
        return None


def _worker_matches(row: sqlite3.Row, columns: set[str]) -> bool:
    """Check liveness and any creation identity available in this schema."""
    if "worker_pid" not in columns or row["worker_pid"] is None:
        return True
    pid = row["worker_pid"]
    if not _pid_alive(pid):
        return False
    if "worker_birth_identity" in columns and row["worker_birth_identity"] is not None:
        return _process_birth_identity(pid) == str(row["worker_birth_identity"])
    expected_col = next(
        (name for name in ("process_start_ticks", "worker_start_ticks")
         if name in columns and row[name] is not None),
        None,
    )
    if expected_col is None:
        return True
    actual = _process_start_ticks(pid)
    try:
        return actual is not None and actual == int(row[expected_col])
    except (TypeError, ValueError):
        return False


def collect_exceptions(conn: sqlite3.Connection, now: int | None = None) -> list[dict[str, str]]:
    """Return sorted, content-free canonical exception records."""
    now = int(time.time()) if now is None else now
    tc = _columns(conn, "tasks")
    if not {"id", "status"} <= tc:
        return []
    task_rows = conn.execute("SELECT * FROM tasks ORDER BY id").fetchall()
    tasks = {str(r["id"]): r for r in task_rows}
    found: set[tuple[str, str]] = set()
    managed_ids = {
        task_id for task_id, row in tasks.items()
        if "assignee" in tc and str(row["assignee"] or "") in MANAGED_PROFILES
    }
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
            if not _worker_matches(run, rc) or (task is not None and not _worker_matches(task, tc)):
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

    # Read-only safety net: only signal; never claim, promote, or bypass gates.
    event_columns = _columns(conn, "task_events")
    ready_since: dict[str, int] = {}
    if {"task_id", "kind", "created_at"} <= event_columns:
        for row in conn.execute(
            "SELECT task_id, MAX(created_at) AS changed_at FROM task_events "
            "WHERE kind IN ('created','promoted','unblocked','status') "
            "GROUP BY task_id"
        ):
            if row["changed_at"] is not None:
                ready_since[str(row["task_id"])] = int(row["changed_at"])

    actionable: set[str] = set()
    link_columns = _columns(conn, "task_links")
    for task_id in managed_ids:
        task = tasks[task_id]
        status = str(task["status"] or "")
        if status == "ready":
            actionable.add(task_id)
            created = int(task["created_at"] or now) if "created_at" in tc else now
            since = ready_since.get(task_id, created)
            if now - since >= READY_STALE_SECONDS and not active.get(task_id):
                found.add(("ready_stale", task_id))
        elif status == "todo" and {"parent_id", "child_id"} <= link_columns:
            parents = conn.execute(
                "SELECT parent_id FROM task_links WHERE child_id = ?", (task_id,)
            ).fetchall()
            known_parents = [
                tasks.get(str(parent["parent_id"])) for parent in parents
            ]
            parent_statuses = [
                str(parent["status"] or "")
                for parent in known_parents
                if parent is not None
            ]
            if not parents or (
                len(parent_statuses) == len(known_parents)
                and all(status in {"done", "archived"} for status in parent_statuses)
            ):
                actionable.add(task_id)
                found.add(("promotion_drift", task_id))
            elif parents:
                created = int(task["created_at"] or now) if "created_at" in tc else now
                since = ready_since.get(task_id, created)
                unfinished_ids = [
                    str(parent["parent_id"])
                    for parent, parent_row in zip(parents, known_parents)
                    if parent_row is None
                    or str(parent_row["status"] or "") not in {"done", "archived"}
                ]
                all_parents_working = bool(unfinished_ids) and all(
                    parent_id in tasks
                    and str(tasks[parent_id]["status"] or "") == "running"
                    and any(
                        _worker_matches(run, rc)
                        and _worker_matches(tasks[parent_id], tc)
                        for run in active.get(parent_id, [])
                    )
                    for parent_id in unfinished_ids
                )
                if now - since >= READY_STALE_SECONDS and not all_parents_working:
                    # Signal only.  The coordinator decides whether the edge is
                    # still valid; this watcher never removes a dependency or
                    # promotes the child itself.
                    found.add(("dependency_stall", task_id))

    managed_running = any(
        str(tasks[task_id]["status"] or "") == "running"
        and any(
            _worker_matches(run, rc) and _worker_matches(tasks[task_id], tc)
            for run in active.get(task_id, [])
        )
        for task_id in managed_ids
    )
    if actionable and not managed_running:
        found.add(("fleet_idle", min(actionable)))

    sub_columns = _columns(conn, "kanban_notify_subs")
    if (
        {"id", "task_id", "kind"} <= event_columns
        and {"task_id", "last_event_id", "delivery_mode"} <= sub_columns
    ):
        ack_column = (
            "s.delivered_event_id"
            if "delivered_event_id" in sub_columns
            else "s.last_event_id"
        )
        placeholders = ",".join("?" for _ in ORCHESTRATION_EVENT_KINDS)
        for row in conn.execute(
            f"SELECT e.task_id FROM task_events e "
            "JOIN kanban_notify_subs s ON s.task_id = e.task_id "
            f"WHERE e.kind IN ({placeholders}) AND s.delivery_mode = 'wake' "
            f"AND e.id > COALESCE({ack_column}, 0) ORDER BY e.task_id",
            tuple(sorted(ORCHESTRATION_EVENT_KINDS)),
        ):
            task_id = str(row["task_id"])
            if task_id in managed_ids:
                found.add(("orchestrator_report_missing", task_id))
    return [{"kind": kind, "task": task} for kind, task in sorted(found)]


def fingerprint(exceptions: list[dict[str, str]]) -> str:
    raw = json.dumps(exceptions, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()


def save_state_atomic(path: Path, value: str | set[str]) -> None:
    _reject_symlink(path, "상태")
    path.parent.mkdir(parents=True, exist_ok=True)
    _reject_symlink(path.parent, "상태 디렉터리")
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            payload = (
                {"fingerprints": sorted(value)}
                if isinstance(value, set)
                else {"fingerprint": value}
            )
            json.dump(payload, fh, sort_keys=True, separators=(",", ":"))
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


def _exception_fingerprints(items: list[dict[str, str]]) -> dict[str, dict[str, str]]:
    """Index each active condition independently for stable deduplication."""
    return {fingerprint([item]): item for item in items}


def emit_coordination_events(
    db_path: Path,
    items: list[dict[str, str]],
    *,
    now: int,
    timeout: float = 2.0,
) -> None:
    """Append deduplicated internal wake events without changing task state."""
    if not items:
        return
    _reject_symlink(db_path, "DB")
    grouped: dict[str, set[str]] = {}
    for item in items:
        grouped.setdefault(item["task"], set()).add(item["kind"])
    conn = sqlite3.connect(db_path, timeout=timeout)
    try:
        conn.execute(f"PRAGMA busy_timeout={max(1, int(timeout * 1000))}")
        if not conn.execute(
            "SELECT 1 FROM sqlite_master "
            "WHERE type='table' AND name='task_events'"
        ).fetchone():
            return
        conn.execute("BEGIN IMMEDIATE")
        for task_id, kinds in sorted(grouped.items()):
            canonical = [{"kind": kind, "task": task_id} for kind in sorted(kinds)]
            payload = json.dumps(
                {"kinds": sorted(kinds), "signal_id": fingerprint(canonical)},
                sort_keys=True,
                separators=(",", ":"),
            )
            exists = conn.execute(
                "SELECT 1 FROM task_events signal "
                "WHERE signal.task_id = ? "
                "AND signal.kind = 'coordination_required' "
                "AND signal.payload = ? "
                "AND NOT EXISTS ("
                "  SELECT 1 FROM task_events later "
                "  WHERE later.id > signal.id "
                "  AND later.task_id = signal.task_id "
                "  AND later.kind != 'coordination_required'"
                ") LIMIT 1",
                (task_id, payload),
            ).fetchone()
            if exists is None:
                inserted = conn.execute(
                    "INSERT INTO task_events (task_id, kind, payload, created_at) "
                    "SELECT id, 'coordination_required', ?, ? FROM tasks WHERE id = ?",
                    (payload, int(now), task_id),
                )
                if inserted.rowcount != 1:
                    raise RuntimeError(
                        f"coordination signal target disappeared: {task_id}"
                    )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _old_exception_fingerprints(path: Path) -> set[str]:
    _reject_symlink(path, "상태")
    if not path.exists():
        return set()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        values = data.get("fingerprints") if isinstance(data, dict) else None
        if isinstance(values, list):
            return {str(value) for value in values}
    except (OSError, ValueError):
        pass
    # A legacy whole-set fingerprint cannot identify individual conditions;
    # begin the new state format on this tick rather than carrying ambiguity.
    return set()


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
    kinds = {
        "human": "사람 확인", "overdue": "기한 초과",
        "run_card": "실행 불일치", "card_run": "실행 누락",
        "heartbeat": "하트비트 이상", "ready_stale": "준비 작업 지연",
        "promotion_drift": "승격 누락", "fleet_idle": "전체 유휴",
        "dependency_stall": "의존성 정체",
        "orchestrator_report_missing": "총괄 보고 누락",
    }
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
            current = _exception_fingerprints(items)
            old = _old_exception_fingerprints(args.state)
            if old == set(current):
                return 0
            new_items = [item for key, item in current.items() if key not in old]
            emit_coordination_events(
                args.db,
                new_items,
                now=args.now if args.now is not None else int(time.time()),
                timeout=args.timeout,
            )
            save_state_atomic(args.state, set(current))
            if new_items:
                print(_message(new_items))
            return 0
    except BlockingIOError:
        return 0
    except Exception:
        print("칸반 점검을 완료하지 못했어요. 경로와 권한을 확인해 주세요.")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
