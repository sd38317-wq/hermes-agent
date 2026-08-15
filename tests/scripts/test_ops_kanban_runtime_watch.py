from __future__ import annotations

import contextlib
import io
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from scripts.ops import kanban_runtime_watch as watch


class RuntimeWatchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.db = self.root / "kanban.db"
        self.evidence = self.root / "evidence.jsonl"
        with contextlib.closing(sqlite3.connect(self.db)) as conn:
            conn.executescript(
                """
                CREATE TABLE tasks (
                  id TEXT PRIMARY KEY, title TEXT, assignee TEXT, status TEXT,
                  created_at INTEGER, started_at INTEGER, current_run_id INTEGER,
                  worker_pid INTEGER, last_heartbeat_at INTEGER,
                  block_kind TEXT
                );
                CREATE TABLE task_runs (
                  id INTEGER PRIMARY KEY, task_id TEXT, profile TEXT, status TEXT,
                  worker_pid INTEGER, started_at INTEGER, ended_at INTEGER,
                  last_heartbeat_at INTEGER
                );
                CREATE TABLE task_links (
                  parent_id TEXT NOT NULL, child_id TEXT NOT NULL,
                  PRIMARY KEY (parent_id, child_id)
                );
                CREATE TABLE task_events (
                  id INTEGER PRIMARY KEY, task_id TEXT, kind TEXT,
                  payload TEXT, created_at INTEGER
                );
                """
            )

    def tearDown(self):
        self.tmp.cleanup()

    def run_main(self, *, now: int = 2000):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = watch.main([
                "--db", str(self.db),
                "--evidence", str(self.evidence),
                "--now", str(now),
            ])
        return code, json.loads(output.getvalue())

    def test_zero_input_rows_can_never_pass(self):
        code, result = self.run_main()

        self.assertEqual(1, code)
        self.assertEqual("ERROR", result["status"])
        self.assertEqual(0, result["input_row_count"])
        self.assertGreater(result["query_count"], 0)
        self.assertNotEqual("PASS", result["status"])

    def test_live_research_regression_reconciles_pid_and_heartbeat(self):
        with contextlib.closing(sqlite3.connect(self.db)) as conn:
            conn.execute(
                "INSERT INTO tasks VALUES "
                "('t_adf495b7','research','research','running',1800,1800,268,321,1990,NULL)"
            )
            conn.execute(
                "INSERT INTO task_runs VALUES "
                "(268,'t_adf495b7','research','running',321,1800,NULL,1990)"
            )
            conn.commit()

        with watch.pid_probe(lambda pid: pid == 321):
            code, result = self.run_main()

        self.assertEqual(0, code)
        self.assertEqual("PASS", result["status"])
        self.assertEqual(1, result["input_row_count"])
        self.assertEqual(1, result["pid_reconciliation"]["checked"])
        self.assertEqual(1, result["pid_reconciliation"]["alive"])

    def test_restore_and_three_hour_cards_are_not_false_ready_findings(self):
        with contextlib.closing(sqlite3.connect(self.db)) as conn:
            conn.executemany(
                "INSERT INTO tasks VALUES (?,?,?, 'todo',1800,NULL,NULL,NULL,NULL,NULL)",
                (
                    ("t_a40a0e65", "restore", "dev"),
                    ("t_b965de12", "verify", "productdev"),
                ),
            )
            conn.execute(
                "INSERT INTO tasks VALUES "
                "('parent','parent','dev','blocked',1700,NULL,NULL,NULL,NULL,'dependency')"
            )
            conn.executemany(
                "INSERT INTO task_links VALUES ('parent',?)",
                (("t_a40a0e65",), ("t_b965de12",)),
            )
            conn.commit()

        _, result = self.run_main()

        stale_tasks = {
            finding["task_id"] for finding in result["findings"]
            if finding["kind"] in {"ready_stale", "unassigned_followup"}
        }
        self.assertFalse({"t_a40a0e65", "t_b965de12"} & stale_tasks)

    def test_detects_runtime_and_queue_invariants(self):
        with contextlib.closing(sqlite3.connect(self.db)) as conn:
            conn.executemany(
                "INSERT INTO tasks VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    ("READY", "ready", "dev", "ready", 1800, None, None, None, None, None),
                    ("NO-PID", "running", "research", "running", 1800, 1800, 11, None, 1700, None),
                    ("MISMATCH", "running", "dev", "running", 1800, 1800, 12, 444, 1700, None),
                    ("FOLLOWUP", "follow", None, "todo", 1900, None, None, None, None, None),
                ),
            )
            conn.executemany(
                "INSERT INTO task_runs VALUES (?,?,?,?,?,?,?,?)",
                (
                    (11, "NO-PID", "research", "running", None, 1800, None, 1700),
                    (12, "MISMATCH", "design", "running", 444, 1800, None, 1700),
                ),
            )
            conn.commit()

        with watch.pid_probe(lambda pid: False):
            _, result = self.run_main()

        kinds = {finding["kind"] for finding in result["findings"]}
        self.assertTrue({
            "ready_stale", "running_without_pid", "heartbeat_stale",
            "pid_missing", "role_mismatch", "unassigned_followup",
        } <= kinds)
        self.assertEqual(len(result["findings"]), result["finding_count"])
        self.assertEqual(0, result["action_count"])

    def test_duplicate_current_run_reference_is_reported_for_each_task(self):
        with contextlib.closing(sqlite3.connect(self.db)) as conn:
            conn.executemany(
                "INSERT INTO tasks VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    ("A", "a", "dev", "running", 1800, 1800, 77, 501, 1990, None),
                    ("B", "b", "research", "running", 1800, 1800, 77, 502, 1990, None),
                ),
            )
            conn.executemany(
                "INSERT INTO task_runs VALUES (?,?,?,?,?,?,?,?)",
                (
                    (77, "A", "dev", "running", 501, 1800, None, 1990),
                    (78, "B", "research", "running", 502, 1800, None, 1990),
                ),
            )
            conn.commit()

        with watch.pid_probe(lambda pid: True):
            _, result = self.run_main()

        duplicate_tasks = {
            finding["task_id"] for finding in result["findings"]
            if finding["kind"] == "duplicate_current_run"
        }
        self.assertEqual({"A", "B"}, duplicate_tasks)

    def test_duplicate_worker_pid_is_reported_for_each_task(self):
        with contextlib.closing(sqlite3.connect(self.db)) as conn:
            conn.executemany(
                "INSERT INTO tasks VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    ("A", "a", "dev", "running", 1800, 1800, 70, 500, 1990, None),
                    ("B", "b", "research", "running", 1800, 1800, 71, 500, 1990, None),
                ),
            )
            conn.executemany(
                "INSERT INTO task_runs VALUES (?,?,?,?,?,?,?,?)",
                (
                    (70, "A", "dev", "running", 500, 1800, None, 1990),
                    (71, "B", "research", "running", 500, 1800, None, 1990),
                ),
            )
            conn.commit()

        with watch.pid_probe(lambda pid: True):
            _, result = self.run_main()

        duplicate_tasks = {
            finding["task_id"] for finding in result["findings"]
            if finding["kind"] == "duplicate_pid"
        }
        self.assertEqual({"A", "B"}, duplicate_tasks)

    def test_orphan_active_run_is_reported_even_with_another_healthy_task(self):
        with contextlib.closing(sqlite3.connect(self.db)) as conn:
            conn.execute(
                "INSERT INTO tasks VALUES "
                "('HEALTHY','ok','dev','running',1800,1800,80,600,1990,NULL)"
            )
            conn.executemany(
                "INSERT INTO task_runs VALUES (?,?,?,?,?,?,?,?)",
                (
                    (80, "HEALTHY", "dev", "running", 600, 1800, None, 1990),
                    (81, "DONE-CARD", "research", "running", 601, 1800, None, 1990),
                ),
            )
            conn.commit()

        with watch.pid_probe(lambda pid: True):
            _, result = self.run_main()

        self.assertIn(
            {"kind": "orphan_run", "task_id": "DONE-CARD"},
            [{"kind": item["kind"], "task_id": item["task_id"]}
             for item in result["findings"]],
        )

    def test_runtime_cap_overrun_is_reported(self):
        with contextlib.closing(sqlite3.connect(self.db)) as conn:
            conn.execute("ALTER TABLE tasks ADD COLUMN max_runtime_seconds INTEGER")
            conn.execute(
                "INSERT INTO tasks "
                "(id,title,assignee,status,created_at,started_at,current_run_id,"
                "worker_pid,last_heartbeat_at,max_runtime_seconds) VALUES "
                "('LONG','long','dev','running',1,1,90,700,1990,10)"
            )
            conn.execute(
                "INSERT INTO task_runs VALUES "
                "(90,'LONG','dev','running',700,1,NULL,1990)"
            )
            conn.commit()

        with watch.pid_probe(lambda pid: True):
            _, result = self.run_main()

        self.assertIn("runtime_exceeded", {item["kind"] for item in result["findings"]})

    def test_task_and_run_heartbeat_disagreement_is_reported(self):
        with contextlib.closing(sqlite3.connect(self.db)) as conn:
            conn.execute(
                "INSERT INTO tasks VALUES "
                "('HB','hb','dev','running',1800,1800,91,701,1990,NULL)"
            )
            conn.execute(
                "INSERT INTO task_runs VALUES "
                "(91,'HB','dev','running',701,1800,NULL,1980)"
            )
            conn.commit()

        with watch.pid_probe(lambda pid: True):
            _, result = self.run_main()

        self.assertIn("heartbeat_mismatch", {item["kind"] for item in result["findings"]})

    def test_recent_ready_transition_uses_event_age_not_creation_age(self):
        with contextlib.closing(sqlite3.connect(self.db)) as conn:
            conn.execute(
                "INSERT INTO tasks VALUES "
                "('RECENT','recent','dev','ready',1,NULL,NULL,NULL,NULL,NULL)"
            )
            conn.execute(
                "INSERT INTO task_events VALUES "
                "(1,'RECENT','status','{\"status\":\"ready\"}',1990)"
            )
            conn.commit()

        _, result = self.run_main()

        self.assertNotIn("ready_stale", {item["kind"] for item in result["findings"]})

    def test_cross_profile_waiting_parent_can_block_an_entire_profile(self):
        with contextlib.closing(sqlite3.connect(self.db)) as conn:
            conn.executemany(
                "INSERT INTO tasks VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    ("GATE", "gate", "coordinator", "blocked", 1, None, None, None, None, "dependency"),
                    ("DEV-1", "one", "dev", "todo", 1, None, None, None, None, None),
                    ("DEV-2", "two", "dev", "todo", 1, None, None, None, None, None),
                ),
            )
            conn.executemany(
                "INSERT INTO task_links VALUES ('GATE',?)",
                (("DEV-1",), ("DEV-2",)),
            )
            conn.commit()

        _, result = self.run_main()

        self.assertIn(
            {"kind": "profile_blocked", "task_id": "GATE"},
            [{"kind": item["kind"], "task_id": item["task_id"]}
             for item in result["findings"]],
        )

    def test_human_alert_has_single_action_and_evidence_is_appended(self):
        with contextlib.closing(sqlite3.connect(self.db)) as conn:
            conn.execute(
                "INSERT INTO tasks VALUES "
                "('HUMAN','approval','plan','blocked',1800,NULL,NULL,NULL,NULL,'needs_input')"
            )
            conn.commit()

        _, result = self.run_main()

        self.assertEqual(1, len(result["external_alerts"]))
        alert = result["external_alerts"][0]
        self.assertEqual({"cause", "impact", "minimum_action", "follow_up"}, set(alert))
        self.assertIsInstance(alert["minimum_action"], str)
        evidence_rows = [json.loads(line) for line in self.evidence.read_text().splitlines()]
        self.assertEqual(1, len(evidence_rows))
        self.assertEqual(result["timestamp"], evidence_rows[0]["timestamp"])

    def test_remediation_plan_uses_only_official_tool_calls(self):
        plan = watch.build_remediation_plan([
            {"kind": "ready_stale", "task_id": "R1", "human_only": False},
            {"kind": "needs_input", "task_id": "B1", "human_only": True},
        ])

        self.assertEqual(["kanban_comment", "kanban_comment"], [step["tool"] for step in plan])
        self.assertNotIn("kanban_block", [step["tool"] for step in plan])
        self.assertTrue(all("sql" not in json.dumps(step).lower() for step in plan))


if __name__ == "__main__":
    unittest.main()