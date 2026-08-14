from __future__ import annotations

import contextlib
import io
import json
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

    def invoke(self, sender, **kwargs):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = dashboard.main(["--db", str(self.db), "--state", str(self.state),
                "--lock", str(self.lock), "--now", "2000"], sender=sender,
                token_getter=lambda: "fake-token", **kwargs)
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
        self.assertEqual("1786717991.405699", calls[0][1])
        self.assertEqual((0, ""), self.invoke(sender))
        self.assertEqual(1, len(calls))

    def test_render_has_korean_no_current_work_text_without_aggregates(self):
        with contextlib.closing(dashboard._read_db(self.db)) as conn:
            text = dashboard.render(dashboard.compute_dashboard(conn, 2000))
        self.assertEqual("현재 진행 중인 작업이 없어요.", text)

    def test_focus_prefers_valid_running_then_needs_input_then_ready(self):
        with contextlib.closing(sqlite3.connect(self.db)) as conn:
            conn.execute("ALTER TABLE tasks ADD COLUMN block_kind TEXT")
            conn.execute("INSERT INTO tasks VALUES ('ready','Ready task','dev','ready',NULL,NULL,NULL,3,NULL)")
            conn.execute("INSERT INTO tasks VALUES ('blocked','Need answer','dev','blocked',NULL,NULL,NULL,2,'needs_input')")
            conn.execute("INSERT INTO tasks VALUES ('running','Do now','dev','running',123,1,1900,1,NULL)")
            conn.execute("INSERT INTO task_runs VALUES (1,'running','dev','running',123,1,NULL,1900,NULL)")
            conn.commit()
        with contextlib.closing(dashboard._read_db(self.db)) as conn, \
                mock.patch.object(dashboard, "_pid_alive", return_value=True):
            text = dashboard.render(dashboard.compute_dashboard(conn, 2000))
        self.assertIn("Do now", text)
        self.assertNotIn("Need answer", text)
        self.assertNotIn("Ready task", text)
        self.assertNotIn("0", text)
        with contextlib.closing(sqlite3.connect(self.db)) as conn:
            conn.execute("DELETE FROM task_runs")
            conn.execute("UPDATE tasks SET status='done' WHERE id='running'")
            conn.commit()
        with contextlib.closing(dashboard._read_db(self.db)) as conn:
            self.assertIn("Need answer", dashboard.render(dashboard.compute_dashboard(conn, 2000)))
        with contextlib.closing(sqlite3.connect(self.db)) as conn:
            conn.execute("UPDATE tasks SET block_kind='dependency' WHERE id='blocked'")
            conn.commit()
        with contextlib.closing(dashboard._read_db(self.db)) as conn:
            self.assertIn("Ready task", dashboard.render(dashboard.compute_dashboard(conn, 2000)))

    def test_blocked_fallback_only_accepts_needs_input_block_kind(self):
        with contextlib.closing(sqlite3.connect(self.db)) as conn:
            conn.execute("ALTER TABLE tasks ADD COLUMN block_kind TEXT")
            for task_id, status, block_kind in (
                    ("generic", "blocked", None),
                    ("triage", "triage", "needs_input"),
                    ("capability", "blocked", "capability"),
                    ("transient", "blocked", "transient"),
                    ("human", "blocked", "human"),
                    ("ready", "ready", None)):
                conn.execute(
                    "INSERT INTO tasks VALUES (?, ?, 'dev', ?, NULL, NULL, NULL, 1, ?)",
                    (task_id, task_id, status, block_kind),
                )
            conn.commit()
        with contextlib.closing(dashboard._read_db(self.db)) as conn:
            data = dashboard.compute_dashboard(conn, 2000)
        self.assertEqual("ready", data["focus"]["id"])
        with contextlib.closing(sqlite3.connect(self.db)) as conn:
            conn.execute(
                "INSERT INTO tasks VALUES ('answer', 'answer', 'dev', 'blocked', "
                "NULL, NULL, NULL, 1, 'needs_input')")
            conn.commit()
        with contextlib.closing(dashboard._read_db(self.db)) as conn:
            data = dashboard.compute_dashboard(conn, 2000)
        self.assertEqual("answer", data["focus"]["id"])

    def test_focus_uses_highest_priority_with_deterministic_id_tiebreak(self):
        with contextlib.closing(sqlite3.connect(self.db)) as conn:
            conn.execute("ALTER TABLE tasks ADD COLUMN priority INTEGER")
            conn.execute("INSERT INTO tasks VALUES ('a-low','low','dev','ready',NULL,NULL,NULL,1,1)")
            conn.execute("INSERT INTO tasks VALUES ('z-high','z-high','dev','ready',NULL,NULL,NULL,1,9)")
            conn.execute("INSERT INTO tasks VALUES ('y-high','y-high','dev','ready',NULL,NULL,NULL,1,9)")
            conn.commit()
        with contextlib.closing(dashboard._read_db(self.db)) as conn:
            data = dashboard.compute_dashboard(conn, 2000)
        self.assertEqual("y-high", data["focus"]["id"])

    def test_unchanged_legacy_state_recreates_missing_message_and_persists_ts(self):
        text = "현재 진행 중인 작업이 없어요."
        current = dashboard.fingerprint([{"dashboard": text, "blocks": dashboard.render_blocks(text)}])
        self.state.write_text(json.dumps({"fingerprint": current}), encoding="utf-8")
        posts = []
        code, output = self.invoke(
            lambda *args: self.fail("must not update a missing message"),
            verifier=lambda *args: False,
            poster=lambda channel, body, token, timeout: posts.append((channel, body)) or "new-ts",
        )
        self.assertEqual((0, ""), (code, output))
        self.assertEqual([("C0BPXD9TBB7", text)], posts)
        self.assertEqual({"fingerprint": current, "ts": "new-ts"}, json.loads(self.state.read_text()))

    def test_stored_timestamp_is_preferred_and_verified_before_update(self):
        self.state.write_text(json.dumps({"fingerprint": "old", "ts": "stored-ts"}), encoding="utf-8")
        checked, updated = [], []
        code, output = self.invoke(
            lambda channel, ts, *args: updated.append(ts),
            verifier=lambda channel, ts, *args: checked.append(ts) or True,
        )
        self.assertEqual((0, ""), (code, output))
        self.assertEqual(["stored-ts"], checked)
        self.assertEqual(["stored-ts"], updated)

    def test_slack_update_and_post_include_fallback_text_and_one_current_task_block(self):
        class Response:
            def __init__(self, payload): self.payload = payload
            def __enter__(self): return self
            def __exit__(self, *args): return None
            def read(self, limit): return json.dumps(self.payload).encode()

        requests = []

        def urlopen(request, timeout):
            requests.append(json.loads(request.data))
            payload = {"ok": True, "ts": "new-ts"} if request.full_url.endswith("postMessage") else {"ok": True}
            return Response(payload)

        text = "현재 집중 · 준비됨\nImportant task (T1)"
        blocks = dashboard.render_blocks(text)
        with mock.patch.object(dashboard.urllib.request, "urlopen", side_effect=urlopen):
            dashboard.slack_sender("channel", "old-ts", text, "token", 5, blocks=blocks)
            self.assertEqual("new-ts", dashboard.slack_poster(
                "channel", text, "token", 5, blocks=blocks))
        self.assertEqual(2, len(requests))
        for payload in requests:
            self.assertEqual(text, payload["text"])
            self.assertEqual(blocks, payload["blocks"])
            self.assertEqual(1, len(payload["blocks"]))
            self.assertEqual("section", payload["blocks"][0]["type"])

    def test_running_process_and_heartbeat_determine_working_or_blocked(self):
        with contextlib.closing(sqlite3.connect(self.db)) as conn:
            conn.execute("INSERT INTO tasks VALUES ('T1','private','dev','running',123,1,1900,1)")
            conn.execute("INSERT INTO task_runs VALUES (1,'T1','dev','running',123,1,NULL,1900,NULL)")
            conn.commit()
        with contextlib.closing(dashboard._read_db(self.db)) as conn, mock.patch.object(dashboard, "_pid_alive", return_value=True):
            data = dashboard.compute_dashboard(conn, 2000)
        self.assertEqual("T1", data["focus"]["id"])
        with contextlib.closing(dashboard._read_db(self.db)) as conn, mock.patch.object(dashboard, "_pid_alive", return_value=False):
            data = dashboard.compute_dashboard(conn, 2000)
        self.assertIsNone(data["focus"])

    def test_worker_birth_identity_must_match_live_process(self):
        with contextlib.closing(sqlite3.connect(self.db)) as conn:
            conn.execute("INSERT INTO tasks VALUES ('T1','private','dev','running',123,1,1900,1)")
            conn.execute("INSERT INTO task_runs VALUES (1,'T1','dev','running',123,1,NULL,1900,'linux:boot:old')")
            conn.commit()
        with contextlib.closing(dashboard._read_db(self.db)) as conn, \
                mock.patch.object(dashboard, "_pid_alive", return_value=True), \
                mock.patch.object(dashboard, "_process_birth_identity", return_value="linux:boot:new"):
            data = dashboard.compute_dashboard(conn, 2000)
        self.assertIsNone(data["focus"])

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
