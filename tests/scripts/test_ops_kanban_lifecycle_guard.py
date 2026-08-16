from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import unittest
from unittest import mock
from pathlib import Path

from scripts.ops import kanban_lifecycle_guard as guard
from agent import kanban_tool_ledger as ledger


class LifecycleGuardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.jobs = self.root / "jobs.json"
        self.watch = self.root / "watch.json"
        self.audit = self.root / "audit.jsonl"
        self.state = self.root / "guard.json"
        self.fallback = self.root / "fallback.jsonl"
        self.db = self.root / "kanban.db"
        self.now = 2_000.0

    def tearDown(self):
        self.temp.cleanup()

    def healthy(self):
        self.jobs.write_text(json.dumps({"jobs": [{
            "id": "coordinator-id", "name": guard.DEFAULT_COORDINATOR_NAME, "enabled": True,
            "last_run_at": "1970-01-01T00:32:50+00:00", "last_status": "ok",
            "last_delivery_error": None,
        }]}))
        self.watch.write_text('{"version":2,"cursor":4,"manifests":{},"mutation_event":{}}')
        self.audit.write_text('{"status":"ok"}\n')
        os.utime(self.audit, (self.now, self.now))

    def test_healthy_is_silent_and_changed_incident_dedupes(self):
        self.healthy()
        self.assertIsNone(guard.run(self.jobs, self.watch, self.audit, self.state, now=self.now))
        jobs = json.loads(self.jobs.read_text())
        jobs["jobs"][0]["last_delivery_error"] = "raw secret and destination"
        self.jobs.write_text(json.dumps(jobs))
        first = guard.run(self.jobs, self.watch, self.audit, self.state, now=self.now)
        self.assertIn("확인", first)
        self.assertNotIn("secret", first)
        self.assertIsNone(guard.run(self.jobs, self.watch, self.audit, self.state, now=self.now))

    def test_recovery_allows_same_incident_to_alert_again(self):
        self.healthy()
        self.audit.unlink()
        self.assertIsNotNone(guard.run(self.jobs, self.watch, self.audit, self.state, now=self.now))
        self.healthy()
        self.assertIsNone(guard.run(self.jobs, self.watch, self.audit, self.state, now=self.now))
        self.audit.unlink()
        self.assertIsNotNone(guard.run(self.jobs, self.watch, self.audit, self.state, now=self.now))

    def test_stale_cron_and_interrupted_watcher_alert_once(self):
        self.healthy()
        jobs = json.loads(self.jobs.read_text())
        jobs["jobs"][0]["last_run_at"] = "1970-01-01T00:00:01+00:00"
        self.jobs.write_text(json.dumps(jobs))
        self.watch.write_text("{")
        self.assertIsNotNone(guard.run(self.jobs, self.watch, self.audit, self.state, now=self.now))
        self.assertIsNone(guard.run(self.jobs, self.watch, self.audit, self.state, now=self.now))

    def test_configurable_exact_id_recognizes_renamed_coordinator(self):
        self.healthy()
        jobs = json.loads(self.jobs.read_text())
        jobs["jobs"][0]["name"] = "renamed exactly"
        self.jobs.write_text(json.dumps(jobs))
        self.assertNotIn("cron_missing", guard.incidents(
            self.jobs, self.watch, self.audit, now=self.now,
            coordinator_id="coordinator-id",
        ))

    def test_canonical_production_coordinator_name_is_documented(self):
        docs = Path("docs/operations/kanban-runtime-watch-rollout.md").read_text(encoding="utf-8")
        self.assertIn(f'--name "{guard.DEFAULT_COORDINATOR_NAME}"', docs)
        self.assertNotIn('--name "kanban-lifecycle-coordinator"', docs)

    def test_recent_fallback_incident_alerts_without_raw_data(self):
        self.healthy()
        self.fallback.write_text(json.dumps({
            "version": 1, "kind": "tool_ledger_write_failure", "created_at": int(self.now),
        }) + "\n")
        os.utime(self.fallback, (self.now, self.now))
        message = guard.run(
            self.jobs, self.watch, self.audit, self.state, now=self.now,
            fallback=self.fallback, db=self.db,
        )
        self.assertIsNotNone(message)
        self.assertNotIn("tool_ledger", message)

    def test_excessive_tool_event_total_or_rate_alerts(self):
        self.healthy()
        with sqlite3.connect(self.db) as conn:
            conn.execute("CREATE TABLE task_events (kind TEXT, created_at INTEGER)")
            conn.executemany(
                "INSERT INTO task_events VALUES ('tool_started',?)",
                [(int(self.now),)] * 3,
            )
        with mock.patch.object(guard, "TOOL_EVENT_HOURLY_LIMIT", 2):
            found = guard.incidents(
                self.jobs, self.watch, self.audit, now=self.now,
                fallback=self.fallback, db=self.db,
            )
        self.assertIn("tool_event_growth", found)

    def test_database_lock_creates_private_fallback_and_guard_alert(self):
        self.healthy()
        with sqlite3.connect(self.db) as conn:
            conn.execute(
                "CREATE TABLE task_events (id INTEGER PRIMARY KEY, task_id TEXT, run_id TEXT, "
                "kind TEXT, payload TEXT, created_at INTEGER)"
            )
        locker = sqlite3.connect(self.db, timeout=0)
        try:
            locker.execute("BEGIN EXCLUSIVE")
            env = {
                "HERMES_KANBAN_TASK": "PRIVATE-TASK", "HERMES_KANBAN_RUN_ID": "PRIVATE-RUN",
                "HERMES_KANBAN_DB": str(self.db), "HERMES_KANBAN_ROOT": str(self.root),
            }
            with mock.patch.dict(os.environ, env, clear=False), \
                 mock.patch("agent.delegation_context.is_dispatcher_owned_worker_context", return_value=True):
                ledger.record("tool_started", "PRIVATE-TOOL", "PRIVATE-CALL")
        finally:
            locker.rollback()
            locker.close()
        fallback = self.root / "cron" / "evidence" / "kanban-tool-ledger-incidents.jsonl"
        raw = fallback.read_text(encoding="utf-8")
        for forbidden in ("PRIVATE-TASK", "PRIVATE-RUN", "PRIVATE-TOOL", "PRIVATE-CALL", str(self.db)):
            self.assertNotIn(forbidden, raw)
        record = json.loads(raw.splitlines()[-1])
        found = guard.incidents(
            self.jobs, self.watch, self.audit, now=float(record["created_at"]),
            fallback=fallback, db=self.db,
        )
        self.assertIn("fallback_incident", found)

    def test_stdout_failure_does_not_advance_incident_state(self):
        self.healthy()
        self.audit.unlink()
        with self.assertRaises(BrokenPipeError):
            guard.run(
                self.jobs, self.watch, self.audit, self.state, now=self.now,
                emit=lambda _message: (_ for _ in ()).throw(BrokenPipeError()),
            )
        self.assertFalse(self.state.exists())

    def test_state_write_retries_partial_writes_and_fsyncs_directory(self):
        real_write = os.write
        calls = []
        def partial(fd, data):
            calls.append(len(data))
            return real_write(fd, data[:max(1, len(data) // 2)])
        with mock.patch("scripts.ops.kanban_lifecycle_guard.os.write", side_effect=partial), \
             mock.patch("scripts.ops.kanban_lifecycle_guard.os.fsync", wraps=os.fsync) as fsync:
            guard._save(self.state, "fingerprint")
        self.assertGreater(len(calls), 1)
        self.assertGreaterEqual(fsync.call_count, 2)

    def test_state_write_rejects_symlink_leaf_and_parent(self):
        target = self.root / "target.json"
        target.write_text("{}")
        self.state.symlink_to(target)
        with self.assertRaises(OSError):
            guard._save(self.state, "fingerprint")
        self.state.unlink()
        real_parent = self.root / "real-parent"
        real_parent.mkdir()
        linked_parent = self.root / "linked-parent"
        linked_parent.symlink_to(real_parent, target_is_directory=True)
        with self.assertRaises(OSError):
            guard._save(linked_parent / "guard.json", "fingerprint")


if __name__ == "__main__":
    unittest.main()
