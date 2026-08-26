from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from app import db
from app import main as main_module
from app.services.agent_runtime import create_task_record
from app.services.task_state import RunIntakeClosed, TaskStateService


class LegacyTerminalProjectionRecoveryTests(unittest.TestCase):
    """A terminal Task must never be restarted because of one legacy Run."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_db_path = db.DB_PATH
        self.original_app_db_path = os.environ.get("APP_DB_PATH")
        self.db_path = Path(self.temp_dir.name) / "legacy-terminal.db"
        os.environ["APP_DB_PATH"] = str(self.db_path)
        db.DB_PATH = self.db_path
        db.init_db()
        self.state = TaskStateService(db.get_conn)

    def tearDown(self) -> None:
        db.DB_PATH = self.original_db_path
        if self.original_app_db_path is None:
            os.environ.pop("APP_DB_PATH", None)
        else:
            os.environ["APP_DB_PATH"] = self.original_app_db_path
        self.temp_dir.cleanup()

    def _nonterminal_run(self, task_id: str, status: str) -> dict:
        if status == "queued":
            return self.state.create_run(task_id)
        db.update_task_status(task_id, "running")
        run = self.state.begin_run(task_id)
        if status in {"paused", "waiting_approval"}:
            run = self.state.transition_run(run["id"], status)
        return run

    @staticmethod
    def _task_row(task_id: str) -> dict:
        row = db.query_one("SELECT * FROM tasks WHERE id = ?", (task_id,))
        if row is None:
            raise AssertionError(f"missing task {task_id}")
        return row

    @staticmethod
    def _insert_artifact(
        *,
        artifact_id: str,
        task_id: str,
        run_id: str,
        delivery_status: str,
        verification_id: str = "",
        published_at: str = "",
        tool_effect_id: str = "",
    ) -> None:
        now = db.utc_now()
        db.execute(
            """
            INSERT INTO artifacts(
                id, task_id, name, kind, path, run_id, delivery_status,
                verification_id, published_at, tool_effect_id, created_at
            ) VALUES (?, ?, ?, 'md', ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                artifact_id,
                task_id,
                f"{artifact_id}.md",
                f"/private/tmp/{artifact_id}.md",
                run_id,
                delivery_status,
                verification_id,
                published_at,
                tool_effect_id,
                now,
            ),
        )

    @staticmethod
    def _drop_artifact_run_fence() -> None:
        db.execute("DROP TRIGGER IF EXISTS trg_artifacts_pending_run_insert")
        db.execute("DROP TRIGGER IF EXISTS trg_artifacts_pending_run_update")

    @staticmethod
    def _insert_late_node(*, node_id: str, task_id: str, run_id: str) -> None:
        now = db.utc_now()
        db.execute(
            """
            INSERT INTO task_nodes(
                id, run_id, task_id, node_key, title, sequence, status,
                metadata_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, '晚到节点', 999, 'running', '{}', ?, ?)
            """,
            (node_id, run_id, task_id, f"late:{node_id}", now, now),
        )

    @staticmethod
    def _insert_late_command(
        *, command_id: str, task_id: str, run_id: str | None
    ) -> None:
        now = db.utc_now()
        db.execute(
            """
            INSERT INTO task_commands(
                id, task_id, run_id, command_type, payload_json, status,
                available_at, created_at, updated_at
            ) VALUES (?, ?, ?, 'late_continuation', '{}', 'queued', ?, ?, ?)
            """,
            (command_id, task_id, run_id, now, now, now),
        )

    def _startup_patches(self) -> ExitStack:
        stack = ExitStack()
        stack.enter_context(
            patch.object(main_module.loop_scheduler, "start", return_value=None)
        )
        stack.enter_context(
            patch.object(
                main_module.loop_scheduler,
                "stop",
                new_callable=AsyncMock,
            )
        )
        stack.enter_context(
            patch.object(main_module.skill_registry, "load_builtin_skills")
        )
        stack.enter_context(
            patch.object(main_module.mcp_gateway, "seed_builtin_servers")
        )
        stack.enter_context(patch.object(main_module, "seed_agents"))
        stack.enter_context(patch.object(main_module, "_reload_policy_rules"))
        return stack

    def test_all_terminal_task_and_nonterminal_run_states_converge(self) -> None:
        for task_status in ("completed", "failed", "cancelled"):
            for run_status in ("running", "paused", "waiting_approval"):
                with self.subTest(task_status=task_status, run_status=run_status):
                    task = create_task_record(
                        f"legacy split {task_status} {run_status}",
                        "general-agent",
                    )
                    run = self._nonterminal_run(task["id"], run_status)
                    expected_result = {
                        "answer": f"authoritative-{task_status}-{run_status}",
                        "nested": {"keep": True},
                    }
                    expected_artifacts = [
                        {
                            "id": f"public-{task_status}-{run_status}",
                            "name": "authoritative.md",
                        }
                    ]
                    db.update_task_status(
                        task["id"],
                        task_status,
                        result=expected_result,
                        artifacts=expected_artifacts,
                    )
                    task_before = self._task_row(task["id"])

                    outcome = self.state.reconcile_legacy_terminal_projection(
                        run["id"]
                    )

                    self.assertTrue(outcome["reconciled"])
                    self.assertFalse(outcome["idempotent"])
                    self.assertEqual(outcome["previous_run_status"], run_status)
                    reconciled = self.state.get_run(run["id"])
                    self.assertEqual(reconciled["status"], task_status)
                    self.assertEqual(reconciled["intake_state"], "closed")
                    self.assertTrue(reconciled["intake_closed_at"])
                    self.assertTrue(reconciled["finished_at"])
                    marker = reconciled["metadata"][
                        "legacy_terminal_projection_reconciled"
                    ]
                    self.assertTrue(marker["reconciled"])
                    self.assertEqual(marker["previous_run_status"], run_status)
                    self.assertEqual(marker["task_status"], task_status)
                    self.assertEqual(self._task_row(task["id"]), task_before)
                    self.state.assert_terminal_clean(
                        task_id=task["id"], run_id=run["id"]
                    )

                    repeated = self.state.reconcile_legacy_terminal_projection(
                        run["id"]
                    )
                    self.assertTrue(repeated["reconciled"])
                    self.assertTrue(repeated["idempotent"])
                    events = db.query_all(
                        "SELECT id FROM task_events WHERE task_id = ? "
                        "AND type = 'legacy_terminal_projection_reconciled'",
                        (task["id"],),
                    )
                    self.assertEqual(len(events), 1)
                    self.assertEqual(self._task_row(task["id"]), task_before)

    def test_queued_retry_of_terminal_task_is_preserved_for_startup_dispatch(
        self,
    ) -> None:
        task = create_task_record("terminal task queued for explicit retry", "general-agent")
        db.update_task_status(
            task["id"],
            "completed",
            result={"answer": "previous attempt"},
            artifacts=[{"id": "previous-public"}],
        )
        task_before = self._task_row(task["id"])
        retry = self.state.create_run(
            task["id"], metadata={"trigger": "retry"}
        )

        outcome = self.state.reconcile_legacy_terminal_projection(retry["id"])

        self.assertFalse(outcome["reconciled"])
        self.assertEqual(outcome["reason"], "run_not_active")
        self.assertEqual(self.state.get_run(retry["id"])["status"], "queued")
        self.assertEqual(self._task_row(task["id"]), task_before)
        self.assertFalse(
            db.query_all(
                "SELECT id FROM task_events WHERE task_id = ? "
                "AND type = 'legacy_terminal_projection_reconciled'",
                (task["id"],),
            )
        )

        scheduled = main_module._recover_interrupted_runs()

        self.assertEqual([item["id"] for item in scheduled], [retry["id"]])
        self.assertEqual(self.state.get_run(retry["id"])["status"], "queued")
        self.assertEqual(self._task_row(task["id"]), task_before)

    def test_reconciliation_closes_residue_without_corrupting_public_state(self) -> None:
        task = create_task_record("legacy task with residue", "general-agent")
        db.update_task_status(task["id"], "running")
        run = self.state.begin_run(task["id"])

        running_node = self.state.create_node(
            run["id"], "execute", "正在执行", metadata={"keep": "running"}
        )
        self.state.start_node(running_node["id"])
        pending_node = self.state.create_node(
            run["id"], "deliver", "等待交付", metadata={"keep": "pending"}
        )
        completed_node = self.state.create_node(
            run["id"], "understand", "已理解", metadata={"keep": "completed"}
        )
        self.state.start_node(completed_node["id"])
        self.state.finish_node(
            completed_node["id"], output={"do_not_change": True}
        )

        message = self.state.enqueue_command(
            task["id"],
            "message",
            run_id=run["id"],
            payload={"message": "旧的未处理消息"},
        )
        claimed = self.state.enqueue_command(
            task["id"],
            "tool_continuation",
            run_id=run["id"],
            payload={"tool": "legacy"},
        )
        claimed_after = self.state.claim_command(
            "dead-worker",
            task_id=task["id"],
            run_id=run["id"],
            command_types=["tool_continuation"],
        )
        self.assertEqual(claimed_after["id"], claimed["id"])
        task_scoped = self.state.enqueue_command(
            task["id"], "legacy_followup", payload={"keep": "audit"}
        )

        self._insert_artifact(
            artifact_id="artifact_pending_legacy",
            task_id=task["id"],
            run_id=run["id"],
            delivery_status="pending_verification",
            verification_id="draft-verification",
        )
        published_at = db.utc_now()
        self._insert_artifact(
            artifact_id="artifact_published_legacy",
            task_id=task["id"],
            run_id=run["id"],
            delivery_status="published",
            verification_id="verified-public",
            published_at=published_at,
        )
        other_task = create_task_record("unrelated task", "general-agent")
        other_run = self.state.begin_run(other_task["id"])
        self._insert_artifact(
            artifact_id="artifact_unrelated_pending",
            task_id=other_task["id"],
            run_id=other_run["id"],
            delivery_status="pending_verification",
            verification_id="unrelated-draft",
        )

        expected_result = {
            "answer": "这是已经交付给用户的最终答案",
            "evidence": ["source-a", "source-b"],
        }
        expected_artifacts = [
            {
                "id": "artifact_published_legacy",
                "name": "artifact_published_legacy.md",
            }
        ]
        db.update_task_status(
            task["id"],
            "completed",
            result=expected_result,
            artifacts=expected_artifacts,
        )
        task_before = self._task_row(task["id"])
        completed_before = self.state.get_node(completed_node["id"])
        published_before = db.query_one(
            "SELECT * FROM artifacts WHERE id = 'artifact_published_legacy'"
        )
        unrelated_before = db.query_one(
            "SELECT * FROM artifacts WHERE id = 'artifact_unrelated_pending'"
        )

        outcome = self.state.reconcile_legacy_terminal_projection(run["id"])

        self.assertCountEqual(
            outcome["closed_node_ids"], [running_node["id"], pending_node["id"]]
        )
        self.assertCountEqual(
            outcome["cancelled_command_ids"],
            [message["id"], claimed["id"], task_scoped["id"]],
        )
        self.assertEqual(
            outcome["rejected_artifact_ids"], ["artifact_pending_legacy"]
        )
        for node_id in (running_node["id"], pending_node["id"]):
            node = self.state.get_node(node_id)
            self.assertEqual(node["status"], "cancelled")
            self.assertTrue(
                node["metadata"]["legacy_terminal_projection_reconciled"][
                    "reconciled"
                ]
            )
        self.assertEqual(self.state.get_node(completed_node["id"]), completed_before)
        for command_id in (message["id"], claimed["id"], task_scoped["id"]):
            command = self.state.get_command(command_id)
            self.assertEqual(command["status"], "cancelled")
            self.assertEqual(
                command["result"]["reason"],
                "legacy_terminal_projection_reconciled",
            )
        reconciled_run = self.state.get_run(run["id"])
        self.assertEqual(reconciled_run["accepted_generation"], 1)
        self.assertEqual(reconciled_run["applied_generation"], 1)
        pending_after = db.query_one(
            "SELECT * FROM artifacts WHERE id = 'artifact_pending_legacy'"
        )
        self.assertEqual(pending_after["delivery_status"], "rejected")
        self.assertEqual(pending_after["verification_id"], "")
        self.assertEqual(pending_after["published_at"], "")
        self.assertEqual(
            db.query_one(
                "SELECT * FROM artifacts WHERE id = 'artifact_published_legacy'"
            ),
            published_before,
        )
        self.assertEqual(
            db.query_one(
                "SELECT * FROM artifacts WHERE id = 'artifact_unrelated_pending'"
            ),
            unrelated_before,
        )
        self.assertEqual(self._task_row(task["id"]), task_before)
        self.state.assert_terminal_clean(task_id=task["id"], run_id=run["id"])

    def test_event_failure_rolls_back_every_reconciliation_write(self) -> None:
        task = create_task_record("rollback legacy reconciliation", "general-agent")
        db.update_task_status(task["id"], "running")
        run = self.state.begin_run(task["id"])
        node = self.state.create_node(run["id"], "execute", "正在执行")
        self.state.start_node(node["id"])
        command = self.state.enqueue_command(
            task["id"],
            "message",
            run_id=run["id"],
            payload={"message": "不能因失败而消失"},
        )
        self._insert_artifact(
            artifact_id="artifact_rollback_pending",
            task_id=task["id"],
            run_id=run["id"],
            delivery_status="pending_verification",
            verification_id="rollback-draft",
        )
        db.update_task_status(
            task["id"],
            "completed",
            result={"answer": "must survive rollback"},
            artifacts=[{"id": "public-stable"}],
        )
        task_before = self._task_row(task["id"])
        run_before = self.state.get_run(run["id"])
        node_before = self.state.get_node(node["id"])
        command_before = self.state.get_command(command["id"])
        artifact_before = db.query_one(
            "SELECT * FROM artifacts WHERE id = 'artifact_rollback_pending'"
        )
        db.execute(
            """
            CREATE TRIGGER fail_legacy_terminal_reconciliation_event
            BEFORE INSERT ON task_events
            WHEN NEW.type = 'legacy_terminal_projection_reconciled'
            BEGIN
                SELECT RAISE(ABORT, 'injected legacy reconciliation failure');
            END
            """
        )

        with self.assertRaisesRegex(
            sqlite3.IntegrityError, "injected legacy reconciliation failure"
        ):
            self.state.reconcile_legacy_terminal_projection(run["id"])

        self.assertEqual(self._task_row(task["id"]), task_before)
        self.assertEqual(self.state.get_run(run["id"]), run_before)
        self.assertEqual(self.state.get_node(node["id"]), node_before)
        self.assertEqual(self.state.get_command(command["id"]), command_before)
        self.assertEqual(
            db.query_one(
                "SELECT * FROM artifacts WHERE id = 'artifact_rollback_pending'"
            ),
            artifact_before,
        )
        self.assertFalse(
            db.query_all(
                "SELECT id FROM task_events WHERE task_id = ? "
                "AND type = 'legacy_terminal_projection_reconciled'",
                (task["id"],),
            )
        )

    def test_marker_repair_failure_rolls_back_late_residue_and_marker(self) -> None:
        task = create_task_record("rollback late marker repair", "general-agent")
        db.update_task_status(task["id"], "running")
        run = self.state.begin_run(task["id"])
        db.update_task_status(
            task["id"], "completed", result={"answer": "already public"}
        )
        self.state.reconcile_legacy_terminal_projection(run["id"])

        self._drop_artifact_run_fence()
        self._insert_late_node(
            node_id="node_late_rollback", task_id=task["id"], run_id=run["id"]
        )
        self._insert_late_command(
            command_id="command_late_rollback",
            task_id=task["id"],
            run_id=run["id"],
        )
        self._insert_artifact(
            artifact_id="artifact_late_rollback",
            task_id=task["id"],
            run_id=run["id"],
            delivery_status="pending_verification",
        )
        db.execute(
            "UPDATE task_runs SET intake_state = 'open', intake_closed_at = '', "
            "accepted_generation = 4, applied_generation = 2, "
            "current_node_id = 'node_late_rollback' WHERE id = ?",
            (run["id"],),
        )
        task_before = self._task_row(task["id"])
        run_before = self.state.get_run(run["id"])
        node_before = self.state.get_node("node_late_rollback")
        command_before = self.state.get_command("command_late_rollback")
        artifact_before = db.query_one(
            "SELECT * FROM artifacts WHERE id = 'artifact_late_rollback'"
        )
        db.execute(
            """
            CREATE TRIGGER fail_legacy_terminal_projection_repair_event
            BEFORE INSERT ON task_events
            WHEN NEW.type = 'legacy_terminal_projection_repaired'
            BEGIN
                SELECT RAISE(ABORT, 'injected marker repair failure');
            END
            """
        )

        with self.assertRaisesRegex(
            sqlite3.IntegrityError, "injected marker repair failure"
        ):
            self.state.reconcile_legacy_terminal_projection(run["id"])

        self.assertEqual(self._task_row(task["id"]), task_before)
        self.assertEqual(self.state.get_run(run["id"]), run_before)
        self.assertEqual(self.state.get_node("node_late_rollback"), node_before)
        self.assertEqual(
            self.state.get_command("command_late_rollback"), command_before
        )
        self.assertEqual(
            db.query_one(
                "SELECT * FROM artifacts WHERE id = 'artifact_late_rollback'"
            ),
            artifact_before,
        )
        self.assertFalse(
            db.query_all(
                "SELECT id FROM task_events WHERE task_id = ? "
                "AND type = 'legacy_terminal_projection_repaired'",
                (task["id"],),
            )
        )

    def test_startup_sweep_repairs_late_marker_residue_once(self) -> None:
        task = create_task_record("startup repairs late residue", "general-agent")
        db.update_task_status(task["id"], "running")
        run = self.state.begin_run(task["id"])
        db.update_task_status(
            task["id"],
            "completed",
            result={"answer": "authoritative public answer"},
            artifacts=[{"id": "already-published"}],
        )
        self.state.reconcile_legacy_terminal_projection(run["id"])
        task_before = self._task_row(task["id"])

        # Model a write from a pre-v8 worker that was already in flight when
        # the original marker committed.  The next startup reinstalls the
        # trigger before sweeping this persisted residue.
        self._drop_artifact_run_fence()
        self._insert_late_node(
            node_id="node_late_startup", task_id=task["id"], run_id=run["id"]
        )
        self._insert_late_command(
            command_id="command_late_startup_run",
            task_id=task["id"],
            run_id=run["id"],
        )
        self._insert_late_command(
            command_id="command_late_startup_task",
            task_id=task["id"],
            run_id=None,
        )
        self._insert_artifact(
            artifact_id="artifact_late_startup",
            task_id=task["id"],
            run_id=run["id"],
            delivery_status="pending_verification",
            verification_id="stale-draft",
        )
        db.execute(
            "UPDATE task_runs SET status = 'failed', intake_state = 'open', intake_closed_at = '', "
            "finished_at = '', accepted_generation = 5, applied_generation = 2, "
            "current_node_id = 'node_late_startup' WHERE id = ?",
            (run["id"],),
        )

        for startup_number in (1, 2):
            with self._startup_patches() as stack:
                schedule_runtime = stack.enter_context(
                    patch.object(main_module, "_schedule_runtime")
                )
                with TestClient(main_module.app) as client:
                    response = client.get("/api/health")
                    self.assertEqual(response.status_code, 200, response.text)
                schedule_runtime.assert_not_called()

            repaired = self.state.get_run(run["id"])
            marker = repaired["metadata"][
                "legacy_terminal_projection_reconciled"
            ]
            self.assertEqual(marker["repair_count"], 1)
            repair_events = db.query_all(
                "SELECT * FROM task_events WHERE task_id = ? "
                "AND type = 'legacy_terminal_projection_repaired'",
                (task["id"],),
            )
            self.assertEqual(len(repair_events), 1, startup_number)

        self.assertEqual(self.state.get_node("node_late_startup")["status"], "cancelled")
        for command_id in (
            "command_late_startup_run",
            "command_late_startup_task",
        ):
            command = self.state.get_command(command_id)
            self.assertEqual(command["status"], "cancelled")
            self.assertEqual(
                command["result"]["reason"],
                "legacy_terminal_projection_repaired",
            )
        artifact = db.query_one(
            "SELECT * FROM artifacts WHERE id = 'artifact_late_startup'"
        )
        self.assertEqual(artifact["delivery_status"], "rejected")
        self.assertEqual(artifact["verification_id"], "")
        repaired = self.state.get_run(run["id"])
        self.assertEqual(repaired["status"], "completed")
        self.assertEqual(repaired["accepted_generation"], 5)
        self.assertEqual(repaired["applied_generation"], 5)
        self.assertEqual(repaired["intake_state"], "closed")
        self.assertTrue(repaired["intake_closed_at"])
        self.assertTrue(repaired["finished_at"])
        self.assertEqual(self._task_row(task["id"]), task_before)
        self.state.assert_terminal_clean(task_id=task["id"], run_id=run["id"])

        with self.assertRaisesRegex(
            sqlite3.IntegrityError, "matching running/open run"
        ):
            self._insert_artifact(
                artifact_id="artifact_blocked_after_startup",
                task_id=task["id"],
                run_id=run["id"],
                delivery_status="pending_verification",
            )

    def test_startup_sweep_never_consumes_a_later_queued_retry(self) -> None:
        task = create_task_record("reconciled attempt then explicit retry", "general-agent")
        db.update_task_status(task["id"], "running")
        old_run = self.state.begin_run(task["id"])
        db.update_task_status(
            task["id"], "completed", result={"answer": "old answer"}
        )
        self.state.reconcile_legacy_terminal_projection(old_run["id"])
        retry = self.state.create_run(task["id"], metadata={"trigger": "retry"})
        self._insert_late_command(
            command_id="command_for_later_retry",
            task_id=task["id"],
            run_id=None,
        )

        sweep = self.state.sweep_reconciled_terminal_invariants()

        self.assertNotIn(old_run["id"], sweep["checked_run_ids"])
        self.assertEqual(self.state.get_run(retry["id"])["status"], "queued")
        self.assertEqual(
            self.state.get_command("command_for_later_retry")["status"], "queued"
        )
        self.assertFalse(
            db.query_all(
                "SELECT id FROM task_events WHERE task_id = ? "
                "AND type = 'legacy_terminal_projection_repaired'",
                (task["id"],),
            )
        )

    def test_terminal_task_rejects_every_new_task_scoped_command(self) -> None:
        task = create_task_record("terminal command fence", "general-agent")
        db.update_task_status(task["id"], "completed", result={"answer": "done"})

        for command_type in (
            "message",
            "cancel",
            "approval",
            "tool_continuation",
            "expert_continuation",
        ):
            with self.subTest(command_type=command_type):
                with self.assertRaises(RunIntakeClosed):
                    self.state.enqueue_command(
                        task["id"], command_type, payload={"proof": command_type}
                    )
        self.assertEqual(self.state.list_commands(task_id=task["id"]), [])

    def test_pending_artifact_trigger_and_effect_identity_are_enforced(self) -> None:
        task = create_task_record("artifact database fence", "general-agent")
        queued = self.state.create_run(task["id"])

        with self.assertRaisesRegex(
            sqlite3.IntegrityError, "matching running/open run"
        ):
            self._insert_artifact(
                artifact_id="artifact_queued_rejected",
                task_id=task["id"],
                run_id=queued["id"],
                delivery_status="pending_verification",
            )

        running = self.state.begin_run(task["id"], run_id=queued["id"])
        different_task = create_task_record("different artifact owner", "general-agent")
        with self.assertRaisesRegex(
            sqlite3.IntegrityError, "matching running/open run"
        ):
            self._insert_artifact(
                artifact_id="artifact_wrong_task_rejected",
                task_id=different_task["id"],
                run_id=running["id"],
                delivery_status="pending_verification",
            )
        self._insert_artifact(
            artifact_id="artifact_running_allowed",
            task_id=task["id"],
            run_id=running["id"],
            delivery_status="pending_verification",
            tool_effect_id="effect-artifact-running",
        )
        self.state.transition_run(running["id"], "paused")
        with self.assertRaisesRegex(
            sqlite3.IntegrityError, "matching running/open run"
        ):
            self._insert_artifact(
                artifact_id="artifact_paused_rejected",
                task_id=task["id"],
                run_id=running["id"],
                delivery_status="pending_verification",
            )
        with self.assertRaisesRegex(
            sqlite3.IntegrityError, "matching running/open run"
        ):
            db.execute(
                "UPDATE artifacts SET name = 'late-change.md' "
                "WHERE id = 'artifact_running_allowed'"
            )
        with self.assertRaises(sqlite3.IntegrityError):
            self._insert_artifact(
                artifact_id="artifact_duplicate_effect",
                task_id=task["id"],
                run_id="",
                delivery_status="published",
                published_at=db.utc_now(),
                tool_effect_id="effect-artifact-running",
            )
        self._insert_artifact(
            artifact_id="artifact_published_without_run",
            task_id=task["id"],
            run_id="",
            delivery_status="published",
            published_at=db.utc_now(),
        )
        with self.assertRaisesRegex(
            sqlite3.IntegrityError, "matching running/open run"
        ):
            db.execute(
                "UPDATE artifacts SET delivery_status = 'pending_verification' "
                "WHERE id = 'artifact_published_without_run'"
            )

        self.state.transition_run(running["id"], "running")
        db.execute(
            "UPDATE task_runs SET intake_state = 'closed', intake_closed_at = ? "
            "WHERE id = ?",
            (db.utc_now(), running["id"]),
        )
        with self.assertRaisesRegex(
            sqlite3.IntegrityError, "matching running/open run"
        ):
            self._insert_artifact(
                artifact_id="artifact_closed_intake_rejected",
                task_id=task["id"],
                run_id=running["id"],
                delivery_status="pending_verification",
            )
        migrations = db.query_all(
            "SELECT version FROM schema_migrations WHERE version = 8"
        )
        self.assertEqual([row["version"] for row in migrations], [8])
        triggers = {
            row["name"]
            for row in db.query_all(
                "SELECT name FROM sqlite_master WHERE type = 'trigger' "
                "AND name LIKE 'trg_artifacts_pending_run_%'"
            )
        }
        self.assertEqual(
            triggers,
            {
                "trg_artifacts_pending_run_insert",
                "trg_artifacts_pending_run_update",
            },
        )

    def test_repeated_real_startup_is_idempotent_and_never_reschedules_task(
        self,
    ) -> None:
        task = create_task_record("real startup legacy split", "general-agent")
        db.update_task_status(task["id"], "running")
        run = self.state.begin_run(task["id"])
        db.update_task_status(
            task["id"],
            "completed",
            result={"answer": "already delivered"},
            artifacts=[{"id": "already-public"}],
        )
        task_before = self._task_row(task["id"])

        for _ in range(2):
            with self._startup_patches() as stack:
                schedule_runtime = stack.enter_context(
                    patch.object(main_module, "_schedule_runtime")
                )
                with TestClient(main_module.app) as client:
                    response = client.get("/api/health")
                    self.assertEqual(response.status_code, 200, response.text)
                schedule_runtime.assert_not_called()

        reconciled = self.state.get_run(run["id"])
        self.assertEqual(reconciled["status"], "completed")
        self.assertEqual(len(self.state.list_runs(task_id=task["id"])), 1)
        self.assertEqual(self._task_row(task["id"]), task_before)
        events = db.query_all(
            "SELECT id FROM task_events WHERE task_id = ? "
            "AND type = 'legacy_terminal_projection_reconciled'",
            (task["id"],),
        )
        self.assertEqual(len(events), 1)
        self.state.assert_terminal_clean(task_id=task["id"], run_id=run["id"])


if __name__ == "__main__":
    unittest.main()
