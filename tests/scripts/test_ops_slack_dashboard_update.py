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

    def invoke_with_env_file(self, env_file, sender, *, token_getter=lambda: None, poster=None):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = dashboard.main(["--db", str(self.db), "--state", str(self.state),
                "--lock", str(self.lock), "--now", "2000", "--env-file", str(env_file)],
                sender=sender, token_getter=token_getter,
                poster=poster or (lambda *args: "local-ts"))
        return code, out.getvalue()

    def test_missing_timestamp_posts_once_then_content_change_updates_saved_timestamp(self):
        posts, updates, checks = [], [], []
        poster = lambda channel, text, token, timeout: posts.append((channel, text)) or "new-ts"
        sender = lambda channel, ts, text, token, timeout: updates.append((ts, text))
        verifier = lambda channel, ts, token, timeout: checks.append(ts) or True

        self.assertEqual((0, ""), self.invoke(sender, poster=poster, verifier=verifier))
        self.assertEqual(1, len(posts))
        self.assertEqual([], updates)
        self.assertEqual([], checks)
        self.assertEqual("new-ts", json.loads(self.state.read_text())["ts"])

        with contextlib.closing(sqlite3.connect(self.db)) as conn:
            conn.execute(
                "INSERT INTO tasks VALUES ('task','새 작업','dev','ready',NULL,NULL,NULL,1)")
            conn.commit()
        self.assertEqual((0, ""), self.invoke(sender, poster=poster, verifier=verifier))
        self.assertEqual(1, len(posts))
        self.assertEqual(["new-ts"], checks)
        self.assertEqual("new-ts", updates[0][0])

    def test_render_has_korean_no_current_work_text_without_aggregates(self):
        with contextlib.closing(dashboard._read_db(self.db)) as conn:
            text = dashboard.render(dashboard.compute_dashboard(conn, 2000))
        self.assertEqual("현재 진행 중인 작업이 없어요.", text)

    def test_main_posts_one_complete_multiline_dashboard(self):
        with contextlib.closing(sqlite3.connect(self.db)) as conn:
            conn.execute("ALTER TABLE tasks ADD COLUMN priority INTEGER")
            for task_id, title, assignee, status, priority in (
                    ("one", "API 구현", "dev", "todo", 40),
                    ("two", "시장 조사", "research", "blocked", 30),
                    ("three", "기획 정리", "plan", "ready", 20),
                    ("four", "화면 설계", "design", "todo", 10)):
                conn.execute(
                    "INSERT INTO tasks VALUES (?,?,?,?,NULL,NULL,NULL,1,?)",
                    (task_id, title, assignee, status, priority))
            conn.commit()
        posts = []

        def poster(channel, text, token, timeout, *, blocks):
            posts.append({"channel": channel, "text": text, "blocks": blocks})
            return "one-message-ts"

        self.assertEqual((0, ""), self.invoke(
            lambda *args: self.fail("must post, not update"), poster=poster))
        self.assertEqual(1, len(posts))
        payload = posts[0]
        fallback_lines = payload["text"].splitlines()
        block_text = [block.get("text", {}).get("text", "")
                      for block in payload["blocks"]]
        self.assertEqual(9, len(fallback_lines))
        self.assertEqual(11, len(payload["blocks"]))
        labels = ("Hermes", "개발", "조사", "기획문서", "디자인")
        self.assertEqual(list(labels),
                         [line.split(" ", 1)[1].split(" ·", 1)[0]
                          for line in fallback_lines[:5]])
        profile_blocks = [line for line in block_text
                          if line.startswith(tuple("🔵🟢🟣🟠🔴"))]
        self.assertEqual(5, len(profile_blocks))
        self.assertEqual(list(labels),
                         [line.split(" ", 1)[1].split(" ·", 1)[0]
                          for line in profile_blocks])
        titles = ("API 구현", "시장 조사", "기획 정리", "화면 설계")
        self.assertEqual(list(titles),
                         [next(title for title in titles if title in line)
                          for line in fallback_lines[-4:]])
        queue_blocks = [line for line in block_text if line[:1].isdigit()]
        self.assertEqual(4, len(queue_blocks))
        self.assertEqual(list(titles),
                         [next(title for title in titles if title in line)
                          for line in queue_blocks])
        self.assertEqual("header", payload["blocks"][0]["type"])
        self.assertIn("divider", [block["type"] for block in payload["blocks"]])

    def test_mobile_blocks_have_five_distinct_one_line_profile_rows(self):
        with contextlib.closing(sqlite3.connect(self.db)) as conn:
            conn.execute("ALTER TABLE tasks ADD COLUMN block_kind TEXT")
            conn.execute(
                "INSERT INTO tasks VALUES ('dev-task','API 구현','dev','running',123,1,1900,1,NULL)")
            conn.execute(
                "INSERT INTO tasks VALUES ('plan-task','요건 확인','plan','blocked',NULL,NULL,NULL,2,'needs_input')")
            conn.execute(
                "INSERT INTO task_runs VALUES (1,'dev-task','dev','running',123,1,NULL,1900,NULL)")
            conn.commit()
        with contextlib.closing(dashboard._read_db(self.db)) as conn, \
                mock.patch.object(dashboard, "_pid_alive", return_value=True):
            data = dashboard.compute_dashboard(conn, 2000)
        blocks = dashboard.render_blocks(dashboard.render(data), data)
        rows = [block["text"]["text"] for block in blocks
                if block["type"] == "section"][:5]

        self.assertEqual(5, len(rows))
        self.assertEqual(["Hermes", "개발", "조사", "기획문서", "디자인"],
                         [row.split(" ", 1)[1].split(" ·", 1)[0] for row in rows])
        self.assertEqual(5, len({row.split(" ", 1)[0] for row in rows}))
        self.assertTrue(all("\n" not in row for row in rows))
        self.assertIn("현재 작업: API 구현", rows[1])
        self.assertIn("막힘: 요건 확인", rows[3])
        self.assertIn("대기 중", rows[0])
        self.assertIn("대기 중", rows[2])
        self.assertIn("대기 중", rows[4])

    def test_top_four_queue_uses_priority_and_deterministic_evidence_scores(self):
        with contextlib.closing(sqlite3.connect(self.db)) as conn:
            conn.execute("ALTER TABLE tasks ADD COLUMN priority INTEGER")
            conn.execute("ALTER TABLE task_runs ADD COLUMN outcome TEXT")
            conn.execute("ALTER TABLE task_runs ADD COLUMN metadata TEXT")
            conn.executescript("""
                CREATE TABLE task_attachments (
                  id INTEGER PRIMARY KEY, task_id TEXT, filename TEXT, created_at INTEGER);
                CREATE TABLE task_events (
                  id INTEGER PRIMARY KEY, task_id TEXT, kind TEXT, payload TEXT, created_at INTEGER);
            """)
            for task_id, title, priority in (
                    ("a", "첫 번째", 50), ("b", "두 번째", 40), ("c", "세 번째", 30),
                    ("d", "네 번째", 20), ("e", "다섯 번째", 10)):
                conn.execute(
                    "INSERT INTO tasks VALUES (?,?,'dev','ready',NULL,NULL,NULL,1,?)",
                    (task_id, title, priority))
            evidence = json.dumps({
                "output": {"artifact": "report"},
                "verification": {"tests": "passed"},
                "delivery": {"channel": "slack"},
            })
            conn.execute(
                "INSERT INTO task_runs VALUES "
                "(10,'a','dev','completed',NULL,1,2,2,NULL,'completed',?)", (evidence,))
            conn.execute(
                "INSERT INTO task_runs VALUES "
                "(11,'d','dev','done',NULL,1,2,2,NULL,'failed',?)", (evidence,))
            conn.execute("INSERT INTO task_attachments VALUES (1,'a','proof.txt',2)")
            conn.execute("INSERT INTO task_attachments VALUES (2,'b','brief.pdf',2)")
            conn.execute("INSERT INTO task_attachments VALUES (3,'d','draft.txt',2)")
            conn.execute("INSERT INTO task_events VALUES (1,'a','created','{}',2)")
            conn.execute("INSERT INTO task_events VALUES (2,'c','heartbeat','{}',2)")
            conn.commit()

        with contextlib.closing(dashboard._read_db(self.db)) as conn:
            data = dashboard.compute_dashboard(conn, 2000)
        self.assertEqual(
            [("첫 번째", 95), ("두 번째", 25), ("세 번째", 0), ("네 번째", 95)],
            [(item["title"], item["percent"]) for item in data["remaining"]],
        )
        queue_rows = [block["text"]["text"] for block in
                      dashboard.render_blocks(dashboard.render(data), data)[-4:]]
        self.assertEqual([
            "1. [대기] 첫 번째 · 95%", "2. [대기] 두 번째 · 25%",
            "3. [대기] 세 번째 · 0%", "4. [대기] 네 번째 · 95%",
        ], queue_rows)

        with contextlib.closing(sqlite3.connect(self.db)) as conn:
            conn.execute("UPDATE tasks SET status='done' WHERE id='a'")
            conn.commit()
        with contextlib.closing(dashboard._read_db(self.db)) as conn:
            promoted = dashboard.compute_dashboard(conn, 2000)
        self.assertEqual(["두 번째", "세 번째", "네 번째", "다섯 번째"],
                         [item["title"] for item in promoted["remaining"]])

    def test_render_excludes_internal_identifiers_statuses_logs_and_self_development(self):
        with contextlib.closing(sqlite3.connect(self.db)) as conn:
            conn.execute("ALTER TABLE tasks ADD COLUMN body TEXT")
            conn.execute("ALTER TABLE tasks ADD COLUMN result TEXT")
            conn.execute("ALTER TABLE tasks ADD COLUMN last_failure_error TEXT")
            conn.execute(
                "INSERT INTO tasks VALUES "
                "('CARD-SECRET-42','고객 보고서','dev','running',123,1,1900,1,"
                "'self-development notes','internal logs: traceback','raw-worker-log')")
            conn.execute(
                "INSERT INTO task_runs VALUES "
                "(1,'CARD-SECRET-42','dev','running',123,1,NULL,1900,NULL)")
            conn.execute(
                "CREATE TABLE task_events (id INTEGER PRIMARY KEY, task_id TEXT, kind TEXT, payload TEXT)")
            conn.execute(
                "INSERT INTO task_events VALUES "
                "(1,'CARD-SECRET-42','worker_log','self-development internal logs')")
            for task_id, title, status in (
                    ("NO-TITLE", None, "ready"),
                    ("K-PRIVATE", "[자기개발] 독서", "ready"),
                    ("E-PRIVATE", "Self Development plan", "ready"),
                    ("ARCHIVED", "보관 카드", "archived"),
                    ("TRIAGE", "분류 카드", "triage"),
                    ("INTERNAL", "내부 카드", "internal")):
                conn.execute(
                    "INSERT INTO tasks VALUES (?,?,'dev',?,NULL,NULL,NULL,2,NULL,NULL,NULL)",
                    (task_id, title, status))
            conn.commit()
        with contextlib.closing(dashboard._read_db(self.db)) as conn, \
                mock.patch.object(dashboard, "_pid_alive", return_value=True):
            data = dashboard.compute_dashboard(conn, 2000)
        text = dashboard.render(data)
        exposed = text + json.dumps(
            dashboard.render_blocks(text, data), ensure_ascii=False)

        self.assertIn("고객 보고서", exposed)
        for internal in ("CARD-SECRET-42", "running", "ready", "worker_log",
                         "internal logs", "raw-worker-log", "self-development",
                         "NO-TITLE", "K-PRIVATE", "E-PRIVATE", "ARCHIVED", "TRIAGE",
                         "INTERNAL", "자기개발", "Self Development", "보관 카드",
                         "분류 카드", "내부 카드"):
            self.assertNotIn(internal, exposed)

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
            data = dashboard.compute_dashboard(conn, 2000)
        self.assertEqual("Do now", data["focus"]["title"])
        with contextlib.closing(sqlite3.connect(self.db)) as conn:
            conn.execute("DELETE FROM task_runs")
            conn.execute("UPDATE tasks SET status='done' WHERE id='running'")
            conn.commit()
        with contextlib.closing(dashboard._read_db(self.db)) as conn:
            self.assertEqual(
                "Need answer", dashboard.compute_dashboard(conn, 2000)["focus"]["title"])
        with contextlib.closing(sqlite3.connect(self.db)) as conn:
            conn.execute("UPDATE tasks SET block_kind='dependency' WHERE id='blocked'")
            conn.commit()
        with contextlib.closing(dashboard._read_db(self.db)) as conn:
            self.assertEqual(
                "Need answer", dashboard.compute_dashboard(conn, 2000)["focus"]["title"])

    def test_profile_selection_accepts_normal_blocked_status_before_ready(self):
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
        self.assertEqual("capability", data["focus"]["id"])
        with contextlib.closing(sqlite3.connect(self.db)) as conn:
            conn.execute(
                "INSERT INTO tasks VALUES ('answer', 'answer', 'dev', 'blocked', "
                "NULL, NULL, NULL, 1, 'needs_input')")
            conn.commit()
        with contextlib.closing(dashboard._read_db(self.db)) as conn:
            data = dashboard.compute_dashboard(conn, 2000)
        self.assertEqual("answer", data["focus"]["id"])

    def test_review_is_visible_as_profile_focus(self):
        with contextlib.closing(sqlite3.connect(self.db)) as conn:
            conn.execute(
                "INSERT INTO tasks VALUES "
                "('review','검수 대기','dev','review',NULL,NULL,NULL,1)"
            )
            conn.commit()

        with contextlib.closing(dashboard._read_db(self.db)) as conn:
            data = dashboard.compute_dashboard(conn, 2000)

        dev = next(row for row in data["profiles"] if row["name"] == "개발")
        self.assertEqual(
            {"title": "검수 대기", "state": "review"},
            dev["work"],
        )
        self.assertIn("검토: 검수 대기", dashboard.render(data))

    def test_dashboard_warns_on_recovery_dependency_stall_and_queue_imbalance(self):
        with contextlib.closing(sqlite3.connect(self.db)) as conn:
            conn.execute("ALTER TABLE tasks ADD COLUMN priority INTEGER")
            conn.execute(
                "CREATE TABLE task_events "
                "(id INTEGER PRIMARY KEY, task_id TEXT, kind TEXT, payload TEXT, created_at INTEGER)"
            )
            for index in range(3):
                conn.execute(
                    "INSERT INTO tasks VALUES (?,?, 'dev','ready',NULL,NULL,NULL,1,?)",
                    (f"dev-{index}", f"개발 작업 {index}", 30 - index),
                )
            conn.execute(
                "INSERT INTO tasks VALUES "
                "('nas','NAS 복구','research','blocked',NULL,NULL,NULL,1,50)"
            )
            conn.execute(
                "INSERT INTO tasks VALUES "
                "('child','후속 작업','plan','todo',NULL,NULL,NULL,1,20)"
            )
            conn.executemany(
                "INSERT INTO task_events VALUES (?,?,?,?,?)",
                (
                    (1, "nas", "gave_up", "{}", 1900),
                    (2, "child", "coordination_required",
                     '{"kinds":["dependency_stall"]}', 1901),
                    (3, "nas", "commented", "{}", 1902),
                ),
            )
            conn.commit()

        with contextlib.closing(dashboard._read_db(self.db)) as conn:
            data = dashboard.compute_dashboard(conn, 2000)

        warnings = data["warnings"]
        self.assertTrue(any("자동복구" in warning and "NAS 복구" in warning
                            for warning in warnings))
        self.assertTrue(any("의존성 정체" in warning and "후속 작업" in warning
                            for warning in warnings))
        self.assertTrue(any("대기열 불균형" in warning and "개발" in warning
                            for warning in warnings))
        rendered = dashboard.render(data)
        self.assertTrue(all(warning in rendered for warning in warnings))

    def test_non_object_coordination_payload_does_not_break_dashboard(self):
        with contextlib.closing(sqlite3.connect(self.db)) as conn:
            conn.execute(
                "CREATE TABLE task_events "
                "(id INTEGER PRIMARY KEY, task_id TEXT, kind TEXT, payload TEXT)"
            )
            conn.execute(
                "INSERT INTO tasks VALUES "
                "('child','후속 작업','plan','todo',NULL,NULL,NULL,1)"
            )
            conn.execute(
                "INSERT INTO task_events VALUES "
                "(1,'child','coordination_required','[]')"
            )
            conn.commit()

        with contextlib.closing(dashboard._read_db(self.db)) as conn:
            data = dashboard.compute_dashboard(conn, 2000)

        self.assertEqual([], data["warnings"])

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
        self.state.write_text(json.dumps({"fingerprint": "legacy"}), encoding="utf-8")
        posts = []
        code, output = self.invoke(
            lambda *args: self.fail("must not update a missing message"),
            verifier=lambda *args: False,
            poster=lambda channel, body, token, timeout: posts.append((channel, body)) or "new-ts",
        )
        self.assertEqual((0, ""), (code, output))
        self.assertEqual([("C0BPXD9TBB7", text)], posts)
        saved = json.loads(self.state.read_text())
        self.assertEqual("new-ts", saved["ts"])
        self.assertNotEqual("legacy", saved["fingerprint"])

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
            code, output = self.invoke_with_env_file(
                env_file, lambda *args: self.fail("updated"),
                poster=lambda *args: calls.append(args) or "local-ts")
            self.assertNotIn("OTHER_SECRET", os.environ)
            self.assertNotIn("SLACK_BOT_TOKEN", os.environ)
        self.assertEqual((0, ""), (code, output))
        self.assertEqual("literal-$OTHER_SECRET", calls[0][2])

    def test_existing_environment_token_takes_priority_without_reading_env_file(self):
        missing = Path(self.tmp.name) / "must-not-be-read"
        calls = []
        code, output = self.invoke_with_env_file(
            missing, lambda *args: self.fail("updated"), token_getter=lambda: "existing-token",
            poster=lambda *args: calls.append(args) or "local-ts")
        self.assertEqual((0, ""), (code, output))
        self.assertEqual("existing-token", calls[0][2])

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
