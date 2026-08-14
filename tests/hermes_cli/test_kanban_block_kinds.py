"""Tests for typed block reasons + the unblock-loop breaker.

Covers the built-in fix for the kanban "blocked loop" — a worker blocks a
task, a cron unblocks it, the worker re-blocks for the same reason, repeat
forever. The fix gives ``block_task`` a typed ``kind`` and a persistent
``block_recurrences`` counter:

* ``dependency`` blocks route to ``todo`` (parent-gated, auto-resumed) and
  never enter the human ``blocked`` bucket a cron would keep unblocking.
* ``needs_input`` / ``capability`` / un-typed blocks land in ``blocked``;
  each same-cause re-block after an unblock increments ``block_recurrences``,
  and at ``BLOCK_RECURRENCE_LIMIT`` the task routes to ``triage`` for a human.
* ``transient`` closes the attempt and returns to the waiting pool, while
  retaining the same recurrence breaker and parent gating.
* ``unblock_task`` deliberately does NOT reset ``block_recurrences`` (the
  amnesia that let the loop run unbounded).
* A successful ``complete_task`` resets the loop memory.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _running_task(conn, title="t"):
    """Create a task and drive it to ``running`` so block_task can act."""
    tid = kb.create_task(conn, title=title, assignee="worker")
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (tid,))
    claimed = kb.claim_task(conn, tid, claimer="worker")
    assert claimed is not None
    return tid


def _make_running_again(conn, tid):
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (tid,))
    assert kb.claim_task(conn, tid, claimer="worker") is not None


# ---------------------------------------------------------------------------
# Loop breaker
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("kind", [None, "needs_input", "capability"])
def test_human_block_kinds_remain_sticky(kanban_home: Path, kind: str | None) -> None:
    with kb.connect_closing() as conn:
        tid = _running_task(conn)
        assert kb.block_task(conn, tid, reason="human needed", kind=kind)
        assert kb.get_task(conn, tid).status == "blocked"
        assert kb.recompute_ready(conn) == 0
        assert kb.get_task(conn, tid).status == "blocked"


def test_repeated_transient_blocks_triage_instead_of_hot_looping(
    kanban_home: Path,
) -> None:
    with kb.connect_closing() as conn:
        tid = _running_task(conn)
        assert kb.block_task(conn, tid, reason="temporary", kind="transient")
        assert kb.get_task(conn, tid).status == "ready"
        assert kb.claim_task(conn, tid, claimer="worker") is not None

        assert kb.block_task(conn, tid, reason="temporary", kind="transient")
        task = kb.get_task(conn, tid)
        assert task.status == "triage"
        assert task.block_recurrences == kb.BLOCK_RECURRENCE_LIMIT


def test_transient_block_with_unfinished_parent_stays_gated(
    kanban_home: Path,
) -> None:
    with kb.connect_closing() as conn:
        parent = kb.create_task(conn, title="parent", assignee="worker")
        child = _running_task(conn, title="child")
        kb.link_tasks(conn, parent_id=parent, child_id=child)

        assert kb.block_task(conn, child, reason="retry later", kind="transient")
        assert kb.get_task(conn, child).status == "todo"
        assert kb.recompute_ready(conn) == 0
        assert kb.get_task(conn, child).status == "todo"


def test_legacy_sticky_transient_block_recovers_on_tick(
    kanban_home: Path,
) -> None:
    """Rows written before transient blocks requeued directly must self-heal."""
    with kb.connect_closing() as conn:
        tid = _running_task(conn)
        assert kb.block_task(conn, tid, reason="temporary", kind="needs_input")
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET block_kind = 'transient' WHERE id = ?",
                (tid,),
            )

        assert kb.get_task(conn, tid).status == "blocked"
        assert kb.recompute_ready(conn) == 1
        assert kb.get_task(conn, tid).status == "ready"










def test_block_loop_detected_event_emitted(kanban_home: Path) -> None:
    with kb.connect_closing() as conn:
        tid = _running_task(conn)
        kb.block_task(conn, tid, reason="x", kind="capability")
        kb.unblock_task(conn, tid)
        _make_running_again(conn, tid)
        kb.block_task(conn, tid, reason="x", kind="capability")
        events = [e for e in kb.list_events(conn, tid)
                  if e.kind == "block_loop_detected"]
        assert events, "expected a block_loop_detected event"
        payload = events[-1].payload or {}
        assert payload.get("recurrences") == 2
        assert payload.get("kind") == "capability"


# ---------------------------------------------------------------------------
# Dependency routing
# ---------------------------------------------------------------------------


def test_dependency_then_parent_done_promotes(kanban_home: Path) -> None:
    """A dependency-parked child becomes ready once its parent completes."""
    with kb.connect_closing() as conn:
        parent = kb.create_task(conn, title="parent", assignee="worker")
        child = _running_task(conn, title="child")
        kb.link_tasks(conn, parent_id=parent, child_id=child)
        kb.block_task(conn, child, reason="wait", kind="dependency")
        assert kb.get_task(conn, child).status == "todo"
        # Finish the parent, then let recompute_ready run.
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (parent,))
        kb.claim_task(conn, parent, claimer="worker")
        kb.complete_task(conn, parent, result="done")
        kb.recompute_ready(conn)
        assert kb.get_task(conn, child).status == "ready"


# ---------------------------------------------------------------------------
# Completion resets loop memory
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Validation + back-compat
# ---------------------------------------------------------------------------
