from __future__ import annotations

import contextlib
import io
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts.ops import slack_dashboard_update as dashboard


class DashboardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.db, self.state, self.lock = root / "k.db", root / "state.json", root / "lock"
        with contextlib.closing(sqlite3.connect(self.db)) as conn:
            conn.executescript("""
            CREATE TABLE tasks (id TEXT PRIMARY KEY, title TEXT, assignee TEXT, status TEXT,
              worker_pid INTEGER, current_run_id INTEGER, last_heartbeat_at INTEGER, created_at INTEGER);
            CREATE TABLE task_runs (id INTEGER PRIMARY KEY, task_id TEXT, profile TEXT, status TEXT,
              worker_pid INTEGER, started_at INTEGER, ended_at INTEGER, last_heartbeat_at INTEGER,
              worker_birth_identity TEXT);
            """)
            conn.commit()

    def tearDown(self): self.tmp.cleanup()

    def invoke(self, sender):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = dashboard.main(["--db", str(self.db), "--state", str(self.state),
                "--lock", str(self.lock), "--now", "2000"], sender=sender, token_getter=lambda: "fake-token")
        return code, out.getvalue()

    def invoke_with_env_file(self, env_file, sender, *, token_getter=lambda: None):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = dashboard.main(["--db", str(self.db), "--state", str(self.state),
                "--lock", str(self.lock), "--now", "2000", "--env-file", str(env_file)],
                sender=sender, token_getter=token_getter)
        return code, out.getvalue()

    def test_changed_updates_existing_timestamp_then_unchanged_skips(self):
        calls = []
        sender = lambda channel, ts, text, token, timeout: calls.append((channel, ts, text, token))
        self.assertEqual((0, ""), self.invoke(sender))
        self.assertEqual(1, len(calls))
        self.assertEqual("C0BPXD9TBB7", calls[0][0])
        self.assertEqual("1786674259.552709", calls[0][1])
        self.assertEqual((0, ""), self.invoke(sender))
        self.assertEqual(1, len(calls))

    def test_running_process_and_heartbeat_determine_working_or_blocked(self):
        with contextlib.closing(sqlite3.connect(self.db)) as conn:
            conn.execute("INSERT INTO tasks VALUES ('T1','private','dev','running',123,1,1900,1)")
            conn.execute("INSERT INTO task_runs VALUES (1,'T1','dev','running',123,1,NULL,1900,NULL)")
            conn.commit()
        with contextlib.closing(dashboard._read_db(self.db)) as conn, mock.patch.object(dashboard, "_pid_alive", return_value=True):
            data = dashboard.compute_dashboard(conn, 2000)
        self.assertEqual(1, data["profiles"]["dev"]["working"])
        with contextlib.closing(dashboard._read_db(self.db)) as conn, mock.patch.object(dashboard, "_pid_alive", return_value=False):
            data = dashboard.compute_dashboard(conn, 2000)
        self.assertEqual(1, data["profiles"]["dev"]["blocked"])

    def test_worker_birth_identity_must_match_live_process(self):
        with contextlib.closing(sqlite3.connect(self.db)) as conn:
            conn.execute("INSERT INTO tasks VALUES ('T1','private','dev','running',123,1,1900,1)")
            conn.execute("INSERT INTO task_runs VALUES (1,'T1','dev','running',123,1,NULL,1900,'linux:boot:old')")
            conn.commit()
        with contextlib.closing(dashboard._read_db(self.db)) as conn, \
                mock.patch.object(dashboard, "_pid_alive", return_value=True), \
                mock.patch.object(dashboard, "_process_birth_identity", return_value="linux:boot:new"):
            data = dashboard.compute_dashboard(conn, 2000)
        self.assertEqual(1, data["profiles"]["dev"]["blocked"])

    def test_api_failure_is_one_sanitized_line_and_nonzero(self):
        def fail(*args):
            raise RuntimeError("raw response token=fake-token")
        code, output = self.invoke(fail)
        self.assertEqual(1, code)
        self.assertEqual(1, len(output.splitlines()))
        self.assertNotIn("fake-token", output)
        self.assertNotIn("raw response", output)
        self.assertFalse(self.state.exists())

    def test_loads_exact_quoted_token_from_secure_env_without_export_or_expansion(self):
        env_file = Path(self.tmp.name) / "secrets.env"
        env_file.write_text(
            "OTHER_SECRET=do-not-export\n"
            "SLACK_BOT_TOKEN_EXTRA=wrong\n"
            'export SLACK_BOT_TOKEN="literal-$OTHER_SECRET"\n',
            encoding="utf-8",
        )
        env_file.chmod(0o600)
        calls = []
        with mock.patch.dict(os.environ, {}, clear=True):
            code, output = self.invoke_with_env_file(env_file, lambda *args: calls.append(args))
            self.assertNotIn("OTHER_SECRET", os.environ)
            self.assertNotIn("SLACK_BOT_TOKEN", os.environ)
        self.assertEqual((0, ""), (code, output))
        self.assertEqual("literal-$OTHER_SECRET", calls[0][3])

    def test_existing_environment_token_takes_priority_without_reading_env_file(self):
        missing = Path(self.tmp.name) / "must-not-be-read"
        calls = []
        code, output = self.invoke_with_env_file(
            missing, lambda *args: calls.append(args), token_getter=lambda: "existing-token")
        self.assertEqual((0, ""), (code, output))
        self.assertEqual("existing-token", calls[0][3])

    def test_env_file_must_be_owned_regular_nonsymlink_mode_0600(self):
        target = Path(self.tmp.name) / "target.env"
        target.write_text("SLACK_BOT_TOKEN=secret-token\n", encoding="utf-8")
        target.chmod(0o600)
        symlink = Path(self.tmp.name) / "linked.env"
        symlink.symlink_to(target)
        insecure = Path(self.tmp.name) / "insecure.env"
        insecure.write_text("SLACK_BOT_TOKEN=secret-token\n", encoding="utf-8")
        insecure.chmod(0o640)
        for env_file in (symlink, insecure):
            with self.subTest(env_file=env_file.name):
                code, output = self.invoke_with_env_file(env_file, lambda *x: self.fail("sent"))
                self.assertEqual(1, code)
                self.assertEqual(1, len(output.splitlines()))
                self.assertNotIn("secret-token", output)
                self.assertNotIn(str(env_file), output)

        fake_stat = target.stat()
        with mock.patch.object(dashboard.os, "lstat") as lstat:
            lstat.return_value = os.stat_result((*fake_stat[:4], os.geteuid() + 1, *fake_stat[5:]))
            code, output = self.invoke_with_env_file(target, lambda *x: self.fail("sent"))
        self.assertEqual(1, code)
        self.assertEqual(1, len(output.splitlines()))

    def test_smoke_does_not_access_env_file(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), mock.patch.object(
                dashboard, "_token_from_env_file", side_effect=AssertionError("read env")):
            code = dashboard.main(["--db", str(self.db), "--state", str(self.state),
                "--lock", str(self.lock), "--smoke", "--env-file", str(Path(self.tmp.name) / "env")],
                sender=lambda *x: self.fail("sent"), token_getter=lambda: self.fail("token read"))
        self.assertEqual(0, code)
        self.assertFalse(self.state.exists())

    def test_duplicate_lock_does_not_send(self):
        calls = []
        with dashboard.advisory_lock(self.lock):
            self.assertEqual((0, ""), self.invoke(lambda *x: calls.append(x)))
        self.assertEqual([], calls)

    def test_smoke_never_sends_or_writes_state_or_reads_token(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = dashboard.main(["--db", str(self.db), "--state", str(self.state),
                "--lock", str(self.lock), "--smoke"], sender=lambda *x: self.fail("sent"),
                token_getter=lambda: self.fail("token read"))
        self.assertEqual(0, code)
        self.assertFalse(self.state.exists())

    def test_symlink_db_is_rejected_without_target_details(self):
        link = Path(self.tmp.name) / "linked.db"
        link.symlink_to(self.db)
        self.db = link
        code, output = self.invoke(lambda *x: self.fail("sent"))
        self.assertEqual(1, code)
        self.assertEqual(1, len(output.splitlines()))
        self.assertNotIn(str(self.db), output)


if __name__ == "__main__": unittest.main()
