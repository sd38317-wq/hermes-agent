from __future__ import annotations

import contextlib
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent import kanban_tool_ledger as ledger


class KanbanToolLedgerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / "board.db"
        with contextlib.closing(sqlite3.connect(self.db)) as conn:
            conn.execute(
                "CREATE TABLE task_events (id INTEGER PRIMARY KEY, task_id TEXT, run_id TEXT, "
                "kind TEXT, payload TEXT, created_at INTEGER)"
            )

    def tearDown(self):
        self.temp.cleanup()

    def env(self, **extra):
        values = {
            "HERMES_KANBAN_TASK": "T1", "HERMES_KANBAN_RUN_ID": "R1",
            "HERMES_KANBAN_DB": str(self.db), **extra,
        }
        return mock.patch.dict(os.environ, values, clear=False)

    def test_records_only_bounded_metadata_for_dispatcher_worker(self):
        with self.env():
            ledger.record("tool_started", "terminal", "raw-call-id")
            ledger.record("tool_completed", "terminal", "raw-call-id", status="success", duration_ms=10**12)
        with contextlib.closing(sqlite3.connect(self.db)) as conn:
            rows = conn.execute("SELECT task_id,run_id,kind,payload FROM task_events ORDER BY id").fetchall()
        self.assertEqual(["tool_started", "tool_completed"], [row[2] for row in rows])
        payload = json.loads(rows[1][3])
        self.assertEqual({
            "tool": "terminal", "call_id": payload["call_id"],
            "status": "success", "duration_ms": 86_400_000,
        }, payload)
        self.assertNotIn("raw-call-id", rows[0][3])

    def test_delegate_and_cron_contexts_do_not_write(self):
        with self.env():
            with mock.patch(
                "agent.delegation_context.is_dispatcher_owned_worker_context",
                return_value=False,
            ):
                ledger.record("tool_started", "terminal", "one")
                ledger.record("tool_started", "terminal", "two")
        with contextlib.closing(sqlite3.connect(self.db)) as conn:
            self.assertEqual(0, conn.execute("SELECT COUNT(*) FROM task_events").fetchone()[0])

    def test_ledger_failure_never_raises_and_is_observable(self):
        missing = Path(self.temp.name) / "missing" / "db"
        home = Path(self.temp.name) / "hermes-home"
        with self.assertLogs("agent.kanban_tool_ledger", level="WARNING") as logs:
            with self.env(HERMES_KANBAN_DB=str(missing), HERMES_KANBAN_ROOT=str(home)):
                ledger.record("tool_started", "terminal", "one")
                ledger.record("tool_started", "terminal", "two")
        self.assertIn("coverage write failed", "\n".join(logs.output))
        records = [json.loads(line) for line in (
            home / "cron" / "evidence" / "kanban-tool-ledger-incidents.jsonl"
        ).read_text().splitlines()]
        self.assertEqual(2, len(records))
        rendered = json.dumps(records)
        for forbidden in ("T1", "R1", "terminal", "one", "two", str(missing)):
            self.assertNotIn(forbidden, rendered)
        self.assertEqual(
            {"version", "kind", "created_at"}, set(records[0]),
        )

    def test_result_status_parses_structured_success_and_failures(self):
        successes = [
            {"error": None}, {"last_delivery_error": None}, {"exit_code": 0},
            '{"error":null,"last_delivery_error":null,"exit_code":0}',
        ]
        failures = [
            {"error": "boom"}, {"last_delivery_error": "boom"},
            {"exit_code": 2}, {"status": "failed"},
        ]
        for value in successes:
            with self.subTest(value=value):
                self.assertEqual("success", ledger.result_status(value))
        for value in failures:
            with self.subTest(value=value):
                self.assertEqual("error", ledger.result_status(value))


if __name__ == "__main__":
    unittest.main()
