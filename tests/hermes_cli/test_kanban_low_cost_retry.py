from __future__ import annotations

import sys

import pytest


@pytest.fixture()
def isolated_kanban(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    for name in list(sys.modules):
        if name.startswith("hermes_cli") or name.startswith("hermes_state") or name == "hermes_constants":
            del sys.modules[name]
    from hermes_cli import kanban_db as kb

    yield kb


def test_first_failed_attempt_uses_ephemeral_low_cost_retry_model(isolated_kanban):
    kb = isolated_kanban
    seen = []

    def spawn(task, workspace):
        seen.append(
            (task.model_override, task.provider_override, task.reasoning_effort)
        )
        return 12345

    with kb.connect_closing() as conn:
        task_id = kb.create_task(conn, title="retry cheaply", assignee="default")
        conn.execute(
            "UPDATE tasks SET consecutive_failures = 1 WHERE id = ?",
            (task_id,),
        )
        conn.commit()
        result = kb.dispatch_once(
            conn,
            spawn_fn=spawn,
            retry_model="gpt-5.4-mini",
            retry_provider="openai-codex",
            retry_reasoning_effort="low",
        )
        persisted = kb.get_task(conn, task_id)

    assert len(result.spawned) == 1
    assert seen == [("gpt-5.4-mini", "openai-codex", "low")]
    assert persisted is not None
    assert persisted.model_override is None
    assert persisted.provider_override is None
    assert persisted.reasoning_effort is None


def test_low_cost_retry_settings_are_explicitly_opt_in():
    from hermes_cli.config_defaults import DEFAULT_CONFIG

    kanban = DEFAULT_CONFIG["kanban"]
    assert kanban["retry_model"] == ""
    assert kanban["retry_provider"] == ""
    assert kanban["retry_reasoning_effort"] == "low"


def test_dispatch_entry_points_inherit_retry_settings_from_config(
    isolated_kanban, monkeypatch
):
    kb = isolated_kanban
    seen = []
    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {
            "kanban": {
                "retry_model": "gpt-5.4-mini",
                "retry_provider": "openai-codex",
                "retry_reasoning_effort": "low",
            }
        },
    )

    with kb.connect_closing() as conn:
        task_id = kb.create_task(conn, title="retry from config", assignee="default")
        conn.execute(
            "UPDATE tasks SET consecutive_failures = 1 WHERE id = ?",
            (task_id,),
        )
        conn.commit()
        kb.dispatch_once(
            conn,
            spawn_fn=lambda task, workspace: seen.append(
                (task.model_override, task.provider_override, task.reasoning_effort)
            ) or 12345,
        )

    assert seen == [("gpt-5.4-mini", "openai-codex", "low")]
