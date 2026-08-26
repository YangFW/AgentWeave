from __future__ import annotations

import asyncio
import os
import sqlite3
import tempfile
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, patch

from app import db
from app.seed import seed_agents
from app.services import agent_runtime as runtime_module
from app.services import mcp_gateway as mcp_module
from app.services.agent_runtime import AgentRuntime, create_task_record
from app.services.context_service import ContextService, ExecutionScope
from app.services.mcp_gateway import McpGateway
from app.services.model_gateway import ModelGateway
from app.services.policy_engine import PolicyEngine
from app.services.skill_registry import SkillRegistry
from app.services.task_state import (
    ActiveRunConflict,
    PublicationConflict,
    RunIntakeClosed,
    TaskStateService,
)


async def _send_delta(
    callback: Callable[[str], Awaitable[None] | None] | None,
    text: str,
) -> None:
    if callback is None:
        return
    result = callback(text)
    if isinstance(result, Awaitable):
        await result


class RegressionSkillRegistry:
    """Small immutable registry so the tests exercise runtime contracts only."""

    skill = {
        "id": "steering_adversarial",
        "name": "Steering 对抗回归 Skill",
        "description": "验证目标修订、恢复和最终发布边界。",
        "version": "1.0.0",
        "content": "始终遵守当前 GoalSpec，不得遗失用户已经确认的目标。",
        "enabled": True,
        "required_mcps": [],
    }

    def list_skills(self, enabled_only: bool = False) -> list[dict[str, Any]]:
        return [dict(self.skill)]

    def score_skills(
        self,
        message: str,
        allowed_ids: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        if allowed_ids and self.skill["id"] not in allowed_ids:
            return []
        return [{"skill": dict(self.skill), "score": 10.0}]

    def get_skill(self, skill_id: str) -> dict[str, Any] | None:
        return dict(self.skill) if skill_id == self.skill["id"] else None

    def runtime_content(self, skill_id: str, max_chars: int = 16_000) -> str:
        if skill_id != self.skill["id"]:
            return ""
        return str(self.skill["content"])[:max_chars]


class EmptyGateway:
    def list_tools(self) -> list[dict[str, Any]]:
        return []

    async def invoke_tool(
        self,
        server_id: str,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        task_id: str = "",
    ) -> dict[str, Any]:
        raise AssertionError(f"unexpected tool call: {server_id}.{tool_name}")


class OneSteerModel:
    """The first solve blocks; a message command forces one steering revision."""

    def __init__(self) -> None:
        self.resolve_messages: list[str] = []
        self.solve_calls = 0
        self.first_started = asyncio.Event()
        self.first_cancelled = asyncio.Event()

    async def resolve_intent(
        self,
        message: str,
        history: list[dict[str, str]],
        model_config_id: str,
    ) -> dict[str, Any]:
        self.resolve_messages.append(message)
        return {
            "standalone_request": message,
            "intent": "analysis",
            "parameters": {},
            "missing_information": [],
            "is_follow_up": bool(history),
            "source": "adversarial-test",
        }

    async def solve_with_tools(
        self,
        prompt: str,
        system_prompt: str,
        model_config_id: str,
        tools: list[dict[str, Any]],
        invoke: Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]],
        max_steps: int = 8,
        on_delta: Callable[[str], Awaitable[None] | None] | None = None,
        history: list[dict[str, str]] | None = None,
    ) -> str:
        self.solve_calls += 1
        if self.solve_calls == 1:
            self.first_started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.first_cancelled.set()
                raise
            raise AssertionError("unreachable")
        answer = "已根据当前版本的目标生成发布结论。"
        await _send_delta(on_delta, answer)
        return answer


class SteeringNeedsInputModel(OneSteerModel):
    """A running replacement changes the GoalSpec into needs_input."""

    async def resolve_intent(
        self,
        message: str,
        history: list[dict[str, str]],
        model_config_id: str,
    ) -> dict[str, Any]:
        self.resolve_messages.append(message)
        if len(self.resolve_messages) == 1:
            return {
                "standalone_request": message,
                "intent": "analysis",
                "parameters": {},
                "missing_information": [],
                "is_follow_up": False,
                "source": "adversarial-test",
            }
        return {
            "standalone_request": "按指定地区生成新的发布结论",
            "intent": "analysis",
            "parameters": {},
            "missing_information": ["region"],
            "is_follow_up": True,
            "source": "adversarial-test",
        }


class QuickRecordingModel:
    def __init__(self, answer: str = "恢复后的候选结果。") -> None:
        self.answer = answer
        self.resolve_messages: list[str] = []

    async def resolve_intent(
        self,
        message: str,
        history: list[dict[str, str]],
        model_config_id: str,
    ) -> dict[str, Any]:
        self.resolve_messages.append(message)
        return {
            "standalone_request": message,
            "intent": "analysis",
            "parameters": {},
            "missing_information": [],
            "is_follow_up": bool(history),
            "source": "adversarial-test",
        }

    async def solve_with_tools(
        self,
        prompt: str,
        system_prompt: str,
        model_config_id: str,
        tools: list[dict[str, Any]],
        invoke: Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]],
        max_steps: int = 8,
        on_delta: Callable[[str], Awaitable[None] | None] | None = None,
        history: list[dict[str, str]] | None = None,
    ) -> str:
        await _send_delta(on_delta, self.answer)
        return self.answer


class BlockingSolveModel(QuickRecordingModel):
    """Blocks the restored candidate so a new command can amend an old branch."""

    def __init__(self) -> None:
        super().__init__("新分支目标已经完成。")
        self.started = asyncio.Event()
        self.cancelled = asyncio.Event()
        self.solve_calls = 0

    async def solve_with_tools(
        self,
        prompt: str,
        system_prompt: str,
        model_config_id: str,
        tools: list[dict[str, Any]],
        invoke: Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]],
        max_steps: int = 8,
        on_delta: Callable[[str], Awaitable[None] | None] | None = None,
        history: list[dict[str, str]] | None = None,
    ) -> str:
        self.solve_calls += 1
        if self.solve_calls == 1:
            self.started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.cancelled.set()
                raise
            raise AssertionError("unreachable")
        return await super().solve_with_tools(
            prompt,
            system_prompt,
            model_config_id,
            tools,
            invoke,
            max_steps=max_steps,
            on_delta=on_delta,
            history=history,
        )


class BlockingClarificationModel(QuickRecordingModel):
    def __init__(self) -> None:
        super().__init__("华东地区发布结论已经生成。")
        self.resolve_started = asyncio.Event()
        self.release_first_resolve = asyncio.Event()
        self.resolve_calls = 0

    async def resolve_intent(
        self,
        message: str,
        history: list[dict[str, str]],
        model_config_id: str,
    ) -> dict[str, Any]:
        self.resolve_messages.append(message)
        self.resolve_calls += 1
        if self.resolve_calls == 1:
            self.resolve_started.set()
            await self.release_first_resolve.wait()
            return {
                "standalone_request": message,
                "intent": "analysis",
                "parameters": {},
                "missing_information": ["region"],
                "is_follow_up": False,
                "source": "adversarial-test",
            }
        return {
            "standalone_request": "请给出华东地区的发布结论",
            "intent": "analysis",
            "parameters": {"region": "华东"},
            "missing_information": [],
            "is_follow_up": True,
            "source": "adversarial-test",
        }


class CandidateSequenceModel(QuickRecordingModel):
    OLD = "OLD_CANDIDATE：尚未处理最后一刻的新要求。"
    REVISED = "REVISED_CANDIDATE：已经处理最后一刻的新要求。"

    def __init__(self) -> None:
        super().__init__()
        self.solve_calls = 0

    async def solve_with_tools(
        self,
        prompt: str,
        system_prompt: str,
        model_config_id: str,
        tools: list[dict[str, Any]],
        invoke: Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]],
        max_steps: int = 8,
        on_delta: Callable[[str], Awaitable[None] | None] | None = None,
        history: list[dict[str, str]] | None = None,
    ) -> str:
        self.solve_calls += 1
        answer = self.OLD if self.solve_calls == 1 else self.REVISED
        await _send_delta(on_delta, answer)
        return answer


class ResidueThenFailModel(QuickRecordingModel):
    """Create every unfinished row type immediately before a model failure."""

    def __init__(self, state: TaskStateService, task_id: str) -> None:
        super().__init__()
        self.state = state
        self.task_id = task_id
        self.injected_commands: list[dict[str, Any]] = []
        self.artifact_id = f"art_failure_{task_id}"

    async def solve_with_tools(
        self,
        prompt: str,
        system_prompt: str,
        model_config_id: str,
        tools: list[dict[str, Any]],
        invoke: Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]],
        max_steps: int = 8,
        on_delta: Callable[[str], Awaitable[None] | None] | None = None,
        history: list[dict[str, str]] | None = None,
    ) -> str:
        run = self.state.list_runs(task_id=self.task_id)[0]
        self.injected_commands = [
            self.state.enqueue_command(
                self.task_id,
                "approval",
                run_id=run["id"],
                payload={"approved": True},
            ),
            self.state.enqueue_command(
                self.task_id,
                "message",
                run_id=run["id"],
                payload={"message": "失败线性化前到达的追加要求"},
            ),
            self.state.request_cancel(
                self.task_id,
                run_id=run["id"],
                reason="失败线性化前到达的取消请求",
            ),
        ]
        db.execute(
            """
            INSERT INTO artifacts(
                id, task_id, run_id, name, kind, path, created_at,
                delivery_status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 'pending_verification')
            """,
            (
                self.artifact_id,
                self.task_id,
                run["id"],
                "unfinished.md",
                "md",
                str(Path(tempfile.gettempdir()) / "unfinished.md"),
                db.utc_now(),
            ),
        )
        raise RuntimeError("injected terminal failure")


class EnqueueAtPublicationCommit(TaskStateService):
    """Let a second DB client win immediately before the publication CAS."""

    def __init__(self) -> None:
        super().__init__(db.get_conn)
        self.injected_command: dict[str, Any] | None = None

    def commit_verified_publication(self, **kwargs: Any) -> dict[str, Any]:
        if self.injected_command is None:
            competing_client = TaskStateService(db.get_conn)
            self.injected_command = competing_client.enqueue_command(
                str(kwargs["task_id"]),
                "message",
                run_id=str(kwargs["run_id"]),
                payload={"message": "LAST_MOMENT_REQUIREMENT"},
            )
        return super().commit_verified_publication(**kwargs)


class EnqueueAtClarificationCommit(TaskStateService):
    """Inject a user answer at the clarification linearization point."""

    def __init__(self) -> None:
        super().__init__(db.get_conn)
        self.injected_command: dict[str, Any] | None = None

    def commit_clarification_completion(self, **kwargs: Any) -> dict[str, Any]:
        if self.injected_command is None:
            competing_client = TaskStateService(db.get_conn)
            self.injected_command = competing_client.enqueue_command(
                str(kwargs["task_id"]),
                "message",
                run_id=str(kwargs["run_id"]),
                payload={"message": "地区是华东"},
            )
        return super().commit_clarification_completion(**kwargs)


class EnqueueAtPlatformCommandCommit(TaskStateService):
    """Inject a supplement at the direct-command terminal boundary."""

    def __init__(self) -> None:
        super().__init__(db.get_conn)
        self.injected_command: dict[str, Any] | None = None

    def commit_platform_command_completion(self, **kwargs: Any) -> dict[str, Any]:
        if self.injected_command is None:
            competing_client = TaskStateService(db.get_conn)
            self.injected_command = competing_client.enqueue_command(
                str(kwargs["task_id"]),
                "message",
                run_id=str(kwargs["run_id"]),
                payload={"message": "并补充说明这些能力分别适合什么任务"},
            )
        return super().commit_platform_command_completion(**kwargs)


class EnqueueAtPolicyApprovalRequest(TaskStateService):
    """Inject a replacement exactly inside the policy approval CAS window."""

    def __init__(self) -> None:
        super().__init__(db.get_conn)
        self.injected_command: dict[str, Any] | None = None

    def commit_policy_approval_request(self, **kwargs: Any) -> dict[str, Any]:
        if self.injected_command is None:
            self.injected_command = self.enqueue_command(
                str(kwargs["task_id"]),
                "message",
                run_id=str(kwargs["run_id"]),
                payload={
                    "message": "取消之前任务，新的目标：只做安全的只读审计"
                },
            )
        return super().commit_policy_approval_request(**kwargs)


class CancelAtPlatformCommandCommit(TaskStateService):
    """Let cancellation win immediately before a transactional side effect."""

    def __init__(self) -> None:
        super().__init__(db.get_conn)
        self.injected_command: dict[str, Any] | None = None

    def commit_platform_command_completion(self, **kwargs: Any) -> dict[str, Any]:
        if self.injected_command is None:
            competing_client = TaskStateService(db.get_conn)
            self.injected_command = competing_client.request_cancel(
                str(kwargs["task_id"]),
                run_id=str(kwargs["run_id"]),
                reason="取消先于平台副作用提交",
            )
        return super().commit_platform_command_completion(**kwargs)


class FailAfterPlatformEffect(TaskStateService):
    """Raise after the SQL effect to prove the outer transaction rolls back."""

    def commit_platform_command_completion(self, **kwargs: Any) -> dict[str, Any]:
        effect = kwargs.get("transaction_effect")
        if effect is None:
            return super().commit_platform_command_completion(**kwargs)

        def failing_effect(conn: sqlite3.Connection) -> Mapping[str, Any] | None:
            effect(conn)
            raise RuntimeError("injected failure after platform effect")

        kwargs["transaction_effect"] = failing_effect
        return super().commit_platform_command_completion(**kwargs)


class EnqueueAtCancellationCommit(TaskStateService):
    """Let a final message commit immediately before cancellation closes intake."""

    def __init__(self) -> None:
        super().__init__(db.get_conn)
        self.injected_message: dict[str, Any] | None = None

    def commit_cancellation(self, **kwargs: Any) -> dict[str, Any]:
        if self.injected_message is None:
            competing_client = TaskStateService(db.get_conn)
            self.injected_message = competing_client.enqueue_command(
                str(kwargs["task_id"]),
                "message",
                run_id=str(kwargs["run_id"]),
                payload={"message": "取消边界到达的最后一条要求"},
            )
        return super().commit_cancellation(**kwargs)


class CancellationWinsFailureCommit(TaskStateService):
    """Linearize a complete cancellation immediately before failure."""

    def __init__(self) -> None:
        super().__init__(db.get_conn)
        self.cancel_command: dict[str, Any] | None = None

    def commit_failure(self, **kwargs: Any) -> dict[str, Any]:
        if self.cancel_command is None:
            competing_client = TaskStateService(db.get_conn)
            self.cancel_command = competing_client.request_cancel(
                str(kwargs["task_id"]),
                run_id=str(kwargs["run_id"]),
                reason="取消先于失败提交",
            )
            competing_client.commit_cancellation(
                task_id=str(kwargs["task_id"]),
                run_id=str(kwargs["run_id"]),
            )
        return super().commit_failure(**kwargs)


class RuntimeSteeringAdversarialTests(IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.original_db_path = db.DB_PATH
        self.temp_dir = tempfile.TemporaryDirectory()
        db.DB_PATH = Path(self.temp_dir.name) / "steering-adversarial.db"
        db.init_db()
        self.state: TaskStateService = TaskStateService(db.get_conn)

    async def asyncTearDown(self) -> None:
        db.DB_PATH = self.original_db_path
        self.temp_dir.cleanup()

    def _runtime(
        self,
        model: Any,
        *,
        state: TaskStateService | None = None,
        policy: PolicyEngine | None = None,
    ) -> AgentRuntime:
        return AgentRuntime(
            RegressionSkillRegistry(),
            EmptyGateway(),
            model,
            task_state=state or self.state,
            policy_engine=policy or PolicyEngine(),
        )

    @staticmethod
    def _task(message: str) -> dict[str, Any]:
        return create_task_record(
            message,
            "general-agent",
            conversation_id="conv_steering_adversarial",
        )

    def _generic_waiting_approval(
        self, message: str
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        task = self._task(message)
        run = self.state.begin_run(
            task["id"],
            activate_task_projection=True,
        )
        self.state.transition_run(run["id"], "waiting_approval")
        db.update_task_status(
            task["id"],
            "waiting_approval",
            result={
                "pending_action": "external_write",
                "summary": "请确认是否执行外部写入。",
            },
        )
        return task, self.state.get_run(run["id"]) or run

    async def _complete_one_steering(
        self,
        *,
        original: str = "ORIGINAL_RELEASE_CONCLUSION",
        amendment: str = "ADD_RISK_EVIDENCE",
    ) -> tuple[
        dict[str, Any],
        dict[str, Any],
        dict[str, Any],
        OneSteerModel,
    ]:
        model = OneSteerModel()
        runtime = self._runtime(model)
        task = self._task(original)
        worker = asyncio.create_task(runtime.run_task(task["id"]))
        try:
            await asyncio.wait_for(model.first_started.wait(), timeout=3)
            run = self.state.list_runs(task_id=task["id"])[0]
            command = self.state.enqueue_command(
                task["id"],
                "message",
                run_id=run["id"],
                payload={"message": amendment},
            )
            await asyncio.wait_for(worker, timeout=5)
        finally:
            if not worker.done():
                worker.cancel()
                await asyncio.gather(worker, return_exceptions=True)
        return task, run, command, model

    async def test_message_enqueued_after_last_empty_claim_is_not_lost(self) -> None:
        state = EnqueueAtPublicationCommit()
        self.state = state
        model = CandidateSequenceModel()
        task = self._task("生成初始发布结论")

        await self._runtime(model, state=state).run_task(task["id"])

        command = state.injected_command
        self.assertIsNotNone(command, "fixture must inject at the final publication boundary")
        command = state.get_command(str(command["id"]))
        task_row = db.query_one("SELECT status FROM tasks WHERE id = ?", (task["id"],))
        answers = [
            row["content"]
            for row in db.query_all(
                "SELECT content FROM task_events "
                "WHERE task_id = ? AND type = 'answer' ORDER BY id",
                (task["id"],),
            )
        ]
        self.assertEqual(task_row["status"], "completed")
        self.assertEqual(command["status"], "completed")
        self.assertNotIn(CandidateSequenceModel.OLD, answers)
        self.assertEqual(answers, [CandidateSequenceModel.REVISED])

    async def test_steering_to_needs_input_advances_generation_before_clarifying(
        self,
    ) -> None:
        model = SteeringNeedsInputModel()
        runtime = self._runtime(model)
        task = self._task("先生成一份无需补充参数的发布结论")
        worker = asyncio.create_task(runtime.run_task(task["id"]))
        try:
            await asyncio.wait_for(model.first_started.wait(), timeout=3)
            run = self.state.list_runs(task_id=task["id"])[0]
            command = self.state.enqueue_command(
                task["id"],
                "message",
                run_id=run["id"],
                payload={
                    "message": "取消之前任务，新的目标：按指定地区生成发布结论"
                },
            )
            await asyncio.wait_for(worker, timeout=5)
        finally:
            if not worker.done():
                worker.cancel()
                await asyncio.gather(worker, return_exceptions=True)

        task_row = db.query_one(
            "SELECT * FROM tasks WHERE id = ?", (task["id"],)
        )
        run_after = self.state.get_run(run["id"])
        command_after = self.state.get_command(command["id"])
        goal = self.state.latest_goal_spec(run_id=run["id"])
        self.assertEqual(task_row["status"], "completed")
        self.assertTrue(
            db.json_loads(task_row["result_json"], {})["needs_clarification"]
        )
        self.assertEqual(run_after["status"], "completed")
        self.assertEqual(run_after["accepted_generation"], 1)
        self.assertEqual(run_after["applied_generation"], 1)
        self.assertEqual(command_after["status"], "completed")
        self.assertTrue(command_after["result"]["applied"])
        self.assertTrue(command_after["result"]["needs_clarification"])
        self.assertEqual(goal["status"], "needs_input")
        self.assertFalse(
            db.query_all(
                "SELECT id FROM task_events WHERE task_id = ? AND type = 'error'",
                (task["id"],),
            )
        )
        self.state.assert_terminal_clean(task_id=task["id"], run_id=run["id"])

    async def test_message_arriving_during_initial_clarification_is_applied(self) -> None:
        model = BlockingClarificationModel()
        task = self._task("请给出地区性发布结论")
        worker = asyncio.create_task(self._runtime(model).run_task(task["id"]))
        try:
            await asyncio.wait_for(model.resolve_started.wait(), timeout=3)
            run = self.state.list_runs(task_id=task["id"])[0]
            command = self.state.enqueue_command(
                task["id"],
                "message",
                run_id=run["id"],
                payload={"message": "地区是华东"},
            )
            model.release_first_resolve.set()
            await asyncio.wait_for(worker, timeout=5)
        finally:
            model.release_first_resolve.set()
            if not worker.done():
                worker.cancel()
                await asyncio.gather(worker, return_exceptions=True)

        task_row = db.query_one("SELECT status FROM tasks WHERE id = ?", (task["id"],))
        command = self.state.get_command(command["id"])
        self.assertFalse(
            task_row["status"] == "completed" and command["status"] == "queued",
            "a clarification early-return must not strand a message that supplies "
            "the missing input",
        )
        self.assertEqual(command["status"], "completed")

    async def test_plain_clarification_atomically_closes_task_run_and_intake(self) -> None:
        model = BlockingClarificationModel()
        model.release_first_resolve.set()
        task = self._task("请给出地区性发布结论")

        await self._runtime(model).run_task(task["id"])

        task_row = db.query_one("SELECT * FROM tasks WHERE id = ?", (task["id"],))
        run = self.state.list_runs(task_id=task["id"])[0]
        goal = self.state.latest_goal_spec(run_id=run["id"])
        event_types = [
            row["type"]
            for row in db.query_all(
                "SELECT type FROM task_events WHERE task_id = ? ORDER BY id",
                (task["id"],),
            )
        ]
        nodes = self.state.list_nodes(run["id"])

        self.assertEqual(task_row["status"], "completed")
        self.assertTrue(db.json_loads(task_row["result_json"], {})["needs_clarification"])
        self.assertEqual(run["status"], "completed")
        self.assertEqual(run["intake_state"], "closed")
        self.assertTrue(run["intake_closed_at"])
        self.assertEqual(run["published_verification_id"], "")
        self.assertEqual(run["metadata"]["completion_kind"], "clarification")
        self.assertEqual(goal["status"], "needs_input")
        self.assertFalse(self.state.list_verifications(task_id=task["id"]))
        self.assertEqual(event_types.count("clarification"), 1)
        self.assertEqual(event_types.count("done"), 1)
        self.assertNotIn("answer", event_types)
        self.assertTrue(
            all(node["status"] not in {"pending", "running"} for node in nodes)
        )

    async def test_clarification_cancels_non_runtime_commands_before_terminal_commit(self) -> None:
        model = BlockingClarificationModel()
        task = self._task("请给出地区性发布结论")
        worker = asyncio.create_task(self._runtime(model).run_task(task["id"]))
        try:
            await asyncio.wait_for(model.resolve_started.wait(), timeout=3)
            run = self.state.list_runs(task_id=task["id"])[0]
            approval = self.state.enqueue_command(
                task["id"],
                "approval",
                run_id=run["id"],
                payload={"approved": True},
            )
            model.release_first_resolve.set()
            await asyncio.wait_for(worker, timeout=5)
        finally:
            model.release_first_resolve.set()
            if not worker.done():
                worker.cancel()
                await asyncio.gather(worker, return_exceptions=True)

        run = self.state.get_run(run["id"])
        approval = self.state.get_command(approval["id"])
        self.assertEqual(run["status"], "completed")
        self.assertEqual(run["metadata"]["completion_kind"], "clarification")
        self.assertEqual(approval["status"], "cancelled")
        self.assertFalse(
            self.state.list_commands(
                task_id=task["id"], run_id=run["id"], status="queued"
            )
        )
        self.assertFalse(
            self.state.list_commands(
                task_id=task["id"], run_id=run["id"], status="claimed"
            )
        )

    async def test_message_winning_clarification_commit_is_applied_not_stranded(self) -> None:
        state = EnqueueAtClarificationCommit()
        self.state = state
        model = BlockingClarificationModel()
        model.release_first_resolve.set()
        task = self._task("请给出地区性发布结论")

        await self._runtime(model, state=state).run_task(task["id"])

        command = state.injected_command
        self.assertIsNotNone(command)
        command = state.get_command(str(command["id"]))
        run = state.list_runs(task_id=task["id"])[0]
        task_row = db.query_one("SELECT status FROM tasks WHERE id = ?", (task["id"],))
        event_types = [
            row["type"]
            for row in db.query_all(
                "SELECT type FROM task_events WHERE task_id = ? ORDER BY id",
                (task["id"],),
            )
        ]

        self.assertEqual(command["status"], "completed")
        self.assertEqual(task_row["status"], "completed")
        self.assertEqual(run["status"], "completed")
        self.assertEqual(run["accepted_generation"], run["applied_generation"])
        self.assertNotIn("clarification", event_types)
        self.assertEqual(event_types.count("answer"), 1)

    async def test_message_winning_platform_command_commit_continues_normally(
        self,
    ) -> None:
        state = EnqueueAtPlatformCommandCommit()
        self.state = state
        task = self._task("查看已安装技能")

        await self._runtime(
            QuickRecordingModel("已结合补充要求说明各项能力的适用任务。"),
            state=state,
        ).run_task(task["id"])

        command = state.injected_command
        self.assertIsNotNone(command)
        assert command is not None
        command = state.get_command(command["id"])
        run = state.list_runs(task_id=task["id"])[0]
        task_row = db.query_one("SELECT status FROM tasks WHERE id = ?", (task["id"],)) or {}
        event_types = [
            row["type"]
            for row in db.query_all(
                "SELECT type FROM task_events WHERE task_id = ? ORDER BY id",
                (task["id"],),
            )
        ]

        self.assertEqual(command["status"], "completed")
        self.assertEqual(task_row.get("status"), "completed")
        self.assertEqual(run["status"], "completed")
        self.assertEqual(run["accepted_generation"], run["applied_generation"])
        self.assertEqual(event_types.count("answer"), 1)
        self.assertIn("notice", event_types)
        state.assert_terminal_clean(task_id=task["id"], run_id=run["id"])

    async def test_cancel_winning_remember_commit_leaves_no_memory_effect(
        self,
    ) -> None:
        state = CancelAtPlatformCommandCommit()
        context = ContextService()
        runtime = AgentRuntime(
            SkillRegistry(),
            McpGateway(),
            QuickRecordingModel(),
            task_state=state,
            context_service=context,
        )
        task = self._task("记住：所有正式报告默认使用中文")

        await runtime.run_task(task["id"])

        self.assertEqual(context.list_memories(ExecutionScope()), [])
        run = state.list_runs(task_id=task["id"])[0]
        self.assertEqual(run["status"], "cancelled")
        self.assertFalse(
            db.query_all(
                "SELECT id FROM task_events WHERE task_id = ? AND type = 'memory_saved'",
                (task["id"],),
            )
        )
        state.assert_terminal_clean(task_id=task["id"], run_id=run["id"])

    async def test_failure_after_memory_effect_rolls_back_business_write(
        self,
    ) -> None:
        state = FailAfterPlatformEffect(db.get_conn)
        context = ContextService()
        runtime = AgentRuntime(
            SkillRegistry(),
            McpGateway(),
            QuickRecordingModel(),
            task_state=state,
            context_service=context,
        )
        task = self._task("记住：生成演示文稿前先确认大纲")

        await runtime.run_task(task["id"])

        self.assertEqual(context.list_memories(ExecutionScope()), [])
        stored_task = db.query_one(
            "SELECT status FROM tasks WHERE id = ?", (task["id"],)
        ) or {}
        run = state.list_runs(task_id=task["id"])[0]
        self.assertEqual(stored_task.get("status"), "failed")
        self.assertEqual(run["status"], "failed")
        self.assertFalse(
            db.query_all(
                "SELECT id FROM task_events WHERE task_id = ? AND type = 'memory_saved'",
                (task["id"],),
            )
        )
        state.assert_terminal_clean(task_id=task["id"], run_id=run["id"])

    async def test_cancel_winning_forget_commit_preserves_existing_memory(
        self,
    ) -> None:
        state = CancelAtPlatformCommandCommit()
        context = ContextService()
        remembered = context.remember(
            ExecutionScope(),
            "默认称呼用户为负责人",
            title="默认称呼用户为负责人",
        )
        runtime = AgentRuntime(
            SkillRegistry(),
            McpGateway(),
            QuickRecordingModel(),
            task_state=state,
            context_service=context,
        )
        task = self._task("忘记：默认称呼用户为负责人")

        await runtime.run_task(task["id"])

        remaining = context.list_memories(ExecutionScope())
        self.assertEqual([item["id"] for item in remaining], [remembered["id"]])
        run = state.list_runs(task_id=task["id"])[0]
        self.assertEqual(run["status"], "cancelled")
        state.assert_terminal_clean(task_id=task["id"], run_id=run["id"])

    async def test_cancel_winning_skill_install_leaves_registry_unchanged(
        self,
    ) -> None:
        state = CancelAtPlatformCommandCommit()
        registry = SkillRegistry()
        runtime = AgentRuntime(
            registry,
            McpGateway(),
            QuickRecordingModel(),
            task_state=state,
        )
        content = (
            "---\n"
            "id: atomic_cancel_skill\n"
            "name: Atomic Cancel Skill\n"
            "version: 1.0.0\n"
            "---\n"
            "Only installed after the terminal fence wins.\n"
        )
        task = self._task("安装 Skill\n" + content)

        await runtime.run_task(task["id"])

        self.assertIsNone(registry.get_skill("atomic_cancel_skill"))
        run = state.list_runs(task_id=task["id"])[0]
        self.assertEqual(run["status"], "cancelled")
        self.assertFalse(
            db.query_all(
                "SELECT id FROM task_events WHERE task_id = ? AND type = 'install'",
                (task["id"],),
            )
        )
        state.assert_terminal_clean(task_id=task["id"], run_id=run["id"])

    async def test_cancel_winning_mcp_install_leaves_registry_unchanged(
        self,
    ) -> None:
        state = CancelAtPlatformCommandCommit()
        gateway = McpGateway()
        runtime = AgentRuntime(
            SkillRegistry(),
            gateway,
            QuickRecordingModel(),
            task_state=state,
        )
        task = self._task(
            '安装 MCP\n```json\n{"mcpServers":{"atomic-cancel-mcp":'
            '{"command":"npx","args":["demo"]}}}\n```'
        )

        await runtime.run_task(task["id"])

        self.assertIsNone(gateway.get_server("atomic-cancel-mcp"))
        run = state.list_runs(task_id=task["id"])[0]
        self.assertEqual(run["status"], "cancelled")
        self.assertFalse(
            db.query_all(
                "SELECT id FROM task_events WHERE task_id = ? AND type = 'install'",
                (task["id"],),
            )
        )
        state.assert_terminal_clean(task_id=task["id"], run_id=run["id"])

    async def test_cancel_atomically_consumes_message_winning_terminal_race(self) -> None:
        state = EnqueueAtCancellationCommit()
        self.state = state
        model = BlockingSolveModel()
        task = self._task("生成一个需要较长时间的结论")
        worker = asyncio.create_task(self._runtime(model, state=state).run_task(task["id"]))
        try:
            await asyncio.wait_for(model.started.wait(), timeout=3)
            run = state.list_runs(task_id=task["id"])[0]
            approval = state.enqueue_command(
                task["id"],
                "approval",
                run_id=run["id"],
                payload={"approved": True},
            )
            cancel = state.request_cancel(
                task["id"], run_id=run["id"], reason="用户停止任务"
            )
            await asyncio.wait_for(worker, timeout=5)
        finally:
            if not worker.done():
                worker.cancel()
                await asyncio.gather(worker, return_exceptions=True)

        late_message = state.injected_message
        self.assertIsNotNone(late_message)
        late_message = state.get_command(str(late_message["id"]))
        cancel = state.get_command(cancel["id"])
        approval = state.get_command(approval["id"])
        run = state.get_run(run["id"])
        task_row = db.query_one("SELECT status FROM tasks WHERE id = ?", (task["id"],))
        formal_events = db.query_all(
            "SELECT type FROM task_events WHERE task_id = ? "
            "AND type IN ('candidate_verified', 'answer', 'done')",
            (task["id"],),
        )

        self.assertEqual(task_row["status"], "cancelled")
        self.assertEqual(run["status"], "cancelled")
        self.assertEqual(run["intake_state"], "closed")
        self.assertEqual(run["accepted_generation"], 2)
        self.assertEqual(run["applied_generation"], 2)
        self.assertEqual(cancel["status"], "completed")
        self.assertEqual(approval["status"], "cancelled")
        self.assertEqual(late_message["status"], "cancelled")
        self.assertEqual(formal_events, [])
        self.assertTrue(model.cancelled.is_set())

    async def test_generic_failure_atomically_closes_every_runtime_residue(self) -> None:
        task = self._task("生成一个将在终态边界失败的结论")
        model = ResidueThenFailModel(self.state, task["id"])

        await self._runtime(model).run_task(task["id"])

        task_row = db.query_one("SELECT * FROM tasks WHERE id = ?", (task["id"],))
        run = self.state.list_runs(task_id=task["id"])[0]
        nodes = self.state.list_nodes(run["id"])
        commands = self.state.list_commands(task_id=task["id"], run_id=run["id"])
        artifact = db.query_one(
            "SELECT delivery_status FROM artifacts WHERE id = ?",
            (model.artifact_id,),
        )
        error_events = db.query_all(
            "SELECT id FROM task_events WHERE task_id = ? AND type = 'error'",
            (task["id"],),
        )

        self.assertEqual(task_row["status"], "failed")
        self.assertEqual(run["status"], "failed")
        self.assertEqual(run["intake_state"], "closed")
        self.assertEqual(run["accepted_generation"], run["applied_generation"])
        self.assertTrue(nodes)
        self.assertTrue(
            all(node["status"] not in {"pending", "running"} for node in nodes)
        )
        self.assertTrue(commands)
        self.assertTrue(
            all(command["status"] not in {"queued", "claimed"} for command in commands)
        )
        self.assertEqual(artifact["delivery_status"], "rejected")
        self.assertEqual(len(error_events), 1)
        with self.assertRaises(RunIntakeClosed):
            self.state.enqueue_command(
                task["id"],
                "approval",
                run_id=run["id"],
                payload={"approved": True},
            )

    async def test_runtime_start_activates_task_and_run_before_checkpoint_decode(
        self,
    ) -> None:
        task = self._task("从上一完成结果恢复并验证启动原子性")
        db.update_task_status(
            task["id"],
            "completed",
            result={"summary": "旧尝试结果"},
            artifacts=[{"id": "old-artifact"}],
        )
        queued = self.state.create_run(task["id"], metadata={"trigger": "resume"})
        runtime = self._runtime(QuickRecordingModel())
        observed: dict[str, str] = {}

        def reject_checkpoint() -> None:
            task_row = db.query_one(
                "SELECT status, result_json, artifacts_json FROM tasks WHERE id = ?",
                (task["id"],),
            ) or {}
            run_row = self.state.get_run(queued["id"]) or {}
            observed.update(
                {
                    "task_status": str(task_row.get("status") or ""),
                    "run_status": str(run_row.get("status") or ""),
                    "result_json": str(task_row.get("result_json") or ""),
                    "artifacts_json": str(task_row.get("artifacts_json") or ""),
                }
            )
            raise RuntimeError("拒绝不可信恢复状态")

        with patch.object(
            runtime,
            "_rehydrate_goal_specs",
            side_effect=reject_checkpoint,
        ):
            await runtime.run_task(task["id"], run_id=queued["id"])

        self.assertEqual(observed["task_status"], "running")
        self.assertEqual(observed["run_status"], "running")
        self.assertEqual(observed["result_json"], "{}")
        self.assertEqual(observed["artifacts_json"], "[]")
        self.assertEqual(
            (db.query_one("SELECT status FROM tasks WHERE id = ?", (task["id"],)) or {})[
                "status"
            ],
            "failed",
        )
        self.assertEqual((self.state.get_run(queued["id"]) or {})["status"], "failed")
        self.state.assert_terminal_clean(task_id=task["id"], run_id=queued["id"])

    async def test_runtime_start_projection_failure_rolls_back_run_claim(self) -> None:
        task = self._task("验证启动事务整体回滚")
        queued = self.state.create_run(task["id"])
        db.update_task_status(task["id"], "waiting_approval")

        with self.assertRaises(PublicationConflict):
            self.state.begin_run(
                task["id"],
                run_id=queued["id"],
                activate_task_projection=True,
            )

        self.assertEqual((self.state.get_run(queued["id"]) or {})["status"], "queued")
        task_row = db.query_one("SELECT status FROM tasks WHERE id = ?", (task["id"],)) or {}
        self.assertEqual(task_row.get("status"), "waiting_approval")

    async def test_duplicate_worker_start_does_not_fail_the_owned_attempt(self) -> None:
        task = self._task("验证重复 Worker 不会破坏活动运行")
        owned = self.state.begin_run(
            task["id"],
            activate_task_projection=True,
        )

        await self._runtime(QuickRecordingModel()).run_task(
            task["id"],
            run_id=owned["id"],
        )

        task_row = db.query_one("SELECT status FROM tasks WHERE id = ?", (task["id"],)) or {}
        self.assertEqual(task_row.get("status"), "running")
        self.assertEqual((self.state.get_run(owned["id"]) or {})["status"], "running")
        errors = db.query_all(
            "SELECT id FROM task_events WHERE task_id = ? AND type = 'error'",
            (task["id"],),
        )
        self.assertEqual(errors, [])

    async def test_duplicate_worker_without_run_id_loses_start_silently(self) -> None:
        task = self._task("验证未指定 Run 的重复 Worker 静默退出")
        owner_model = BlockingSolveModel()
        owner = asyncio.create_task(self._runtime(owner_model).run_task(task["id"]))
        try:
            await asyncio.wait_for(owner_model.started.wait(), timeout=3)
            owned_runs = self.state.list_runs(task_id=task["id"])
            self.assertEqual(len(owned_runs), 1)
            owned_run_id = owned_runs[0]["id"]
            task_before = dict(
                db.query_one("SELECT * FROM tasks WHERE id = ?", (task["id"],)) or {}
            )
            run_before = self.state.get_run(owned_run_id)

            await self._runtime(QuickRecordingModel()).run_task(task["id"])

            task_after = dict(
                db.query_one("SELECT * FROM tasks WHERE id = ?", (task["id"],)) or {}
            )
            run_after = self.state.get_run(owned_run_id)
            errors = db.query_all(
                "SELECT id FROM task_events WHERE task_id = ? AND type = 'error'",
                (task["id"],),
            )
            self.assertEqual(self.state.list_runs(task_id=task["id"]), owned_runs)
            self.assertEqual(task_after, task_before)
            self.assertEqual(run_after, run_before)
            self.assertEqual(errors, [])
        finally:
            if not owner.done():
                owner.cancel()
                await asyncio.gather(owner, return_exceptions=True)

    async def test_duplicate_worker_uses_structured_owner_not_error_text(self) -> None:
        task = self._task("验证重复 Worker 不依赖异常文本识别活动运行")
        owned = self.state.begin_run(
            task["id"],
            activate_task_projection=True,
        )
        conflict = ActiveRunConflict(task["id"], owned["id"])
        conflict.args = ("intentionally unparseable conflict message",)

        runtime = self._runtime(QuickRecordingModel())
        with patch.object(self.state, "begin_run", side_effect=conflict):
            await runtime.run_task(task["id"])

        task_row = db.query_one("SELECT status FROM tasks WHERE id = ?", (task["id"],)) or {}
        self.assertEqual(task_row.get("status"), "running")
        self.assertEqual((self.state.get_run(owned["id"]) or {}).get("status"), "running")
        self.assertEqual(
            db.query_all(
                "SELECT id FROM task_events WHERE task_id = ? AND type = 'error'",
                (task["id"],),
            ),
            [],
        )

    async def test_generic_approval_rejection_commits_task_and_run_atomically(
        self,
    ) -> None:
        task, run = self._generic_waiting_approval("拒绝一次通用外部写入")
        runtime = self._runtime(QuickRecordingModel())

        with patch.object(
            self.state,
            "finish_run",
            side_effect=AssertionError("不得使用分事务 Run 终态写入"),
        ):
            await runtime.resume_after_approval(task["id"], False, "不允许写入")

        stored_task = db.query_one("SELECT * FROM tasks WHERE id = ?", (task["id"],)) or {}
        stored_run = self.state.get_run(run["id"]) or {}
        self.assertEqual(stored_task.get("status"), "completed")
        self.assertEqual(stored_run.get("status"), "completed")
        self.assertEqual(stored_run.get("intake_state"), "closed")
        self.assertEqual(
            db.json_loads(stored_task.get("result_json"), {}).get("approval"),
            "rejected",
        )
        approval_commands = self.state.list_commands(
            task_id=task["id"], command_types=["approval"]
        )
        self.assertEqual(len(approval_commands), 1)
        self.assertEqual(approval_commands[0]["status"], "completed")
        self.assertEqual(
            approval_commands[0]["result"]["action"], "generic_approval"
        )
        self.state.assert_terminal_clean(task_id=task["id"], run_id=run["id"])

    async def test_generic_unsupported_completion_commits_task_and_run_atomically(
        self,
    ) -> None:
        task, run = self._generic_waiting_approval("批准但当前不支持的外部写入")
        runtime = self._runtime(QuickRecordingModel())

        with patch.object(
            self.state,
            "finish_run",
            side_effect=AssertionError("不得使用分事务 Run 终态写入"),
        ):
            await runtime.resume_after_approval(task["id"], True, "允许写入")

        stored_task = db.query_one("SELECT * FROM tasks WHERE id = ?", (task["id"],)) or {}
        stored_run = self.state.get_run(run["id"]) or {}
        result = db.json_loads(stored_task.get("result_json"), {})
        self.assertEqual(stored_task.get("status"), "completed")
        self.assertEqual(stored_run.get("status"), "completed")
        self.assertEqual(stored_run.get("intake_state"), "closed")
        self.assertEqual(result.get("approval"), "approved")
        self.assertEqual(result.get("write_back"), "not_configured")
        approval_commands = self.state.list_commands(
            task_id=task["id"], command_types=["approval"]
        )
        self.assertEqual(len(approval_commands), 1)
        self.assertEqual(approval_commands[0]["status"], "completed")
        self.assertEqual(
            approval_commands[0]["result"]["action"], "generic_approval"
        )
        self.state.assert_terminal_clean(task_id=task["id"], run_id=run["id"])

    async def test_generic_approval_resolution_routes_pending_message_to_steering(
        self,
    ) -> None:
        for approved in (False, True):
            with self.subTest(approved=approved):
                model = QuickRecordingModel("已按最新补充要求完成分析。")
                runtime = self._runtime(model)
                task, run = self._generic_waiting_approval(
                    f"通用审批竞争 approved={approved}"
                )
                command = self.state.enqueue_command(
                    task["id"],
                    "message",
                    run_id=run["id"],
                    payload={"message": "补充要求：改为只读审计并给出结论"},
                )

                await runtime.resume_after_approval(
                    task["id"], approved, "记录审批决定"
                )

                stored_task = db.query_one(
                    "SELECT * FROM tasks WHERE id = ?", (task["id"],)
                ) or {}
                stored_run = self.state.get_run(run["id"]) or {}
                stored_command = self.state.get_command(command["id"]) or {}
                answers = db.query_all(
                    "SELECT title, content FROM task_events "
                    "WHERE task_id = ? AND type = 'answer' ORDER BY id",
                    (task["id"],),
                )
                self.assertEqual(stored_task.get("status"), "completed")
                self.assertEqual(stored_run.get("status"), "completed")
                self.assertEqual(stored_command.get("status"), "completed")
                approval_commands = self.state.list_commands(
                    task_id=task["id"], command_types=["approval"]
                )
                self.assertEqual(len(approval_commands), 1)
                self.assertEqual(approval_commands[0]["status"], "completed")
                self.assertTrue(approval_commands[0]["result"]["superseded"])
                self.assertEqual(
                    stored_run.get("accepted_generation"),
                    stored_run.get("applied_generation"),
                )
                self.assertTrue(model.resolve_messages)
                self.assertFalse(
                    any(item["title"] == "未执行外部写入" for item in answers)
                )
                self.state.assert_terminal_clean(
                    task_id=task["id"], run_id=run["id"]
                )

    async def test_generic_superseded_approval_replays_after_commit_before_continuation(
        self,
    ) -> None:
        """A restart after the decision transaction must not strand steering."""

        model = QuickRecordingModel("已在恢复后按最新要求完成只读审计。")
        runtime = self._runtime(model)
        task, run = self._generic_waiting_approval(
            "审批后执行外部写入，但允许用户在等待期间改变目标"
        )
        message = self.state.enqueue_command(
            task["id"],
            "message",
            run_id=run["id"],
            payload={"message": "补充要求：不要写入，只做只读审计"},
        )

        with patch.object(
            runtime,
            "run_task",
            new_callable=AsyncMock,
            side_effect=asyncio.CancelledError,
        ) as interrupted_continuation:
            with self.assertRaises(asyncio.CancelledError):
                await runtime.resume_after_approval(
                    task["id"], False, "记录决定后模拟进程退出"
                )
            interrupted_continuation.assert_awaited_once()

        approval = self.state.list_commands(
            task_id=task["id"], command_types=["approval"]
        )[0]
        self.assertEqual(approval["status"], "completed")
        self.assertTrue(approval["result"]["superseded"])
        self.assertEqual(self.state.get_command(message["id"])["status"], "queued")
        self.assertEqual(
            db.query_one("SELECT status FROM tasks WHERE id = ?", (task["id"],))[
                "status"
            ],
            "waiting_approval",
        )

        await runtime.resume_after_approval(
            task["id"],
            False,
            "记录决定后模拟进程退出",
            command_id=approval["id"],
        )

        stored_task = db.query_one(
            "SELECT * FROM tasks WHERE id = ?", (task["id"],)
        ) or {}
        stored_run = self.state.get_run(run["id"]) or {}
        self.assertEqual(stored_task.get("status"), "completed")
        self.assertEqual(stored_run.get("status"), "completed")
        self.assertEqual(self.state.get_command(message["id"])["status"], "completed")
        self.assertTrue(model.resolve_messages)
        self.state.assert_terminal_clean(task_id=task["id"], run_id=run["id"])

    async def test_pre_goal_recovery_message_replaces_old_request_before_policy(
        self,
    ) -> None:
        policy = PolicyEngine(
            [
                {
                    "id": "deny-old-dangerous-request",
                    "event": "task.created",
                    "scope": "organization",
                    "match": {
                        "conditions": [
                            {
                                "path": "task.message",
                                "op": "contains",
                                "value": "执行危险写入",
                            }
                        ]
                    },
                    "handler": {
                        "type": "builtin_rule",
                        "decision": "deny",
                        "reason": "旧写入目标不得执行",
                    },
                }
            ]
        )
        model = QuickRecordingModel("只读审计已经完成。")
        runtime = self._runtime(model, policy=policy)
        task = self._task("执行危险写入")
        queued_run = self.state.create_run(task["id"])
        message = self.state.enqueue_command(
            task["id"],
            "message",
            run_id=queued_run["id"],
            payload={"message": "取消之前任务，新的目标：只做只读审计"},
        )

        await runtime.run_task(task["id"], run_id=queued_run["id"])

        stored_task = db.query_one(
            "SELECT * FROM tasks WHERE id = ?", (task["id"],)
        ) or {}
        self.assertEqual(stored_task.get("status"), "completed")
        self.assertEqual(self.state.get_command(message["id"])["status"], "completed")
        self.assertTrue(model.resolve_messages)
        self.assertEqual(
            model.resolve_messages[0],
            "取消之前任务，新的目标：只做只读审计",
        )
        self.assertFalse(
            db.query_all(
                "SELECT id FROM task_events WHERE task_id = ? AND type = 'error'",
                (task["id"],),
            )
        )
        self.state.assert_terminal_clean(
            task_id=task["id"], run_id=queued_run["id"]
        )

    async def test_pre_goal_recovery_cancel_wins_before_policy_evaluation(
        self,
    ) -> None:
        policy = PolicyEngine(
            [
                {
                    "id": "deny-every-created-task",
                    "event": "task.created",
                    "scope": "organization",
                    "handler": {
                        "type": "builtin_rule",
                        "decision": "deny",
                        "reason": "若策略先执行就会错误失败",
                    },
                }
            ]
        )
        runtime = self._runtime(QuickRecordingModel(), policy=policy)
        task = self._task("该任务已在恢复前取消")
        queued_run = self.state.create_run(task["id"])
        cancel = self.state.request_cancel(
            task["id"], run_id=queued_run["id"], reason="用户取消"
        )

        await runtime.run_task(task["id"], run_id=queued_run["id"])

        self.assertEqual(
            db.query_one("SELECT status FROM tasks WHERE id = ?", (task["id"],))[
                "status"
            ],
            "cancelled",
        )
        self.assertEqual(self.state.get_run(queued_run["id"])["status"], "cancelled")
        self.assertEqual(self.state.get_command(cancel["id"])["status"], "completed")
        self.assertFalse(
            db.query_all(
                "SELECT id FROM task_events "
                "WHERE task_id = ? AND type = 'policy_decision'",
                (task["id"],),
            )
        )
        self.state.assert_terminal_clean(
            task_id=task["id"], run_id=queued_run["id"]
        )

    async def test_message_wins_inside_policy_approval_request_transaction(
        self,
    ) -> None:
        state = EnqueueAtPolicyApprovalRequest()
        self.state = state
        policy = PolicyEngine(
            [
                {
                    "id": "approve-old-dangerous-request",
                    "event": "task.created",
                    "scope": "organization",
                    "match": {
                        "conditions": [
                            {
                                "path": "task.message",
                                "op": "contains",
                                "value": "执行危险写入",
                            }
                        ]
                    },
                    "handler": {
                        "type": "builtin_rule",
                        "decision": "require_approval",
                        "reason": "旧写入目标需要审批",
                    },
                }
            ]
        )
        model = QuickRecordingModel("只读审计已经完成。")
        runtime = self._runtime(model, state=state, policy=policy)
        task = self._task("执行危险写入")

        await runtime.run_task(task["id"])

        command = state.injected_command
        self.assertIsNotNone(command)
        self.assertEqual(state.get_command(command["id"])["status"], "completed")
        self.assertEqual(
            db.query_one("SELECT status FROM tasks WHERE id = ?", (task["id"],))[
                "status"
            ],
            "completed",
        )
        self.assertEqual(
            model.resolve_messages[0],
            "取消之前任务，新的目标：只做安全的只读审计",
        )
        self.assertFalse(
            db.query_all(
                "SELECT id FROM task_events "
                "WHERE task_id = ? AND type = 'approval_required'",
                (task["id"],),
            )
        )
        run = state.list_runs(task_id=task["id"])[0]
        state.assert_terminal_clean(task_id=task["id"], run_id=run["id"])

    async def test_queued_sibling_start_cannot_fail_an_owned_attempt(self) -> None:
        task = self._task("验证双运行启动竞争不会误伤活动任务")
        owned = self.state.begin_run(
            task["id"],
            activate_task_projection=True,
        )
        sibling = self.state.create_run(task["id"], metadata={"trigger": "duplicate"})
        runtime = self._runtime(QuickRecordingModel())

        with patch.object(
            runtime,
            "_evaluate_policy",
            new_callable=AsyncMock,
        ) as evaluate_policy:
            await runtime.run_task(task["id"], run_id=sibling["id"])

        task_row = db.query_one("SELECT status FROM tasks WHERE id = ?", (task["id"],)) or {}
        self.assertEqual(task_row.get("status"), "running")
        self.assertEqual((self.state.get_run(owned["id"]) or {})["status"], "running")
        self.assertEqual((self.state.get_run(sibling["id"]) or {})["status"], "queued")
        failed_policy_calls = [
            call
            for call in evaluate_policy.await_args_list
            if call.args and call.args[0] == "task.failed"
        ]
        self.assertEqual(failed_policy_calls, [])

    async def test_cancellation_winning_failure_race_is_accepted_only_when_clean(self) -> None:
        state = CancellationWinsFailureCommit()
        self.state = state
        task = self._task("验证取消与失败的终态竞争")
        model = ResidueThenFailModel(state, task["id"])

        await self._runtime(model, state=state).run_task(task["id"])

        task_row = db.query_one("SELECT status FROM tasks WHERE id = ?", (task["id"],))
        run = state.list_runs(task_id=task["id"])[0]
        commands = state.list_commands(task_id=task["id"], run_id=run["id"])
        artifact = db.query_one(
            "SELECT delivery_status FROM artifacts WHERE id = ?",
            (model.artifact_id,),
        )
        self.assertEqual(task_row["status"], "cancelled")
        self.assertEqual(run["status"], "cancelled")
        self.assertEqual(run["accepted_generation"], run["applied_generation"])
        self.assertTrue(
            all(command["status"] not in {"queued", "claimed"} for command in commands)
        )
        self.assertEqual(artifact["delivery_status"], "rejected")
        state.assert_terminal_clean(task_id=task["id"], run_id=run["id"])

    async def test_failure_terminal_transaction_rolls_back_every_write_on_fault(self) -> None:
        task = self._task("验证失败终态事务回滚")
        run = self.state.begin_run(task["id"])
        db.update_task_status(task["id"], "running")
        running_node = self.state.create_node(
            run["id"], "running", "正在执行", sequence=1
        )
        self.state.transition_node(running_node["id"], "running")
        pending_node = self.state.create_node(
            run["id"], "pending", "等待执行", sequence=2
        )
        command = self.state.enqueue_command(
            task["id"],
            "approval",
            run_id=run["id"],
            payload={"approved": True},
        )
        artifact_id = f"art_rollback_{task['id']}"
        db.execute(
            """
            INSERT INTO artifacts(
                id, task_id, run_id, name, kind, path, created_at,
                delivery_status
            ) VALUES (?, ?, ?, 'rollback.md', 'md', '', ?, 'pending_verification')
            """,
            (artifact_id, task["id"], run["id"], db.utc_now()),
        )
        db.execute(
            """
            CREATE TRIGGER reject_failed_run_update
            BEFORE UPDATE OF status ON task_runs
            WHEN NEW.status = 'failed'
            BEGIN
              SELECT RAISE(ABORT, 'injected failure commit fault');
            END
            """
        )

        with self.assertRaisesRegex(sqlite3.IntegrityError, "injected failure"):
            self.state.commit_failure(
                task_id=task["id"],
                run_id=run["id"],
                error={"message": "should roll back"},
            )

        task_row = db.query_one("SELECT status FROM tasks WHERE id = ?", (task["id"],))
        run_row = self.state.get_run(run["id"])
        nodes = {item["id"]: item for item in self.state.list_nodes(run["id"])}
        command_row = self.state.get_command(command["id"])
        artifact = db.query_one(
            "SELECT delivery_status FROM artifacts WHERE id = ?", (artifact_id,)
        )
        error_count = db.query_one(
            "SELECT COUNT(*) AS count FROM task_events WHERE task_id = ? AND type = 'error'",
            (task["id"],),
        )

        self.assertEqual(task_row["status"], "running")
        self.assertEqual(run_row["status"], "running")
        self.assertEqual(run_row["intake_state"], "open")
        self.assertEqual(nodes[running_node["id"]]["status"], "running")
        self.assertEqual(nodes[pending_node["id"]]["status"], "pending")
        self.assertEqual(command_row["status"], "queued")
        self.assertEqual(artifact["delivery_status"], "pending_verification")
        self.assertEqual(int(error_count["count"]), 0)

    async def test_supplement_preserves_original_goal_and_command_provenance(self) -> None:
        task, run, command, _ = await self._complete_one_steering()

        latest = self.state.latest_goal_spec(run_id=run["id"])
        objective = latest["spec"]["objective"]
        self.assertIn("ORIGINAL_RELEASE_CONCLUSION", objective["statement"])
        self.assertIn("ADD_RISK_EVIDENCE", objective["statement"])
        self.assertTrue(
            any(
                item.get("source_type") == "conversation_event"
                and item.get("source_id") == command["id"]
                for item in objective.get("provenance", [])
            ),
            "the revised objective must cite the message command that amended it",
        )
        self.assertEqual(latest["task_id"], task["id"])

    async def test_explicit_replacement_does_not_merge_the_previous_objective(self) -> None:
        task, run, command, _ = await self._complete_one_steering(
            original="ORIGINAL_GOAL_MUST_BE_REPLACED",
            amendment="将当前任务目标替换为 NEW_REPLACEMENT_GOAL",
        )

        latest = self.state.latest_goal_spec(run_id=run["id"])
        objective = latest["spec"]["objective"]
        self.assertIn("NEW_REPLACEMENT_GOAL", objective["statement"])
        self.assertNotIn("ORIGINAL_GOAL_MUST_BE_REPLACED", objective["statement"])
        self.assertTrue(
            any(
                item.get("source_type") == "conversation_event"
                and item.get("source_id") == command["id"]
                for item in objective.get("provenance", [])
            )
        )
        self.assertEqual(latest["task_id"], task["id"])

    async def test_restoring_completed_pending_command_does_not_reapply_it(self) -> None:
        task, run, command, _ = await self._complete_one_steering()
        version_before_restore = int(
            self.state.latest_goal_spec(run_id=run["id"])["version"]
        )
        steering_events_before_restore = int(
            db.query_one(
                "SELECT COUNT(*) AS count FROM task_events "
                "WHERE task_id = ? AND type = 'steering'",
                (task["id"],),
            )["count"]
        )
        checkpoints = self.state.list_checkpoints(
            run_id=run["id"], include_state=True, limit=200
        )
        checkpoint = next(
            item
            for item in checkpoints
            if item["state"].get("pending_steering_commands")
            and "新版目标与执行计划已固化" in item["reason"]
        )
        self.assertEqual(self.state.get_command(command["id"])["status"], "completed")

        resumed = self.state.create_run(
            task["id"], resumed_from_checkpoint_id=checkpoint["id"]
        )
        recovery_model = QuickRecordingModel()
        await self._runtime(recovery_model).run_task(task["id"], run_id=resumed["id"])

        resumed_goal = self.state.latest_goal_spec(run_id=resumed["id"])
        self.assertEqual(
            recovery_model.resolve_messages,
            [],
            "a completed message command restored from pending checkpoint state "
            "must not be sent through intent resolution again",
        )
        self.assertEqual(int(resumed_goal["version"]), version_before_restore)
        self.assertEqual(self.state.get_command(command["id"])["status"], "completed")
        steering_events_after_restore = int(
            db.query_one(
                "SELECT COUNT(*) AS count FROM task_events "
                "WHERE task_id = ? AND type = 'steering'",
                (task["id"],),
            )["count"]
        )
        self.assertEqual(
            steering_events_after_restore,
            steering_events_before_restore,
            "restoring a checkpoint that still lists an already-completed command "
            "must not emit a second 'command applied' event",
        )

    async def test_old_checkpoint_branch_allocates_monotonic_goal_version(self) -> None:
        task, first_run, _, _ = await self._complete_one_steering(
            amendment="FIRST_AMENDMENT"
        )
        maximum_before_restore = max(
            int(item["version"])
            for item in self.state.list_goal_specs(task_id=task["id"], limit=100)
        )
        checkpoints = self.state.list_checkpoints(
            run_id=first_run["id"], include_state=True, limit=200
        )
        old_checkpoint = next(
            item
            for item in checkpoints
            if int((item["state"].get("goal_spec") or {}).get("version") or 0) == 2
        )
        resumed = self.state.create_run(
            task["id"], resumed_from_checkpoint_id=old_checkpoint["id"]
        )
        model = BlockingSolveModel()
        worker = asyncio.create_task(
            self._runtime(model).run_task(task["id"], run_id=resumed["id"])
        )
        try:
            try:
                await asyncio.wait_for(model.started.wait(), timeout=3)
            except TimeoutError as exc:
                resumed_debug = self.state.get_run(resumed["id"])
                errors = db.query_all(
                    "SELECT type, title, content FROM task_events "
                    "WHERE task_id = ? ORDER BY id DESC LIMIT 5",
                    (task["id"],),
                )
                raise AssertionError(
                    f"restored run failed before model start: {resumed_debug}; "
                    f"events={errors}"
                ) from exc
            command = self.state.enqueue_command(
                task["id"],
                "message",
                run_id=resumed["id"],
                payload={"message": "SECOND_BRANCH_AMENDMENT"},
            )
            await asyncio.wait_for(worker, timeout=5)
        finally:
            if not worker.done():
                worker.cancel()
                await asyncio.gather(worker, return_exceptions=True)

        resumed_row = self.state.get_run(resumed["id"])
        latest = self.state.latest_goal_spec(run_id=resumed["id"])
        self.assertEqual(
            resumed_row["status"],
            "completed",
            f"restoring an older branch must allocate above the task-wide maximum; "
            f"runtime error was {resumed_row.get('error')}",
        )
        self.assertEqual(self.state.get_command(command["id"])["status"], "completed")
        self.assertGreater(int(latest["version"]), maximum_before_restore)
        resumed_lineage = self.state.list_goal_specs(
            run_id=resumed["id"], limit=100
        )
        branch_revision = next(
            item
            for item in resumed_lineage
            if int(item["version"]) > maximum_before_restore
            and int(
                (item["spec"].get("supersedes") or {}).get("version") or 0
            )
            == 2
        )
        self.assertGreater(int(branch_revision["version"]), maximum_before_restore)
        self.assertEqual(
            int((latest["spec"].get("supersedes") or {}).get("version") or 0),
            int(branch_revision["version"]),
            "the confirmed revision must continue the newly allocated branch",
        )


class ArtifactCacheResumeAdversarialTests(IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.original_db_path = db.DB_PATH
        self.original_gateway_artifact_dir = mcp_module.ARTIFACT_DIR
        self.original_runtime_artifact_dir = runtime_module.ARTIFACT_DIR
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        db.DB_PATH = root / "artifact-resume-adversarial.db"
        mcp_module.ARTIFACT_DIR = root / "artifacts"
        runtime_module.ARTIFACT_DIR = root / "artifacts"
        db.init_db()
        seed_agents()
        registry = SkillRegistry()
        registry.load_builtin_skills()
        gateway = McpGateway()
        gateway.seed_builtin_servers()
        self.state = TaskStateService(db.get_conn)
        self.runtime = AgentRuntime(
            registry,
            gateway,
            ModelGateway(),
            task_state=self.state,
            policy_engine=PolicyEngine(),
        )

    async def asyncTearDown(self) -> None:
        runtime_module.ARTIFACT_DIR = self.original_runtime_artifact_dir
        mcp_module.ARTIFACT_DIR = self.original_gateway_artifact_dir
        db.DB_PATH = self.original_db_path
        self.temp_dir.cleanup()

    async def test_artifact_tool_cache_is_not_reused_with_old_run_ownership(self) -> None:
        task = create_task_record(
            "请将当前任务结果导出为 CSV 文件供我下载",
            "general-agent",
            model_id="deterministic",
            conversation_id="conv_artifact_resume_adversarial",
        )
        with patch.dict(
            os.environ, {"APP_DETERMINISTIC_STREAM_DELAY_MS": "0"}, clear=False
        ):
            await self.runtime.run_task(task["id"])
        first_run = self.state.list_runs(task_id=task["id"])[0]
        checkpoint = self.state.latest_checkpoint(first_run["id"], include_state=True)
        resumed = self.state.create_run(
            task["id"], resumed_from_checkpoint_id=checkpoint["id"]
        )

        with patch.dict(
            os.environ, {"APP_DETERMINISTIC_STREAM_DELAY_MS": "0"}, clear=False
        ):
            await self.runtime.run_task(task["id"], run_id=resumed["id"])

        resumed_row = self.state.get_run(resumed["id"])
        current_artifacts = db.query_all(
            "SELECT id, run_id, delivery_status FROM artifacts "
            "WHERE task_id = ? AND run_id = ?",
            (task["id"], resumed["id"]),
        )
        self.assertTrue(
            current_artifacts,
            "a resumed run must not reuse a cached artifact that is still owned "
            "by the earlier run; it needs a current-run artifact record",
        )
        self.assertEqual(
            resumed_row["status"],
            "completed",
            "restoring a checkpoint with an artifact cache must not fail ownership "
            f"verification; runtime error was {resumed_row.get('error')}",
        )
        self.assertTrue(
            all(item["delivery_status"] == "published" for item in current_artifacts)
        )


if __name__ == "__main__":
    import unittest

    unittest.main()
