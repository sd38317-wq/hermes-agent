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
                  created_at INTEGER, started_at INTEGER, claim_expires INTEGER,
                  worker_pid INTEGER, last_heartbeat_at INTEGER,
                  current_run_id INTEGER, block_kind TEXT, due_at INTEGER
                );
                CREATE TABLE task_runs (
                  id INTEGER PRIMARY KEY, task_id TEXT, status TEXT,
                  worker_pid INTEGER, started_at INTEGER, ended_at INTEGER,
                  last_heartbeat_at INTEGER, claim_expires INTEGER
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


if __name__ == "__main__":
    unittest.main()
