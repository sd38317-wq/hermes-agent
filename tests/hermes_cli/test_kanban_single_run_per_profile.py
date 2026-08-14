"""Regression tests: one live run per profile, and per-run worker reaping.

Incident: the dispatcher claimed a second card for a profile that already had
a healthy worker in flight. Two ``hermes -p <profile>`` processes then shared
one ``HERMES_HOME`` (one state.db, one browser profile, one auth session) and
corrupted each other's run.

Four invariants are covered here:

1. A profile with a *live* run — ``tasks.current_run_id`` pointing at an OPEN
   ``task_runs`` row, a recent heartbeat, and a genuinely live worker PID —
   never gets a second card claimed. The three-part liveness contract matters:
   a dead PID, a stale heartbeat, or a leaked/closed run pointer must NOT wedge
   the profile, otherwise the guard becomes a permanent deadlock.
2. Priority ``-100`` self-development work is deferred while normal work for
   the same profile is ready or running.
3. A ``done``/``blocked`` transition reaps that run's worker process tree —
   and only that tree. Browser / auth sessions living in their own session
   (their own pgid) are never signalled.
4. Finishing a run is atomic: the run row is closed and
   ``tasks.current_run_id`` is cleared together, so neither half can leak and
   fool the guard in (1).

Plus concurrent-claim races: parallel ``claim_task`` and parallel
``dispatch_once`` must never hand the same profile two live runs.
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import tempfile
import threading
import time

import pytest


LIVE_CHILD = (
    "import subprocess, sys, time; "
    "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)']); "
    "time.sleep(120)"
)

# A worker that ignores SIGTERM — the shape seen in the field, where a run's
# process survived TERM and only went away on a process-group SIGKILL.
STUBBORN_CHILD = (
    "import signal, subprocess, sys, time; "
    "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
    "subprocess.Popen([sys.executable, '-c', "
    "'import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
    "time.sleep(120)']); "
    "time.sleep(120)"
)

# The leader honors SIGTERM, but its child does not. Group reaping must not
# mistake the leader's prompt exit for completion while the group lives on.
TERM_EXITING_LEADER = (
    "import subprocess, sys, time; "
    "subprocess.Popen([sys.executable, '-c', "
    "'import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
    "time.sleep(120)']); "
    "time.sleep(120)"
)


@pytest.fixture()
def kb(monkeypatch):
    """Fresh HERMES_HOME with a kanban DB and alpha/beta profiles on disk."""
    test_home = tempfile.mkdtemp(prefix="kanban_single_run_test_")
    for prof in ("alpha", "beta", "default"):
        os.makedirs(os.path.join(test_home, "profiles", prof), exist_ok=True)
    monkeypatch.setenv("HERMES_HOME", test_home)
    from hermes_cli import kanban_db

    with kanban_db.connect_closing() as conn:
        kanban_db.create_board(slug="default", name="Test")
        conn.commit()
    yield kanban_db


@pytest.fixture()
def procs():
    """Track spawned helper processes and always tear them down."""
    spawned: list[subprocess.Popen] = []
    yield spawned
    for p in spawned:
        # Only ever signal a group we own outright — one of these helpers
        # deliberately shares the test runner's process group.
        try:
            if os.getpgid(p.pid) == p.pid:
                os.killpg(p.pid, signal.SIGKILL)
            else:
                p.kill()
        except (ProcessLookupError, PermissionError, OSError):
            pass
        try:
            p.wait(timeout=5)
        except Exception:
            pass


def _spawn_live_session(procs, *, stubborn: bool = False) -> subprocess.Popen:
    """Start a session leader (pid == pgid == sid) that forks one child.

    ``stubborn=True`` makes the whole tree ignore SIGTERM, so the reap has to
    escalate to a group SIGKILL to finish the job.
    """
    proc = subprocess.Popen(
        [sys.executable, "-c", STUBBORN_CHILD if stubborn else LIVE_CHILD],
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    procs.append(proc)
    # Give the child a moment to fork its grandchild.
    time.sleep(0.5)
    return proc


def _dead_pid() -> int:
    """Return a PID that has certainly exited and been reaped."""
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


def _make_running(kb, conn, task_id, *, pid, heartbeat_age=0):
    """Claim ``task_id`` and pin its worker pid + heartbeat age."""
    claimed = kb.claim_task(conn, task_id)
    assert claimed is not None
    if pid is not None:
        kb._set_worker_pid(conn, task_id, pid)
    hb = int(time.time()) - heartbeat_age
    with kb.write_txn(conn):
        conn.execute(
            "UPDATE tasks SET last_heartbeat_at = ? WHERE id = ?", (hb, task_id)
        )
    return claimed


def _seed_running_overlap(kb, conn, task_id, *, pid):
    """Seed a historical impossible overlap without using the public API."""
    claimed = kb._claim_task_lock_held(conn, task_id)
    assert claimed is not None
    kb._set_worker_pid(conn, task_id, pid)
    return claimed


def _spawn_stub(pid_value):
    def _spawn(task, workspace, *, board=None):
        return pid_value

    return _spawn


# ---------------------------------------------------------------------------
# 1. one live run per profile
# ---------------------------------------------------------------------------


def test_live_run_blocks_second_claim_for_same_profile(kb, procs):
    """A profile with an open run + fresh heartbeat + live PID gets no 2nd card."""
    live = _spawn_live_session(procs)
    with kb.connect_closing() as conn:
        busy = kb.create_task(conn, title="in flight", assignee="alpha")
        nxt = kb.create_task(conn, title="queued", assignee="alpha")
        _make_running(kb, conn, busy, pid=live.pid)

    with kb.connect_closing() as conn:
        res = kb.dispatch_once(conn, spawn_fn=_spawn_stub(None))

    assert [s[0] for s in res.spawned] == []
    assert [(t, a) for t, a, _ in res.skipped_profile_busy] == [(nxt, "alpha")]
    with kb.connect_closing() as conn:
        assert kb.get_task(conn, nxt).status == "ready"


def test_fresh_pidless_claim_blocks_second_claim_on_same_board(kb):
    """The claim owns the slot before the dispatcher records its worker PID."""
    with kb.connect_closing() as conn:
        busy = kb.create_task(conn, title="claimed, spawn pending", assignee="alpha")
        nxt = kb.create_task(conn, title="queued", assignee="alpha")
        _make_running(kb, conn, busy, pid=None)

    with kb.connect_closing() as conn:
        res = kb.dispatch_once(conn, spawn_fn=_spawn_stub(None))

    assert res.spawned == []
    assert [task_id for task_id, _, _ in res.skipped_profile_busy] == [nxt]
    with kb.connect_closing() as conn:
        assert kb.get_task(conn, nxt).status == "ready"


def test_fresh_pidless_claim_blocks_second_claim_across_boards(kb):
    """The pre-PID claim window occupies the profile's global board slot."""
    kb.create_board(slug="other", name="Other")
    with kb.connect_closing(board="other") as conn:
        busy = kb.create_task(conn, title="claimed elsewhere", assignee="alpha")
        _make_running(kb, conn, busy, pid=None)
    with kb.connect_closing(board="default") as conn:
        nxt = kb.create_task(conn, title="queued here", assignee="alpha")

    with kb.connect_closing(board="default") as conn:
        res = kb.dispatch_once(
            conn, board="default", spawn_fn=_spawn_stub(None),
        )

    assert res.spawned == []
    assert [task_id for task_id, _, _ in res.skipped_profile_busy] == [nxt]
    with kb.connect_closing(board="default") as conn:
        assert kb.get_task(conn, nxt).status == "ready"


@pytest.mark.parametrize("claim_api,source_status", [
    ("claim_task", "ready"),
    ("claim_review_task", "review"),
])
def test_direct_claim_honors_live_run_in_unregistered_custom_db(
    kb, tmp_path, claim_api, source_status,
):
    """The supplied connection is part of the global profile scan.

    A caller may connect to an explicit DB path that has never been added to
    the board registry.  Both implementation and review claims must still see
    a live run in that DB and refuse a second claim for the same profile.
    """
    custom_db = tmp_path / "private" / "custom.db"
    with kb.connect_closing(db_path=custom_db) as conn:
        first = kb.create_task(conn, title="already claimed", assignee="alpha")
        contender = kb.create_task(conn, title="contender", assignee="alpha")
        if source_status == "review":
            with kb.write_txn(conn):
                conn.execute(
                    "UPDATE tasks SET status = 'review' WHERE id IN (?, ?)",
                    (first, contender),
                )
        assert getattr(kb, claim_api)(conn, first) is not None
        assert getattr(kb, claim_api)(conn, contender) is None
        assert kb.get_task(conn, contender).status == source_status


def test_live_run_does_not_block_other_profiles(kb, procs):
    """The guard is per profile — beta still dispatches while alpha is busy."""
    live = _spawn_live_session(procs)
    with kb.connect_closing() as conn:
        busy = kb.create_task(conn, title="in flight", assignee="alpha")
        other = kb.create_task(conn, title="beta work", assignee="beta")
        _make_running(kb, conn, busy, pid=live.pid)

    with kb.connect_closing() as conn:
        res = kb.dispatch_once(conn, spawn_fn=_spawn_stub(None))

    assert [s[0] for s in res.spawned] == [other]


def test_one_tick_spawns_at_most_one_card_per_profile(kb, procs):
    """Two ready cards for one profile: exactly one spawns in a single tick."""
    live = _spawn_live_session(procs)
    with kb.connect_closing() as conn:
        first = kb.create_task(conn, title="a", assignee="alpha", priority=10)
        second = kb.create_task(conn, title="b", assignee="alpha", priority=5)

    with kb.connect_closing() as conn:
        res = kb.dispatch_once(conn, spawn_fn=_spawn_stub(live.pid))

    assert [s[0] for s in res.spawned] == [first]
    assert [(t, a) for t, a, _ in res.skipped_profile_busy] == [(second, "alpha")]


def test_explicit_per_profile_cap_raises_the_live_run_budget(kb, procs):
    """One live run is the DEFAULT, not a ceiling.

    ``kanban.max_in_progress_per_profile`` is a documented opt-in to fan-out;
    an operator who set it to 2 asked for 2. The bug was the default being
    unbounded, so the knob keeps meaning what it says.
    """
    live = _spawn_live_session(procs)
    with kb.connect_closing() as conn:
        ids = [
            kb.create_task(conn, title=f"a{i}", assignee="alpha", priority=10 - i)
            for i in range(3)
        ]

    with kb.connect_closing() as conn:
        res = kb.dispatch_once(
            conn, spawn_fn=_spawn_stub(live.pid), max_in_progress_per_profile=2,
        )

    assert [s[0] for s in res.spawned] == ids[:2]
    # The knob is what's limiting here, so the operator-facing bucket is the
    # explicit cap, not the implicit single-run budget.
    assert [t for t, _, _ in res.skipped_per_profile_capped] == [ids[2]]
    assert res.skipped_profile_busy == []


def test_dead_worker_pid_does_not_wedge_the_profile(kb, monkeypatch):
    """An open run whose worker PID is dead must not wedge the profile."""
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")
    with kb.connect_closing() as conn:
        busy = kb.create_task(conn, title="zombie run", assignee="alpha")
        nxt = kb.create_task(conn, title="queued", assignee="alpha")
        _make_running(kb, conn, busy, pid=_dead_pid())

    with kb.connect_closing() as conn:
        res = kb.dispatch_once(conn, spawn_fn=_spawn_stub(None))

    # The dead run is reclaimed rather than held, so the profile keeps
    # dispatching: exactly one card starts, and the slot is handed out once.
    # WHICH card wins is ordinary scheduling order (the reclaimed card was
    # created first at the same priority), not part of this contract — the
    # wedge is.
    spawned = [task_id for task_id, _, _ in res.spawned]
    assert len(spawned) == 1
    assert spawned[0] in (busy, nxt)
    with kb.connect_closing() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM task_runs WHERE ended_at IS NULL"
        ).fetchone()[0] == 1


def test_stale_heartbeat_does_not_wedge_the_profile(kb, procs):
    """Live PID but no heartbeat for longer than the max-stale window."""
    live = _spawn_live_session(procs)
    with kb.connect_closing() as conn:
        busy = kb.create_task(conn, title="wedged run", assignee="alpha")
        nxt = kb.create_task(conn, title="queued", assignee="alpha")
        _make_running(
            kb, conn, busy, pid=live.pid,
            heartbeat_age=kb.DEFAULT_CLAIM_HEARTBEAT_MAX_STALE_SECONDS + 60,
        )

    with kb.connect_closing() as conn:
        res = kb.dispatch_once(conn, spawn_fn=_spawn_stub(None))

    assert nxt in [s[0] for s in res.spawned]


def test_claim_resets_inherited_stale_heartbeat_atomically(kb, procs):
    """A new run starts with claim-time activity, never the prior run's age."""
    live = _spawn_live_session(procs)
    before_claim = int(time.time())
    stale = before_claim - kb.DEFAULT_CLAIM_HEARTBEAT_MAX_STALE_SECONDS - 60
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="reclaimed work", assignee="alpha")
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET last_heartbeat_at = ? WHERE id = ?",
                (stale, tid),
            )

        assert kb.claim_task(conn, tid) is not None
        kb._set_worker_pid(conn, tid, live.pid)

        row = conn.execute(
            "SELECT last_heartbeat_at FROM tasks WHERE id = ?", (tid,),
        ).fetchone()
        assert row["last_heartbeat_at"] >= before_claim
        assert [run["task_id"] for run in kb.profile_live_runs(conn, "alpha")] == [
            tid
        ]


def test_review_claim_refreshes_stale_implementation_heartbeat_and_holds_profile_slot(
    kb, procs,
):
    """A new reviewer run must not inherit the implementation run's age."""
    live = _spawn_live_session(procs)
    before_claim = int(time.time())
    stale = before_claim - kb.DEFAULT_CLAIM_HEARTBEAT_MAX_STALE_SECONDS - 60
    kb.create_board(slug="other", name="Other")

    with kb.connect_closing() as conn:
        reviewable = kb.create_task(
            conn, title="stale implementation", assignee="alpha",
        )
        same_board = kb.create_task(
            conn, title="same-board contender", assignee="alpha",
        )
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET status = 'review', last_heartbeat_at = ? "
                "WHERE id = ?",
                (stale, reviewable),
            )

        assert kb.claim_review_task(conn, reviewable) is not None
        kb._set_worker_pid(conn, reviewable, live.pid)

        row = conn.execute(
            "SELECT last_heartbeat_at FROM tasks WHERE id = ?", (reviewable,),
        ).fetchone()
        assert row["last_heartbeat_at"] >= before_claim
        assert [run["task_id"] for run in kb.profile_live_runs(conn, "alpha")] == [
            reviewable
        ]

    with kb.connect_closing(board="other") as conn:
        cross_board = kb.create_task(
            conn, title="cross-board contender", assignee="alpha",
        )

    for board, contender in (("default", same_board), ("other", cross_board)):
        with kb.connect_closing(board=board) as conn:
            result = kb.dispatch_once(
                conn, board=board, spawn_fn=_spawn_stub(None),
            )
            assert [task_id for task_id, _, _ in result.spawned] == []
            assert [task_id for task_id, _, _ in result.skipped_profile_busy] == [
                contender
            ]
            assert kb.get_task(conn, contender).status == "ready"


def test_missing_run_pointer_does_not_wedge_the_profile(kb, procs):
    """status='running' with no current_run_id is broken bookkeeping, not a run."""
    live = _spawn_live_session(procs)
    with kb.connect_closing() as conn:
        busy = kb.create_task(conn, title="no run row", assignee="alpha")
        nxt = kb.create_task(conn, title="queued", assignee="alpha")
        _make_running(kb, conn, busy, pid=live.pid)
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET current_run_id = NULL WHERE id = ?", (busy,)
            )

    with kb.connect_closing() as conn:
        res = kb.dispatch_once(
            conn, spawn_fn=_spawn_stub(None), reconcile_orphans=False,
        )

    assert nxt in [s[0] for s in res.spawned]


def test_leaked_pointer_to_closed_run_does_not_wedge_the_profile(kb, procs):
    """A current_run_id pointing at an already-ended run is not a live run.

    This is the half-finished state requirement (4) exists to prevent; the
    guard must not treat it as in-flight work.
    """
    live = _spawn_live_session(procs)
    with kb.connect_closing() as conn:
        busy = kb.create_task(conn, title="leaked pointer", assignee="alpha")
        nxt = kb.create_task(conn, title="queued", assignee="alpha")
        _make_running(kb, conn, busy, pid=live.pid)
        run_id = kb._current_run_id(conn, busy)
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE task_runs SET ended_at = ?, status = 'completed', "
                "outcome = 'completed' WHERE id = ?",
                (int(time.time()), run_id),
            )

    with kb.connect_closing() as conn:
        res = kb.dispatch_once(
            conn, spawn_fn=_spawn_stub(None), reconcile_orphans=False,
        )

    assert nxt in [s[0] for s in res.spawned]


def test_review_lane_respects_the_live_run_guard(kb, procs):
    """The review lane shares the profile's single-run budget."""
    live = _spawn_live_session(procs)
    with kb.connect_closing() as conn:
        busy = kb.create_task(conn, title="in flight", assignee="alpha")
        reviewable = kb.create_task(conn, title="needs review", assignee="alpha")
        _make_running(kb, conn, busy, pid=live.pid)
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET status = 'review' WHERE id = ?", (reviewable,)
            )

    with kb.connect_closing() as conn:
        res = kb.dispatch_once(conn, spawn_fn=_spawn_stub(None))

    assert res.spawned == []
    assert [t for t, _, _ in res.skipped_profile_busy] == [reviewable]


# ---------------------------------------------------------------------------
# 2. self-development work (priority -100) yields to normal work
# ---------------------------------------------------------------------------


def test_self_dev_deferred_while_normal_work_is_ready(kb):
    """A -100 card waits while a normal card for the same profile is ready.

    The normal card is held back by the respawn guard this tick, so ordering
    alone would let the self-dev card take the profile's slot.
    """
    with kb.connect_closing() as conn:
        normal = kb.create_task(conn, title="real work", assignee="alpha")
        selfdev = kb.create_task(
            conn, title="self improvement", assignee="alpha",
            priority=kb.SELF_DEV_PRIORITY,
        )
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET last_failure_error = ? WHERE id = ?",
                ("429 rate limit exceeded", normal),
            )

    with kb.connect_closing() as conn:
        res = kb.dispatch_once(conn, spawn_fn=_spawn_stub(None))

    assert res.spawned == []
    assert [t for t, _ in res.skipped_self_dev_deferred] == [selfdev]
    with kb.connect_closing() as conn:
        assert kb.get_task(conn, selfdev).status == "ready"


def test_self_dev_deferred_while_normal_work_is_running(kb, procs):
    """A -100 card waits while normal work for the profile is running."""
    live = _spawn_live_session(procs)
    with kb.connect_closing() as conn:
        normal = kb.create_task(conn, title="real work", assignee="alpha")
        selfdev = kb.create_task(
            conn, title="self improvement", assignee="alpha",
            priority=kb.SELF_DEV_PRIORITY,
        )
        _make_running(kb, conn, normal, pid=live.pid)

    with kb.connect_closing() as conn:
        res = kb.dispatch_once(
            conn, spawn_fn=_spawn_stub(None), reconcile_orphans=False,
        )

    assert [t for t, _ in res.skipped_self_dev_deferred] == [selfdev]


def test_self_dev_deferred_by_normal_work_on_another_board(kb):
    """Self-development cannot take the global slot ahead of other-board work."""
    kb.create_board(slug="other", name="Other")
    with kb.connect_closing(board="other") as conn:
        normal = kb.create_task(conn, title="real work elsewhere", assignee="alpha")
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET last_failure_error = ? WHERE id = ?",
                ("429 rate limit exceeded", normal),
            )
    with kb.connect_closing(board="default") as conn:
        selfdev = kb.create_task(
            conn, title="self improvement", assignee="alpha",
            priority=kb.SELF_DEV_PRIORITY,
        )

    with kb.connect_closing(board="default") as conn:
        res = kb.dispatch_once(
            conn, board="default", spawn_fn=_spawn_stub(None),
        )

    assert res.spawned == []
    assert [task_id for task_id, _ in res.skipped_self_dev_deferred] == [selfdev]
    with kb.connect_closing(board="default") as conn:
        assert kb.get_task(conn, selfdev).status == "ready"


@pytest.mark.parametrize(
    "liveness",
    [
        pytest.param("dead-pid", id="cross-board-dead-running"),
        pytest.param("stale-heartbeat", id="cross-board-stale-running"),
    ],
)
def test_self_dev_not_deferred_by_nonlive_running_work_on_another_board(
    kb, procs, liveness,
):
    """A broken cross-board running row cannot starve self-dev forever.

    Ready normal work always wins, but running normal work wins only while it
    satisfies the same PID/claim/heartbeat contract as ``profile_live_runs``.
    """
    kb.create_board(slug="other", name="Other")
    live = _spawn_live_session(procs) if liveness == "stale-heartbeat" else None
    with kb.connect_closing(board="other") as conn:
        stranded = kb.create_task(
            conn, title=f"cross-board {liveness} normal run", assignee="alpha",
        )
        _make_running(
            kb,
            conn,
            stranded,
            pid=live.pid if live is not None else _dead_pid(),
            heartbeat_age=(
                kb.DEFAULT_CLAIM_HEARTBEAT_MAX_STALE_SECONDS + 1
                if liveness == "stale-heartbeat" else 0
            ),
        )
    with kb.connect_closing(board="default") as conn:
        selfdev = kb.create_task(
            conn, title="self improvement after nonlive run", assignee="alpha",
            priority=kb.SELF_DEV_PRIORITY,
        )
        res = kb.dispatch_once(
            conn, board="default", spawn_fn=_spawn_stub(None),
            reconcile_orphans=False,
        )

    assert [task_id for task_id, _, _ in res.spawned] == [selfdev]
    assert res.skipped_self_dev_deferred == []


def test_self_dev_runs_when_no_normal_work_exists(kb):
    """With nothing else queued for the profile, self-dev work dispatches."""
    with kb.connect_closing() as conn:
        selfdev = kb.create_task(
            conn, title="self improvement", assignee="alpha",
            priority=kb.SELF_DEV_PRIORITY,
        )
        # Normal work for a DIFFERENT profile must not defer alpha's self-dev.
        kb.create_task(conn, title="beta work", assignee="beta")

    with kb.connect_closing() as conn:
        res = kb.dispatch_once(conn, spawn_fn=_spawn_stub(None))

    assert selfdev in [s[0] for s in res.spawned]
    assert res.skipped_self_dev_deferred == []


def test_self_dev_not_deferred_by_finished_normal_work(kb):
    """Done/blocked normal work is not 'ready or running' — self-dev proceeds."""
    with kb.connect_closing() as conn:
        normal = kb.create_task(conn, title="real work", assignee="alpha")
        selfdev = kb.create_task(
            conn, title="self improvement", assignee="alpha",
            priority=kb.SELF_DEV_PRIORITY,
        )
        assert kb.complete_task(conn, normal, result="done")

    with kb.connect_closing() as conn:
        res = kb.dispatch_once(conn, spawn_fn=_spawn_stub(None))

    assert selfdev in [s[0] for s in res.spawned]


# ---------------------------------------------------------------------------
# 3. terminal transitions reap that run's worker tree — and nothing else
# ---------------------------------------------------------------------------


def _child_pids(pid: int) -> list[int]:
    out = subprocess.run(
        ["pgrep", "-P", str(pid)], capture_output=True, text=True, check=False,
    )
    return [int(x) for x in out.stdout.split()]


def _wait_gone(pid: int, timeout: float = 10.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            os.kill(pid, 0)
        except (ProcessLookupError, PermissionError):
            return True
        # A reaped-but-unwaited child shows up as a zombie; that counts.
        try:
            with open(f"/proc/{pid}/status", encoding="utf-8") as f:
                if any(
                    line.startswith("State:") and "Z" in line
                    for line in f
                ):
                    return True
        except OSError:
            return True
        time.sleep(0.1)
    return False


@pytest.mark.skipif(os.name == "nt", reason="POSIX process groups")
def test_complete_task_reaps_the_run_worker_tree(kb, procs):
    """done → the worker and everything it forked are terminated."""
    live = _spawn_live_session(procs)
    grandchildren = _child_pids(live.pid)
    assert grandchildren, "helper did not fork a child"

    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="work", assignee="alpha")
        _make_running(kb, conn, tid, pid=live.pid)
        assert kb.complete_task(conn, tid, result="ok")

    assert _wait_gone(live.pid), "worker survived completion"
    for gc in grandchildren:
        assert _wait_gone(gc), f"forked child {gc} survived completion"


@pytest.mark.skipif(os.name == "nt", reason="POSIX process groups")
def test_block_task_reaps_the_run_worker_tree(kb, procs):
    """blocked → same reaping contract as done."""
    live = _spawn_live_session(procs)
    grandchildren = _child_pids(live.pid)
    assert grandchildren

    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="work", assignee="alpha")
        _make_running(kb, conn, tid, pid=live.pid)
        assert kb.block_task(conn, tid, reason="needs a human", kind="needs_input")

    assert _wait_gone(live.pid), "worker survived block"
    for gc in grandchildren:
        assert _wait_gone(gc), f"forked child {gc} survived block"


@pytest.mark.skipif(os.name == "nt", reason="POSIX process groups")
def test_request_review_reaps_exact_implementation_tree_only(kb, procs):
    implementer = _spawn_live_session(procs)
    descendants = _child_pids(implementer.pid)
    browser = _spawn_live_session(procs)
    auth = _spawn_live_session(procs)
    assert descendants

    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="review handoff", assignee="alpha")
        claimed = _make_running(kb, conn, tid, pid=implementer.pid)
        assert kb.request_review(
            conn, tid, summary="ready", reviewer="beta",
            expected_run_id=claimed.current_run_id,
        )

    assert _wait_gone(implementer.pid), "implementer survived review handoff"
    assert all(_wait_gone(pid) for pid in descendants)
    assert browser.poll() is None
    assert auth.poll() is None


@pytest.mark.skipif(os.name == "nt", reason="POSIX process groups")
def test_request_changes_reaps_exact_reviewer_tree_only(kb, procs):
    reviewer = _spawn_live_session(procs)
    descendants = _child_pids(reviewer.pid)
    browser = _spawn_live_session(procs)
    assert descendants

    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="review verdict", assignee="alpha")
        assert kb.request_review(
            conn, tid, summary="ready", reviewer="beta", force=True,
        )
        claimed = kb.claim_review_task(conn, tid)
        assert claimed is not None
        kb._set_worker_pid(conn, tid, reviewer.pid)
        assert kb.request_changes(
            conn, tid, reason="fix it", expected_run_id=claimed.current_run_id,
        )[0]

    assert _wait_gone(reviewer.pid), "reviewer survived changes handoff"
    assert all(_wait_gone(pid) for pid in descendants)
    assert browser.poll() is None


@pytest.mark.skipif(os.name == "nt", reason="POSIX process groups")
def test_reopen_review_task_reaps_a_dangling_review_run(kb, procs):
    reviewer = _spawn_live_session(procs)
    descendants = _child_pids(reviewer.pid)
    assert descendants

    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="manual review reopen", assignee="alpha")
        assert kb.request_review(conn, tid, summary="ready", reviewer="beta")
        claimed = kb.claim_review_task(conn, tid)
        assert claimed is not None
        kb._set_worker_pid(conn, tid, reviewer.pid)
        # Reproduce the defensive recovery state handled by
        # reopen_review_task: review lane with a still-open reviewer run.
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status = 'review' WHERE id = ?", (tid,))
        assert kb.reopen_review_task(conn, tid)

    assert _wait_gone(reviewer.pid), "dangling reviewer survived reopen"
    assert all(_wait_gone(pid) for pid in descendants)


def test_request_review_self_handoff_never_signals_the_caller(kb, monkeypatch):
    signalled: list[tuple[int, int]] = []
    monkeypatch.setattr(kb.os, "kill", lambda pid, sig: signalled.append((pid, sig)))
    monkeypatch.setattr(kb, "_same_group_descendants", lambda pid: [])

    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="self handoff", assignee="alpha")
        claimed = _make_running(kb, conn, tid, pid=os.getpid())
        assert kb.request_review(
            conn, tid, summary="ready", expected_run_id=claimed.current_run_id,
        )

    assert signalled == []


def test_request_review_reaps_committed_run_snapshot_not_successor(
    kb, monkeypatch,
):
    """A reviewer claiming after commit cannot replace the reap target."""
    observed: dict[str, object] = {}
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="handoff race", assignee="alpha")
        implementation = _make_running(kb, conn, tid, pid=111111)

        def claim_successor(task_id, snapshot):
            assert conn.in_transaction is False
            observed.update(snapshot)
            reviewer = kb.claim_review_task(conn, task_id)
            assert reviewer is not None
            kb._set_worker_pid(conn, task_id, 222222)

        monkeypatch.setattr(kb, "_reap_finished_run_worker", claim_successor)
        assert kb.request_review(
            conn, tid, summary="ready", reviewer="beta",
            expected_run_id=implementation.current_run_id,
        )
        successor_run = kb.get_task(conn, tid).current_run_id

    assert observed["run_id"] == implementation.current_run_id
    assert observed["worker_pid"] == 111111
    assert successor_run != implementation.current_run_id


@pytest.mark.skipif(os.name == "nt", reason="POSIX process groups")
def test_unblock_reaps_a_defensively_reclaimed_dangling_run(kb, procs):
    worker = _spawn_live_session(procs)
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="dangling blocked run", assignee="alpha")
        _make_running(kb, conn, tid, pid=worker.pid)
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status = 'blocked' WHERE id = ?", (tid,))
        assert kb.unblock_task(conn, tid)

    assert _wait_gone(worker.pid), "reclaimed blocked worker survived unblock"


@pytest.mark.skipif(os.name == "nt", reason="POSIX process groups")
def test_group_reap_waits_for_descendants_after_leader_exits(procs, kb):
    """A TERM-compliant leader must not hide a TERM-ignoring descendant."""
    leader = subprocess.Popen(
        [sys.executable, "-c", TERM_EXITING_LEADER],
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    procs.append(leader)
    time.sleep(0.5)
    children = _child_pids(leader.pid)
    assert children, "helper did not fork a child"

    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="mixed TERM behavior", assignee="alpha")
        _make_running(kb, conn, tid, pid=leader.pid)
        lock = conn.execute(
            "SELECT claim_lock FROM tasks WHERE id = ?", (tid,)
        ).fetchone()["claim_lock"]

    info = kb._reap_run_worker_tree(
        leader.pid, lock, grace_seconds=0.2,
    )

    assert info["sigkill"] is True
    for child in children:
        assert _wait_gone(child), f"TERM-ignoring child {child} survived group reap"


@pytest.mark.skipif(os.name == "nt", reason="POSIX process groups")
def test_reap_leaves_separate_browser_and_auth_sessions_alone(kb, procs):
    """Only the run's own process group is signalled.

    A browser / auth session started in its own session (its own pgid) is
    untouched by a completion, even though it belongs to the same profile.
    """
    live = _spawn_live_session(procs)
    browser = _spawn_live_session(procs)
    auth = _spawn_live_session(procs)

    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="work", assignee="alpha")
        _make_running(kb, conn, tid, pid=live.pid)
        assert kb.complete_task(conn, tid, result="ok")

    assert _wait_gone(live.pid)
    assert browser.poll() is None, "unrelated browser session was killed"
    assert auth.poll() is None, "unrelated auth session was killed"


@pytest.mark.skipif(os.name == "nt", reason="POSIX process groups")
def test_reap_never_signals_a_shared_process_group(kb, procs):
    """A worker pid that is not its own group leader is signalled alone.

    Killing a shared group is how an in-process/foreground worker takes the
    operator's whole session — browser and auth sessions included — down with
    it. Such a pid gets a direct SIGTERM, never a killpg.
    """
    shared = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(120)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )  # no start_new_session → shares pytest's process group
    procs.append(shared)
    assert os.getpgid(shared.pid) != shared.pid

    killed: list[tuple[int, int]] = []
    killpg_calls: list[tuple[int, int]] = []

    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="work", assignee="alpha")
        _make_running(kb, conn, tid, pid=shared.pid)
        lock = conn.execute(
            "SELECT claim_lock FROM tasks WHERE id = ?", (tid,)
        ).fetchone()["claim_lock"]

    info = kb._reap_run_worker_tree(
        shared.pid, lock,
        signal_fn=lambda p, s: killed.append((p, s)),
        killpg_fn=lambda g, s: killpg_calls.append((g, s)),
    )

    assert info["group_reaped"] is False
    assert killpg_calls == []
    assert killed and all(p == shared.pid for p, _ in killed)
    assert shared.poll() is None


@pytest.mark.skipif(os.name == "nt", reason="POSIX process groups")
def test_self_transition_reaps_descendants_but_never_itself(kb, procs):
    """A worker finishing its own run: subprocesses die, the worker does not.

    This is the in-process path — ``kanban_complete`` / ``kanban_block``
    called by the worker itself — so it is the common case, not the edge one.
    The worker must survive long enough to return its tool result, its own
    forked helpers must not outlive the run, and anything detached into its
    own session (browser, auth login) must be left completely alone.
    """
    # A helper the run forked normally: inherits our process group.
    in_group = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(120)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    procs.append(in_group)
    # A detached session — how a browser / auth session is started.
    detached = _spawn_live_session(procs)
    assert os.getpgid(in_group.pid) == os.getpgid(0)
    assert os.getpgid(detached.pid) != os.getpgid(0)

    killpg_calls: list[tuple[int, int]] = []
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="self complete", assignee="alpha")
        _make_running(kb, conn, tid, pid=os.getpid())
        lock = conn.execute(
            "SELECT claim_lock FROM tasks WHERE id = ?", (tid,)
        ).fetchone()["claim_lock"]

    info = kb._reap_run_worker_tree(
        os.getpid(), lock,
        killpg_fn=lambda g, s: killpg_calls.append((g, s)),
    )

    # Never a group signal — that group contains us.
    assert killpg_calls == []
    assert info["group_reaped"] is False
    assert in_group.pid in info["descendants_reaped"]
    assert _wait_gone(in_group.pid), "in-group child survived the self-reap"
    # We are still here, and so is the detached session.
    assert detached.poll() is None, "detached session was killed"


def test_reap_ignores_a_foreign_host_claim(kb):
    """A pid owned by another host is never signalled locally."""
    killed: list[tuple[int, int]] = []
    info = kb._reap_run_worker_tree(
        424242, "some-other-host:abc123",
        signal_fn=lambda p, s: killed.append((p, s)),
    )
    assert killed == []
    assert info["host_local"] is False


# ---------------------------------------------------------------------------
# 4. finishing a run is atomic
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("finish", ["complete", "block"])
def test_terminal_transition_closes_run_and_clears_pointer(kb, finish):
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="work", assignee="alpha")
        _make_running(kb, conn, tid, pid=None)
        run_id = kb._current_run_id(conn, tid)
        if finish == "complete":
            assert kb.complete_task(conn, tid, result="ok")
        else:
            assert kb.block_task(conn, tid, reason="stuck", kind="needs_input")

        assert kb.get_task(conn, tid).current_run_id is None
        run = conn.execute(
            "SELECT ended_at, status, outcome FROM task_runs WHERE id = ?",
            (run_id,),
        ).fetchone()
        assert run["ended_at"] is not None
        assert run["outcome"] in ("completed", "blocked")


def test_end_run_does_not_clear_a_successor_pointer(kb):
    """The pointer clear is a CAS on the run being finished.

    Without it, a finish computed for run N would blank a pointer that has
    since moved to run N+1 — leaving the successor's live run invisible to
    the guard in (1).
    """
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="work", assignee="alpha")
        _make_running(kb, conn, tid, pid=None)
        first_run = kb._current_run_id(conn, tid)
        # Simulate a successor run taking ownership of the card.
        with kb.write_txn(conn):
            cur = conn.execute(
                "INSERT INTO task_runs (task_id, profile, status, started_at) "
                "VALUES (?, 'alpha', 'running', ?)",
                (tid, int(time.time())),
            )
            successor = int(cur.lastrowid)
            conn.execute(
                "UPDATE tasks SET current_run_id = ? WHERE id = ?",
                (successor, tid),
            )
            # A finish computed for the FIRST run must be a no-op on the row.
            kb._end_run(conn, tid, outcome="completed", run_id=first_run)

        assert kb.get_task(conn, tid).current_run_id == successor


# ---------------------------------------------------------------------------
# The reported incident, end to end
# ---------------------------------------------------------------------------


@pytest.mark.skipif(os.name == "nt", reason="POSIX process groups")
@pytest.mark.parametrize(
    "finish, stubborn",
    [
        pytest.param("dependency", False, id="run83-dependency"),
        pytest.param("dependency", True, id="run91-dependency-sigterm-resistant"),
        pytest.param("complete", True, id="run86-completed-sigterm-resistant"),
    ],
)
def test_incident_live_run_overlap_then_stranded_worker(kb, procs, monkeypatch, finish, stubborn):
    """Field incidents: two runs overlap on one profile, then a strand.

    Reported shapes (ids/pids below stand in for the real ones):

    * dev run 83 for ``t_b9fbe688`` was live — fresh heartbeats, files
      changing, worker PID 14013 up — yet a second card for the SAME profile
      was claimed as run 84 (PID 14265), so two workers shared one
      HERMES_HOME. ``t_b9fbe688`` was then linked as a child dependency and
      transitioned out of ``running``, and PID 14013 stayed up until an
      operator sent SIGTERM by hand.
    * research run 86 for ``t_99d37365`` (PID 16072) was live/fresh when
      ``t_0bace830`` run 91 (PID 26741) was concurrently claimed. Clearing
      run 91 via ``dependency_wait`` left PID 26741 alive — it survived
      SIGTERM and only died to a process-group SIGKILL — and completing run
      86 likewise left PID 16072 running.

    So both terminal transitions are exercised, against a worker tree that
    ignores SIGTERM: the second claim must never happen, and the transition
    must reap the run's tree, escalating past TERM when it has to.
    """
    # Keep the TERM→KILL escalation window short; the production default is
    # a human-scale 5s shutdown budget.
    monkeypatch.setattr(kb, "WORKER_REAP_GRACE_SECONDS", 1.5)

    first = _spawn_live_session(procs, stubborn=stubborn)  # PID 14013 / 16072
    first_children = _child_pids(first.pid)
    assert first_children, "helper did not fork a child"

    with kb.connect_closing() as conn:
        live_card = kb.create_task(conn, title="live work", assignee="alpha")
        second_card = kb.create_task(conn, title="second work", assignee="alpha")
        parent = kb.create_task(conn, title="upstream dependency", assignee="beta")
        _make_running(kb, conn, live_card, pid=first.pid)

    # 1. the second run must not be claimed while the first is demonstrably
    #    live. The beta parent card is unrelated work on another profile and
    #    is expected to dispatch — the guard is per profile, not board-wide.
    with kb.connect_closing() as conn:
        res = kb.dispatch_once(conn, spawn_fn=_spawn_stub(26741))

    assert [s[0] for s in res.spawned if s[1] == "alpha"] == []
    assert [s[0] for s in res.spawned] == [parent]
    assert [t for t, _, _ in res.skipped_profile_busy] == [second_card]
    with kb.connect_closing() as conn:
        assert kb.get_task(conn, second_card).status == "ready"
        assert kb.get_task(conn, live_card).current_run_id is not None

    # 2. the live card takes a terminal transition; its worker tree goes too.
    with kb.connect_closing() as conn:
        if finish == "dependency":
            kb.link_tasks(conn, parent, live_card)
            assert kb.block_task(
                conn, live_card, reason=f"waiting on {parent}", kind="dependency",
            )
            assert kb.get_task(conn, live_card).status == "todo"
        else:
            assert kb.complete_task(conn, live_card, result="research done")
            assert kb.get_task(conn, live_card).status == "done"
        assert kb.get_task(conn, live_card).current_run_id is None

    assert _wait_gone(first.pid), "worker survived the terminal transition"
    for child in first_children:
        assert _wait_gone(child), f"forked child {child} survived"

    # 3. with the slot released, the second card dispatches on the next tick.
    with kb.connect_closing() as conn:
        res2 = kb.dispatch_once(conn, spawn_fn=_spawn_stub(None))
    assert second_card in [s[0] for s in res2.spawned]


@pytest.mark.skipif(os.name == "nt", reason="POSIX process groups")
def test_production_run102_fresh_claim_blocks_run105_and_block_reaps_tree(
    kb, procs, monkeypatch,
):
    """Production shape: a stale prior heartbeat must not age a fresh claim.

    Research ``t_5ae6f80e`` run 102 / PID 54934 was live and fresh when
    ``t_ee6ae69e`` run 105 / PID 58698 was wrongly spawned on the same board
    and profile. Blocking the duplicate cleared DB ownership but left its PID
    and child alive until an operator killed them. The helper PIDs below stand
    in for those production PIDs while preserving the observed task/run values.
    """
    monkeypatch.setattr(kb, "WORKER_REAP_GRACE_SECONDS", 1.5)
    run102 = _spawn_live_session(procs, stubborn=True)  # production PID 54934
    children = _child_pids(run102.pid)
    assert children

    with kb.connect_closing() as conn:
        t_5ae6f80e = kb.create_task(
            conn, title="research t_5ae6f80e run 102", assignee="alpha",
            priority=10,
        )
        t_ee6ae69e = kb.create_task(
            conn, title="follow-up t_ee6ae69e run 105", assignee="alpha",
            priority=5,
        )
        inherited = (
            int(time.time())
            - kb.DEFAULT_CLAIM_HEARTBEAT_MAX_STALE_SECONDS
            - 60
        )
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET last_heartbeat_at = ? WHERE id = ?",
                (inherited, t_5ae6f80e),
            )

    with kb.connect_closing() as conn:
        first = kb.dispatch_once(conn, spawn_fn=_spawn_stub(run102.pid))
    assert [task_id for task_id, _, _ in first.spawned] == [t_5ae6f80e]

    with kb.connect_closing() as conn:
        second = kb.dispatch_once(conn, spawn_fn=_spawn_stub(58698))
        assert [task_id for task_id, _, _ in second.spawned] == []
        assert [task_id for task_id, _, _ in second.skipped_profile_busy] == [
            t_ee6ae69e
        ]
        assert kb.get_task(conn, t_ee6ae69e).status == "ready"
        assert kb.block_task(
            conn, t_5ae6f80e, reason="operator block", kind="needs_input",
        )

    assert _wait_gone(run102.pid), "run 102 worker survived block transition"
    for child in children:
        assert _wait_gone(child), f"run 102 child {child} survived block"


@pytest.mark.skipif(os.name == "nt", reason="POSIX process groups")
def test_production_run84_blocks_run107_run108_and_dependency_reaps_both(
    kb, procs, monkeypatch,
):
    """Exact 1786676568 incident: prevent overlap and repair its DB shape.

    Dev repair ``t_9c9dbeaa`` run 84 / PID 14265 was live and fresh, yet the
    dispatcher spawned ``t_5c06e194`` run 107 / PID 105903 and
    ``t_32cd9de8`` run 108 / PID 105904.  The operator parent-linked and
    dependency-blocked both successors; their rows were parked correctly but
    both worker trees survived for 2+ minutes.  Helper process IDs stand in for
    those production PID labels while the exact labels remain in task data.
    """
    incident_at = 1786676568
    monkeypatch.setattr(kb, "WORKER_REAP_GRACE_SECONDS", 1.5)
    ids = iter(("t_9c9dbeaa", "t_5c06e194", "t_32cd9de8"))
    monkeypatch.setattr(kb, "_new_task_id", lambda: next(ids))

    run84_worker = _spawn_live_session(procs, stubborn=True)
    browser = _spawn_live_session(procs)
    auth = _spawn_live_session(procs)
    with kb.connect_closing() as conn:
        parent = kb.create_task(
            conn,
            title="dev repair t_9c9dbeaa run84 PID14265",
            body=f"live/fresh at timestamp{incident_at}",
            assignee="alpha",
            priority=10,
        )
        run107_task = kb.create_task(
            conn,
            title="successor t_5c06e194 run107 PID105903",
            body=f"must not spawn while run84 is live at timestamp{incident_at}",
            assignee="alpha",
            priority=5,
        )
        run108_task = kb.create_task(
            conn,
            title="successor t_32cd9de8 run108 PID105904",
            body=f"must not spawn while run84 is live at timestamp{incident_at}",
            assignee="alpha",
            priority=4,
        )
        conn.execute(
            "INSERT INTO sqlite_sequence(name, seq) VALUES ('task_runs', 83)"
        )
        _make_running(kb, conn, parent, pid=run84_worker.pid)
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET last_heartbeat_at = ? WHERE id = ?",
                (incident_at, parent),
            )
        assert kb._current_run_id(conn, parent) == 84

    # Freeze only the liveness probe's clock at the reported dispatch instant;
    # process-reap polling keeps the real clock.
    real_profile_live_runs = kb.profile_live_runs

    def incident_profile_live_runs(conn, profile, **kwargs):
        kwargs.setdefault("now", incident_at)
        return real_profile_live_runs(conn, profile, **kwargs)

    monkeypatch.setattr(kb, "profile_live_runs", incident_profile_live_runs)
    attempted_spawns: list[str] = []

    def forbidden_spawn(task, workspace, *, board=None):
        attempted_spawns.append(task.id)
        return None

    with kb.connect_closing() as conn:
        prevented = kb.dispatch_once(conn, spawn_fn=forbidden_spawn)

    assert attempted_spawns == []
    assert prevented.spawned == []
    assert [task_id for task_id, _, _ in prevented.skipped_profile_busy] == [
        run107_task, run108_task,
    ]

    # Seed the historical impossible overlap directly: public claims correctly
    # refuse it now, but recovery must still close ownership and kill both trees.
    run107_worker = _spawn_live_session(procs, stubborn=True)
    run108_worker = _spawn_live_session(procs, stubborn=True)
    worker_children = {
        run107_task: _child_pids(run107_worker.pid),
        run108_task: _child_pids(run108_worker.pid),
    }
    assert all(worker_children.values())
    with kb.connect_closing() as conn:
        conn.execute("UPDATE sqlite_sequence SET seq = 106 WHERE name = 'task_runs'")
        run107 = _seed_running_overlap(
            kb, conn, run107_task, pid=run107_worker.pid,
        )
        run108 = _seed_running_overlap(
            kb, conn, run108_task, pid=run108_worker.pid,
        )
        assert run107.current_run_id == 107
        assert run108.current_run_id == 108
        for child_task, run_id in ((run107_task, 107), (run108_task, 108)):
            kb.link_tasks(conn, parent, child_task)
            assert kb.block_task(
                conn,
                child_task,
                reason=(
                    f"operator repair timestamp{incident_at}: child of "
                    "t_9c9dbeaa run84 PID14265"
                ),
                kind="dependency",
                expected_run_id=run_id,
            )

        for child_task, run_id in ((run107_task, 107), (run108_task, 108)):
            task = kb.get_task(conn, child_task)
            run = conn.execute(
                "SELECT ended_at, outcome FROM task_runs WHERE id = ?", (run_id,),
            ).fetchone()
            assert task.status == "todo"
            assert task.current_run_id is None
            assert run["ended_at"] is not None
            assert run["outcome"] == "blocked"

    for child_task, worker in (
        (run107_task, run107_worker), (run108_task, run108_worker),
    ):
        assert _wait_gone(worker.pid), f"{child_task} worker tree survived"
        for child_pid in worker_children[child_task]:
            assert _wait_gone(child_pid), f"{child_task} child {child_pid} survived"
    assert run84_worker.poll() is None, "live run84 was killed during child repair"
    assert browser.poll() is None, "unrelated browser session was killed"
    assert auth.poll() is None, "unrelated auth session was killed"


def test_production_run105_transient_duplicate_waits_for_run102_then_resumes(
    kb, procs, monkeypatch,
):
    """A transient duplicate returns to the pool without bypassing its profile.

    Production ``t_ee6ae69e`` run 105 discovered that it duplicated the still
    live research ``t_5ae6f80e`` run 102 and called
    ``block_task(kind="transient")``.  Closing/reaping run 105 must make its
    card dispatchable again, while the ordinary single-live-run profile guard
    keeps it waiting until run 102 finishes.  No manual unblock is involved.
    """
    monkeypatch.setattr(kb, "WORKER_REAP_GRACE_SECONDS", 1.5)
    run102 = _spawn_live_session(procs)
    run105 = _spawn_live_session(procs)
    run105_children = _child_pids(run105.pid)
    assert run105_children

    with kb.connect_closing() as conn:
        t_5ae6f80e = kb.create_task(
            conn, title="research t_5ae6f80e run 102", assignee="alpha",
            priority=10,
        )
        t_ee6ae69e = kb.create_task(
            conn, title="duplicate t_ee6ae69e run 105", assignee="alpha",
            priority=5,
        )
        _make_running(kb, conn, t_5ae6f80e, pid=run102.pid)
        # Reproduce the already-overlapped production state directly: the
        # profile guard prevents creating this state now, but must recover it.
        _seed_running_overlap(kb, conn, t_ee6ae69e, pid=run105.pid)

        assert kb.block_task(
            conn,
            t_ee6ae69e,
            reason=f"duplicate of still-live {t_5ae6f80e} run 102",
            kind="transient",
        )

    assert _wait_gone(run105.pid), "run 105 worker survived transient block"
    for child in run105_children:
        assert _wait_gone(child), f"run 105 child {child} survived transient block"

    with kb.connect_closing() as conn:
        waiting = kb.get_task(conn, t_ee6ae69e)
        assert waiting.status == "ready"
        assert waiting.current_run_id is None
        while_102_live = kb.dispatch_once(conn, spawn_fn=_spawn_stub(None))
        assert t_ee6ae69e not in [task_id for task_id, _, _ in while_102_live.spawned]
        assert [task_id for task_id, _, _ in while_102_live.skipped_profile_busy] == [
            t_ee6ae69e
        ]
        assert kb.get_task(conn, t_ee6ae69e).status == "ready"

        assert kb.complete_task(conn, t_5ae6f80e, result="research complete")

    assert _wait_gone(run102.pid), "run 102 worker survived completion"
    with kb.connect_closing() as conn:
        after_102 = kb.dispatch_once(conn, spawn_fn=_spawn_stub(None))
        assert [task_id for task_id, _, _ in after_102.spawned] == [t_ee6ae69e]
        assert kb.get_task(conn, t_ee6ae69e).status == "running"


# ---------------------------------------------------------------------------
# Parent-gated todo stays gated
# ---------------------------------------------------------------------------


def test_parent_gated_todo_is_never_promoted_or_claimed(kb, procs):
    """Field incident: dev run 96 and plan run 97 were moved to
    dependency-gated ``todo`` and their ``current_run_id`` cleared — then the
    dispatcher re-promoted / re-claimed them before the parent had completed.

    A card parked behind an unfinished parent must stay parked, through any
    number of dispatcher ticks, and must refuse a direct claim.
    """
    with kb.connect_closing() as conn:
        parent = kb.create_task(conn, title="upstream", assignee="beta")
        run96 = kb.create_task(conn, title="dev work", assignee="alpha")
        run97 = kb.create_task(conn, title="plan work", assignee="alpha")
        for child in (run96, run97):
            # Both were live runs when the dependency was discovered.
            _make_running(kb, conn, child, pid=None)
            kb.link_tasks(conn, parent, child)
            assert kb.block_task(
                conn, child, reason=f"waiting on {parent}", kind="dependency",
            )
            assert kb.get_task(conn, child).status == "todo"
            assert kb.get_task(conn, child).current_run_id is None

    # Several ticks, including the parent's own dispatch, must not move them.
    for _ in range(3):
        with kb.connect_closing() as conn:
            res = kb.dispatch_once(conn, spawn_fn=_spawn_stub(None))
        assert run96 not in [s[0] for s in res.spawned]
        assert run97 not in [s[0] for s in res.spawned]
        with kb.connect_closing() as conn:
            for child in (run96, run97):
                assert kb.get_task(conn, child).status == "todo"

    with kb.connect_closing() as conn:
        # A direct claim is refused, and refuses without opening a run.
        assert kb.claim_task(conn, run96) is None
        assert kb.get_task(conn, run96).current_run_id is None
        # So is a manual promote, unless the operator forces it.
        ok, reason = kb.promote_task(conn, run96, actor="operator")
        assert ok is False
        assert "parent" in (reason or "").lower()

    # Once the parent is done, the gate opens.
    with kb.connect_closing() as conn:
        assert kb.complete_task(conn, parent, result="upstream done")
    with kb.connect_closing() as conn:
        res = kb.dispatch_once(conn, spawn_fn=_spawn_stub(None))
    assert run96 in [s[0] for s in res.spawned]


def test_reclaim_of_parent_gated_card_lands_in_todo_not_ready(kb):
    """A reclaimed card with an unfinished parent must not surface as ready.

    The reclaim paths restore "the phase this run should retry from". If that
    restore ignores parents, a dependency-gated card reappears in the Ready
    lane and the very next tick treats it as dispatchable work — the
    re-promotion half of the run96 / run97 report.
    """
    with kb.connect_closing() as conn:
        parent = kb.create_task(conn, title="upstream", assignee="beta")
        child = kb.create_task(conn, title="gated work", assignee="alpha")
        _make_running(kb, conn, child, pid=_dead_pid())
        kb.link_tasks(conn, parent, child)
        # Expire the claim so the stale-claim reclaim path picks it up.
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET claim_expires = ? WHERE id = ?",
                (int(time.time()) - 1, child),
            )
        assert kb.release_stale_claims(conn) == 1

        gated = kb.get_task(conn, child)
        assert gated.status == "todo", (
            f"parent-gated card landed in {gated.status!r} after reclaim"
        )
        assert gated.current_run_id is None


# ---------------------------------------------------------------------------
# 5. concurrent claims
# ---------------------------------------------------------------------------


def test_concurrent_claim_task_yields_exactly_one_run(kb):
    """N threads claiming one card: exactly one wins, exactly one run row."""
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="contended", assignee="alpha")

    results: list[object] = []
    barrier = threading.Barrier(8)
    lock = threading.Lock()

    def worker(i: int) -> None:
        with kb.connect_closing() as conn:
            barrier.wait(timeout=30)
            try:
                claimed = kb.claim_task(conn, tid, claimer=f"host:{i}")
            except Exception as exc:  # surfaced below
                claimed = exc
        with lock:
            results.append(claimed)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)

    winners = [r for r in results if r is not None and not isinstance(r, Exception)]
    errors = [r for r in results if isinstance(r, Exception)]
    assert errors == []
    assert len(winners) == 1
    with kb.connect_closing() as conn:
        runs = conn.execute(
            "SELECT COUNT(*) AS n FROM task_runs WHERE task_id = ?", (tid,)
        ).fetchone()["n"]
        assert runs == 1
        assert kb.get_task(conn, tid).current_run_id is not None


@pytest.mark.parametrize("claim_api,source_status", [
    ("claim_task", "ready"),
    ("claim_review_task", "review"),
])
@pytest.mark.parametrize("boards", [("default", "default"), ("default", "other")])
def test_concurrent_direct_claims_take_one_profile_slot(
    kb, claim_api, source_status, boards,
):
    """Distinct direct claims share the default one-run profile budget."""
    if boards[0] != boards[1]:
        kb.create_board(slug=boards[1], name="Other")

    task_ids: list[str] = []
    for index, board in enumerate(boards):
        with kb.connect_closing(board=board) as conn:
            task_id = kb.create_task(
                conn, title=f"direct contender {index}", assignee="alpha",
            )
            if source_status == "review":
                with kb.write_txn(conn):
                    conn.execute(
                        "UPDATE tasks SET status = 'review' WHERE id = ?",
                        (task_id,),
                    )
            task_ids.append(task_id)

    barrier = threading.Barrier(2)
    results: list[object] = []
    result_lock = threading.Lock()

    def worker(index: int) -> None:
        try:
            with kb.connect_closing(board=boards[index]) as conn:
                barrier.wait(timeout=30)
                claimed = getattr(kb, claim_api)(
                    conn, task_ids[index], claimer=f"host:{index}",
                )
        except BaseException as exc:
            claimed = exc
        with result_lock:
            results.append(claimed)

    threads = [threading.Thread(target=worker, args=(index,)) for index in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    assert not any(thread.is_alive() for thread in threads)
    assert not [result for result in results if isinstance(result, BaseException)]
    assert len([result for result in results if result is not None]) == 1

    running = 0
    open_runs = 0
    for board in set(boards):
        with kb.connect_closing(board=board) as conn:
            running += conn.execute(
                "SELECT COUNT(*) FROM tasks "
                "WHERE assignee = 'alpha' AND status = 'running'"
            ).fetchone()[0]
            open_runs += conn.execute(
                "SELECT COUNT(*) FROM task_runs WHERE ended_at IS NULL"
            ).fetchone()[0]
    assert running == 1
    assert open_runs == 1


def test_concurrent_dispatch_never_double_claims_a_profile(kb, procs):
    """Parallel dispatcher ticks give one profile at most one live run."""
    live = _spawn_live_session(procs)
    with kb.connect_closing() as conn:
        for i in range(4):
            kb.create_task(conn, title=f"a{i}", assignee="alpha", priority=10 - i)

    spawned: list[tuple] = []
    lock = threading.Lock()
    barrier = threading.Barrier(4)

    def tick() -> None:
        with kb.connect_closing() as conn:
            barrier.wait(timeout=30)
            res = kb.dispatch_once(conn, spawn_fn=_spawn_stub(live.pid))
        with lock:
            spawned.extend(res.spawned)

    threads = [threading.Thread(target=tick) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=120)

    assert len(spawned) <= 1
    with kb.connect_closing() as conn:
        running = conn.execute(
            "SELECT COUNT(*) AS n FROM tasks "
            "WHERE assignee = 'alpha' AND status = 'running'"
        ).fetchone()["n"]
    assert running <= 1


def test_concurrent_dispatch_across_boards_claims_profile_at_most_once(kb, procs):
    """Board-scoped locks cannot permit a cross-board profile race.

    Regression shape: dev run 110 (t_338c2e27) and run 111 (t_83208337)
    were claimed at the same instant and both worker trees stayed alive.
    """
    live = _spawn_live_session(procs)
    kb.create_board(slug="other", name="Other")
    task_ids: dict[str, str] = {}
    for board in ("default", "other"):
        with kb.connect_closing(board=board) as conn:
            task_ids[board] = kb.create_task(
                conn, title=f"{board} work", assignee="alpha",
            )

    spawned: list[tuple[str, str]] = []
    errors: list[BaseException] = []
    barrier = threading.Barrier(2)
    lock = threading.Lock()

    def spawn(task, workspace, *, board=None):
        # Widen the old check/claim/PID-persist race: without profile-scoped
        # coordination both board dispatchers claim before either PID lands.
        time.sleep(0.2)
        return live.pid

    def tick(board: str) -> None:
        try:
            with kb.connect_closing(board=board) as conn:
                barrier.wait(timeout=30)
                result = kb.dispatch_once(conn, board=board, spawn_fn=spawn)
            with lock:
                spawned.extend((board, task_id) for task_id, _, _ in result.spawned)
        except BaseException as exc:
            with lock:
                errors.append(exc)

    threads = [threading.Thread(target=tick, args=(board,)) for board in task_ids]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    assert not any(thread.is_alive() for thread in threads)
    assert errors == []
    assert len(spawned) == 1
    running = 0
    open_runs = 0
    for board in task_ids:
        with kb.connect_closing(board=board) as conn:
            running += conn.execute(
                "SELECT COUNT(*) FROM tasks "
                "WHERE assignee = 'alpha' AND status = 'running'"
            ).fetchone()[0]
            open_runs += conn.execute(
                "SELECT COUNT(*) FROM task_runs WHERE ended_at IS NULL"
            ).fetchone()[0]
    assert running == 1
    assert open_runs == 1


def test_dispatch_fails_closed_when_board_lock_path_cannot_be_resolved(
    kb, monkeypatch,
):
    """Lock-path failure must never reopen the unguarded claim race."""
    spawned: list[str] = []
    with kb.connect_closing() as conn:
        task_id = kb.create_task(conn, title="must stay ready", assignee="alpha")

        real_path = kb.kanban_db_path
        path_calls = 0

        def broken_path(*args, **kwargs):
            nonlocal path_calls
            path_calls += 1
            if path_calls == 1:
                raise OSError("board path unavailable")
            return real_path(*args, **kwargs)

        monkeypatch.setattr(kb, "kanban_db_path", broken_path)
        with pytest.raises(RuntimeError, match="dispatch lock path"):
            kb.dispatch_once(
                conn,
                spawn_fn=lambda task, workspace, **kwargs: spawned.append(task.id),
            )

        assert spawned == []
        assert kb.get_task(conn, task_id).status == "ready"


def test_assignee_introduced_while_dispatch_waits_cannot_race_cross_board(
    kb, procs, monkeypatch,
):
    """A profile introduced after the lock snapshot is still serialized.

    The first board starts with ``beta`` and waits for the shared decision
    lock.  While it is waiting, the card is reassigned to ``alpha``; the second
    board already has alpha work.  The post-wait decision must observe that
    newly introduced profile and allow at most one claim across both boards.
    """
    live = _spawn_live_session(procs)
    kb.create_board(slug="other", name="Other")
    with kb.connect_closing(board="default") as conn:
        reassigned = kb.create_task(
            conn, title="reassigned while waiting", assignee="beta",
        )
    with kb.connect_closing(board="other") as conn:
        contender = kb.create_task(
            conn, title="existing alpha work", assignee="alpha",
        )

    waiting_for_decision_lock = threading.Event()
    original_decision_lock = kb._dispatch_decision_lock

    def observed_decision_lock():
        waiting_for_decision_lock.set()
        return original_decision_lock()

    monkeypatch.setattr(kb, "_dispatch_decision_lock", observed_decision_lock)

    spawned: list[tuple[str, str]] = []
    errors: list[BaseException] = []
    result_lock = threading.Lock()

    def tick(board: str) -> None:
        try:
            with kb.connect_closing(board=board) as conn:
                result = kb.dispatch_once(
                    conn, board=board, spawn_fn=_spawn_stub(live.pid),
                )
            with result_lock:
                spawned.extend(
                    (board, task_id) for task_id, _, _ in result.spawned
                )
        except BaseException as exc:
            with result_lock:
                errors.append(exc)

    with original_decision_lock():
        first = threading.Thread(target=tick, args=("default",))
        first.start()
        assert waiting_for_decision_lock.wait(timeout=30)
        with kb.connect_closing(board="default") as conn:
            with kb.write_txn(conn):
                conn.execute(
                    "UPDATE tasks SET assignee = 'alpha' WHERE id = ?",
                    (reassigned,),
                )
        second = threading.Thread(target=tick, args=("other",))
        second.start()

    first.join(timeout=60)
    second.join(timeout=60)

    assert not first.is_alive()
    assert not second.is_alive()
    assert errors == []
    assert {task_id for _, task_id in spawned} <= {reassigned, contender}
    assert len(spawned) <= 1


def test_concurrent_two_board_blocked_promotion_claims_profile_at_most_once(
    kb, procs, monkeypatch,
):
    """Non-sticky blocked cards promoted this tick share the decision lock."""
    live = _spawn_live_session(procs)
    kb.create_board(slug="other", name="Other")
    task_ids: dict[str, str] = {}
    for board in ("default", "other"):
        with kb.connect_closing(board=board) as conn:
            task_id = kb.create_task(
                conn, title=f"{board} recoverable block", assignee="alpha",
            )
            # No sticky block event: recompute_ready intentionally promotes
            # this circuit-breaker/direct-DB shape during the same tick.
            with kb.write_txn(conn):
                conn.execute(
                    "UPDATE tasks SET status = 'blocked' WHERE id = ?",
                    (task_id,),
                )
            task_ids[board] = task_id

    # Hold the first unlocked dispatcher after its liveness scan so the other
    # board can reach the same point. The shared decision lock makes the
    # second dispatcher run only after the first decision is durable.
    reached_guard = threading.Event()
    guard_calls = 0
    guard_lock = threading.Lock()
    original_guard = kb.check_respawn_guard

    def synchronized_guard(conn, task_id, *args, **kwargs):
        nonlocal guard_calls
        with guard_lock:
            guard_calls += 1
            call_number = guard_calls
            if call_number == 2:
                reached_guard.set()
        if call_number == 1:
            reached_guard.wait(timeout=1)
        return original_guard(conn, task_id, *args, **kwargs)

    monkeypatch.setattr(kb, "check_respawn_guard", synchronized_guard)
    spawned: list[tuple[str, str]] = []
    errors: list[BaseException] = []
    start = threading.Barrier(2)
    result_lock = threading.Lock()

    def tick(board: str) -> None:
        try:
            with kb.connect_closing(board=board) as conn:
                start.wait(timeout=30)
                result = kb.dispatch_once(
                    conn, board=board, spawn_fn=_spawn_stub(live.pid),
                )
            with result_lock:
                spawned.extend(
                    (board, task_id) for task_id, _, _ in result.spawned
                )
        except BaseException as exc:
            with result_lock:
                errors.append(exc)

    threads = [threading.Thread(target=tick, args=(board,)) for board in task_ids]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    assert not any(thread.is_alive() for thread in threads)
    assert errors == []
    assert len(spawned) == 1
    running = 0
    for board in task_ids:
        with kb.connect_closing(board=board) as conn:
            running += conn.execute(
                "SELECT COUNT(*) FROM tasks "
                "WHERE assignee = 'alpha' AND status = 'running'"
            ).fetchone()[0]
    assert running == 1
