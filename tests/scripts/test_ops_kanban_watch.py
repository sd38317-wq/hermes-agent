from __future__ import annotations

import contextlib
import io
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts.ops import kanban_exception_watch as watch


class WatcherTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.db = root / "kanban.db"
        self.state = root / "watch.json"
        self.lock = root / "watch.lock"
        with contextlib.closing(sqlite3.connect(self.db)) as conn:
            conn.executescript(
                """
                CREATE TABLE tasks (
                  id TEXT PRIMARY KEY, title TEXT, assignee TEXT, status TEXT,
                  created_at INTEGER, started_at INTEGER, completed_at INTEGER,
                  claim_expires INTEGER,
                  worker_pid INTEGER, last_heartbeat_at INTEGER,
                  current_run_id INTEGER, block_kind TEXT, due_at INTEGER
                );
                CREATE TABLE task_runs (
                  id INTEGER PRIMARY KEY, task_id TEXT, status TEXT,
                  worker_pid INTEGER, started_at INTEGER, ended_at INTEGER,
                  last_heartbeat_at INTEGER, claim_expires INTEGER
                );
                CREATE TABLE task_links (
                  parent_id TEXT NOT NULL, child_id TEXT NOT NULL,
                  PRIMARY KEY (parent_id, child_id)
                );
                CREATE TABLE task_events (
                  id INTEGER PRIMARY KEY, task_id TEXT, run_id INTEGER,
                  kind TEXT, payload TEXT, created_at INTEGER
                );
                CREATE TABLE kanban_notify_subs (
                  task_id TEXT, platform TEXT, chat_id TEXT, thread_id TEXT,
                  last_event_id INTEGER, delivered_event_id INTEGER,
                  delivery_mode TEXT
                );
                """
            )
            conn.commit()

    def tearDown(self):
        self.tmp.cleanup()

    def run_main(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = watch.main([
                "--db", str(self.db), "--state", str(self.state),
                "--lock", str(self.lock), "--now", "2000",
            ])
        return code, out.getvalue()

    def test_healthy_is_silent(self):
        code, output = self.run_main()
        self.assertEqual(0, code)
        self.assertEqual("", output)

    def test_new_once_identical_silent_changed_reemits(self):
        with contextlib.closing(sqlite3.connect(self.db)) as conn:
            conn.execute("INSERT INTO tasks (id,title,status,created_at,block_kind) VALUES ('T1','secret body is not title','blocked',1,'human')")
            conn.commit()
        code, first = self.run_main()
        self.assertEqual(0, code)
        self.assertEqual(1, len(first.splitlines()))
        self.assertNotIn("secret body", first)
        self.assertEqual("", self.run_main()[1])
        with contextlib.closing(sqlite3.connect(self.db)) as conn:
            conn.execute("UPDATE tasks SET due_at=1000 WHERE id='T1'")
            conn.commit()
        self.assertEqual(1, len(self.run_main()[1].splitlines()))

    def test_card_run_and_heartbeat_mismatch(self):
        with contextlib.closing(sqlite3.connect(self.db)) as conn:
            conn.execute("INSERT INTO tasks (id,title,status,created_at,current_run_id,last_heartbeat_at) VALUES ('T2','x','running',1,7,1900)")
            conn.execute("INSERT INTO task_runs (id,task_id,status,started_at,last_heartbeat_at) VALUES (8,'T2','running',1,100)")
            conn.commit()
        with contextlib.closing(watch._read_db(self.db)) as conn:
            kinds = {x["kind"] for x in watch.collect_exceptions(conn, 2000)}
        self.assertTrue({"run_card", "heartbeat"} <= kinds)

    def test_task_and_run_pid_mismatch(self):
        with contextlib.closing(sqlite3.connect(self.db)) as conn:
            conn.execute("INSERT INTO tasks (id,title,status,created_at,current_run_id,last_heartbeat_at,worker_pid) VALUES ('T3','x','running',1,9,1900,111)")
            conn.execute("INSERT INTO task_runs (id,task_id,status,started_at,last_heartbeat_at,worker_pid) VALUES (9,'T3','running',1,1900,222)")
            conn.commit()
        with contextlib.closing(watch._read_db(self.db)) as conn:
            kinds = {x["kind"] for x in watch.collect_exceptions(conn, 2000)}
        self.assertIn("run_card", kinds)

    def test_dead_worker_pid_is_run_card_exception(self):
        with contextlib.closing(sqlite3.connect(self.db)) as conn:
            conn.execute("INSERT INTO tasks (id,title,status,created_at,current_run_id,last_heartbeat_at,worker_pid) VALUES ('T4','x','running',1,10,1900,123)")
            conn.execute("INSERT INTO task_runs (id,task_id,status,started_at,last_heartbeat_at,worker_pid) VALUES (10,'T4','running',1,1900,123)")
            conn.commit()
        with contextlib.closing(watch._read_db(self.db)) as conn, \
                mock.patch("scripts.ops.kanban_exception_watch.os.kill", side_effect=ProcessLookupError):
            kinds = {x["kind"] for x in watch.collect_exceptions(conn, 2000)}
        self.assertIn("run_card", kinds)

    def test_worker_birth_identity_mismatch_is_run_card_exception(self):
        with contextlib.closing(sqlite3.connect(self.db)) as conn:
            conn.execute("ALTER TABLE task_runs ADD COLUMN worker_birth_identity TEXT")
            conn.execute("INSERT INTO tasks (id,title,status,created_at,current_run_id,worker_pid) VALUES ('T5','x','running',1,11,123)")
            conn.execute("INSERT INTO task_runs (id,task_id,status,started_at,worker_pid,worker_birth_identity) VALUES (11,'T5','running',1,123,'linux:boot:old')")
            conn.commit()
        with contextlib.closing(watch._read_db(self.db)) as conn, \
                mock.patch.object(watch, "_pid_alive", return_value=True), \
                mock.patch.object(watch, "_process_birth_identity", return_value="linux:boot:new"):
            kinds = {x["kind"] for x in watch.collect_exceptions(conn, 2000)}
        self.assertIn("run_card", kinds)

    def test_supported_start_tick_columns_detect_pid_reuse(self):
        with contextlib.closing(sqlite3.connect(self.db)) as conn:
            conn.execute("ALTER TABLE task_runs ADD COLUMN process_start_ticks INTEGER")
            conn.execute("ALTER TABLE task_runs ADD COLUMN worker_start_ticks INTEGER")
            for run_id, column in ((12, "process_start_ticks"), (13, "worker_start_ticks")):
                task_id = f"T{run_id}"
                conn.execute(
                    "INSERT INTO tasks (id,title,status,created_at,current_run_id,worker_pid) VALUES (?,?,?,?,?,?)",
                    (task_id, "x", "running", 1, run_id, 123),
                )
                conn.execute(
                    f"INSERT INTO task_runs (id,task_id,status,started_at,worker_pid,{column}) VALUES (?,?,?,?,?,?)",
                    (run_id, task_id, "running", 1, 123, 111),
                )
            conn.commit()
        with contextlib.closing(watch._read_db(self.db)) as conn, \
                mock.patch.object(watch, "_pid_alive", return_value=True), \
                mock.patch.object(watch, "_process_start_ticks", return_value=222):
            exceptions = watch.collect_exceptions(conn, 2000)
        self.assertEqual({"T12", "T13"}, {x["task"] for x in exceptions if x["kind"] == "run_card"})

    def test_duplicate_lock_is_silent(self):
        with watch.advisory_lock(self.lock):
            self.assertEqual((0, ""), self.run_main())

    def test_atomic_state_save_replaces_complete_json(self):
        self.state.write_text('{"fingerprint":"old"}\n', encoding="utf-8")
        with mock.patch("scripts.ops.kanban_exception_watch.os.replace", wraps=watch.os.replace) as replace:
            watch.save_state_atomic(self.state, "new")
        self.assertEqual({"fingerprint": "new"}, __import__("json").loads(self.state.read_text()))
        replace.assert_called_once()

    def test_smoke_does_not_write_state_or_lock(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = watch.main(["--db", str(self.db), "--state", str(self.state),
                "--lock", str(self.lock), "--smoke", "--now", "2000"])
        self.assertEqual(0, code)
        self.assertFalse(self.state.exists())
        self.assertFalse(self.lock.exists())

    def test_stale_ready_for_managed_profile_and_idle_fleet_are_reported(self):
        with contextlib.closing(sqlite3.connect(self.db)) as conn:
            conn.execute(
                "INSERT INTO tasks (id,title,assignee,status,created_at) "
                "VALUES ('R1','x','dev','ready',1800)"
            )
            conn.commit()
        with contextlib.closing(watch._read_db(self.db)) as conn:
            exceptions = watch.collect_exceptions(conn, 2000)
        kinds = {item["kind"] for item in exceptions}
        self.assertIn("ready_stale", kinds)
        self.assertIn("fleet_idle", kinds)

    def test_five_active_profiles_are_not_idle(self):
        profiles = ("dev", "productdev", "research", "plan", "design")
        with contextlib.closing(sqlite3.connect(self.db)) as conn:
            for index, profile in enumerate(profiles, start=20):
                task_id = f"P{index}"
                conn.execute(
                    "INSERT INTO tasks (id,title,assignee,status,created_at,current_run_id) "
                    "VALUES (?,?,?,?,?,?)",
                    (task_id, "x", profile, "running", 1, index),
                )
                conn.execute(
                    "INSERT INTO task_runs (id,task_id,status,started_at,last_heartbeat_at) "
                    "VALUES (?,?,?,?,?)",
                    (index, task_id, "running", 1900, 1990),
                )
            conn.commit()
        with contextlib.closing(watch._read_db(self.db)) as conn:
            kinds = {item["kind"] for item in watch.collect_exceptions(conn, 2000)}
        self.assertNotIn("fleet_idle", kinds)
        self.assertNotIn("ready_stale", kinds)

    def test_independent_todo_not_promoted_is_reported_without_mutating(self):
        with contextlib.closing(sqlite3.connect(self.db)) as conn:
            conn.execute(
                "INSERT INTO tasks (id,title,assignee,status,created_at) "
                "VALUES ('Q1','x','research','todo',1900)"
            )
            conn.commit()
        with contextlib.closing(watch._read_db(self.db)) as conn:
            kinds = {item["kind"] for item in watch.collect_exceptions(conn, 2000)}
        self.assertIn("promotion_drift", kinds)
        with contextlib.closing(sqlite3.connect(self.db)) as conn:
            self.assertEqual("todo", conn.execute(
                "SELECT status FROM tasks WHERE id='Q1'"
            ).fetchone()[0])

    def test_unacknowledged_terminal_event_is_reported_until_cursor_catches_up(self):
        with contextlib.closing(sqlite3.connect(self.db)) as conn:
            conn.execute(
                "INSERT INTO tasks (id,title,assignee,status,created_at,completed_at) "
                "VALUES ('C1','x','design','done',1,1900)"
            )
            conn.execute(
                "INSERT INTO task_events (id,task_id,kind,created_at) "
                "VALUES (41,'C1','completed',1900)"
            )
            conn.execute(
                "INSERT INTO kanban_notify_subs "
                "(task_id,platform,chat_id,thread_id,last_event_id,"
                "delivered_event_id,delivery_mode) "
                "VALUES ('C1','slack','internal','',41,40,'wake')"
            )
            conn.commit()
        with contextlib.closing(watch._read_db(self.db)) as conn:
            kinds = {item["kind"] for item in watch.collect_exceptions(conn, 2000)}
        self.assertIn("orchestrator_report_missing", kinds)
        with contextlib.closing(sqlite3.connect(self.db)) as conn:
            conn.execute(
                "UPDATE kanban_notify_subs SET delivered_event_id=41 "
                "WHERE task_id='C1'"
            )
            conn.commit()
        with contextlib.closing(watch._read_db(self.db)) as conn:
            kinds = {item["kind"] for item in watch.collect_exceptions(conn, 2000)}
        self.assertNotIn("orchestrator_report_missing", kinds)

    def test_crash_and_gave_up_reports_are_part_of_orchestration_contract(self):
        with contextlib.closing(sqlite3.connect(self.db)) as conn:
            conn.execute(
                "INSERT INTO tasks (id,title,assignee,status,created_at) "
                "VALUES ('NAS','x','dev','blocked',1)"
            )
            conn.executemany(
                "INSERT INTO task_events (id,task_id,kind,created_at) VALUES (?,?,?,?)",
                ((51, "NAS", "crashed", 1900), (52, "NAS", "gave_up", 1901)),
            )
            conn.execute(
                "INSERT INTO kanban_notify_subs "
                "(task_id,platform,chat_id,thread_id,last_event_id,"
                "delivered_event_id,delivery_mode) "
                "VALUES ('NAS','slack','internal','',52,50,'wake')"
            )
            conn.commit()

        with contextlib.closing(watch._read_db(self.db)) as conn:
            exceptions = watch.collect_exceptions(conn, 2000)

        self.assertIn(
            {"kind": "orchestrator_report_missing", "task": "NAS"},
            exceptions,
        )

    def test_inactive_unfinished_parent_stalls_todo_child_after_two_minutes(self):
        with contextlib.closing(sqlite3.connect(self.db)) as conn:
            conn.execute(
                "INSERT INTO tasks (id,title,assignee,status,created_at) "
                "VALUES ('PARENT','x','research','blocked',1)"
            )
            conn.execute(
                "INSERT INTO tasks (id,title,assignee,status,created_at) "
                "VALUES ('CHILD','x','dev','todo',1800)"
            )
            conn.execute(
                "INSERT INTO task_links (parent_id,child_id) VALUES ('PARENT','CHILD')"
            )
            conn.execute(
                "INSERT INTO task_events (task_id,kind,created_at) "
                "VALUES ('CHILD','created',1800)"
            )
            conn.commit()

        with contextlib.closing(watch._read_db(self.db)) as conn:
            exceptions = watch.collect_exceptions(conn, 2000)

        self.assertIn({"kind": "dependency_stall", "task": "CHILD"}, exceptions)

    def test_live_parent_does_not_report_dependency_stall(self):
        with contextlib.closing(sqlite3.connect(self.db)) as conn:
            conn.execute(
                "INSERT INTO tasks "
                "(id,title,assignee,status,created_at,current_run_id) "
                "VALUES ('PARENT','x','research','running',1,61)"
            )
            conn.execute(
                "INSERT INTO task_runs "
                "(id,task_id,status,started_at,last_heartbeat_at) "
                "VALUES (61,'PARENT','running',1,1999)"
            )
            conn.execute(
                "INSERT INTO tasks (id,title,assignee,status,created_at) "
                "VALUES ('CHILD','x','dev','todo',1800)"
            )
            conn.execute(
                "INSERT INTO task_links (parent_id,child_id) VALUES ('PARENT','CHILD')"
            )
            conn.commit()

        with contextlib.closing(watch._read_db(self.db)) as conn:
            exceptions = watch.collect_exceptions(conn, 2000)

        self.assertNotIn({"kind": "dependency_stall", "task": "CHILD"}, exceptions)

    def test_stalled_parent_is_reported_when_a_sibling_parent_is_live(self):
        with contextlib.closing(sqlite3.connect(self.db)) as conn:
            conn.execute(
                "INSERT INTO tasks "
                "(id,title,assignee,status,created_at,current_run_id) "
                "VALUES ('LIVE','x','research','running',1,71)"
            )
            conn.execute(
                "INSERT INTO task_runs "
                "(id,task_id,status,started_at,last_heartbeat_at) "
                "VALUES (71,'LIVE','running',1,1999)"
            )
            conn.execute(
                "INSERT INTO tasks (id,title,assignee,status,created_at) "
                "VALUES ('STALLED','x','plan','blocked',1)"
            )
            conn.execute(
                "INSERT INTO tasks (id,title,assignee,status,created_at) "
                "VALUES ('CHILD','x','dev','todo',1800)"
            )
            conn.executemany(
                "INSERT INTO task_links (parent_id,child_id) VALUES (?, 'CHILD')",
                (("LIVE",), ("STALLED",)),
            )
            conn.commit()

        with contextlib.closing(watch._read_db(self.db)) as conn:
            exceptions = watch.collect_exceptions(conn, 2000)

        self.assertIn({"kind": "dependency_stall", "task": "CHILD"}, exceptions)

    def test_unrelated_change_does_not_reemit_existing_condition(self):
        with contextlib.closing(sqlite3.connect(self.db)) as conn:
            conn.execute(
                "INSERT INTO tasks (id,title,status,created_at,block_kind) "
                "VALUES ('B1','x','blocked',1,'human')"
            )
            conn.commit()
        self.assertIn("B1", self.run_main()[1])
        with contextlib.closing(sqlite3.connect(self.db)) as conn:
            conn.execute(
                "INSERT INTO tasks (id,title,status,created_at,block_kind) "
                "VALUES ('B2','x','blocked',1,'human')"
            )
            conn.commit()
        output = self.run_main()[1]
        self.assertIn("B2", output)
        self.assertNotIn("B1", output)

    def test_new_exception_emits_one_internal_coordination_event(self):
        with contextlib.closing(sqlite3.connect(self.db)) as conn:
            conn.execute(
                "INSERT INTO tasks (id,title,assignee,status,created_at) "
                "VALUES ('R1','x','dev','ready',1800)"
            )
            conn.commit()

        self.assertEqual(self.run_main()[0], 0)
        self.assertEqual(self.run_main()[0], 0)

        with contextlib.closing(sqlite3.connect(self.db)) as conn:
            events = conn.execute(
                "SELECT kind, payload FROM task_events "
                "WHERE task_id='R1' AND kind='coordination_required'"
            ).fetchall()
        self.assertEqual(len(events), 1)
        self.assertIn("ready_stale", events[0][1])

    def test_coordination_event_reemits_after_condition_clears_and_recurs(self):
        with contextlib.closing(sqlite3.connect(self.db)) as conn:
            conn.execute(
                "INSERT INTO tasks (id,title,assignee,status,created_at) "
                "VALUES ('R1','x','dev','ready',1800)"
            )
            conn.commit()
        self.run_main()

        with contextlib.closing(sqlite3.connect(self.db)) as conn:
            conn.execute("UPDATE tasks SET status='done' WHERE id='R1'")
            conn.commit()
        self.run_main()

        with contextlib.closing(sqlite3.connect(self.db)) as conn:
            conn.execute("UPDATE tasks SET status='ready' WHERE id='R1'")
            conn.execute(
                "INSERT INTO task_events (task_id,kind,created_at) "
                "VALUES ('R1','status',1800)"
            )
            conn.commit()
        self.run_main()

        with contextlib.closing(sqlite3.connect(self.db)) as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM task_events "
                "WHERE task_id='R1' AND kind='coordination_required'"
            ).fetchone()[0]
        self.assertEqual(count, 2)

    def test_dead_managed_worker_does_not_suppress_fleet_idle(self):
        with contextlib.closing(sqlite3.connect(self.db)) as conn:
            conn.execute(
                "INSERT INTO tasks "
                "(id,title,assignee,status,created_at,current_run_id,worker_pid) "
                "VALUES ('RUN','x','design','running',1,7,99999999)"
            )
            conn.execute(
                "INSERT INTO task_runs "
                "(id,task_id,status,worker_pid,started_at) "
                "VALUES (7,'RUN','running',99999999,1)"
            )
            conn.execute(
                "INSERT INTO tasks (id,title,assignee,status,created_at) "
                "VALUES ('READY','x','dev','ready',1999)"
            )
            conn.commit()
        with contextlib.closing(watch._read_db(self.db)) as conn:
            kinds = {item["kind"] for item in watch.collect_exceptions(conn, 2000)}
        self.assertIn("fleet_idle", kinds)

    def test_minimal_legacy_schema_without_events_still_runs(self):
        legacy = self.db.with_name("legacy.db")
        legacy_state = self.state.with_name("legacy.json")
        legacy_lock = self.lock.with_name("legacy.lock")
        with contextlib.closing(sqlite3.connect(legacy)) as conn:
            conn.executescript(
                "CREATE TABLE tasks (id TEXT PRIMARY KEY, status TEXT, "
                "block_kind TEXT, created_at INTEGER);"
                "CREATE TABLE task_runs (id INTEGER PRIMARY KEY, task_id TEXT, "
                "status TEXT);"
                "INSERT INTO tasks VALUES ('B1','blocked','human',1);"
            )
            conn.commit()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = watch.main([
                "--db", str(legacy), "--state", str(legacy_state),
                "--lock", str(legacy_lock), "--now", "2000",
            ])
        self.assertEqual(code, 0)
        self.assertIn("B1", out.getvalue())


if __name__ == "__main__":
    unittest.main()
