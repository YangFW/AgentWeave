from __future__ import annotations

import asyncio
import inspect
import tempfile
import unittest
from pathlib import Path
from typing import Any, Awaitable, Callable
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from app import db
from app.services.agent_runtime import AgentRuntime, create_task_record
from app.services.policy_engine import PolicyEngine
from app.services.task_state import TaskStateService
from app.services.verification_service import SemanticResult, VerificationService


async def _send_delta(
    callback: Callable[[str], Awaitable[None] | None] | None,
    text: str,
) -> None:
    if callback is None:
        return
    result = callback(text)
    if inspect.isawaitable(result):
        await result


class SteeringSkillRegistry:
    """Deterministic Skill registry used only by runtime-steering tests."""

    def __init__(self, *, required_server: str = "") -> None:
        self.skill = {
            "id": "steering_lookup" if required_server else "steering_general",
            "name": "Steering 回归 Skill",
            "description": "验证运行中追加指令会重新确认目标、能力和计划。",
            "version": "1.0.0",
            "content": "严格按照当前 GoalSpec 和当前计划完成任务。",
            "enabled": True,
            "required_mcps": [required_server] if required_server else [],
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


class CountingLookupGateway:
    def __init__(self, *, enabled: bool = False) -> None:
        self.enabled = enabled
        self.calls: list[dict[str, Any]] = []

    def list_tools(self) -> list[dict[str, Any]]:
        if not self.enabled:
            return []
        return [
            {
                "server_id": "lookup",
                "name": "read",
                "description": "读取同一个稳定键，验证缓存隔离。",
                "effect": "read",
                "input_schema": {
                    "type": "object",
                    "properties": {"key": {"type": "string"}},
                    "required": ["key"],
                    "additionalProperties": False,
                },
            }
        ]

    async def invoke_tool(
        self,
        server_id: str,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        task_id: str = "",
    ) -> dict[str, Any]:
        self.calls.append(
            {
                "server_id": server_id,
                "tool_name": tool_name,
                "arguments": dict(arguments),
                "task_id": task_id,
            }
        )
        return {"value": f"lookup-call-{len(self.calls)}"}


class RestartForSteeringModel:
    def __init__(self, revised_answer: str = "已补充风险与验收证据。") -> None:
        self.revised_answer = revised_answer
        self.prompts: list[str] = []
        self.first_started = asyncio.Event()
        self.first_cancelled = asyncio.Event()

    async def resolve_intent(
        self,
        message: str,
        history: list[dict[str, str]],
        model_config_id: str,
    ) -> dict[str, Any]:
        return {
            "standalone_request": message,
            "intent": "analysis",
            "parameters": {},
            "missing_information": [],
            "is_follow_up": bool(history),
            "source": "test",
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
        self.prompts.append(prompt)
        if len(self.prompts) == 1:
            self.first_started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.first_cancelled.set()
                raise
            raise AssertionError("unreachable")
        await _send_delta(on_delta, self.revised_answer)
        return self.revised_answer


class ToolThenSteerModel:
    def __init__(self) -> None:
        self.prompts: list[str] = []
        self.first_tool_completed = asyncio.Event()
        self.first_cancelled = asyncio.Event()

    async def resolve_intent(
        self,
        message: str,
        history: list[dict[str, str]],
        model_config_id: str,
    ) -> dict[str, Any]:
        return {
            "standalone_request": message,
            "intent": "lookup",
            "parameters": {"key": "stable-key"},
            "missing_information": [],
            "is_follow_up": bool(history),
            "source": "test",
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
        self.prompts.append(prompt)
        result = await invoke("lookup__read", {"key": "stable-key"})
        if len(self.prompts) == 1:
            self.first_tool_completed.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.first_cancelled.set()
                raise
            raise AssertionError("unreachable")
        answer = f"新版目标使用了独立工具证据：{result['value']}。"
        await _send_delta(on_delta, answer)
        return answer


class CandidateSequenceModel:
    OLD_CANDIDATE = "OLD_CANDIDATE：只满足追加指令之前的旧目标。"
    REVISED_CANDIDATE = "REVISED_CANDIDATE：已满足追加的风险复核要求。"

    def __init__(self) -> None:
        self.calls = 0

    async def resolve_intent(
        self,
        message: str,
        history: list[dict[str, str]],
        model_config_id: str,
    ) -> dict[str, Any]:
        return {
            "standalone_request": message,
            "intent": "analysis",
            "parameters": {},
            "missing_information": [],
            "is_follow_up": bool(history),
            "source": "test",
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
        self.calls += 1
        answer = self.OLD_CANDIDATE if self.calls == 1 else self.REVISED_CANDIDATE
        await _send_delta(on_delta, answer)
        return answer


class FirstVerificationBlocks:
    def __init__(self) -> None:
        self.calls = 0
        self.first_started = asyncio.Event()
        self.release_first = asyncio.Event()

    async def evaluate(self, **_: Any) -> SemanticResult:
        self.calls += 1
        if self.calls == 1:
            self.first_started.set()
            await self.release_first.wait()
        return SemanticResult(
            status="passed",
            public_reason="候选结果与本轮 GoalSpec 一致。",
            repair_instructions=[],
        )


class CompletionAuditingTaskState(TaskStateService):
    """Captures durable state at the exact message-command completion boundary."""

    def __init__(self) -> None:
        super().__init__(db.get_conn)
        self.expected_baselines: dict[str, int] = {}
        self.completion_audits: list[dict[str, Any]] = []

    def expect_revision(self, command_id: str, baseline_version: int) -> None:
        self.expected_baselines[command_id] = baseline_version

    def _audit_command_completion(self, command_id: str) -> None:
        command = self.get_command(command_id)
        baseline = self.expected_baselines.get(command_id)
        if command and command.get("type") == "message" and baseline is not None:
            run_id = str(command.get("run_id") or "")
            latest_goal = self.latest_goal_spec(run_id=run_id)
            checkpoint = self.latest_checkpoint(run_id, include_state=True)
            checkpoint_state = (checkpoint or {}).get("state") or {}
            plan = checkpoint_state.get("execution_plan") or {}
            goal_ref = plan.get("goal_spec_ref") or {}
            self.completion_audits.append(
                {
                    "command_id": command_id,
                    "baseline_version": baseline,
                    "latest_goal_version": int((latest_goal or {}).get("version") or 0),
                    "latest_goal_id": str((latest_goal or {}).get("id") or ""),
                    "plan_goal_spec_id": str(goal_ref.get("id") or ""),
                    "plan_goal_spec_version": int(goal_ref.get("version") or 0),
                    "plan_id": str(plan.get("plan_id") or ""),
                    "checkpoint_id": str((checkpoint or {}).get("id") or ""),
                }
            )
    def complete_command(
        self,
        command_id: str,
        *,
        result: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self._audit_command_completion(command_id)
        return super().complete_command(command_id, result=result)

    def complete_runtime_commands(
        self,
        run_id: str,
        completions: dict[str, dict[str, Any]],
    ) -> dict[str, Any]:
        for command_id in completions:
            self._audit_command_completion(command_id)
        return super().complete_runtime_commands(run_id, completions)


class RuntimeSteeringTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.original_db_path = db.DB_PATH
        self.temp_dir = tempfile.TemporaryDirectory()
        db.DB_PATH = Path(self.temp_dir.name) / "runtime-steering.db"
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
        mcp: CountingLookupGateway | None = None,
        required_server: str = "",
        verification_service: VerificationService | None = None,
    ) -> AgentRuntime:
        return AgentRuntime(
            SteeringSkillRegistry(required_server=required_server),
            mcp or CountingLookupGateway(),
            model,
            task_state=state or self.state,
            policy_engine=PolicyEngine(),
            verification_service=verification_service,
        )

    @staticmethod
    def _task(message: str) -> dict[str, Any]:
        return create_task_record(
            message,
            "general-agent",
            conversation_id="conv_runtime_steering",
        )

    @staticmethod
    def _plan_events(task_id: str) -> list[dict[str, Any]]:
        rows = db.query_all(
            "SELECT id, data_json FROM task_events "
            "WHERE task_id = ? AND type = 'plan' ORDER BY id",
            (task_id,),
        )
        return [db.json_loads(row["data_json"], {}).get("plan") or {} for row in rows]

    async def _run_with_one_steering_message(
        self,
        *,
        state: TaskStateService | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], RestartForSteeringModel]:
        state = state or self.state
        model = RestartForSteeringModel()
        runtime = self._runtime(model, state=state)
        task = self._task("请生成本次发布结论")
        worker = asyncio.create_task(runtime.run_task(task["id"]))
        try:
            await asyncio.wait_for(model.first_started.wait(), timeout=3)
            run = state.list_runs(task_id=task["id"])[0]
            baseline_goal = state.latest_goal_spec(run_id=run["id"])
            self.assertIsNotNone(baseline_goal)
            command = state.enqueue_command(
                task["id"],
                "message",
                run_id=run["id"],
                payload={"message": "请补充风险与验收证据，并据此重新规划"},
            )
            if isinstance(state, CompletionAuditingTaskState):
                state.expect_revision(command["id"], int(baseline_goal["version"]))
            await asyncio.wait_for(worker, timeout=5)
        finally:
            if not worker.done():
                worker.cancel()
                await asyncio.gather(worker, return_exceptions=True)
        return task, run, baseline_goal, model

    async def test_steering_creates_new_goal_spec_version_and_replans(self) -> None:
        task, run, baseline_goal, model = await self._run_with_one_steering_message()

        goals = self.state.list_goal_specs(run_id=run["id"], limit=100)
        latest_goal = goals[0]
        plans = self._plan_events(task["id"])

        self.assertTrue(model.first_cancelled.is_set())
        with self.subTest("GoalSpec revision"):
            self.assertGreater(
                int(latest_goal["version"]), int(baseline_goal["version"])
            )
            self.assertTrue(
                any(
                    int(item["version"]) > int(baseline_goal["version"])
                    for item in goals
                )
            )
        revised_contract = db.json_dumps(latest_goal["spec"])
        with self.subTest("steering is represented by the revised contract"):
            self.assertIn("风险", revised_contract)
            self.assertIn("验收证据", revised_contract)
        with self.subTest("a distinct plan is persisted for the revision"):
            self.assertGreaterEqual(len(plans), 2)
            self.assertNotEqual(plans[0].get("plan_id"), plans[-1].get("plan_id"))
        with self.subTest("latest plan is bound to the latest GoalSpec"):
            self.assertEqual(
                int((plans[-1].get("goal_spec_ref") or {}).get("version") or 0),
                int(latest_goal["version"]),
            )
            self.assertEqual(
                (plans[-1].get("goal_spec_ref") or {}).get("id"),
                latest_goal["id"],
            )
            self.assertIn("风险", db.json_dumps(plans[-1]))

    async def test_old_goal_tool_cache_is_not_reused_across_goal_versions(self) -> None:
        gateway = CountingLookupGateway(enabled=True)
        model = ToolThenSteerModel()
        runtime = self._runtime(
            model,
            mcp=gateway,
            required_server="lookup",
        )
        task = self._task("请读取 stable-key 并给出结论")
        worker = asyncio.create_task(runtime.run_task(task["id"]))
        try:
            await asyncio.wait_for(model.first_tool_completed.wait(), timeout=3)
            run = self.state.list_runs(task_id=task["id"])[0]
            baseline_goal = self.state.latest_goal_spec(run_id=run["id"])
            self.state.enqueue_command(
                task["id"],
                "message",
                run_id=run["id"],
                payload={"message": "保持同一查询参数，但将风险说明加入新目标"},
            )
            await asyncio.wait_for(worker, timeout=5)
        finally:
            if not worker.done():
                worker.cancel()
                await asyncio.gather(worker, return_exceptions=True)

        latest_goal = self.state.latest_goal_spec(run_id=run["id"])
        answer = db.query_one(
            "SELECT content FROM task_events "
            "WHERE task_id = ? AND type = 'answer' ORDER BY id DESC LIMIT 1",
            (task["id"],),
        )
        with self.subTest("same fingerprint is invoked once per GoalSpec version"):
            self.assertEqual(len(gateway.calls), 2, gateway.calls)
        with self.subTest("the second call belongs to a revised GoalSpec"):
            self.assertGreater(
                int(latest_goal["version"]), int(baseline_goal["version"])
            )
        self.assertTrue(model.first_cancelled.is_set())
        with self.subTest("published result uses evidence observed under the new goal"):
            self.assertIn("lookup-call-2", answer["content"])

    async def test_steering_during_final_verification_blocks_old_candidate_publication(self) -> None:
        judge = FirstVerificationBlocks()
        model = CandidateSequenceModel()
        runtime = self._runtime(
            model,
            verification_service=VerificationService(judge),
        )
        task = self._task("请生成初始发布结论")
        worker = asyncio.create_task(runtime.run_task(task["id"]))
        try:
            await asyncio.wait_for(judge.first_started.wait(), timeout=3)
            run = self.state.list_runs(task_id=task["id"])[0]
            self.state.enqueue_command(
                task["id"],
                "message",
                run_id=run["id"],
                payload={"message": "发布前新增要求：必须复核风险"},
            )
            judge.release_first.set()
            await asyncio.wait_for(worker, timeout=5)
        finally:
            judge.release_first.set()
            if not worker.done():
                worker.cancel()
                await asyncio.gather(worker, return_exceptions=True)

        answers = db.query_all(
            "SELECT content FROM task_events "
            "WHERE task_id = ? AND type = 'answer' ORDER BY id",
            (task["id"],),
        )
        published_text = "\n".join(item["content"] for item in answers)
        self.assertNotIn(CandidateSequenceModel.OLD_CANDIDATE, published_text)
        self.assertIn(CandidateSequenceModel.REVISED_CANDIDATE, published_text)
        self.assertGreaterEqual(model.calls, 2)
        commands = self.state.list_commands(task_id=task["id"], command_types=["message"])
        self.assertEqual(commands[0]["status"], "completed")

    async def test_message_command_completes_only_after_revised_plan_is_durable(self) -> None:
        auditing_state = CompletionAuditingTaskState()
        self.state = auditing_state
        task, run, baseline_goal, _ = await self._run_with_one_steering_message(
            state=auditing_state
        )

        self.assertEqual(len(auditing_state.completion_audits), 1)
        audit = auditing_state.completion_audits[0]
        with self.subTest("revision exists before command completion"):
            self.assertGreater(
                audit["latest_goal_version"], int(baseline_goal["version"])
            )
        with self.subTest("revised plan checkpoint exists before command completion"):
            self.assertTrue(audit["checkpoint_id"])
            self.assertEqual(audit["plan_goal_spec_id"], audit["latest_goal_id"])
            self.assertEqual(
                audit["plan_goal_spec_version"], audit["latest_goal_version"]
            )
            self.assertEqual(
                audit["plan_id"],
                f"plan_{self.state.latest_goal_spec(run_id=run['id'])['spec']['goal_id']}"
                f"_v{audit['latest_goal_version']}",
            )
        command = self.state.list_commands(
            task_id=task["id"], command_types=["message"]
        )[0]
        self.assertEqual(command["status"], "completed")

    async def test_revised_plan_nodes_are_namespaced_and_api_remains_two_level(self) -> None:
        task, run, baseline_goal, _ = await self._run_with_one_steering_message()
        latest_goal = self.state.latest_goal_spec(run_id=run["id"])
        plans = self._plan_events(task["id"])
        latest_plan = plans[-1]
        plan_id = str(latest_plan.get("plan_id") or "")
        with self.subTest("latest plan belongs to revised GoalSpec"):
            self.assertGreater(
                int(latest_goal["version"]), int(baseline_goal["version"])
            )
            self.assertEqual(
                plan_id,
                f"plan_{latest_goal['spec']['goal_id']}_v{latest_goal['version']}",
            )

        nodes = self.state.list_nodes(run["id"])
        prefix = f"{plan_id}:"
        namespaced_nodes = [
            node for node in nodes if str(node.get("node_key") or "").startswith(prefix)
        ]
        expected_logical_ids = {
            str(node["id"])
            for node in latest_plan.get("nodes") or []
        } | {
            str(child["id"])
            for node in latest_plan.get("nodes") or []
            for child in node.get("children") or []
        }
        persisted_logical_ids = {
            str(node["node_key"])[len(prefix):] for node in namespaced_nodes
        }
        with self.subTest("physical node keys are isolated by plan version"):
            self.assertTrue(expected_logical_ids)
            self.assertTrue(namespaced_nodes)
            self.assertTrue(expected_logical_ids.issubset(persisted_logical_ids))

        from app import main as main_module

        with (
            patch.object(main_module.loop_scheduler, "start", return_value=None),
            patch.object(main_module.loop_scheduler, "stop", new_callable=AsyncMock),
            TestClient(main_module.app) as client,
        ):
            response = client.get(f"/api/tasks/{task['id']}/runtime")

        self.assertEqual(response.status_code, 200, response.text)
        node_tree = response.json()["node_tree"]
        self.assertTrue(node_tree)
        self.assertTrue(all("children" in root for root in node_tree))
        self.assertTrue(
            all(
                child.get("children") == []
                for root in node_tree
                for child in root.get("children") or []
            )
        )
        projected_keys = {
            str(root.get("node_key") or "")
            for root in node_tree
        } | {
            str(child.get("node_key") or "")
            for root in node_tree
            for child in root.get("children") or []
        }
        self.assertTrue(
            {node["node_key"] for node in namespaced_nodes}.issubset(projected_keys)
        )


if __name__ == "__main__":
    unittest.main()
