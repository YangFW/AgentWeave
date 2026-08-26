from __future__ import annotations

import sqlite3
import tempfile
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from app.services.task_state import (
    ActiveRunConflict,
    InvalidStateTransition,
    PublicationConflict,
    RunIntakeClosed,
    StateNotFoundError,
    TaskCancellationRequested,
    TaskStateError,
    TaskStateService,
    deserialize_checkpoint_state,
    serialize_checkpoint_state,
)
from app.services.goal_spec_service import compile_draft, finalize, public_goal_summary


class TaskStateServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "task-state.db"

        def connect() -> sqlite3.Connection:
            return sqlite3.connect(self.db_path, timeout=5, check_same_thread=False)

        self.connect = connect
        self.service = TaskStateService(connect)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_schema_initialisation_is_idempotent_and_creates_all_tables(self) -> None:
        self.service.init_schema()
        with closing(self.connect()) as conn, conn:
            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                ).fetchall()
            }
        self.assertTrue(
            {"task_runs", "task_nodes", "task_checkpoints", "task_commands"}.issubset(tables)
        )

    def test_public_transaction_shares_write_lock_and_rolls_back_all_tables(self) -> None:
        with closing(self.connect()) as conn, conn:
            conn.execute(
                "CREATE TABLE orchestrator_extension(id TEXT PRIMARY KEY, value TEXT NOT NULL)"
            )

        with self.assertRaisesRegex(RuntimeError, "injected orchestrator failure"):
            with self.service.transaction(write=True) as conn:
                now = "2026-08-15T00:00:00+00:00"
                conn.execute(
                    "INSERT INTO task_runs(id, task_id, attempt, created_at, updated_at) "
                    "VALUES ('transaction-run', 'transaction-task', 1, ?, ?)",
                    (now, now),
                )
                conn.execute(
                    "INSERT INTO orchestrator_extension(id, value) VALUES ('extension-row', 'pending')"
                )
                competitor = sqlite3.connect(self.db_path, timeout=0.01)
                try:
                    with self.assertRaisesRegex(sqlite3.OperationalError, "locked"):
                        competitor.execute("BEGIN IMMEDIATE")
                finally:
                    competitor.close()
                raise RuntimeError("injected orchestrator failure")

        with closing(self.connect()) as conn:
            self.assertIsNone(
                conn.execute(
                    "SELECT id FROM task_runs WHERE id = 'transaction-run'"
                ).fetchone()
            )
            self.assertIsNone(
                conn.execute(
                    "SELECT id FROM orchestrator_extension WHERE id = 'extension-row'"
                ).fetchone()
            )

    def test_legacy_schema_migrates_generation_fence_before_creating_index(self) -> None:
        legacy_path = Path(self.temp_dir.name) / "legacy-task-state.db"

        def legacy_connect() -> sqlite3.Connection:
            return sqlite3.connect(legacy_path, timeout=5, check_same_thread=False)

        with closing(legacy_connect()) as conn, conn:
            conn.executescript(
                """
                CREATE TABLE task_runs (
                    id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL,
                    attempt INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    current_node_id TEXT NOT NULL DEFAULT '',
                    resumed_from_checkpoint_id TEXT NOT NULL DEFAULT '',
                    result_json TEXT NOT NULL DEFAULT '{}',
                    error_json TEXT NOT NULL DEFAULT '{}',
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    started_at TEXT NOT NULL DEFAULT '',
                    finished_at TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE (task_id, attempt)
                );
                CREATE TABLE task_commands (
                    id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL,
                    run_id TEXT,
                    command_type TEXT NOT NULL,
                    payload_json TEXT NOT NULL DEFAULT '{}',
                    status TEXT NOT NULL DEFAULT 'queued',
                    priority INTEGER NOT NULL DEFAULT 0,
                    available_at TEXT NOT NULL,
                    worker_id TEXT NOT NULL DEFAULT '',
                    result_json TEXT NOT NULL DEFAULT '{}',
                    error_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    claimed_at TEXT NOT NULL DEFAULT '',
                    completed_at TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE task_verifications (
                    id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL,
                    run_id TEXT NOT NULL,
                    goal_spec_id TEXT NOT NULL,
                    attempt INTEGER NOT NULL,
                    mode TEXT NOT NULL,
                    status TEXT NOT NULL,
                    report_json TEXT NOT NULL,
                    public_report_json TEXT NOT NULL DEFAULT '{}',
                    verifier_model_id TEXT NOT NULL DEFAULT '',
                    candidate_sha256 TEXT NOT NULL,
                    evidence_sha256 TEXT NOT NULL DEFAULT '',
                    repaired_from_id TEXT NOT NULL DEFAULT '',
                    started_at TEXT NOT NULL,
                    finished_at TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE (run_id, goal_spec_id, attempt)
                );
                """
            )
            conn.execute(
                "INSERT INTO task_runs(id, task_id, attempt, status, finished_at, created_at, updated_at) "
                "VALUES ('legacy-finished', 'task-finished', 1, 'completed', '2026-01-01T00:00:00+00:00', "
                "'2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00')"
            )
            conn.execute(
                "INSERT INTO task_runs(id, task_id, attempt, status, created_at, updated_at) "
                "VALUES ('legacy-active', 'task-active', 1, 'running', "
                "'2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00')"
            )
            conn.execute(
                "INSERT INTO task_commands(id, task_id, run_id, command_type, available_at, created_at, updated_at) "
                "VALUES ('legacy-message', 'task-active', 'legacy-active', 'message', "
                "'2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00', "
                "'2026-01-01T00:00:00+00:00')"
            )

        migrated = TaskStateService(legacy_connect)
        migrated.init_schema()

        with closing(legacy_connect()) as conn, conn:
            run_columns = {
                row[1] for row in conn.execute("PRAGMA table_info(task_runs)").fetchall()
            }
            command_columns = {
                row[1]
                for row in conn.execute("PRAGMA table_info(task_commands)").fetchall()
            }
            verification_columns = {
                row[1]
                for row in conn.execute("PRAGMA table_info(task_verifications)").fetchall()
            }
            finished = conn.execute(
                "SELECT intake_state, intake_closed_at FROM task_runs WHERE id = 'legacy-finished'"
            ).fetchone()
            active = conn.execute(
                "SELECT intake_state, accepted_generation, applied_generation "
                "FROM task_runs WHERE id = 'legacy-active'"
            ).fetchone()
            legacy_command = conn.execute(
                "SELECT intake_generation FROM task_commands WHERE id = 'legacy-message'"
            ).fetchone()
            index_sql = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type = 'index' "
                "AND name = 'idx_task_commands_run_generation'"
            ).fetchone()

        self.assertTrue(
            {
                "intake_state",
                "accepted_generation",
                "applied_generation",
                "published_verification_id",
                "publication_hash",
                "intake_closed_at",
            }.issubset(run_columns)
        )
        self.assertIn("intake_generation", command_columns)
        self.assertIn("intake_generation", verification_columns)
        self.assertEqual(finished[0], "closed")
        self.assertEqual(finished[1], "2026-01-01T00:00:00+00:00")
        self.assertEqual(tuple(active), ("open", 0, 0))
        self.assertEqual(legacy_command[0], 0)
        self.assertIsNotNone(index_sql)

    def test_run_lifecycle_metadata_and_illegal_transition(self) -> None:
        queued = self.service.create_run("task-1", metadata={"source": "api"})
        self.assertEqual(queued["status"], "queued")
        self.assertEqual(queued["attempt"], 1)

        running = self.service.begin_run(
            "task-1", run_id=queued["id"], metadata={"worker": "worker-a"}
        )
        self.assertEqual(running["status"], "running")
        self.assertEqual(running["metadata"], {"source": "api", "worker": "worker-a"})
        self.assertTrue(running["started_at"])

        paused = self.service.transition_run(running["id"], "paused")
        self.assertEqual(paused["status"], "paused")
        resumed = self.service.begin_run("task-1", run_id=running["id"])
        self.assertEqual(resumed["status"], "running")

        completed = self.service.finish_run(
            running["id"], result={"summary": "done"}, metadata={"verified": True}
        )
        self.assertEqual(completed["status"], "completed")
        self.assertEqual(completed["result"]["summary"], "done")
        self.assertTrue(completed["metadata"]["verified"])
        self.assertTrue(completed["finished_at"])

        with self.assertRaises(InvalidStateTransition):
            self.service.begin_run("task-1", run_id=running["id"])
        with self.assertRaises(TaskStateError):
            self.service.create_node(running["id"], "late", "不应创建")

    def test_only_one_active_run_per_task_and_attempt_is_rolled_back(self) -> None:
        first = self.service.begin_run("task-concurrent")
        with self.assertRaises(ActiveRunConflict) as caught:
            self.service.begin_run("task-concurrent", run_id="another-run")
        self.assertEqual(caught.exception.task_id, "task-concurrent")
        self.assertEqual(caught.exception.run_id, first["id"])
        self.assertEqual(
            str(caught.exception),
            f"Task task-concurrent already has active run {first['id']}",
        )
        runs = self.service.list_runs(task_id="task-concurrent")
        self.assertEqual([item["id"] for item in runs], [first["id"]])
        self.assertEqual(runs[0]["attempt"], 1)

    def test_low_level_terminal_transition_closes_nodes_commands_and_generation(self) -> None:
        run = self.service.begin_run("task-terminal-cleanup")
        running = self.service.create_node(run["id"], "execute", "执行中")
        pending = self.service.create_node(run["id"], "later", "尚未执行")
        self.service.transition_node(running["id"], "running")
        message = self.service.enqueue_command(
            "task-terminal-cleanup",
            "message",
            run_id=run["id"],
            payload={"message": "终态前消息"},
        )
        approval = self.service.enqueue_command(
            "task-terminal-cleanup",
            "approval",
            run_id=run["id"],
            payload={"approved": True},
        )

        with self.assertRaises(PublicationConflict) as conflict:
            self.service.finish_run(run["id"], result={"summary": "done"})
        self.assertEqual(conflict.exception.pending_command_types, ("message",))
        unchanged = self.service.get_run(run["id"])
        self.assertEqual(unchanged["status"], "running")
        self.assertEqual(unchanged["accepted_generation"], 1)
        self.assertEqual(unchanged["applied_generation"], 0)
        self.assertEqual(self.service.get_command(message["id"])["status"], "queued")

        claimed = self.service.claim_command(
            "runtime",
            task_id="task-terminal-cleanup",
            run_id=run["id"],
            command_types=["message"],
        )
        self.assertEqual(claimed["id"], message["id"])
        self.service.complete_runtime_commands(
            run["id"],
            {message["id"]: {"applied": True}},
        )
        self.service.cancel_command(approval["id"])
        completed = self.service.finish_run(run["id"], result={"summary": "done"})

        nodes = {item["id"]: item for item in self.service.list_nodes(run["id"])}
        self.assertEqual(completed["status"], "completed")
        self.assertEqual(completed["intake_state"], "closed")
        self.assertEqual(
            completed["accepted_generation"], completed["applied_generation"]
        )
        self.assertEqual(nodes[running["id"]]["status"], "completed")
        self.assertEqual(nodes[pending["id"]]["status"], "skipped")
        self.assertEqual(self.service.get_command(message["id"])["status"], "completed")
        self.assertEqual(self.service.get_command(approval["id"])["status"], "cancelled")
        with self.assertRaises(RunIntakeClosed):
            self.service.enqueue_command(
                "task-terminal-cleanup",
                "approval",
                run_id=run["id"],
                payload={"approved": True},
            )

    def test_low_level_completion_rejects_queued_unbound_runtime_message(self) -> None:
        run = self.service.begin_run("task-unbound-message-fence")
        message = self.service.enqueue_command(
            "task-unbound-message-fence",
            "message",
            payload={"message": "完成前补充的任务级消息"},
        )

        with self.assertRaises(PublicationConflict) as conflict:
            self.service.finish_run(run["id"], result={"summary": "done"})

        self.assertEqual(conflict.exception.pending_command_types, ("message",))
        unchanged = self.service.get_run(run["id"])
        self.assertEqual(unchanged["status"], "running")
        self.assertEqual(unchanged["intake_state"], "open")
        self.assertEqual(self.service.get_command(message["id"])["status"], "queued")

        self.service.cancel_command(message["id"])
        completed = self.service.finish_run(run["id"], result={"summary": "done"})
        self.assertEqual(completed["status"], "completed")

    def test_low_level_completion_rejects_claimed_unbound_cancel(self) -> None:
        run = self.service.begin_run("task-unbound-cancel-fence")
        cancel = self.service.request_cancel(
            "task-unbound-cancel-fence",
            reason="完成竞争窗口中的取消",
        )
        claimed = self.service.claim_command(
            "runtime-worker",
            task_id="task-unbound-cancel-fence",
            run_id=run["id"],
            command_types=["cancel"],
        )
        self.assertEqual(claimed["id"], cancel["id"])
        self.assertIsNone(claimed["run_id"])
        self.assertEqual(claimed["status"], "claimed")

        with self.assertRaises(PublicationConflict) as conflict:
            self.service.transition_run(run["id"], "completed")

        self.assertEqual(conflict.exception.pending_command_types, ("cancel",))
        self.assertEqual(self.service.get_run(run["id"])["status"], "running")
        self.assertEqual(self.service.get_command(cancel["id"])["status"], "claimed")

        self.service.cancel_command(cancel["id"])
        completed = self.service.transition_run(run["id"], "completed")
        self.assertEqual(completed["status"], "completed")

    def test_late_unbound_runtime_input_cannot_cross_completed_run_fence(self) -> None:
        run = self.service.begin_run("task-unbound-late-input")
        completed = self.service.finish_run(run["id"], result={"summary": "done"})
        self.assertEqual(completed["status"], "completed")

        competing_client = TaskStateService(self.connect)
        with self.assertRaises(RunIntakeClosed):
            competing_client.enqueue_command(
                "task-unbound-late-input",
                "message",
                payload={"message": "终态提交后才到达"},
            )
        with self.assertRaises(RunIntakeClosed):
            competing_client.request_cancel(
                "task-unbound-late-input",
                reason="终态提交后才取消",
            )

        self.assertEqual(
            competing_client.list_commands(task_id="task-unbound-late-input"),
            [],
        )

    def test_run_can_resume_from_previous_checkpoint(self) -> None:
        first = self.service.begin_run("task-resume")
        checkpoint = self.service.create_checkpoint(first["id"], {"cursor": 7})
        self.service.finish_run(first["id"], status="failed", error={"message": "stopped"})

        second = self.service.begin_run(
            "task-resume", resumed_from_checkpoint_id=checkpoint["id"]
        )
        self.assertEqual(second["attempt"], 2)
        self.assertEqual(second["resumed_from_checkpoint_id"], checkpoint["id"])
        self.assertEqual(self.service.restore_checkpoint(checkpoint["id"])["state"], {"cursor": 7})

        foreign = self.service.begin_run("different-task")
        with self.assertRaises(TaskStateError):
            self.service.create_run(
                "different-task", resumed_from_checkpoint_id=checkpoint["id"]
            )
        self.service.finish_run(foreign["id"], status="cancelled")

    def test_node_lifecycle_parent_order_and_current_node_projection(self) -> None:
        run = self.service.begin_run("task-nodes")
        root = self.service.create_node(
            run["id"], "execute", "执行任务", input_data={"prompt": "hello"}
        )
        child = self.service.create_node(
            run["id"],
            "tool:search",
            "调用搜索",
            parent_node_id=root["id"],
            kind="mcp",
            metadata={"server": "search"},
        )
        skipped = self.service.create_node(run["id"], "artifact", "生成产物")
        self.assertEqual(
            [item["node_key"] for item in self.service.list_nodes(run["id"])],
            ["execute", "tool:search", "artifact"],
        )
        self.assertEqual(
            [item["id"] for item in self.service.list_nodes(run["id"], parent_node_id=root["id"])],
            [child["id"]],
        )

        started = self.service.start_node(root["id"])
        self.assertEqual(started["status"], "running")
        self.assertEqual(self.service.get_run(run["id"])["current_node_id"], root["id"])
        finished = self.service.finish_node(root["id"], output={"answer": "ok"})
        self.assertEqual(finished["output"], {"answer": "ok"})
        self.assertEqual(self.service.get_run(run["id"])["current_node_id"], "")
        self.assertEqual(self.service.skip_node(skipped["id"])["status"], "skipped")

        with self.assertRaises(InvalidStateTransition):
            self.service.start_node(root["id"])

    def test_node_failure_and_run_guard(self) -> None:
        queued = self.service.create_run("task-queued")
        node = self.service.create_node(queued["id"], "prepare", "准备")
        with self.assertRaises(TaskStateError):
            self.service.start_node(node["id"])

        self.service.begin_run("task-queued", run_id=queued["id"])
        self.service.start_node(node["id"])
        failed = self.service.fail_node(node["id"], {"message": "tool timeout"})
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(failed["error"]["message"], "tool timeout")

    def test_checkpoint_serializer_round_trips_supported_execution_values(self) -> None:
        identifier = uuid.uuid4()
        timestamp = datetime(2026, 8, 11, 9, 30, tzinfo=timezone.utc)
        state = {
            "中文": "内容",
            "path": Path("artifacts/report.docx"),
            "timestamp": timestamp,
            "identifier": identifier,
            "price": Decimal("12.50"),
            "raw": b"\x00\x01",
            "tuple": (1, "two"),
            "set": {"a", "b"},
            ("compound", 1): {"nested": True},
        }
        encoded = serialize_checkpoint_state(state)
        restored = deserialize_checkpoint_state(encoded)
        self.assertEqual(restored, state)
        self.assertNotIn("pickle", encoded.lower())

        with self.assertRaises(TypeError):
            serialize_checkpoint_state({"unsupported": object()})

    def test_checkpoint_metadata_listing_and_restore_audit(self) -> None:
        run = self.service.begin_run("task-checkpoint")
        node = self.service.create_node(run["id"], "step", "步骤")
        first = self.service.create_checkpoint(
            run["id"],
            {"messages": ["one"]},
            node_id=node["id"],
            reason="before tool",
            metadata={"version": 1},
        )
        second = self.service.create_checkpoint(run["id"], {"messages": ["one", "two"]})

        metadata_only = self.service.list_checkpoints(run_id=run["id"])
        self.assertEqual([item["id"] for item in metadata_only], [second["id"], first["id"]])
        self.assertNotIn("state", metadata_only[0])
        self.assertNotIn("state_json", metadata_only[0])
        self.assertEqual(self.service.latest_checkpoint(run["id"])["id"], second["id"])

        restored = self.service.restore_checkpoint(
            first["id"], restore_metadata={"worker": "worker-a"}
        )
        self.assertEqual(restored["state"], {"messages": ["one"]})
        self.assertEqual(restored["restore_count"], 1)
        self.assertEqual(restored["last_restore_metadata"], {"worker": "worker-a"})
        self.assertTrue(restored["restored_at"])

    def test_command_queue_priority_delay_claim_release_and_completion(self) -> None:
        run = self.service.begin_run("task-commands")
        future = datetime.now(timezone.utc) + timedelta(days=1)
        self.service.enqueue_command(
            "task-commands", "future", run_id=run["id"], priority=1000, available_at=future
        )
        low = self.service.enqueue_command(
            "task-commands", "message", run_id=run["id"], payload={"text": "hello"}
        )
        high = self.service.enqueue_command(
            "task-commands", "pause", run_id=run["id"], priority=10
        )

        claimed = self.service.claim_command("worker-a", task_id="task-commands", run_id=run["id"])
        self.assertEqual(claimed["id"], high["id"])
        self.assertEqual(claimed["status"], "claimed")
        completed = self.service.complete_command(claimed["id"], result={"paused": True})
        self.assertEqual(completed["status"], "completed")
        self.assertEqual(completed["result"], {"paused": True})

        claimed_low = self.service.claim_command("worker-a", task_id="task-commands")
        self.assertEqual(claimed_low["id"], low["id"])
        released = self.service.release_command(claimed_low["id"])
        self.assertEqual(released["status"], "queued")
        self.assertEqual(released["worker_id"], "")
        reclaimed = self.service.claim_command("worker-b", command_types=["message"])
        failed = self.service.fail_command(reclaimed["id"], {"message": "rejected"})
        self.assertEqual(failed["error"], {"message": "rejected"})

        with self.assertRaises(InvalidStateTransition):
            self.service.complete_command(completed["id"])

    def test_command_claim_is_atomic_across_service_instances(self) -> None:
        command = self.service.enqueue_command("task-atomic", "message")
        second_service = TaskStateService(self.connect)

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(
                executor.map(
                    lambda item: item[0].claim_command(item[1], task_id="task-atomic"),
                    [(self.service, "worker-a"), (second_service, "worker-b")],
                )
            )
        claimed = [item for item in results if item is not None]
        self.assertEqual(len(claimed), 1)
        self.assertEqual(claimed[0]["id"], command["id"])

    def test_run_bound_non_approval_dedup_does_not_require_policy_metadata(self) -> None:
        run = self.service.begin_run("task-non-approval-dedup")

        first = self.service.enqueue_command(
            "task-non-approval-dedup",
            "maintenance",
            run_id=run["id"],
            payload={"operation": "refresh"},
            deduplicate=True,
        )
        duplicate = self.service.enqueue_command(
            "task-non-approval-dedup",
            "maintenance",
            run_id=run["id"],
            payload={"operation": "refresh"},
            deduplicate=True,
        )

        self.assertEqual(duplicate["id"], first["id"])
        self.assertEqual(
            len(
                self.service.list_commands(
                    task_id="task-non-approval-dedup",
                    run_id=run["id"],
                    command_types=["maintenance"],
                )
            ),
            1,
        )

    def test_cooperative_cancel_is_deduplicated_scoped_and_acknowledgeable(self) -> None:
        run = self.service.begin_run("task-cancel")
        cancel = self.service.request_cancel(
            "task-cancel", run_id=run["id"], reason="user request", requested_by="user-1"
        )
        duplicate = self.service.request_cancel("task-cancel", run_id=run["id"])
        self.assertEqual(duplicate["id"], cancel["id"])
        self.assertTrue(self.service.is_cancel_requested("task-cancel", run_id=run["id"]))
        with self.assertRaises(TaskCancellationRequested):
            self.service.raise_if_cancel_requested("task-cancel", run_id=run["id"])

        claimed = self.service.claim_command(
            "runtime", task_id="task-cancel", run_id=run["id"], command_types=["cancel"]
        )
        self.assertIsNotNone(claimed)
        self.service.complete_command(claimed["id"], result={"cancelled": True})
        self.assertFalse(self.service.is_cancel_requested("task-cancel", run_id=run["id"]))

    def test_goal_specs_are_versioned_immutable_and_recovery_idempotent(self) -> None:
        run = self.service.begin_run("task-goal-spec")
        first = {
            "id": "goal_task_goal_spec_v1",
            "task_id": "task-goal-spec",
            "run_id": run["id"],
            "version": 1,
            "schema_version": "1.0",
            "status": "confirmed",
            "spec_hash": "1" * 64,
            "objective": {"standalone_request": "生成可靠性报告"},
        }
        saved = self.service.save_goal_spec(
            "task-goal-spec",
            run["id"],
            first,
            public_summary={"objective": "生成可靠性报告"},
        )
        replayed = self.service.save_goal_spec(
            "task-goal-spec",
            run["id"],
            first,
            public_summary={"objective": "生成可靠性报告"},
        )
        self.assertEqual(saved["id"], replayed["id"])
        self.assertEqual(saved["spec"]["objective"]["standalone_request"], "生成可靠性报告")
        self.assertEqual(saved["public_summary"], {"objective": "生成可靠性报告"})

        conflicting = {**first, "id": "goal_conflict", "spec_hash": "2" * 64}
        with self.assertRaises(TaskStateError):
            self.service.save_goal_spec("task-goal-spec", run["id"], conflicting)

        revised = {
            **first,
            "id": "goal_task_goal_spec_v2",
            "version": 2,
            "spec_hash": "3" * 64,
            "supersedes_id": first["id"],
            "objective": {"standalone_request": "生成可靠性报告并包含风险"},
        }
        second = self.service.save_goal_spec("task-goal-spec", run["id"], revised)
        self.assertEqual(second["version"], 2)
        self.assertEqual(
            self.service.latest_goal_spec(run_id=run["id"])["id"], revised["id"]
        )

    def test_verification_report_must_match_goal_spec_and_is_idempotent(self) -> None:
        run = self.service.begin_run("task-verification")
        goal = {
            "id": "goal_task_verification_v1",
            "task_id": "task-verification",
            "run_id": run["id"],
            "version": 1,
            "schema_version": "1.0",
            "status": "confirmed",
            "spec_hash": "4" * 64,
        }
        self.service.save_goal_spec("task-verification", run["id"], goal)
        report = {
            "id": "verify_task_verification_1",
            "attempt": 1,
            "mode": "rules_only",
            "status": "passed",
            "started_at": "2026-08-14T00:00:00+00:00",
            "finished_at": "2026-08-14T00:00:01+00:00",
            "criteria": [{"id": "response", "status": "passed"}],
        }
        saved = self.service.save_verification_report(
            "task-verification",
            run["id"],
            goal["id"],
            report,
            public_report={"status": "passed"},
            candidate_sha256="a" * 64,
        )
        replayed = self.service.save_verification_report(
            "task-verification",
            run["id"],
            goal["id"],
            report,
            public_report={"status": "passed"},
            candidate_sha256="a" * 64,
        )
        self.assertEqual(saved["id"], replayed["id"])
        self.assertEqual(saved["report"]["status"], "passed")
        self.assertEqual(saved["public_report"], {"status": "passed"})
        self.assertEqual(
            self.service.list_verifications(run_id=run["id"])[0]["goal_spec_id"],
            goal["id"],
        )

        other_run = self.service.begin_run("other-task")
        with self.assertRaises(TaskStateError):
            self.service.save_verification_report(
                "other-task",
                other_run["id"],
                goal["id"],
                {**report, "id": "verify_wrong_scope"},
                candidate_sha256="b" * 64,
            )

    def test_typed_goal_spec_is_task_scoped_and_reused_by_resumed_run(self) -> None:
        first_run = self.service.begin_run("task-typed-goal")
        draft = compile_draft(
            task_id="task-typed-goal",
            objective={"statement": "生成可靠性结论", "intent": "analysis"},
        )
        confirmed = finalize(draft)
        self.service.save_goal_spec(
            "task-typed-goal",
            first_run["id"],
            draft.model_dump(mode="json"),
            public_summary=public_goal_summary(draft),
        )
        first = self.service.save_goal_spec(
            "task-typed-goal",
            first_run["id"],
            confirmed.model_dump(mode="json"),
            public_summary=public_goal_summary(confirmed),
        )
        checkpoint = self.service.create_checkpoint(
            first_run["id"], {"goal_spec_id": first["id"]}
        )
        self.service.finish_run(first_run["id"], status="completed")

        resumed_run = self.service.begin_run(
            "task-typed-goal", resumed_from_checkpoint_id=checkpoint["id"]
        )
        attached = self.service.save_goal_spec(
            "task-typed-goal",
            resumed_run["id"],
            confirmed.model_dump(mode="json"),
            public_summary=public_goal_summary(confirmed),
        )
        self.assertEqual(attached["id"], first["id"])
        self.assertEqual(attached["spec_hash"], first["spec_hash"])
        self.assertEqual(
            self.service.latest_goal_spec(run_id=resumed_run["id"])["id"],
            first["id"],
        )

        report = {
            "id": "verify_resumed_goal_1",
            "attempt": 1,
            "mode": "rules_only",
            "status": "passed",
        }
        saved_report = self.service.save_verification_report(
            "task-typed-goal",
            resumed_run["id"],
            first["id"],
            report,
            candidate_sha256="d" * 64,
        )
        self.assertEqual(saved_report["goal_spec_id"], first["id"])

        tampered = confirmed.model_dump(mode="json")
        tampered["objective"]["statement"] = "被篡改的目标"
        with self.assertRaises(ValueError):
            self.service.save_goal_spec(
                "task-typed-goal", resumed_run["id"], tampered
            )

    def test_crud_updates_and_cascade_delete(self) -> None:
        run = self.service.create_run("task-delete")
        self.service.update_run_metadata(run["id"], {"owner": "team"})
        node = self.service.create_node(run["id"], "step", "步骤")
        self.service.update_node_metadata(node["id"], {"progress": 50})
        checkpoint = self.service.create_checkpoint(run["id"], {"position": 1})
        command = self.service.enqueue_command("task-delete", "message", run_id=run["id"])
        self.assertEqual(self.service.get_run(run["id"])["metadata"]["owner"], "team")
        self.assertEqual(self.service.get_node(node["id"])["metadata"]["progress"], 50)

        self.assertTrue(self.service.delete_checkpoint(checkpoint["id"]))
        self.assertTrue(self.service.delete_command(command["id"]))
        self.assertTrue(self.service.delete_node(node["id"]))
        self.assertTrue(self.service.delete_run(run["id"]))
        self.assertIsNone(self.service.get_run(run["id"]))
        self.assertFalse(self.service.delete_run(run["id"]))

    def test_missing_entities_raise_specific_error(self) -> None:
        with self.assertRaises(StateNotFoundError):
            self.service.finish_run("missing")
        with self.assertRaises(StateNotFoundError):
            self.service.restore_checkpoint("missing")


if __name__ == "__main__":
    unittest.main()
