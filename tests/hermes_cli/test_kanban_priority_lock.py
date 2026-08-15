"""Representative-designated priority-lock behavior for Kanban dispatch."""
from __future__ import annotations

import sqlite3
import threading
from argparse import Namespace
from pathlib import Path

import pytest
from hermes_cli import kanban_db as kb


@pytest.fixture()
def isolated_kanban_home_with_profiles(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    for profile in ("dev", "research", "default"):
        (home / "profiles" / profile).mkdir(parents=True)
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()

    yield kb

    kb._INITIALIZED_PATHS.clear()


def _spawn(*_args, **_kwargs):
    return 4242


def test_designated_task_precedes_later_higher_priority_same_profile(
    isolated_kanban_home_with_profiles,
):
    kb = isolated_kanban_home_with_profiles
    with kb.connect_closing() as conn:
        designated = kb.create_task(
            conn, title="representative priority", assignee="dev", priority=10
        )
        kb.set_dispatch_priority_lock(
            conn, designated, designated_by="representative"
        )
        urgent = kb.create_task(
            conn, title="automated urgent", assignee="dev", priority=999_999
        )
        independent = kb.create_task(
            conn, title="independent research", assignee="research", priority=1
        )

        result = kb.dispatch_once(conn, dry_run=True, max_in_progress_per_profile=1)

    spawned = [task_id for task_id, _assignee, _workspace in result.spawned]
    assert designated in spawned
    assert independent in spawned
    assert urgent not in spawned
    assert (urgent, "dev", designated) in result.skipped_priority_locked


def test_only_explicit_redesignation_replaces_profile_lock(
    isolated_kanban_home_with_profiles,
):
    kb = isolated_kanban_home_with_profiles
    with kb.connect_closing() as conn:
        first = kb.create_task(conn, title="first", assignee="dev", priority=1)
        second = kb.create_task(conn, title="second", assignee="dev", priority=2)
        kb.set_dispatch_priority_lock(conn, first, designated_by="representative")

        lock = kb.set_dispatch_priority_lock(
            conn, second, designated_by="representative"
        )

        assert lock["assignee"] == "dev"
        assert lock["task_id"] == second
        events = kb.list_events(conn, first)
        assert any(event.kind == "priority_lock_replaced" for event in events)


def test_redesignation_does_not_interrupt_running_task_before_checkpoint(
    isolated_kanban_home_with_profiles,
):
    kb = isolated_kanban_home_with_profiles
    with kb.connect_closing() as conn:
        first = kb.create_task(conn, title="first", assignee="dev", priority=1)
        second = kb.create_task(conn, title="second", assignee="dev", priority=2)
        kb.set_dispatch_priority_lock(conn, first, designated_by="representative")
        first_tick = kb.dispatch_once(
            conn, spawn_fn=_spawn, max_in_progress_per_profile=1
        )
        assert [item[0] for item in first_tick.spawned] == [first]

        kb.set_dispatch_priority_lock(conn, second, designated_by="representative")
        while_running = kb.dispatch_once(
            conn, dry_run=True, max_in_progress_per_profile=1
        )
        assert not while_running.spawned
        assert kb.get_task(conn, first).status == "running"

        assert kb.complete_task(conn, first, summary="safe checkpoint")
        after_checkpoint = kb.dispatch_once(
            conn, dry_run=True, max_in_progress_per_profile=1
        )
        assert [item[0] for item in after_checkpoint.spawned] == [second]


def test_safe_checkpoint_does_not_depend_on_optional_profile_cap(
    isolated_kanban_home_with_profiles,
):
    kb = isolated_kanban_home_with_profiles
    with kb.connect_closing() as conn:
        first = kb.create_task(conn, title="first", assignee="dev")
        second = kb.create_task(conn, title="second", assignee="dev")
        kb.set_dispatch_priority_lock(conn, first, designated_by="representative")
        started = kb.dispatch_once(conn, spawn_fn=_spawn)
        assert [item[0] for item in started.spawned] == [first]

        kb.set_dispatch_priority_lock(conn, second, designated_by="representative")
        waiting = kb.dispatch_once(conn, dry_run=True)

        assert not waiting.spawned
        assert (second, "dev", first) in waiting.skipped_priority_checkpoint


def test_terminal_lock_target_releases_queue_automatically(
    isolated_kanban_home_with_profiles,
):
    kb = isolated_kanban_home_with_profiles
    with kb.connect_closing() as conn:
        designated = kb.create_task(conn, title="locked", assignee="dev", priority=1)
        queued = kb.create_task(conn, title="queued", assignee="dev", priority=2)
        kb.set_dispatch_priority_lock(
            conn, designated, designated_by="representative"
        )
        assert kb.claim_task(conn, designated) is not None
        assert kb.complete_task(conn, designated, summary="done")

        result = kb.dispatch_once(conn, dry_run=True, max_in_progress_per_profile=1)

    assert [item[0] for item in result.spawned] == [queued]


def test_nonterminal_dependency_demotion_does_not_release_lock(
    isolated_kanban_home_with_profiles,
):
    kb = isolated_kanban_home_with_profiles
    with kb.connect_closing() as conn:
        parent = kb.create_task(conn, title="unfinished parent", assignee="research")
        designated = kb.create_task(conn, title="locked", assignee="dev")
        urgent = kb.create_task(
            conn, title="automated urgent", assignee="dev", priority=999_999
        )
        kb.set_dispatch_priority_lock(
            conn, designated, designated_by="representative"
        )
        kb.link_tasks(conn, parent, designated)
        assert kb.get_task(conn, designated).status == "todo"

        result = kb.dispatch_once(conn, dry_run=True)

        assert urgent not in [task_id for task_id, _, _ in result.spawned]
        assert (urgent, "dev", designated) in result.skipped_priority_locked


def test_terminal_lock_record_does_not_block_later_reassignment(
    isolated_kanban_home_with_profiles,
):
    kb = isolated_kanban_home_with_profiles
    with kb.connect_closing() as conn:
        designated = kb.create_task(conn, title="locked", assignee="dev")
        kb.set_dispatch_priority_lock(
            conn, designated, designated_by="representative"
        )
        assert kb.claim_task(conn, designated) is not None
        kb.complete_task(conn, designated, summary="checkpoint reached")

        assert kb.assign_task(conn, designated, "research") is True
        assert kb.get_task(conn, designated).assignee == "research"


def test_priority_lock_also_defers_review_lane_for_same_profile(
    isolated_kanban_home_with_profiles,
):
    kb = isolated_kanban_home_with_profiles
    with kb.connect_closing() as conn:
        designated = kb.create_task(conn, title="locked", assignee="dev")
        review = kb.create_task(conn, title="review", assignee="dev")
        claimed = kb.claim_task(conn, review)
        assert claimed is not None
        assert kb.request_review(
            conn,
            review,
            summary="ready for review",
            expected_run_id=claimed.current_run_id,
        )
        kb.set_dispatch_priority_lock(
            conn, designated, designated_by="representative"
        )

        result = kb.dispatch_once(conn, dry_run=True, max_in_progress_per_profile=2)

    assert [item[0] for item in result.spawned] == [designated]
    assert (review, "dev", designated) in result.skipped_priority_locked


def test_claim_boundary_rejects_non_designated_task_after_dispatch_snapshot(
    isolated_kanban_home_with_profiles,
):
    kb = isolated_kanban_home_with_profiles
    with kb.connect_closing() as conn:
        designated = kb.create_task(conn, title="locked", assignee="dev")
        contender = kb.create_task(
            conn, title="late urgent", assignee="dev", priority=999_999
        )
        kb.set_dispatch_priority_lock(
            conn, designated, designated_by="representative"
        )

        assert kb.claim_task(conn, contender) is None
        rejection = [
            event
            for event in kb.list_events(conn, contender)
            if event.kind == "claim_rejected"
        ][-1]
        assert rejection.payload["reason"] == "priority_locked"
        assert rejection.payload["designated_task_id"] == designated


def test_review_claim_boundary_rejects_non_designated_task(
    isolated_kanban_home_with_profiles,
):
    kb = isolated_kanban_home_with_profiles
    with kb.connect_closing() as conn:
        designated = kb.create_task(conn, title="locked", assignee="dev")
        review = kb.create_task(conn, title="review", assignee="dev")
        claimed = kb.claim_task(conn, review)
        assert claimed is not None
        assert kb.request_review(
            conn,
            review,
            summary="ready",
            expected_run_id=claimed.current_run_id,
        )
        kb.set_dispatch_priority_lock(
            conn, designated, designated_by="representative"
        )

        assert kb.claim_review_task(conn, review) is None
        rejection = [
            event
            for event in kb.list_events(conn, review)
            if event.kind == "claim_rejected"
        ][-1]
        assert rejection.payload["reason"] == "priority_locked"


def test_concurrent_claim_and_designation_have_serializable_order(
    isolated_kanban_home_with_profiles,
):
    kb = isolated_kanban_home_with_profiles
    with kb.connect_closing() as conn:
        designated = kb.create_task(conn, title="locked", assignee="dev")
        contender = kb.create_task(conn, title="contender", assignee="dev")

    barrier = threading.Barrier(2)
    claimed: list[bool] = []

    def designate() -> None:
        with kb.connect_closing() as conn:
            barrier.wait()
            kb.set_dispatch_priority_lock(
                conn, designated, designated_by="representative"
            )

    def claim() -> None:
        with kb.connect_closing() as conn:
            barrier.wait()
            claimed.append(kb.claim_task(conn, contender) is not None)

    threads = [threading.Thread(target=designate), threading.Thread(target=claim)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)
        assert not thread.is_alive()

    with kb.connect_closing() as conn:
        events = kb.list_events(conn, contender)
        claimed_events = [event for event in events if event.kind == "claimed"]
        rejected_events = [
            event
            for event in events
            if event.kind == "claim_rejected"
            and event.payload.get("reason") == "priority_locked"
        ]
        lock_event = [
            event
            for event in kb.list_events(conn, designated)
            if event.kind == "priority_lock_designated"
        ][-1]

    if claimed == [True]:
        assert claimed_events[-1].id < lock_event.id
    else:
        assert claimed == [False]
        assert lock_event.id < rejected_events[-1].id


def test_assignment_cannot_silently_clear_representative_designation(
    isolated_kanban_home_with_profiles,
):
    kb = isolated_kanban_home_with_profiles
    with kb.connect_closing() as conn:
        designated = kb.create_task(conn, title="locked", assignee="dev")
        kb.set_dispatch_priority_lock(
            conn, designated, designated_by="representative"
        )

        with pytest.raises(RuntimeError, match="priority lock"):
            kb.assign_task(conn, designated, "research")

        assert kb.active_dispatch_priority_locks(conn) == {"dev": designated}


def test_review_routing_cannot_silently_clear_representative_designation(
    isolated_kanban_home_with_profiles,
):
    kb = isolated_kanban_home_with_profiles
    with kb.connect_closing() as conn:
        designated = kb.create_task(conn, title="locked", assignee="dev")
        kb.set_dispatch_priority_lock(
            conn, designated, designated_by="representative"
        )

        ok, reason = kb.request_review(
            conn,
            designated,
            reviewer="research",
            force=True,
            with_reason=True,
        )

        assert ok is False
        assert reason is not None
        assert "representative priority lock is active" in reason
        task = kb.get_task(conn, designated)
        assert task.status == "ready"
        assert task.assignee == "dev"
        assert kb.active_dispatch_priority_locks(conn) == {"dev": designated}


def test_cli_assign_reports_priority_lock_conflict_without_traceback(
    isolated_kanban_home_with_profiles,
    capsys,
):
    kb = isolated_kanban_home_with_profiles
    from hermes_cli import kanban as kb_cli

    with kb.connect_closing() as conn:
        designated = kb.create_task(conn, title="locked", assignee="dev")
        kb.set_dispatch_priority_lock(
            conn, designated, designated_by="representative"
        )

    rc = getattr(kb_cli, "_cmd_assign")(
        Namespace(task_id=designated, profile="research")
    )

    captured = capsys.readouterr()
    assert rc == 2
    assert captured.out == ""
    assert "representative priority lock is active" in captured.err


def test_explicit_reclaim_and_reassign_releases_stuck_designation(
    isolated_kanban_home_with_profiles,
):
    kb = isolated_kanban_home_with_profiles
    with kb.connect_closing() as conn:
        designated = kb.create_task(conn, title="stuck", assignee="dev")
        kb.set_dispatch_priority_lock(
            conn, designated, designated_by="representative"
        )
        assert kb.claim_task(conn, designated) is not None

        assert kb.reassign_task(
            conn,
            designated,
            "research",
            reclaim_first=True,
            reason="representative recovery",
        )

        assert kb.get_task(conn, designated).assignee == "research"
        assert kb.active_dispatch_priority_locks(conn) == {}
        lock = conn.execute(
            "SELECT 1 FROM dispatch_priority_locks WHERE task_id = ?",
            (designated,),
        ).fetchone()
        assert lock is None


def test_task_deletion_cleans_retained_designation_records(
    isolated_kanban_home_with_profiles,
):
    kb = isolated_kanban_home_with_profiles
    with kb.connect_closing() as conn:
        archived = kb.create_task(conn, title="archived", assignee="dev")
        kb.set_dispatch_priority_lock(
            conn, archived, designated_by="representative"
        )
        assert kb.archive_task(conn, archived)
        assert kb.delete_archived_task(conn, archived)

        hard_deleted = kb.create_task(conn, title="hard", assignee="research")
        kb.set_dispatch_priority_lock(
            conn, hard_deleted, designated_by="representative"
        )
        assert kb.delete_task(conn, hard_deleted)

        count = conn.execute(
            "SELECT COUNT(*) FROM dispatch_priority_locks"
        ).fetchone()[0]
        assert count == 0


def test_cli_priority_lock_uses_authenticated_profile_as_audit_actor(
    isolated_kanban_home_with_profiles,
    capsys,
    monkeypatch,
):
    kb = isolated_kanban_home_with_profiles
    from hermes_cli import kanban as kb_cli
    from hermes_cli import profiles

    with kb.connect_closing() as conn:
        task_id = kb.create_task(conn, title="target", assignee="dev")

    monkeypatch.setenv("HERMES_PROFILE_NAME", "spoofed-caller-text")
    monkeypatch.setattr(
        profiles, "get_active_profile_name", lambda: "team-representative"
    )
    ok = getattr(kb_cli, "_cmd_priority_lock")(
        Namespace(task_id=task_id, json=False)
    )
    assert ok == 0
    assert task_id in capsys.readouterr().out
    with kb.connect_closing() as conn:
        assert kb.active_dispatch_priority_locks(conn) == {"dev": task_id}
        lock = conn.execute(
            "SELECT designated_by FROM dispatch_priority_locks WHERE task_id = ?",
            (task_id,),
        ).fetchone()
        assert lock["designated_by"] == "team-representative"


def test_legacy_database_upgrade_adds_priority_lock_table(tmp_path, monkeypatch):
    from hermes_cli import kanban_db as kb

    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    db_path = kb.kanban_db_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    legacy = sqlite3.connect(db_path)
    legacy.execute(
        """
        CREATE TABLE tasks (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            body TEXT,
            assignee TEXT,
            status TEXT NOT NULL DEFAULT 'ready',
            priority INTEGER NOT NULL DEFAULT 0,
            created_by TEXT,
            created_at INTEGER NOT NULL,
            started_at INTEGER,
            completed_at INTEGER,
            workspace_kind TEXT NOT NULL DEFAULT 'scratch',
            workspace_path TEXT,
            claim_lock TEXT,
            claim_expires INTEGER
        )
        """
    )
    legacy.execute(
        "INSERT INTO tasks "
        "(id, title, assignee, status, priority, created_at, workspace_kind) "
        "VALUES ('legacy1', 'existing', 'dev', 'ready', 0, 1, 'scratch')"
    )
    legacy.commit()
    legacy.close()

    kb.init_db()

    with kb.connect_closing() as conn:
        existing = kb.get_task(conn, "legacy1")
        assert existing is not None
        assert existing.title == "existing"
        table = conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type = 'table' AND name = 'dispatch_priority_locks'"
        ).fetchone()
        assert table["name"] == "dispatch_priority_locks"


def test_delegated_child_cli_guard_rejects_priority_designation(
    isolated_kanban_home_with_profiles,
    monkeypatch,
):
    del isolated_kanban_home_with_profiles
    from hermes_cli import kanban as kb_cli

    monkeypatch.setenv("HERMES_DELEGATED_CHILD_CONTEXT", "1")
    guarded = getattr(kb_cli, "_is_delegated_child_cli_mutation")(
        Namespace(kanban_action="priority-lock")
    )
    assert guarded is True
