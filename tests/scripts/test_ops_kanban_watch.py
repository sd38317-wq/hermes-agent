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
    PROFILES = ("dev", "productdev", "research", "plan", "design")

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

    def add_task(self, conn, task_id, profile, status, *, created_at=1,
                 run_id=None, block_kind=None):
        conn.execute(
            "INSERT INTO tasks "
            "(id,title,assignee,status,created_at,current_run_id,block_kind) "
            "VALUES (?,?,?,?,?,?,?)",
            (task_id, "fixture", profile, status, created_at, run_id, block_kind),
        )
        if run_id is not None:
            conn.execute(
                "INSERT INTO task_runs "
                "(id,task_id,status,started_at,last_heartbeat_at) "
                "VALUES (?,?, 'running',1900,1990)",
                (run_id, task_id),
            )

    def exceptions(self):
        with contextlib.closing(watch._read_db(self.db)) as conn:
            return watch.collect_exceptions(conn, 2000)

    def task_state(self):
        with contextlib.closing(sqlite3.connect(self.db)) as conn:
            return {
                row[0]: row[1:]
                for row in conn.execute(
                    "SELECT id,status,assignee,current_run_id,block_kind "
                    "FROM tasks ORDER BY id"
                )
            }

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
        with contextlib.closing(sqlite3.connect(self.db)) as conn:
            for index, profile in enumerate(self.PROFILES, start=20):
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

    def test_zero_running_with_actionable_work_reports_idle_for_every_profile(self):
        with contextlib.closing(sqlite3.connect(self.db)) as conn:
            for index, profile in enumerate(self.PROFILES):
                self.add_task(conn, f"READY-{profile}", profile, "ready",
                              created_at=1999 - index)
            conn.commit()

        exceptions = self.exceptions()
        self.assertEqual(
            [{"kind": "fleet_idle", "task": "READY-design"}],
            [item for item in exceptions if item["kind"] == "fleet_idle"],
        )
        self.assertFalse(any(item["kind"] == "ready_stale" for item in exceptions))

    def test_each_profile_is_managed_when_it_is_the_only_actionable_profile(self):
        for profile in self.PROFILES:
            with self.subTest(profile=profile), \
                    contextlib.closing(sqlite3.connect(self.db)) as conn:
                task_id = f"READY-{profile}"
                self.add_task(conn, task_id, profile, "ready", created_at=1999)
                conn.commit()

                self.assertIn(
                    {"kind": "fleet_idle", "task": task_id},
                    self.exceptions(),
                )

                conn.execute("DELETE FROM tasks WHERE id=?", (task_id,))
                conn.commit()

    def test_ready_becomes_stale_at_exactly_120_seconds(self):
        with contextlib.closing(sqlite3.connect(self.db)) as conn:
            self.add_task(conn, "BEFORE", "dev", "ready", created_at=1881)
            self.add_task(conn, "BOUNDARY", "productdev", "ready", created_at=1880)
            conn.commit()

        stale = {item["task"] for item in self.exceptions()
                 if item["kind"] == "ready_stale"}
        self.assertEqual({"BOUNDARY"}, stale)

    def test_profile_completion_leaves_independent_followup_actionable(self):
        with contextlib.closing(sqlite3.connect(self.db)) as conn:
            for index, profile in enumerate(self.PROFILES, start=100):
                self.add_task(conn, f"DONE-{profile}", profile, "done")
            self.add_task(conn, "FOLLOWUP", "design", "todo", created_at=1990)
            conn.commit()

        before = self.task_state()
        exceptions = self.exceptions()
        self.assertIn({"kind": "promotion_drift", "task": "FOLLOWUP"}, exceptions)
        self.assertIn({"kind": "fleet_idle", "task": "FOLLOWUP"}, exceptions)
        self.assertEqual(before, self.task_state())

    def test_all_todo_work_blocked_by_live_parents_is_not_actionable(self):
        with contextlib.closing(sqlite3.connect(self.db)) as conn:
            for index, profile in enumerate(self.PROFILES, start=200):
                parent = f"PARENT-{profile}"
                child = f"CHILD-{profile}"
                self.add_task(conn, parent, profile, "running", run_id=index)
                self.add_task(conn, child, profile, "todo", created_at=1800)
                conn.execute(
                    "INSERT INTO task_links (parent_id,child_id) VALUES (?,?)",
                    (parent, child),
                )
            conn.commit()

        exceptions = self.exceptions()
        self.assertFalse(any(item["kind"] in {
            "promotion_drift", "dependency_stall", "fleet_idle"
        } for item in exceptions), exceptions)

    def test_all_todo_work_with_inactive_parents_requests_coordination_only(self):
        with contextlib.closing(sqlite3.connect(self.db)) as conn:
            for profile in self.PROFILES:
                parent = f"PARENT-{profile}"
                child = f"CHILD-{profile}"
                self.add_task(conn, parent, "coordinator", "todo", created_at=1)
                self.add_task(conn, child, profile, "todo", created_at=1800)
                conn.execute(
                    "INSERT INTO task_links (parent_id,child_id) VALUES (?,?)",
                    (parent, child),
                )
            conn.commit()

        before = self.task_state()
        exceptions = self.exceptions()
        stalled = {item["task"] for item in exceptions
                   if item["kind"] == "dependency_stall"}

        self.assertEqual(
            {f"CHILD-{profile}" for profile in self.PROFILES},
            stalled,
        )
        self.assertFalse(any(item["kind"] in {
            "promotion_drift", "fleet_idle"
        } for item in exceptions), exceptions)
        self.assertEqual(before, self.task_state())

    def test_human_input_gate_is_reported_but_never_bypassed(self):
        with contextlib.closing(sqlite3.connect(self.db)) as conn:
            self.add_task(conn, "APPROVAL", "plan", "blocked",
                          block_kind="approval")
            self.add_task(conn, "DEPENDENT", "dev", "todo", created_at=1990)
            conn.execute(
                "INSERT INTO task_links (parent_id,child_id) "
                "VALUES ('APPROVAL','DEPENDENT')"
            )
            conn.commit()

        before = self.task_state()
        exceptions = self.exceptions()
        self.assertIn({"kind": "human", "task": "APPROVAL"}, exceptions)
        self.assertNotIn({"kind": "promotion_drift", "task": "DEPENDENT"}, exceptions)
        self.assertNotIn({"kind": "fleet_idle", "task": "DEPENDENT"}, exceptions)
        self.assertEqual(before, self.task_state())

    def test_main_only_signals_and_never_claims_or_rewrites_dependencies(self):
        with contextlib.closing(sqlite3.connect(self.db)) as conn:
            self.add_task(conn, "HUMAN", "research", "blocked",
                          block_kind="input")
            self.add_task(conn, "CHILD", "design", "todo", created_at=1800)
            self.add_task(conn, "READY", "dev", "ready", created_at=1800)
            conn.execute(
                "INSERT INTO task_links (parent_id,child_id) VALUES ('HUMAN','CHILD')"
            )
            conn.commit()

        before = self.task_state()
        self.assertEqual(0, self.run_main()[0])

        self.assertEqual(before, self.task_state())
        with contextlib.closing(sqlite3.connect(self.db)) as conn:
            self.assertEqual(
                [("HUMAN", "CHILD")],
                conn.execute(
                    "SELECT parent_id,child_id FROM task_links ORDER BY parent_id,child_id"
                ).fetchall(),
            )
            self.assertEqual(0, conn.execute(
                "SELECT COUNT(*) FROM task_runs"
            ).fetchone()[0])
            self.assertEqual(
                {"coordination_required"},
                {row[0] for row in conn.execute("SELECT kind FROM task_events")},
            )

    def test_complete_and_block_transitions_require_next_assignment_confirmation(self):
        with contextlib.closing(sqlite3.connect(self.db)) as conn:
            for event_id, (profile, kind, status) in enumerate((
                ("research", "completed", "done"),
                ("productdev", "blocked", "blocked"),
            ), start=300):
                task_id = kind.upper()
                self.add_task(conn, task_id, profile, status,
                              block_kind="dependency" if kind == "blocked" else None)
                conn.execute(
                    "INSERT INTO task_events (id,task_id,kind,created_at) "
                    "VALUES (?,?,?,1990)", (event_id, task_id, kind),
                )
                conn.execute(
                    "INSERT INTO kanban_notify_subs "
                    "(task_id,platform,chat_id,thread_id,last_event_id,"
                    "delivered_event_id,delivery_mode) VALUES (?,?,?,?,?,?,?)",
                    (task_id, "internal", "coordinator", "", event_id,
                     event_id - 1, "wake"),
                )
            conn.commit()

        missing = {item["task"] for item in self.exceptions()
                   if item["kind"] == "orchestrator_report_missing"}
        self.assertEqual({"COMPLETED", "BLOCKED"}, missing)

    def test_telegram_and_slack_normal_or_log_subscriptions_never_notify(self):
        with contextlib.closing(sqlite3.connect(self.db)) as conn:
            self.add_task(conn, "FINISHED", "dev", "done")
            conn.execute(
                "INSERT INTO task_events (id,task_id,kind,created_at) "
                "VALUES (401,'FINISHED','completed',1990)"
            )
            for platform in ("telegram", "slack"):
                for mode in ("normal", "log"):
                    conn.execute(
                        "INSERT INTO kanban_notify_subs "
                        "(task_id,platform,chat_id,thread_id,last_event_id,"
                        "delivered_event_id,delivery_mode) VALUES "
                        "('FINISHED',?,?, '',401,400,?)",
                        (platform, f"{platform}-{mode}-chat", mode),
                    )
            conn.commit()

        self.assertEqual((0, ""), self.run_main())
        self.assertNotIn(
            {"kind": "orchestrator_report_missing", "task": "FINISHED"},
            self.exceptions(),
        )
        with contextlib.closing(sqlite3.connect(self.db)) as conn:
            self.assertEqual(4, conn.execute(
                "SELECT COUNT(*) FROM kanban_notify_subs WHERE last_event_id=401 "
                "AND delivered_event_id=400"
            ).fetchone()[0])
            self.assertEqual(0, conn.execute(
                "SELECT COUNT(*) FROM task_events "
                "WHERE kind='coordination_required'"
            ).fetchone()[0])

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
