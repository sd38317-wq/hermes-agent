from __future__ import annotations

import contextlib
import fcntl
import io
import json
import sqlite3
import subprocess
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
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

    def run_human(self, *, now: int = 2000):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = watch.main([
                "--notification-mode", "human-only",
                "--db", str(self.db),
                "--evidence", str(self.evidence),
                "--state", str(self.root / "notification-state.json"),
                "--now", str(now),
            ])
        return code, output.getvalue()

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
        self.assertEqual(
            [{"pid": 321, "alive": True, "task_ids": ["t_adf495b7"]}],
            result["pid_reconciliation"]["results"],
        )

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

    def test_all_queries_share_one_sqlite_snapshot(self):
        with contextlib.closing(sqlite3.connect(self.db)) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(
                "INSERT INTO tasks VALUES "
                "('A','a','dev','running',1800,1800,70,500,1990,NULL)"
            )
            conn.execute(
                "INSERT INTO task_runs VALUES "
                "(70,'A','dev','running',500,1800,NULL,1990)"
            )
            conn.commit()

        reader = watch._read_db(self.db, 1.0)
        changed = False

        def mutate_after_task_snapshot(statement):
            nonlocal changed
            if changed or not statement.startswith("SELECT * FROM tasks"):
                return
            changed = True
            with contextlib.closing(sqlite3.connect(self.db)) as writer:
                writer.execute("UPDATE tasks SET status='done' WHERE id='A'")
                writer.execute(
                    "UPDATE task_runs SET status='done', ended_at=1995 WHERE id=70"
                )
                writer.commit()

        reader.set_trace_callback(mutate_after_task_snapshot)
        try:
            with watch.pid_probe(lambda pid: True):
                result = watch.collect_evidence(reader, now=2000)
        finally:
            reader.close()

        self.assertTrue(changed)
        self.assertEqual("PASS", result["status"])
        self.assertEqual([], result["findings"])

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

    def test_recent_reclaim_to_ready_resets_ready_age(self):
        with contextlib.closing(sqlite3.connect(self.db)) as conn:
            conn.execute(
                "INSERT INTO tasks VALUES "
                "('RECLAIMED','recent','dev','ready',1,NULL,NULL,NULL,NULL,NULL)"
            )
            conn.execute(
                "INSERT INTO task_events VALUES "
                "(1,'RECLAIMED','reclaimed','{\"retry_status\":\"ready\"}',1990)"
            )
            conn.commit()

        _, result = self.run_main()

        self.assertNotIn("ready_stale", {item["kind"] for item in result["findings"]})

    def test_recent_changes_requested_to_ready_resets_ready_age(self):
        with contextlib.closing(sqlite3.connect(self.db)) as conn:
            conn.execute(
                "INSERT INTO tasks VALUES "
                "('CHANGES','recent','dev','ready',1,NULL,NULL,NULL,NULL,NULL)"
            )
            conn.execute(
                "INSERT INTO task_events VALUES "
                "(1,'CHANGES','changes_requested','{\"status\":\"ready\"}',1990)"
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

    def test_35_existing_blockers_then_new_high_priority_capability_alerts_once(self):
        with contextlib.closing(sqlite3.connect(self.db)) as conn:
            conn.execute("ALTER TABLE tasks ADD COLUMN priority INTEGER")
            conn.executemany(
                "INSERT INTO tasks VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                [
                    (f"CARD-{index}", f"승인 요청 {index}", "plan", "blocked", 1800,
                     None, None, None, None, "needs_input", 1)
                    for index in range(35)
                ],
            )
            conn.commit()

        self.assertEqual((0, ""), self.run_human())

        with contextlib.closing(sqlite3.connect(self.db)) as conn:
            conn.execute(
                "INSERT INTO tasks VALUES "
                "('NEW-CAPABILITY','긴급 운영 권한','ops','blocked',2001,NULL,NULL,NULL,NULL,"
                "'capability',100)"
            )
            conn.execute(
                "INSERT INTO task_events VALUES (1,'NEW-CAPABILITY','blocked',?,2001)",
                (json.dumps({
                    "reason": "운영 권한이 없습니다. 최소 조치: 권한을 부여해 주세요."
                }),),
            )
            conn.commit()

        code, rendered = self.run_human(now=2001)

        self.assertEqual(0, code)
        self.assertEqual(
            ["제목", "원인", "영향", "최소 조치", "후속 확인"],
            [line.split(":", 1)[0] for line in rendered.strip().splitlines()],
        )
        self.assertIn("제목: 긴급 운영 권한", rendered)
        self.assertNotIn("승인 요청", rendered)
        self.assertEqual((0, ""), self.run_human(now=2002))

    def test_reason_action_fingerprint_ignores_title_and_alerts_on_event_reason_change(self):
        with contextlib.closing(sqlite3.connect(self.db)) as conn:
            conn.execute(
                "INSERT INTO tasks VALUES "
                "('SECRET-ID','배포 승인','plan','blocked',1800,NULL,NULL,NULL,NULL,'needs_input')"
            )
            conn.execute(
                "INSERT INTO task_events VALUES "
                "(1,'SECRET-ID','blocked',?,1900)",
                (json.dumps({"reason": "고객 승인 대기. 최소 조치: 승인 여부를 알려 주세요."}),),
            )
            conn.commit()

        self.assertEqual("", self.run_human()[1])
        with contextlib.closing(sqlite3.connect(self.db)) as conn:
            conn.execute("UPDATE tasks SET title='긴급 배포 승인' WHERE id='SECRET-ID'")
            conn.commit()
        self.assertEqual("", self.run_human(now=2001)[1])

        with contextlib.closing(sqlite3.connect(self.db)) as conn:
            conn.execute(
                "INSERT INTO task_events VALUES "
                "(2,'SECRET-ID','block_loop_detected',?,2002)",
                (json.dumps({"reason": "법무 승인 대기. 필요한 조치: 승인 담당자를 지정해 주세요."}),),
            )
            conn.commit()

        changed = self.run_human(now=2002)[1]
        self.assertIn("원인: 법무 승인 대기.", changed)
        self.assertIn("최소 조치: 승인 담당자를 지정해 주세요.", changed)
        self.assertNotIn("SECRET-ID", changed)
        self.assertEqual("", self.run_human(now=2003)[1])

    def test_newer_run_summary_overrides_older_block_event_reason(self):
        with contextlib.closing(sqlite3.connect(self.db)) as conn:
            conn.execute("ALTER TABLE task_runs ADD COLUMN summary TEXT")
            conn.execute(
                "INSERT INTO tasks VALUES "
                "('LATEST','배포 승인','plan','blocked',1800,NULL,NULL,NULL,NULL,'needs_input')"
            )
            conn.execute(
                "INSERT INTO task_events VALUES (1,'LATEST','blocked',?,1900)",
                (json.dumps({"reason": "기존 승인 대기. 최소 조치: 기존 답변을 알려 주세요."}),),
            )
            conn.commit()
        self.assertEqual("", self.run_human()[1])

        with contextlib.closing(sqlite3.connect(self.db)) as conn:
            conn.execute(
                "INSERT INTO task_runs VALUES "
                "(1,'LATEST','plan','blocked',NULL,1990,2001,NULL,"
                "'보안 승인 대기. 최소 조치: 보안 책임자의 답변을 알려 주세요.')"
            )
            conn.commit()

        rendered = self.run_human(now=2002)[1]
        self.assertIn("보안 승인 대기", rendered)
        self.assertIn("보안 책임자의 답변을 알려 주세요.", rendered)

    def test_internal_identifier_only_change_is_sanitized_and_not_realerted(self):
        task_id = "X7-INTERNAL"
        with contextlib.closing(sqlite3.connect(self.db)) as conn:
            conn.execute(
                "INSERT INTO tasks VALUES "
                "(?, '접근 승인', 'plan', 'blocked', 1800, NULL, NULL, NULL, NULL, "
                "'needs_input')",
                (task_id,),
            )
            conn.execute(
                "INSERT INTO task_events VALUES (1,?,'blocked',?,1900)",
                (task_id, json.dumps({
                    "reason": f"접근 승인이 없습니다. 최소 조치: {task_id} 승인 여부를 알려 주세요."
                })),
            )
            conn.commit()
        self.assertEqual("", self.run_human()[1])

        with contextlib.closing(sqlite3.connect(self.db)) as conn:
            conn.execute(
                "INSERT INTO task_events VALUES (2,?,'blocked',?,2001)",
                (task_id, json.dumps({
                    "reason": "접근 승인이 없습니다. 최소 조치: X8-INTERNAL 승인 여부를 알려 주세요."
                })),
            )
            conn.commit()

        self.assertEqual("", self.run_human(now=2002)[1])

    def test_block_loop_triage_uses_latest_canonical_event_reason(self):
        with contextlib.closing(sqlite3.connect(self.db)) as conn:
            conn.execute(
                "INSERT INTO tasks VALUES "
                "('LOOP','반복 승인','plan','triage',1800,NULL,NULL,NULL,NULL,'needs_input')"
            )
            conn.execute(
                "INSERT INTO task_events VALUES (1,'LOOP','block_loop_detected',?,1900)",
                (json.dumps({"reason": "반복 차단 원인. 최소 조치: 최종 결정을 알려 주세요."}),),
            )
            conn.commit()

        self.assertEqual("", self.run_human()[1])
        state = json.loads((self.root / "notification-state.json").read_text())
        self.assertEqual(1, len(state["active"]))

    def test_run_summary_change_alerts_and_resolution_recurrence_alerts_once(self):
        with contextlib.closing(sqlite3.connect(self.db)) as conn:
            conn.execute("ALTER TABLE task_runs ADD COLUMN summary TEXT")
            conn.execute(
                "INSERT INTO tasks VALUES "
                "('RECURRENCE','승인','plan','blocked',1800,NULL,NULL,NULL,NULL,'needs_input')"
            )
            conn.execute(
                "INSERT INTO task_runs VALUES "
                "(1,'RECURRENCE','plan','done',NULL,1800,1900,NULL,"
                "'보안 승인 대기. 대안 1개: 임시 예외를 승인해 주세요.')"
            )
            conn.commit()

        self.assertEqual("", self.run_human()[1])
        with contextlib.closing(sqlite3.connect(self.db)) as conn:
            conn.execute(
                "UPDATE task_runs SET summary="
                "'재무 승인 대기. 최소 조치: 예산을 승인해 주세요.' WHERE id=1"
            )
            conn.commit()
        self.assertIn("재무 승인 대기", self.run_human(now=2001)[1])

        with contextlib.closing(sqlite3.connect(self.db)) as conn:
            conn.execute("UPDATE tasks SET status='todo' WHERE id='RECURRENCE'")
            conn.commit()
        self.assertEqual("", self.run_human(now=2002)[1])
        with contextlib.closing(sqlite3.connect(self.db)) as conn:
            conn.execute("UPDATE tasks SET status='blocked' WHERE id='RECURRENCE'")
            conn.commit()
        recurrence = self.run_human(now=2003)[1]
        self.assertIn("재무 승인 대기", recurrence)
        self.assertEqual(1, recurrence.count("최소 조치:"))
        self.assertEqual("", self.run_human(now=2004)[1])

    def test_public_alert_is_plain_korean_without_internal_identifiers_or_terms(self):
        with contextlib.closing(sqlite3.connect(self.db)) as conn:
            conn.execute(
                "INSERT INTO tasks VALUES "
                "('t_private_123','고객 데이터 반출 승인','plan','blocked',1800,NULL,NULL,NULL,NULL,'needs_input')"
            )
            conn.commit()
        conn_reason = "고객 동의가 없습니다. 필요한 조치: 반출 동의를 확인해 주세요."
        with contextlib.closing(sqlite3.connect(self.db)) as conn:
            conn.execute("INSERT INTO task_events VALUES (1,'t_private_123','blocked',?,1900)",
                         (json.dumps({"reason": conn_reason}),))
            conn.commit()
        self.run_human()
        with contextlib.closing(sqlite3.connect(self.db)) as conn:
            conn.execute(
                "INSERT INTO task_events VALUES (2,'t_private_123','blocked',?,2001)",
                (json.dumps({"reason": "보안 정책 승인이 없습니다. 대안 1개: 책임자 승인을 받아 주세요. "
                                       "task-abc run-77 PID 123 deadbeef"}),),
            )
            conn.commit()

        _, rendered = self.run_human(now=2001)

        self.assertEqual(
            ["제목", "원인", "영향", "최소 조치", "후속 확인"],
            [line.split(":", 1)[0] for line in rendered.strip().splitlines()],
        )
        self.assertEqual(1, rendered.count("최소 조치:"))
        for forbidden in ("t_private_123", "task", "run", "PID", "deadbeef",
                          "block_kind", "needs_input", "capability", "blocked"):
            self.assertNotIn(forbidden, rendered)

    def test_selects_one_highest_operational_human_blocker_and_filters_noise(self):
        with contextlib.closing(sqlite3.connect(self.db)) as conn:
            conn.execute("ALTER TABLE tasks ADD COLUMN operational_impact INTEGER")
            conn.execute("ALTER TABLE tasks ADD COLUMN priority INTEGER")
            conn.execute("ALTER TABLE tasks ADD COLUMN urgency INTEGER")
            conn.executemany(
                "INSERT INTO tasks VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    ("A", "낮은 승인", "plan", "blocked", 1, None, None, None, None,
                     "needs_input", 1, 9, 9),
                    ("Z", "서비스 중단 승인", "plan", "blocked", 1, None, None, None, None,
                     "needs_input", 10, 1, 1),
                    ("TECH", "기술 검증 완료 확인", "dev", "blocked", 1, None, None, None, None,
                     "needs_input", 99, 99, 99),
                    ("BOT", "봇 자동 복구 대기", "dev", "blocked", 1, None, None, None, None,
                     "capability", 99, 99, 99),
                    ("DEP", "선행 작업 대기", "dev", "blocked", 1, None, None, None, None,
                     "dependency", 99, 99, 99),
                ),
            )
            conn.commit()
        with contextlib.closing(sqlite3.connect(self.db)) as conn:
            for index, task_id in enumerate(("A", "Z"), 1):
                conn.execute(
                    "INSERT INTO task_events VALUES (?,?,?,?,?)",
                    (index, task_id, "blocked", json.dumps({"reason": "승인 대기"}), 100 + index),
                )
            conn.commit()
        self.run_human()
        with contextlib.closing(sqlite3.connect(self.db)) as conn:
            conn.execute("INSERT INTO task_events VALUES (10,'A','blocked',?,2001)",
                         (json.dumps({"reason": "낮은 영향 변경. 최소 조치: 답해 주세요."}),))
            conn.execute("INSERT INTO task_events VALUES (11,'Z','blocked',?,2000)",
                         (json.dumps({"reason": "서비스 중단 변경. 최소 조치: 승인해 주세요."}),))
            conn.commit()

        _, rendered = self.run_human(now=2001)

        self.assertIn("서비스 중단 변경", rendered)
        self.assertNotIn("낮은 영향 변경", rendered)
        self.assertNotIn("기술 검증", rendered)
        self.assertNotIn("봇 자동", rendered)
        self.assertEqual(1, rendered.count("제목:"))
        deferred = self.run_human(now=2002)[1]
        self.assertIn("낮은 영향 변경", deferred)
        self.assertNotIn("서비스 중단 변경", deferred)
        self.assertEqual("", self.run_human(now=2003)[1])

    def test_ranking_final_tie_uses_created_or_event_recency_not_id_or_title(self):
        with contextlib.closing(sqlite3.connect(self.db)) as conn:
            conn.execute("ALTER TABLE tasks ADD COLUMN operational_impact INTEGER")
            conn.execute("ALTER TABLE tasks ADD COLUMN priority INTEGER")
            conn.execute("ALTER TABLE tasks ADD COLUMN urgency INTEGER")
            conn.executemany(
                "INSERT INTO tasks VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (("Z", "가", "plan", "blocked", 100, None, None, None, None,
                  "needs_input", 1, 1, 1),
                 ("A", "하", "plan", "blocked", 200, None, None, None, None,
                  "needs_input", 1, 1, 1)),
            )
            conn.commit()
        self.run_human()
        with contextlib.closing(sqlite3.connect(self.db)) as conn:
            conn.executemany(
                "INSERT INTO task_events VALUES (?,?,?,?,?)",
                ((1, "Z", "blocked", json.dumps({"reason": "오래된 사유"}), 300),
                 (2, "A", "blocked", json.dumps({"reason": "최신 사유"}), 400)),
            )
            conn.commit()
        rendered = self.run_human(now=2001)[1]
        self.assertIn("최신 사유", rendered)
        self.assertNotIn("오래된 사유", rendered)

    def test_canonical_fields_and_korean_markers_exclude_non_actionable_blocks(self):
        with contextlib.closing(sqlite3.connect(self.db)) as conn:
            for name in ("actioned_at", "verification_status", "bot_resolving",
                         "dependency_task_id"):
                conn.execute(f"ALTER TABLE tasks ADD COLUMN {name} TEXT")
            conn.executemany(
                "INSERT INTO tasks VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (("ACTIONED", "승인", "plan", "blocked", 1, None, None, None, None,
                  "needs_input", "yes", None, None, None),
                 ("VERIFY", "기술 검증 대기", "plan", "blocked", 1, None, None, None, None,
                  "needs_input", None, "pending", None, None),
                 ("BOT", "자동 해결 중", "plan", "blocked", 1, None, None, None, None,
                  "needs_input", None, None, "yes", None),
                 ("DEP", "승인", "plan", "blocked", 1, None, None, None, None,
                  "needs_input", None, None, None, "PARENT")),
            )
            conn.executemany(
                "INSERT INTO task_events VALUES (?,?,?,?,?)",
                ((1, "ACTIONED", "blocked", json.dumps({"reason": "이미 조치됨"}), 10),
                 (2, "VERIFY", "blocked", json.dumps({"reason": "기술 검증 결과 대기"}), 10),
                 (3, "BOT", "blocked", json.dumps({"reason": "봇이 자동 복구 중"}), 10),
                 (4, "DEP", "blocked", json.dumps({"reason": "선행 작업 완료 대기"}), 10)),
            )
            conn.commit()
        self.assertEqual("", self.run_human()[1])
        state = json.loads((self.root / "notification-state.json").read_text())
        self.assertEqual({}, state["active"])

    def test_corrupt_prior_state_is_replaced_as_silent_baseline(self):
        with contextlib.closing(sqlite3.connect(self.db)) as conn:
            conn.execute(
                "INSERT INTO tasks VALUES "
                "('ONE','승인','plan','blocked',1800,NULL,NULL,NULL,NULL,'needs_input')"
            )
            conn.commit()
        state = self.root / "notification-state.json"
        state.write_text("{broken", encoding="utf-8")

        self.assertEqual("", self.run_human()[1])
        self.assertEqual(1, len(json.loads(state.read_text())["active"]))
        self.assertEqual("", self.run_human(now=2001)[1])

    def test_state_path_cannot_overwrite_database(self):
        with contextlib.closing(sqlite3.connect(self.db)) as conn:
            conn.execute(
                "INSERT INTO tasks VALUES "
                "('ONE','승인','plan','blocked',1800,NULL,NULL,NULL,NULL,'needs_input')"
            )
            conn.commit()
        before = self.db.read_bytes()

        with contextlib.redirect_stdout(io.StringIO()):
            watch.main([
                "--notification-mode", "human-only",
                "--db", str(self.db),
                "--evidence", str(self.evidence),
                "--state", str(self.db),
                "--now", "2000",
            ])

        self.assertEqual(before, self.db.read_bytes())

    def test_state_path_cannot_overwrite_evidence(self):
        with contextlib.closing(sqlite3.connect(self.db)) as conn:
            conn.execute(
                "INSERT INTO tasks VALUES "
                "('ONE','승인','plan','blocked',1800,NULL,NULL,NULL,NULL,'needs_input')"
            )
            conn.commit()
        original = b'{"timestamp":"2026-01-01T00:00:00+00:00"}\n'
        self.evidence.write_bytes(original)

        with contextlib.redirect_stdout(io.StringIO()):
            watch.main([
                "--notification-mode", "human-only",
                "--db", str(self.db),
                "--evidence", str(self.evidence),
                "--state", str(self.evidence),
                "--now", "2000",
            ])

        self.assertTrue(self.evidence.read_bytes().startswith(original))

    def test_overlapping_older_snapshot_cannot_overwrite_newer_notification_state(self):
        with contextlib.closing(sqlite3.connect(self.db)) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(
                "INSERT INTO tasks VALUES "
                "('RACE','운영 승인','plan','blocked',1800,NULL,NULL,NULL,NULL,'needs_input')"
            )
            conn.execute(
                "INSERT INTO task_events VALUES (1,'RACE','blocked',?,1900)",
                (json.dumps({"reason": "최초 원인. 최소 조치: 최초 답변을 알려 주세요."}),),
            )
            conn.commit()
        self.assertEqual("", self.run_human()[1])
        with contextlib.closing(sqlite3.connect(self.db)) as conn:
            conn.execute(
                "INSERT INTO task_events VALUES (2,'RACE','blocked',?,2001)",
                (json.dumps({"reason": "이전 원인. 최소 조치: 이전 답변을 알려 주세요."}),),
            )
            conn.commit()

        older_collected = threading.Event()
        release_older = threading.Event()
        newer_finished = threading.Event()
        original_candidates = watch._notification_candidates

        def delayed_candidates(conn, record):
            candidates = original_candidates(conn, record)
            if threading.current_thread().name == "older-snapshot":
                older_collected.set()
                self.assertTrue(release_older.wait(timeout=2))
            return candidates

        def invoke(now):
            try:
                watch.main([
                    "--notification-mode", "human-only",
                    "--db", str(self.db),
                    "--evidence", str(self.evidence),
                    "--state", str(self.root / "notification-state.json"),
                    "--now", str(now),
                ])
            finally:
                if threading.current_thread().name == "newer-snapshot":
                    newer_finished.set()

        watch._notification_candidates = delayed_candidates
        try:
            older = threading.Thread(target=invoke, args=(2001,), name="older-snapshot")
            older.start()
            self.assertTrue(older_collected.wait(timeout=2))
            with contextlib.closing(sqlite3.connect(self.db)) as conn:
                conn.execute(
                    "INSERT INTO task_events VALUES (3,'RACE','blocked',?,2002)",
                    (json.dumps({"reason": "최신 원인. 최소 조치: 최신 답변을 알려 주세요."}),),
                )
                conn.commit()
            newer = threading.Thread(target=invoke, args=(2002,), name="newer-snapshot")
            newer.start()
            newer_finished.wait(timeout=1)
            release_older.set()
            older.join(timeout=2)
            newer.join(timeout=2)
            self.assertFalse(older.is_alive())
            self.assertFalse(newer.is_alive())
        finally:
            release_older.set()
            watch._notification_candidates = original_candidates

        self.assertEqual("", self.run_human(now=2003)[1])

    def test_three_concurrent_runs_append_three_complete_distinct_records(self):
        with contextlib.closing(sqlite3.connect(self.db)) as conn:
            conn.execute(
                "INSERT INTO tasks VALUES "
                "('HEALTHY','ok','dev','running',1800,1800,80,600,1990,NULL)"
            )
            conn.execute(
                "INSERT INTO task_runs VALUES "
                "(80,'HEALTHY','dev','running',600,1800,NULL,1990)"
            )
            conn.commit()

        command = [
            "python3", str(Path(watch.__file__)), "--db", str(self.db),
            "--evidence", str(self.evidence),
        ]
        with ThreadPoolExecutor(max_workers=3) as pool:
            runs = list(pool.map(lambda _: subprocess.run(
                command, capture_output=True, text=True, check=False,
            ), range(3)))

        self.assertTrue(all(run.returncode in {0, 2} for run in runs))
        records = [json.loads(line) for line in self.evidence.read_text().splitlines()]
        stdout_records = [json.loads(run.stdout) for run in runs]
        self.assertEqual(3, len(records))
        self.assertEqual(3, len({record["timestamp"] for record in records}))
        self.assertEqual(
            {record["timestamp"] for record in records},
            {record["timestamp"] for record in stdout_records},
        )
        for record in records:
            self.assertGreater(record["query_count"], 0)
            self.assertGreater(record["input_row_count"], 0)
            self.assertEqual(len(record["findings"]), record["finding_count"])
            self.assertIn("action_count", record)
            self.assertEqual(
                {"checked", "alive", "missing", "duplicates", "results"},
                set(record["pid_reconciliation"]),
            )

    def test_append_rejects_oversized_record_without_changing_evidence(self):
        original = b'{"timestamp":"2026-01-01T00:00:00+00:00"}\n'
        self.evidence.write_bytes(original)
        record = {
            "timestamp": "2026-01-01T00:00:01+00:00",
            "detail": "x" * watch.MAX_EVIDENCE_RECORD_BYTES,
        }

        with self.assertRaisesRegex(ValueError, "크기 상한"):
            watch.append_evidence(self.evidence, record)

        self.assertEqual(original, self.evidence.read_bytes())

    def test_append_lock_wait_is_bounded(self):
        self.evidence.write_text("")
        locked = threading.Event()

        def hold_lock():
            with self.evidence.open("r+") as handle:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                locked.set()
                time.sleep(watch.EVIDENCE_LOCK_TIMEOUT_SECONDS + 0.3)

        thread = threading.Thread(target=hold_lock)
        thread.start()
        self.assertTrue(locked.wait(timeout=1))
        started = time.monotonic()
        try:
            with self.assertRaisesRegex(TimeoutError, "잠금"):
                watch.append_evidence(self.evidence, {
                    "timestamp": "2026-01-01T00:00:00+00:00",
                })
        finally:
            thread.join()
        self.assertLess(time.monotonic() - started, 2.0)

    def test_no_agent_notification_paths_are_end_to_end_silent_or_korean(self):
        def invoke(task_values):
            self.evidence.unlink(missing_ok=True)
            with contextlib.closing(sqlite3.connect(self.db)) as conn:
                conn.execute("DELETE FROM tasks")
                conn.execute("DELETE FROM task_runs")
                conn.execute("INSERT INTO tasks VALUES (?,?,?,?,?,?,?,?,?,?)", task_values)
                conn.commit()
            return subprocess.run(
                ["python3", str(Path(watch.__file__)),
                 "--notification-mode", "human-only",
                 "--db", str(self.db), "--evidence", str(self.evidence),
                 "--now", "2000"],
                capture_output=True, text=True, check=False,
            )

        healthy = invoke(
            ("READY", "ok", "dev", "ready", 1990, None, None, None, None, None)
        )
        self.assertEqual((0, "", ""), (healthy.returncode, healthy.stdout, healthy.stderr))
        self.assertEqual("PASS", json.loads(self.evidence.read_text())["status"])

        non_human = invoke(
            ("STALE", "stale", "dev", "ready", 1, None, None, None, None, None)
        )
        self.assertEqual((0, "", ""),
                         (non_human.returncode, non_human.stdout, non_human.stderr))
        self.assertEqual("FAIL", json.loads(self.evidence.read_text())["status"])

        human = invoke(
            ("HUMAN", "approval", "plan", "blocked", 1800, None, None, None,
             None, "needs_input")
        )
        self.assertEqual(0, human.returncode)
        self.assertEqual("", human.stderr)
        lines = human.stdout.strip().splitlines()
        self.assertEqual(["제목", "원인", "영향", "최소 조치", "후속 확인"],
                         [line.split(":", 1)[0] for line in lines])
        self.assertEqual(1, sum(line.startswith("최소 조치:") for line in lines))
        evidence = json.loads(self.evidence.read_text())
        self.assertEqual("FAIL", evidence["status"])
        self.assertEqual(1, len(evidence["external_alerts"]))

    def test_only_canonical_human_block_kinds_create_external_alerts(self):
        with contextlib.closing(sqlite3.connect(self.db)) as conn:
            conn.executemany(
                "INSERT INTO tasks VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    ("LEGACY", "legacy", "plan", "blocked", 1, None, None, None, None, None),
                    ("INPUT", "input", "plan", "blocked", 1, None, None, None, None, "needs_input"),
                    ("CAP", "capability", "ops", "blocked", 1, None, None, None, None, "capability"),
                    ("UNKNOWN", "unknown", "ops", "blocked", 1, None, None, None, None, "machine_retry"),
                ),
            )
            conn.commit()

        _, result = self.run_main()

        self.assertEqual(1, len(result["external_alerts"]))
        alert = result["external_alerts"][0]
        self.assertIn("LEGACY", alert["cause"])
        self.assertIn("INPUT", alert["cause"])
        self.assertIn("CAP", alert["cause"])
        self.assertNotIn("UNKNOWN", alert["cause"])
        rendered = watch.format_human_notifications(result)
        self.assertEqual(1, rendered.count("최소 조치:"))

    def test_notification_mode_surfaces_detector_error_in_korean(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = watch.main([
                "--notification-mode", "human-only",
                "--db", str(self.root / "missing.db"),
                "--evidence", str(self.evidence),
                "--now", "2000",
            ])

        self.assertEqual(0, code)
        rendered = output.getvalue()
        self.assertEqual(
            ["제목", "원인", "영향", "최소 조치", "후속 확인"],
            [line.split(":", 1)[0] for line in rendered.strip().splitlines()],
        )
        for forbidden in ("ERROR", "ValueError", "JSON", "task", "run", "PID"):
            self.assertNotIn(forbidden, rendered)
        evidence = json.loads(self.evidence.read_text())
        self.assertEqual("ERROR", evidence["status"])

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
