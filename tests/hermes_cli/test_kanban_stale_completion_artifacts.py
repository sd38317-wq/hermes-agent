"""Completion must not accept a deliverable that predates the card.

Incident t_de97965e: after a crash, a cheap retry "completed" the card by
attaching yesterday's ``ep03_30s_final.mp4`` — a file that existed before
the card was created — instead of the required (never-produced) render.
``complete_task`` must reject declared artifacts whose mtime is older than
``tasks.created_at``.
"""
from __future__ import annotations

import os
import sys
import time

import pytest


@pytest.fixture()
def isolated_kanban(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    for name in list(sys.modules):
        if name.startswith("hermes_cli") or name.startswith("hermes_state") or name == "hermes_constants":
            del sys.modules[name]
    from hermes_cli import kanban_db as kb

    yield kb


def test_completion_rejects_artifact_older_than_card(isolated_kanban, tmp_path):
    kb = isolated_kanban
    stale = tmp_path / "ep03_30s_final.mp4"
    stale.write_bytes(b"yesterday's render")
    day_ago = time.time() - 86400
    os.utime(stale, (day_ago, day_ago))

    with kb.connect_closing() as conn:
        task_id = kb.create_task(conn, title="ep03 v4 render", assignee="default")
        with pytest.raises(kb.StaleArtifactsError) as excinfo:
            kb.complete_task(
                conn, task_id,
                summary="attached final render",
                metadata={"artifacts": [str(stale)]},
            )
        assert str(stale) in excinfo.value.stale

        # No state change: the card is still open and can be completed
        # properly once a real deliverable exists.
        task = kb.get_task(conn, task_id)
        assert task.status != "done"

        # Rejection is auditable.
        kinds = {
            row["kind"]
            for row in conn.execute(
                "SELECT kind FROM task_events WHERE task_id = ?", (task_id,)
            ).fetchall()
        }
        assert "completion_blocked_stale_artifacts" in kinds


def test_completion_accepts_artifact_created_after_card(isolated_kanban, tmp_path):
    kb = isolated_kanban

    with kb.connect_closing() as conn:
        task_id = kb.create_task(conn, title="ep03 v4 render", assignee="default")
        fresh = tmp_path / "ep03_v4_30s.mp4"
        fresh.write_bytes(b"new render for this card")
        ok = kb.complete_task(
            conn, task_id,
            summary="attached final render",
            metadata={"artifacts": [str(fresh)]},
        )
        assert ok is True
        task = kb.get_task(conn, task_id)
        assert task.status == "done"
