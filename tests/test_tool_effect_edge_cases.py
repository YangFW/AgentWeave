from __future__ import annotations

import asyncio
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Mapping
from unittest.mock import AsyncMock, patch

from app import db
from app.services.agent_runtime import AgentRuntime, create_task_record
from app.services.mcp_gateway import ToolError
from app.services.task_state import TaskStateService
from app.services.tool_effect_journal import ToolEffectJournal


class AllowEvaluation:
    denied = False
    requires_approval = False

    @staticmethod
    def apply(context: Mapping[str, Any]) -> dict[str, Any]:
        return dict(context)


class NeverCalledGateway:
    def __init__(self) -> None:
        self.calls = 0

    def list_tools(self) -> list[dict[str, Any]]:
        return [
            {
                "server_id": "external",
                "name": "write",
                "description": "non-idempotent write used by boundary tests",
                "input_schema": {
                    "type": "object",
                    "properties": {"value": {"type": "string"}},
                    "required": ["value"],
                    "additionalProperties": False,
                },
            }
        ]

    async def invoke_tool(
        self,
        server_id: str,
        tool_name: str,
        arguments: dict[str, Any],
        **_: Any,
    ) -> dict[str, Any]:
        self.calls += 1
        return {"written": True}


class InterruptAfterRejectionState(TaskStateService):
    """Model process loss immediately after the approval decision commits."""

    def __init__(self) -> None:
        super().__init__(db.get_conn)
        self.interrupt_once = True

    def commit_policy_approval_decision(self, **kwargs: Any) -> dict[str, Any] | None:
        result = super().commit_policy_approval_decision(**kwargs)
        if result and not result.get("idempotent") and self.interrupt_once:
            self.interrupt_once = False
            raise asyncio.CancelledError
        return result


class ToolEffectEdgeCaseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "tool-effect-edges.db"

    async def asyncTearDown(self) -> None:
        self.temp_dir.cleanup()

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.db_path, check_same_thread=False)

    def _journal(self) -> ToolEffectJournal:
        return ToolEffectJournal(self._connect)

    def test_goal_spec_hash_is_part_of_the_stable_effect_identity(self) -> None:
        journal = self._journal()
        shared = {
            "task_id": "task-goal-revision",
            "run_id": "run-1",
            "operation_key": "plan-slot:write:1",
            "server_id": "external",
            "tool_name": "write",
            "arguments": {"value": "same-payload"},
            "effect_kind": "non_idempotent_write",
        }
        first = journal.prepare_effect(goal_spec_hash="a" * 64, **shared)
        second = journal.prepare_effect(
            goal_spec_hash="b" * 64,
            **{**shared, "run_id": "run-2"},
        )

        self.assertNotEqual(first["effect_key"], second["effect_key"])
        self.assertNotEqual(first["idempotency_key"], second["idempotency_key"])
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT goal_spec_hash, effect_key FROM tool_effects "
                "ORDER BY goal_spec_hash"
            ).fetchall()
        self.assertEqual([row[0] for row in rows], ["a" * 64, "b" * 64])
        self.assertEqual(len({row[1] for row in rows}), 2)

    def test_journal_schema_init_preserves_a_shared_outer_transaction(self) -> None:
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("CREATE TABLE caller_owned_change(id INTEGER)")
            ToolEffectJournal(conn, auto_init=False).init_schema()

            self.assertTrue(conn.in_transaction)
            self.assertIsNotNone(
                conn.execute(
                    "SELECT 1 FROM sqlite_master "
                    "WHERE type = 'table' AND name = 'tool_effects'"
                ).fetchone()
            )
            conn.rollback()

        with closing(self._connect()) as conn:
            names = {
                str(row[0])
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                ).fetchall()
            }
        self.assertNotIn("caller_owned_change", names)
        self.assertNotIn("tool_effects", names)
        self.assertNotIn("tool_effect_transitions", names)

    async def _assert_local_failure_releases_effect(
        self,
        *,
        permissions: dict[str, Any],
        checkpoint_error: BaseException | None = None,
    ) -> None:
        journal = self._journal()
        gateway = NeverCalledGateway()
        runtime = AgentRuntime(
            object(),
            gateway,
            object(),
            task_state=object(),
            tool_effect_journal=journal,
        )
        state = {
            "effective_permissions": dict(permissions),
            "tool_calls_used": 0,
            "permission_elapsed_seconds": 0.0,
            "plan_id": "plan-goal-edge-v1",
        }
        execution = {
            "task_id": "task-pre-dispatch",
            "run_id": "run-pre-dispatch",
            "attempt": 1,
            "worker_id": "runtime:run-pre-dispatch",
            "state": state,
            "nodes": {},
            "plan_nodes": {},
            "permission_timer_started": None,
            "permission_elapsed_base": 0.0,
        }
        token = runtime._execution_context.set(execution)
        checkpoint = (
            patch.object(runtime, "_create_checkpoint", side_effect=checkpoint_error)
            if checkpoint_error is not None
            else patch.object(runtime, "_create_checkpoint", return_value={})
        )
        try:
            with (
                patch.object(
                    runtime,
                    "_evaluate_policy",
                    new=AsyncMock(return_value=AllowEvaluation()),
                ),
                patch.object(runtime, "_enforce_goal_tool_contract"),
                patch.object(runtime, "_enforce_tool_permission"),
                patch.object(runtime, "_raise_if_cancelled"),
                patch.object(runtime, "_claim_runtime_messages", return_value=[]),
                patch.object(runtime, "_current_goal_tool_cache", return_value={}),
                patch.object(
                    runtime,
                    "_current_goal_spec",
                    return_value=SimpleNamespace(spec_hash="c" * 64),
                ),
                patch("app.services.agent_runtime.emit"),
                checkpoint,
            ):
                with self.assertRaises(BaseException) as raised:
                    await runtime._tool(
                        "task-pre-dispatch",
                        "external",
                        "write",
                        {"value": "must-not-dispatch"},
                    )
        finally:
            runtime._execution_context.reset(token)

        self.assertIsNotNone(raised.exception)
        self.assertEqual(gateway.calls, 0)
        with closing(self._connect()) as conn:
            row = conn.execute("SELECT * FROM tool_effects").fetchone()
            self.assertIsNotNone(row)
            effect_key = str(row[0])
        effect = journal.get_effect(effect_key) or {}
        self.assertEqual(effect.get("state"), "prepared")
        self.assertEqual(effect.get("owner_run_id"), "")
        self.assertEqual(effect.get("worker_id"), "")
        self.assertEqual(effect.get("lease_token"), "")
        self.assertEqual(effect.get("unknown_reason"), "")
        self.assertEqual(effect.get("resolution_note"), "local_pre_dispatch_failure")
        # attempt_count is deliberately the number of fenced lease claims,
        # not the number of gateway calls.  The latter remains zero above.
        self.assertEqual(effect.get("attempt_count"), 1)
        self.assertEqual(journal.list_unknown(), [])
        self.assertEqual(journal.recover_interrupted_executions(), [])
        transitions = journal.list_transitions(effect_key)
        self.assertEqual(
            [item["to_state"] for item in transitions],
            ["prepared", "executing", "prepared"],
        )
        self.assertEqual(transitions[-1]["reason"], "dispatch_not_started")

    async def test_exhausted_timeout_releases_claim_without_unknown(self) -> None:
        await self._assert_local_failure_releases_effect(
            permissions={"timeout_seconds": 0, "max_tool_calls": 10}
        )

    async def test_exhausted_call_budget_releases_claim_without_unknown(self) -> None:
        await self._assert_local_failure_releases_effect(
            permissions={"timeout_seconds": 30, "max_tool_calls": 0}
        )

    async def test_checkpoint_failure_releases_claim_without_unknown(self) -> None:
        await self._assert_local_failure_releases_effect(
            permissions={"timeout_seconds": 30, "max_tool_calls": 10},
            checkpoint_error=RuntimeError("injected before-tool checkpoint failure"),
        )


class UnknownEffectRejectionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.original_db_path = db.DB_PATH
        self.temp_dir = tempfile.TemporaryDirectory()
        db.DB_PATH = Path(self.temp_dir.name) / "unknown-rejection.db"
        db.init_db()
        self.state = InterruptAfterRejectionState()
        self.journal = ToolEffectJournal(db.get_conn)

    async def asyncTearDown(self) -> None:
        db.DB_PATH = self.original_db_path
        self.temp_dir.cleanup()

    @staticmethod
    def _execution(task_id: str, run_id: str) -> dict[str, Any]:
        return {
            "task_id": task_id,
            "run_id": run_id,
            "attempt": 1,
            "worker_id": f"runtime:{run_id}",
            "state": {
                "goal_spec_ref": {"spec_hash": "d" * 64},
                "pending_steering_commands": [],
            },
            "nodes": {},
            "plan_nodes": {},
            "permission_timer_started": None,
            "permission_elapsed_base": 0.0,
        }

    async def test_rejected_unknown_effect_survives_restart_without_reapproval(self) -> None:
        task = create_task_record(
            "执行一个可能已经生效的外部写入",
            "general-agent",
            conversation_id="conv-unknown-rejection",
        )
        first_run = self.state.begin_run(
            task["id"], activate_task_projection=True
        )
        effect = self.journal.prepare_effect(
            task_id=task["id"],
            run_id=first_run["id"],
            goal_spec_hash="d" * 64,
            operation_key="plan-slot:external-write:1",
            server_id="external",
            tool_name="write",
            arguments={"value": "only-if-approved-after-reconciliation"},
            effect_kind="non_idempotent_write",
        )
        claim = self.journal.acquire_effect(
            effect["effect_key"],
            run_id=first_run["id"],
            worker_id=f"runtime:{first_run['id']}",
        )
        self.journal.mark_unknown(
            effect["effect_key"],
            lease_token=claim.lease_token,
            reason="simulated_process_loss_after_remote_dispatch",
        )

        runtime = AgentRuntime(
            object(),
            NeverCalledGateway(),
            object(),
            task_state=self.state,
            tool_effect_journal=self.journal,
        )
        token = runtime._execution_context.set(
            self._execution(task["id"], first_run["id"])
        )
        waiting_worker = asyncio.create_task(
            runtime._acquire_tool_effect(
                effect,
                task_id=task["id"],
                run_id=first_run["id"],
                worker_id=f"runtime:{first_run['id']}",
                server_id="external",
                tool_name="write",
            )
        )
        try:
            stored_task: dict[str, Any] = {}
            for _ in range(200):
                stored_task = db.query_one(
                    "SELECT * FROM tasks WHERE id = ?", (task["id"],)
                ) or {}
                if stored_task.get("status") == "waiting_approval":
                    break
                await asyncio.sleep(0.01)
            self.assertEqual(stored_task.get("status"), "waiting_approval")
            result = db.json_loads(stored_task.get("result_json"), {})
            approval_id = str(result.get("policy_approval_id") or "")
            self.assertTrue(approval_id)
            command = self.state.enqueue_command(
                task["id"],
                "approval",
                run_id=first_run["id"],
                payload={
                    "approval_id": approval_id,
                    "approved": False,
                    "note": "外部状态无法确认，拒绝再次执行",
                },
            )
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(waiting_worker, timeout=3)
        finally:
            runtime._execution_context.reset(token)
            if not waiting_worker.done():
                waiting_worker.cancel()
                await asyncio.gather(waiting_worker, return_exceptions=True)

        # The decision transaction committed before the simulated process
        # loss: no approval is left pending and the exact rejection proof is
        # durable even though this attempt still needs restart recovery.
        decided_task = db.query_one(
            "SELECT * FROM tasks WHERE id = ?", (task["id"],)
        ) or {}
        decided_run = self.state.get_run(first_run["id"]) or {}
        self.assertEqual(decided_task.get("status"), "running")
        self.assertEqual(decided_run.get("status"), "running")
        self.assertEqual(self.state.get_command(command["id"])["status"], "completed")
        proof = self.state.get_policy_approval_decision(task["id"], approval_id)
        self.assertIsNotNone(proof)
        self.assertFalse(bool((proof or {}).get("approved")))

        recovery = self.state.recover_interrupted_attempt(first_run["id"])
        recovered_run = recovery["run"]
        normal_state = TaskStateService(db.get_conn)
        active_run = normal_state.begin_run(
            task["id"],
            run_id=recovered_run["id"],
            activate_task_projection=True,
            task_result=(recovered_run.get("metadata") or {}).get(
                "recovery_activation_result"
            ),
        )
        restarted_runtime = AgentRuntime(
            object(),
            NeverCalledGateway(),
            object(),
            task_state=normal_state,
            tool_effect_journal=ToolEffectJournal(db.get_conn),
        )
        restart_token = restarted_runtime._execution_context.set(
            self._execution(task["id"], active_run["id"])
        )
        try:
            with self.assertRaisesRegex(ToolError, "用户拒绝"):
                await restarted_runtime._acquire_tool_effect(
                    effect,
                    task_id=task["id"],
                    run_id=active_run["id"],
                    worker_id=f"runtime:{active_run['id']}",
                    server_id="external",
                    tool_name="write",
                )
        finally:
            restarted_runtime._execution_context.reset(restart_token)

        normal_state.commit_failure(
            task_id=task["id"],
            run_id=active_run["id"],
            error={
                "message": "用户拒绝了不确定外部写入的再次执行",
                "error_type": "ToolError",
            },
            result={"error": "用户拒绝了不确定外部写入的再次执行"},
        )

        events = db.query_all(
            "SELECT type, data_json FROM task_events WHERE task_id = ? ORDER BY id",
            (task["id"],),
        )
        self.assertEqual(
            sum(item["type"] == "approval_required" for item in events), 1
        )
        self.assertEqual(sum(item["type"] == "approval" for item in events), 1)
        commands = normal_state.list_commands(
            task_id=task["id"], command_types=["approval"]
        )
        self.assertEqual(len(commands), 1)
        self.assertEqual(commands[0]["status"], "completed")
        self.assertEqual((self.journal.get_effect(effect["effect_key"]) or {})["state"], "unknown")
        self.assertEqual(
            self.journal.recover_interrupted_executions(reason="second_restart"),
            [],
        )
        self.assertEqual(
            [
                item
                for item in normal_state.list_runs(task_id=task["id"])
                if item["status"] in {"queued", "running", "paused", "waiting_approval"}
            ],
            [],
        )
        stored_task = db.query_one(
            "SELECT status FROM tasks WHERE id = ?", (task["id"],)
        ) or {}
        self.assertEqual(stored_task.get("status"), "failed")
        normal_state.assert_terminal_clean(
            task_id=task["id"], run_id=active_run["id"]
        )


if __name__ == "__main__":
    unittest.main()
