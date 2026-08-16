from __future__ import annotations

import ast
import contextlib
import io
import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts.ops import kanban_lifecycle_watch as watch


CATEGORY_CASES = {
    "start": ("claimed", "spawned"),
    "progress": ("commented", "attached", "claim_extended", "retry_model_selected"),
    "scope_change": watch.CATEGORY_KINDS["scope_change"],
    "blocked": watch.CATEGORY_KINDS["blocked"],
    "resumed": watch.CATEGORY_KINDS["resumed"],
    "completed": ("completed", "review_requested"),
    "internal": watch.CATEGORY_KINDS["internal"],
}

# Production also writes task events through dynamic event_kind variables and
# direct dashboard SQL, not only literal _append_event(...) calls.
PRODUCTION_EXTRA_KINDS = {
    "coordination_required", "crashed", "protocol_violation", "rate_limited",
    "reprioritized", "spawn_failed",
}


class LifecycleWatchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.db = self.root / "kanban.db"
        self.state = self.root / "state.json"
        self.audit = self.root / "audit.jsonl"
        with contextlib.closing(sqlite3.connect(self.db)) as conn:
            conn.executescript(
                """
                CREATE TABLE tasks (
                  id TEXT PRIMARY KEY, title TEXT NOT NULL, body TEXT,
                  assignee TEXT, status TEXT NOT NULL
                );
                CREATE TABLE task_events (
                  id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL,
                  kind TEXT NOT NULL, payload TEXT, created_at INTEGER NOT NULL
                );
                INSERT INTO tasks VALUES
                  ('T1', 'Safe title', 'RAW CARD BODY', 'research', 'running');
                """
            )

    def tearDown(self):
        self.tmp.cleanup()

    def add_event(self, kind, payload=None, *, event_id=None, created_at=1000):
        with contextlib.closing(sqlite3.connect(self.db)) as conn:
            conn.execute(
                "INSERT INTO task_events (id,task_id,kind,payload,created_at) "
                "VALUES (?,?,?,?,?)",
                (event_id, "T1", kind, json.dumps(payload) if payload else None, created_at),
            )
            conn.commit()

    def run_watch(self, *extra):
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = watch.main([
                "--db", str(self.db), "--state", str(self.state),
                "--audit", str(self.audit), *extra,
            ])
        text = stdout.getvalue()
        return code, json.loads(text) if text else None

    def test_every_required_kind_maps_to_a_category(self):
        expected = {kind for kinds in CATEGORY_CASES.values() for kind in kinds}
        self.assertEqual(expected, set(watch.KIND_TO_CATEGORY))
        for category, kinds in CATEGORY_CASES.items():
            for kind in kinds:
                with self.subTest(kind=kind):
                    self.add_event(kind, {"reason": "concise reason"})

        code, batch = self.run_watch("--from-start")
        self.assertEqual(0, code)
        emitted_expected = expected - set(watch.CATEGORY_KINDS["internal"])
        self.assertEqual(len(emitted_expected), len(batch["events"]))
        emitted = {(event["category"], event["kind"]) for event in batch["events"]}
        self.assertEqual(
            {(category, kind) for category, kinds in CATEGORY_CASES.items()
             if category != "internal" for kind in kinds},
            emitted,
        )

    def test_literal_append_event_kinds_are_exhaustively_classified(self):
        source = Path("hermes_cli/kanban_db.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        literals = {
            node.args[2].value
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "_append_event"
            and len(node.args) >= 3
            and isinstance(node.args[2], ast.Constant)
            and isinstance(node.args[2].value, str)
        }
        self.assertEqual(set(), literals - watch.KNOWN_KINDS)

    def test_dynamic_and_direct_sql_production_kinds_are_classified(self):
        self.assertEqual(set(), PRODUCTION_EXTRA_KINDS - watch.KNOWN_KINDS)

    def test_first_run_baselines_silently_then_new_event_emits_once(self):
        self.add_event("claimed")
        self.assertEqual((0, None), self.run_watch())
        self.add_event("blocked", {"reason": "dependency stalled"}, created_at=1001)

        code, batch = self.run_watch()
        self.assertEqual(0, code)
        self.assertEqual(["blocked"], [event["kind"] for event in batch["events"]])
        self.assertEqual("dependency stalled", batch["events"][0]["reason"])
        audit = [json.loads(line) for line in self.audit.read_text().splitlines()]
        self.assertEqual(batch["events"], audit[-1]["events"])
        self.assertEqual((0, None), self.run_watch())

    def test_routine_events_advance_cursor_without_output(self):
        self.assertEqual((0, None), self.run_watch())
        self.add_event("heartbeat")
        self.assertEqual((0, None), self.run_watch())
        audit = json.loads(self.audit.read_text().splitlines()[-1])
        self.assertEqual({"heartbeat": 1, "internal": 0}, audit["aggregated"])

    def test_unknown_kind_fails_closed_without_advancing_cursor(self):
        self.add_event("future_kind")
        code, result = self.run_watch("--from-start")
        self.assertEqual(2, code)
        self.assertEqual("health_error", result["status"])
        self.assertFalse(self.state.exists())

    def test_gap_and_malformed_state_are_audited_health_errors(self):
        self.add_event("claimed", event_id=2)
        code, result = self.run_watch("--from-start")
        self.assertEqual(2, code)
        self.assertEqual("health_error", result["status"])
        self.assertIn("gap", result["error"])

        self.state.write_text("not json", encoding="utf-8")
        code, result = self.run_watch()
        self.assertEqual(2, code)
        self.assertEqual("health_error", result["status"])
        audit = [json.loads(line) for line in self.audit.read_text().splitlines()]
        self.assertEqual("health_error", audit[-1]["status"])

    def test_output_excludes_body_secrets_and_internal_payload_details(self):
        secret = "TOKEN=super-secret"
        self.add_event("crashed", {
            "reason": f"worker pid 4242 failed {secret}",
            "body": "RAW CARD BODY",
            "claim_lock": "lock-private",
            "pid": 4242,
            "nested": {"private": "payload-secret"},
        })
        _, batch = self.run_watch("--from-start")
        rendered = json.dumps(batch)
        for forbidden in (
            "RAW CARD BODY", "super-secret", "4242", "lock-private",
            "payload-secret", "claim_lock", "nested", "pid",
        ):
            self.assertNotIn(forbidden, rendered)
        self.assertEqual(
            {"task_id", "title", "assignee", "category", "kind", "time", "reason"},
            set(batch["events"][0]),
        )

    def test_sanitizer_redacts_structured_secrets_and_locations(self):
        raw = (
            "line\tbreak reason: nested label bearer abc.def provider sk-abcdefgh "
            "password=two word secret; OPENAI_API_KEY=openai secret value; "
            "AWS_SECRET_ACCESS_KEY=aws secret value; GITHUB_TOKEN=github secret value; "
            "https://example.test/x /opt/private/file"
        )
        sanitized = watch._sanitize(raw)
        for forbidden in (
            "\t", "abc.def", "sk-abcdefgh", "two word secret", "openai secret value",
            "aws secret value", "github secret value", "example.test", "/opt/private",
            "reason:",
        ):
            self.assertNotIn(forbidden, sanitized)

    def test_stdout_write_or_flush_failure_does_not_advance_cursor(self):
        self.add_event("claimed")
        for failure in ("write", "flush"):
            with self.subTest(failure=failure):
                for path in (self.state, self.audit):
                    if path.exists():
                        path.unlink()
                broken = mock.Mock()
                broken.write.return_value = 1
                getattr(broken, failure).side_effect = OSError("stdout failed")
                with mock.patch.object(watch.sys, "stdout", broken):
                    code = watch.main([
                        "--db", str(self.db), "--state", str(self.state),
                        "--audit", str(self.audit), "--from-start",
                    ])
                self.assertEqual(2, code)
                self.assertFalse(self.state.exists())
                records = [json.loads(line) for line in self.audit.read_text().splitlines()]
                self.assertFalse(any(record.get("status") == "ok" for record in records))

    def test_batch_boundary_delivers_100_then_one(self):
        for _ in range(101):
            self.add_event("commented")
        code, first = self.run_watch("--from-start")
        self.assertEqual((0, 100), (code, first["count"]))
        code, second = self.run_watch()
        self.assertEqual((0, 1), (code, second["count"]))

    def test_concurrent_runs_serialize_on_lock(self):
        self.add_event("claimed")
        command = [
            sys.executable, "scripts/ops/kanban_lifecycle_watch.py",
            "--db", str(self.db), "--state", str(self.state),
            "--audit", str(self.audit), "--from-start",
        ]
        processes = [subprocess.Popen(command, stdout=subprocess.PIPE, text=True)
                     for _ in range(3)]
        results = [(process.communicate()[0], process.returncode) for process in processes]
        self.assertTrue(all(code == 0 for _, code in results))
        self.assertTrue(all(json.loads(output)["count"] == 1 for output, _ in results))
        self.assertTrue(self.state.exists())
        records = [json.loads(line) for line in self.audit.read_text().splitlines()]
        self.assertEqual(3, len(records))

    def test_rejects_symlink_state_audit_and_lock(self):
        target = self.root / "target"
        target.write_text("{}")
        for path in (self.state, self.audit, Path(str(self.state) + ".lock")):
            with self.subTest(path=path.name):
                if path.exists() or path.is_symlink():
                    path.unlink()
                path.symlink_to(target)
                code, result = self.run_watch("--from-start")
                self.assertEqual(2, code)
                self.assertEqual("health_error", result["status"])
                path.unlink()

    def test_rejects_symlink_parent(self):
        real = self.root / "real"
        real.mkdir()
        linked = self.root / "linked"
        linked.symlink_to(real, target_is_directory=True)
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = watch.main([
                "--db", str(self.db), "--state", str(linked / "state.json"),
                "--audit", str(self.audit), "--from-start",
            ])
        self.assertEqual(2, code)
        self.assertEqual("health_error", json.loads(stdout.getvalue())["status"])
        self.assertFalse((real / "state.json").exists())


if __name__ == "__main__":
    unittest.main()
