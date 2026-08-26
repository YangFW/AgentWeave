from __future__ import annotations

import asyncio
import inspect
import sqlite3
import tempfile
import unittest
from pathlib import Path
from typing import Any, Awaitable, Callable

from app import db
from app.services.agent_runtime import AgentRuntime, create_task_record
from app.services.policy_engine import PolicyEngine
from app.services.task_state import PublicationConflict, TaskStateService
from app.services.verification_service import SemanticResult, VerificationService


class StubSkillRegistry:
    """Small deterministic registry that keeps marketplace routing out of tests."""

    def __init__(self, *, required_server: str = "") -> None:
        self.skill = {
            "id": "reliability_tool" if required_server else "general_task",
            "name": "可靠性测试 Skill" if required_server else "通用任务 Skill",
            "description": "仅用于隔离运行时可靠性测试。",
            "content": "按当前任务目标生成简洁、可验收的结果。",
            "enabled": True,
            "required_mcps": [required_server] if required_server else [],
        }

    def list_skills(self, enabled_only: bool = False) -> list[dict[str, Any]]:
        return [dict(self.skill)]

    def score_skills(
        self, message: str, allowed_ids: list[str] | None = None
    ) -> list[dict[str, Any]]:
        if allowed_ids and self.skill["id"] not in allowed_ids:
            return []
        return [{"skill": dict(self.skill), "score": 10.0}]

    def get_skill(self, skill_id: str) -> dict[str, Any] | None:
        return dict(self.skill) if skill_id == self.skill["id"] else None

    def runtime_content(self, skill_id: str, max_chars: int = 16000) -> str:
        return str(self.skill["content"])[:max_chars] if skill_id == self.skill["id"] else ""


class ApprovalSteeringSkillRegistry:
    """Route the old goal to a tool and its replacement to a safe direct answer."""

    lookup = {
        "id": "lookup_task",
        "name": "受控查询 Skill",
        "description": "旧目标需要调用受控查询工具。",
        "content": "仅按当前 GoalSpec 调用查询工具。",
        "enabled": True,
        "required_mcps": ["lookup"],
    }
    general = {
        "id": "general_task",
        "name": "通用任务 Skill",
        "description": "新的只读目标无需任何外部工具。",
        "content": "直接回答当前目标，不调用工具。",
        "enabled": True,
        "required_mcps": [],
    }

    def list_skills(self, enabled_only: bool = False) -> list[dict[str, Any]]:
        return [dict(self.lookup), dict(self.general)]

    def score_skills(
        self, message: str, allowed_ids: list[str] | None = None
    ) -> list[dict[str, Any]]:
        skill = self.lookup if "OLD_APPROVED_TOOL" in message else self.general
        if allowed_ids and skill["id"] not in allowed_ids:
            return []
        return [{"skill": dict(skill), "score": 10.0}]

    def get_skill(self, skill_id: str) -> dict[str, Any] | None:
        for skill in (self.lookup, self.general):
            if skill["id"] == skill_id:
                return dict(skill)
        return None

    def runtime_content(self, skill_id: str, max_chars: int = 16000) -> str:
        skill = self.get_skill(skill_id)
        return str((skill or {}).get("content") or "")[:max_chars]


class CountingMcpGateway:
    def __init__(
        self,
        *,
        server_id: str = "",
        tool_name: str = "",
        result: dict[str, Any] | None = None,
    ) -> None:
        self.server_id = server_id
        self.tool_name = tool_name
        self.result = result or {"value": "stub-result"}
        self.calls: list[dict[str, Any]] = []

    def list_tools(self) -> list[dict[str, Any]]:
        if not self.server_id or not self.tool_name:
            return []
        input_schema = (
            {
                "type": "object",
                "properties": {
                    "city": {"type": "string"},
                    "day": {
                        "type": "string",
                        "enum": ["today", "tomorrow", "day_after_tomorrow"],
                    },
                },
                "required": ["city", "day"],
                "additionalProperties": False,
            }
            if self.server_id == "weather" and self.tool_name == "forecast"
            else {
                "type": "object",
                "properties": {"key": {"type": "string"}},
                "required": ["key"],
            }
        )
        return [
            {
                "server_id": self.server_id,
                "name": self.tool_name,
                "description": "可计数的隔离测试工具",
                "input_schema": input_schema,
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
        return dict(self.result)


class BlockingResultMcpGateway(CountingMcpGateway):
    def __init__(self, *, server_id: str, tool_name: str, result: dict[str, Any]) -> None:
        super().__init__(server_id=server_id, tool_name=tool_name, result=result)
        self.returning = asyncio.Event()

    async def invoke_tool(
        self,
        server_id: str,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        task_id: str = "",
    ) -> dict[str, Any]:
        result = await super().invoke_tool(
            server_id, tool_name, arguments, task_id=task_id
        )
        self.returning.set()
        return result


class InterruptAfterPolicyDecisionState(TaskStateService):
    """Simulate process loss immediately after the atomic decision commit."""

    def __init__(self) -> None:
        super().__init__(db.get_conn)
        self.interrupt_once = True

    def commit_policy_approval_decision(self, **kwargs: Any) -> dict[str, Any] | None:
        result = super().commit_policy_approval_decision(**kwargs)
        if result and not result.get("idempotent") and self.interrupt_once:
            self.interrupt_once = False
            raise asyncio.CancelledError
        return result


class EnqueueMessageAtPolicyDecisionState(TaskStateService):
    """Place a replacement immediately before the approval decision CAS."""

    def __init__(self) -> None:
        super().__init__(db.get_conn)
        self.injected_command: dict[str, Any] | None = None

    def commit_policy_approval_decision(self, **kwargs: Any) -> dict[str, Any] | None:
        approval_ready = self.list_commands(
            task_id=str(kwargs["task_id"]),
            run_id=str(kwargs["run_id"]),
            status="queued",
            command_types=["approval"],
        )
        if self.injected_command is None and approval_ready:
            competing_client = TaskStateService(db.get_conn)
            self.injected_command = competing_client.enqueue_command(
                str(kwargs["task_id"]),
                "message",
                run_id=str(kwargs["run_id"]),
                payload={
                    "message": (
                        "取消之前任务，新的目标：只输出安全只读结论，"
                        "不要调用任何外部工具"
                    )
                },
            )
        return super().commit_policy_approval_decision(**kwargs)


async def _send_delta(
    callback: Callable[[str], Awaitable[None] | None] | None, text: str
) -> None:
    if callback is None:
        return
    pending = callback(text)
    if inspect.isawaitable(pending):
        await pending


class ImmediateModelGateway:
    def __init__(self, answer: str = "可靠性任务已完成并生成可验收结果。") -> None:
        self.answer = answer
        self.prompts: list[str] = []

    async def resolve_intent(
        self, message: str, history: list[dict[str, str]], model_config_id: str
    ) -> dict[str, Any]:
        return {
            "standalone_request": message,
            "intent": "general",
            "parameters": {},
            "missing_information": [],
            "is_follow_up": False,
            "source": "direct",
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
        await _send_delta(on_delta, self.answer)
        return self.answer


class BlockingModelGateway(ImmediateModelGateway):
    def __init__(self) -> None:
        super().__init__()
        self.started = asyncio.Event()
        self.cancelled = asyncio.Event()

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
        self.started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled.set()
            raise
        raise AssertionError("unreachable")


class RestartOnSteeringModelGateway(ImmediateModelGateway):
    def __init__(self, final_answer: str) -> None:
        super().__init__(final_answer)
        self.first_started = asyncio.Event()
        self.first_cancelled = asyncio.Event()

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
        await _send_delta(on_delta, self.answer)
        return self.answer


class ToolCallingModelGateway(ImmediateModelGateway):
    def __init__(self, qualified_tool_name: str, arguments: dict[str, Any]) -> None:
        super().__init__()
        self.qualified_tool_name = qualified_tool_name
        self.arguments = dict(arguments)
        self.tool_results: list[dict[str, Any]] = []

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
        result = await invoke(self.qualified_tool_name, dict(self.arguments))
        self.tool_results.append(result)
        answer = f"工具结果已纳入最终回答：{result['value']}。"
        await _send_delta(on_delta, answer)
        return answer


class ApprovalThenReplacementModel(ImmediateModelGateway):
    """The old solve requests a tool; the replacement solve answers directly."""

    def __init__(self) -> None:
        super().__init__("已按新的安全只读目标给出结论。")
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
        self.prompts.append(prompt)
        if self.solve_calls == 1:
            await invoke("lookup__read", {"key": "old-approved-operation"})
            raise AssertionError("the superseded tool result must not return")
        await _send_delta(on_delta, self.answer)
        return self.answer


class FixedSemanticJudge:
    def __init__(self, *, passed: bool) -> None:
        self.passed = passed

    async def evaluate(self, **_: Any) -> SemanticResult:
        return SemanticResult(
            status="passed" if self.passed else "failed",
            public_reason=(
                "候选结果与当前目标一致。"
                if self.passed
                else "候选结果没有回答当前目标。"
            ),
            repair_instructions=(
                [] if self.passed else ["重新围绕当前目标生成结果。"]
            ),
        )


class RuntimeReliabilityTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.original_db_path = db.DB_PATH
        self.temp_dir = tempfile.TemporaryDirectory()
        db.DB_PATH = Path(self.temp_dir.name) / "runtime-reliability.db"
        db.init_db()
        self.state = TaskStateService(db.get_conn)

    async def asyncTearDown(self) -> None:
        db.DB_PATH = self.original_db_path
        self.temp_dir.cleanup()

    def _runtime(
        self,
        model: Any,
        *,
        mcp: CountingMcpGateway | None = None,
        required_server: str = "",
        policy: PolicyEngine | None = None,
        verification_service: VerificationService | None = None,
    ) -> AgentRuntime:
        return AgentRuntime(
            StubSkillRegistry(required_server=required_server),
            mcp or CountingMcpGateway(),
            model,
            task_state=self.state,
            policy_engine=policy or PolicyEngine(),
            verification_service=verification_service,
        )

    @staticmethod
    def _task(message: str) -> dict[str, Any]:
        return create_task_record(message, "general-agent", conversation_id="conv_reliability")

    async def test_completed_task_persists_completed_run_nodes_and_checkpoints(self) -> None:
        model = ImmediateModelGateway()
        runtime = self._runtime(model)
        task = self._task("请给出本次可靠性验证的简短结论")

        await runtime.run_task(task["id"])

        stored_task = db.query_one("SELECT * FROM tasks WHERE id = ?", (task["id"],))
        runs = self.state.list_runs(task_id=task["id"])
        self.assertEqual(stored_task["status"], "completed")
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0]["status"], "completed")
        self.assertTrue(runs[0]["finished_at"])

        nodes = self.state.list_nodes(runs[0]["id"])
        plan_id = str(runs[0]["metadata"].get("plan_id") or "")
        self.assertTrue(plan_id.startswith("plan_goal_"))
        by_logical_key = {
            str(node.get("metadata", {}).get("logical_id") or ""): node
            for node in nodes
            if node.get("metadata", {}).get("plan_id") == plan_id
        }
        self.assertTrue(
            {"understand", "prepare", "execute", "validate"}.issubset(
                by_logical_key
            )
        )
        self.assertTrue(
            all(
                by_logical_key[key]["status"] == "completed"
                for key in ("understand", "prepare", "execute", "validate")
            )
        )
        self.assertTrue(
            all(
                node["node_key"].startswith(f"{plan_id}:")
                for node in by_logical_key.values()
            )
        )
        checkpoints = self.state.list_checkpoints(
            run_id=runs[0]["id"], include_state=True
        )
        self.assertGreaterEqual(len(checkpoints), 4)
        self.assertTrue(any(item["state"].get("phase") == "validate" for item in checkpoints))
        verifications = self.state.list_verifications(
            task_id=task["id"], run_id=runs[0]["id"]
        )
        self.assertEqual(len(verifications), 1)
        self.assertEqual(verifications[0]["status"], "rules_passed")
        answer_event = db.query_one(
            "SELECT id, data_json FROM task_events WHERE task_id = ? AND type = 'answer' ORDER BY id DESC LIMIT 1",
            (task["id"],),
        )
        verification_event = db.query_one(
            "SELECT id, data_json FROM task_events WHERE task_id = ? AND type = 'verification_result' ORDER BY id DESC LIMIT 1",
            (task["id"],),
        )
        self.assertLess(verification_event["id"], answer_event["id"])
        self.assertEqual(
            db.json_loads(answer_event["data_json"], {})["verification_id"],
            verifications[0]["id"],
        )
        deltas = db.query_all(
            "SELECT data_json FROM task_events WHERE task_id = ? AND type = 'answer_delta' ORDER BY id",
            (task["id"],),
        )
        self.assertTrue(deltas)
        self.assertTrue(
            all(db.json_loads(item["data_json"], {}).get("draft") is True for item in deltas)
        )

    async def test_completed_runtime_task_compacts_conversation_after_publication(self) -> None:
        conversation_id = "conv_runtime_auto_summary"
        for index in range(9):
            prior = create_task_record(
                f"历史轮次 {index + 1}",
                "general-agent",
                conversation_id=conversation_id,
            )
            created_at = f"2026-08-24T00:00:{index:02d}+00:00"
            db.execute(
                "UPDATE tasks SET status = 'completed', created_at = ?, result_json = ? WHERE id = ?",
                (created_at, db.json_dumps({"summary": f"历史回答 {index + 1}"}), prior["id"]),
            )
            db.execute(
                "INSERT INTO task_events(task_id, type, title, content, data_json, ts) "
                "VALUES (?, 'answer', '回答', ?, '{}', ?)",
                (prior["id"], f"历史回答 {index + 1}", created_at),
            )

        runtime = self._runtime(ImmediateModelGateway())
        task = create_task_record(
            "现在切换到新的目标，只输出当前目标结论",
            "general-agent",
            conversation_id=conversation_id,
        )
        await runtime.run_task(task["id"])

        self.assertEqual(
            db.query_one("SELECT status FROM tasks WHERE id = ?", (task["id"],))["status"],
            "completed",
        )
        summary = db.query_one(
            "SELECT conversation_id, through_task_id, version FROM conversation_summaries "
            "WHERE conversation_id = ?",
            (conversation_id,),
        )
        self.assertIsNotNone(summary)
        self.assertEqual(summary["conversation_id"], conversation_id)
        self.assertEqual(summary["version"], 1)
        self.assertTrue(
            db.query_one(
                "SELECT 1 FROM task_events WHERE task_id = ? AND type = 'conversation_summary'",
                (task["id"],),
            )
        )

    async def test_semantic_failure_persists_report_and_never_publishes_answer(self) -> None:
        runtime = self._runtime(
            ImmediateModelGateway("这是一段非空但与可靠性目标无关的天气闲聊。"),
            verification_service=VerificationService(
                FixedSemanticJudge(passed=False)
            ),
        )
        task = self._task("请给出本次可靠性验证结论")

        await runtime.run_task(task["id"])

        stored = db.query_one("SELECT status FROM tasks WHERE id = ?", (task["id"],))
        run = self.state.list_runs(task_id=task["id"])[0]
        reports = self.state.list_verifications(run_id=run["id"])
        self.assertEqual(stored["status"], "failed")
        self.assertEqual(len(reports), 1)
        self.assertEqual(reports[0]["status"], "failed")
        self.assertFalse(
            db.query_one(
                "SELECT 1 FROM task_events WHERE task_id = ? AND type = 'answer'",
                (task["id"],),
            )
        )
        self.assertTrue(
            db.query_one(
                "SELECT 1 FROM task_events WHERE task_id = ? AND type = 'answer_reset'",
                (task["id"],),
            )
        )

    async def test_output_policy_empty_rewrite_is_reverified_and_blocked(self) -> None:
        policy = PolicyEngine(
            [
                {
                    "id": "empty-after-generation",
                    "event": "output.before",
                    "scope": "organization",
                    "handler": {
                        "type": "builtin_rule",
                        "decision": "modify",
                        "modifications": {"answer": ""},
                    },
                }
            ]
        )
        runtime = self._runtime(
            ImmediateModelGateway("原始答案本来有效。"), policy=policy
        )
        task = self._task("请给出本次可靠性验证结论")

        await runtime.run_task(task["id"])

        run = self.state.list_runs(task_id=task["id"])[0]
        reports = self.state.list_verifications(run_id=run["id"])
        self.assertEqual(
            db.query_one("SELECT status FROM tasks WHERE id = ?", (task["id"],))["status"],
            "failed",
        )
        self.assertEqual(len(reports), 1)
        self.assertEqual(reports[0]["status"], "failed")
        self.assertFalse(
            db.query_one(
                "SELECT 1 FROM task_events WHERE task_id = ? AND type = 'answer'",
                (task["id"],),
            )
        )

    async def test_deterministic_weather_uses_persisted_final_verification(self) -> None:
        mcp = CountingMcpGateway(
            server_id="weather",
            tool_name="forecast",
            result={
                "city": "宁波",
                "region": "中国 浙江 宁波",
                "date": "2026-08-15",
                "day": "tomorrow",
                "condition": "晴",
                "temperature_max_c": 31,
                "temperature_min_c": 24,
                "provider": "fixture",
                "source": "https://example.test/weather",
            },
        )
        runtime = self._runtime(
            ImmediateModelGateway(), mcp=mcp, required_server="weather"
        )
        task = self._task("请查询宁波明天天气")

        await runtime.run_task(task["id"])

        run = self.state.list_runs(task_id=task["id"])[0]
        reports = self.state.list_verifications(run_id=run["id"])
        self.assertEqual(len(mcp.calls), 1)
        self.assertEqual(len(reports), 1)
        self.assertTrue(reports[0]["public_report"]["passed"])
        verification_event = db.query_one(
            "SELECT id FROM task_events WHERE task_id = ? AND type = 'verification_result' ORDER BY id DESC LIMIT 1",
            (task["id"],),
        )
        answer_event = db.query_one(
            "SELECT id FROM task_events WHERE task_id = ? AND type = 'answer' ORDER BY id DESC LIMIT 1",
            (task["id"],),
        )
        self.assertLess(verification_event["id"], answer_event["id"])

    async def test_weather_without_city_clarifies_without_unverified_answer(self) -> None:
        mcp = CountingMcpGateway(
            server_id="weather",
            tool_name="forecast",
            result={"unexpected": True},
        )
        runtime = self._runtime(
            ImmediateModelGateway(), mcp=mcp, required_server="weather"
        )
        task = self._task("今天天气怎么样")

        await runtime.run_task(task["id"])

        stored = db.query_one("SELECT * FROM tasks WHERE id = ?", (task["id"],))
        run = self.state.list_runs(task_id=task["id"])[0]
        goal = self.state.latest_goal_spec(run_id=run["id"])
        event_types = [
            row["type"]
            for row in db.query_all(
                "SELECT type FROM task_events WHERE task_id = ? ORDER BY id",
                (task["id"],),
            )
        ]

        self.assertFalse(mcp.calls)
        self.assertEqual(stored["status"], "completed")
        self.assertTrue(db.json_loads(stored["result_json"], {})["needs_clarification"])
        self.assertEqual(run["status"], "completed")
        self.assertEqual(run["intake_state"], "closed")
        self.assertEqual(run["published_verification_id"], "")
        self.assertEqual(goal["status"], "needs_input")
        self.assertFalse(self.state.list_verifications(task_id=task["id"]))
        self.assertEqual(event_types.count("clarification"), 1)
        self.assertEqual(event_types.count("done"), 1)
        self.assertNotIn("answer", event_types)

    async def test_cancel_during_slow_model_finishes_task_run_and_command_as_cancelled(self) -> None:
        model = BlockingModelGateway()
        runtime = self._runtime(model)
        task = self._task("请生成一个需要较长时间的可靠性分析")
        worker = asyncio.create_task(runtime.run_task(task["id"]))

        try:
            await asyncio.wait_for(model.started.wait(), timeout=2)
            run = self.state.list_runs(task_id=task["id"])[0]
            cancel = self.state.request_cancel(
                task["id"], run_id=run["id"], reason="测试用户主动取消"
            )
            await asyncio.wait_for(worker, timeout=2)
        finally:
            if not worker.done():
                worker.cancel()
                await asyncio.gather(worker, return_exceptions=True)

        stored_task = db.query_one("SELECT * FROM tasks WHERE id = ?", (task["id"],))
        stored_run = self.state.get_run(run["id"])
        stored_command = self.state.get_command(cancel["id"])
        self.assertEqual(stored_task["status"], "cancelled")
        self.assertEqual(stored_run["status"], "cancelled")
        self.assertNotEqual(stored_run["status"], "failed")
        self.assertEqual(stored_command["status"], "completed")
        self.assertTrue(stored_command["claimed_at"])
        self.assertTrue(stored_command["completed_at"])
        self.assertEqual(stored_command["result"], {"cancelled": True})
        self.assertTrue(model.cancelled.is_set())

    async def test_runtime_message_is_claimed_completed_and_restarts_model_with_context(self) -> None:
        steering_message = "请在最终结论中补充风险和验收证据"
        final_answer = "已补充风险、验收证据和最终结论。"
        model = RestartOnSteeringModelGateway(final_answer)
        runtime = self._runtime(model)
        task = self._task("请生成可靠性结论")
        worker = asyncio.create_task(runtime.run_task(task["id"]))

        try:
            await asyncio.wait_for(model.first_started.wait(), timeout=2)
            run = self.state.list_runs(task_id=task["id"])[0]
            command = self.state.enqueue_command(
                task["id"],
                "message",
                run_id=run["id"],
                payload={"message": steering_message},
            )
            await asyncio.wait_for(worker, timeout=2)
        finally:
            if not worker.done():
                worker.cancel()
                await asyncio.gather(worker, return_exceptions=True)

        stored_command = self.state.get_command(command["id"])
        stored_task = db.query_one("SELECT * FROM tasks WHERE id = ?", (task["id"],))
        self.assertEqual(stored_task["status"], "completed")
        self.assertTrue(model.first_cancelled.is_set())
        self.assertEqual(len(model.prompts), 2)
        self.assertIn(steering_message, model.prompts[1])
        self.assertEqual(stored_command["status"], "completed")
        self.assertTrue(stored_command["claimed_at"])
        self.assertTrue(stored_command["completed_at"])
        self.assertTrue(stored_command["result"]["applied"])
        self.assertGreater(stored_command["result"]["goal_spec_version"], 2)
        self.assertTrue(stored_command["result"]["goal_spec_id"])
        self.assertTrue(stored_command["result"]["checkpoint_id"])
        self.assertEqual(
            stored_command["result"]["plan_id"],
            self.state.get_run(run["id"])["metadata"]["plan_id"],
        )
        checkpoints = self.state.list_checkpoints(
            run_id=run["id"], include_state=True
        )
        self.assertTrue(
            any(
                steering_message in item["state"].get("steering_messages", [])
                for item in checkpoints
            )
        )

    async def test_organization_tool_deny_blocks_mcp_and_emits_policy_audit(self) -> None:
        mcp = CountingMcpGateway(
            server_id="danger", tool_name="write", result={"value": "should-not-run"}
        )
        model = ToolCallingModelGateway("danger__write", {"key": "official-record"})
        policy = PolicyEngine(
            [
                {
                    "id": "org-deny-danger-write",
                    "name": "组织级禁止危险写入",
                    "event": "tool.before",
                    "scope": "organization",
                    "priority": 100,
                    "match": {"server": "danger", "tool": "write"},
                    "handler": {
                        "type": "builtin_rule",
                        "decision": "deny",
                        "reason": "组织策略禁止写入正式记录",
                    },
                }
            ]
        )
        runtime = self._runtime(
            model, mcp=mcp, required_server="danger", policy=policy
        )
        task = self._task("调用已授权工具读取可靠性测试值")

        await runtime.run_task(task["id"])

        stored_task = db.query_one("SELECT * FROM tasks WHERE id = ?", (task["id"],))
        run = self.state.list_runs(task_id=task["id"])[0]
        self.assertEqual(stored_task["status"], "failed")
        self.assertEqual(run["status"], "failed")
        self.assertEqual(mcp.calls, [])
        audit_events = db.query_all(
            "SELECT type, content, data_json FROM task_events WHERE task_id = ? AND type = 'policy_decision' ORDER BY id",
            (task["id"],),
        )
        deny_audits = [
            db.json_loads(item["data_json"], {})
            for item in audit_events
            if db.json_loads(item["data_json"], {}).get("event") == "tool.before"
            and db.json_loads(item["data_json"], {}).get("outcome") == "deny"
        ]
        self.assertEqual(len(deny_audits), 1)
        self.assertEqual(deny_audits[0]["decisions"][0]["scope"], "organization")
        self.assertTrue(
            db.query_one(
                "SELECT 1 FROM task_events WHERE task_id = ? AND type = 'tool_blocked'",
                (task["id"],),
            )
        )

    async def test_goal_plan_and_output_policy_modifications_reach_runtime(self) -> None:
        model = ImmediateModelGateway("模型原始答案")
        policy = PolicyEngine(
            [
                {
                    "id": "rewrite-goal",
                    "event": "goal.resolved",
                    "scope": "organization",
                    "handler": {
                        "type": "builtin_rule",
                        "decision": "modify",
                        "modifications": {
                            "goal": {"standalone_request": "策略改写后的可靠性目标"}
                        },
                        "added_context": {
                            "policy_context": {"classification": "internal"}
                        },
                    },
                },
                {
                    "id": "constrain-plan",
                    "event": "plan.created",
                    "scope": "organization",
                    "handler": {
                        "type": "builtin_rule",
                        "decision": "modify",
                        "modifications": {
                            "plan": {
                                "goal_confirmation": {
                                    "message": "计划已由组织策略确认"
                                }
                            }
                        },
                    },
                },
                {
                    "id": "rewrite-output",
                    "event": "output.before",
                    "scope": "organization",
                    "handler": {
                        "type": "builtin_rule",
                        "decision": "modify",
                        "modifications": {"answer": "策略审核后的最终答案"},
                    },
                },
            ]
        )
        runtime = self._runtime(model, policy=policy)
        task = self._task("请给出可靠性结论")

        await runtime.run_task(task["id"])

        stored_task = db.query_one(
            "SELECT * FROM tasks WHERE id = ?", (task["id"],)
        )
        self.assertEqual(
            stored_task["status"],
            "completed",
            db.json_loads(stored_task["result_json"], {}),
        )
        self.assertIn("策略改写后的可靠性目标", model.prompts[0])
        self.assertIn('"classification":"internal"', model.prompts[0])
        plan_event = db.query_one(
            "SELECT data_json FROM task_events WHERE task_id = ? AND type = 'plan' ORDER BY id DESC LIMIT 1",
            (task["id"],),
        )
        plan = db.json_loads(plan_event["data_json"], {})["plan"]
        self.assertEqual(
            plan["goal_confirmation"]["message"], "计划已由组织策略确认"
        )
        answer = db.query_one(
            "SELECT content FROM task_events WHERE task_id = ? AND type = 'answer' ORDER BY id DESC LIMIT 1",
            (task["id"],),
        )
        self.assertEqual(answer["content"], "策略审核后的最终答案")
        self.assertEqual(
            db.json_loads(stored_task["result_json"], {})["summary"],
            "策略审核后的最终答案",
        )
        deltas = db.query_all(
            "SELECT content FROM task_events WHERE task_id = ? AND type = 'answer_delta' ORDER BY id",
            (task["id"],),
        )
        self.assertEqual(
            [item["content"] for item in deltas],
            ["策略审核后的最终答案"],
        )
        self.assertTrue(all(model.answer not in item["content"] for item in deltas))

    async def test_tool_after_policy_modifies_result_before_model_and_cache(self) -> None:
        mcp = CountingMcpGateway(
            server_id="lookup", tool_name="read", result={"value": "raw-value"}
        )
        model = ToolCallingModelGateway("lookup__read", {"key": "stable-key"})
        policy = PolicyEngine(
            [
                {
                    "id": "redact-result",
                    "event": "tool.after",
                    "scope": "organization",
                    "match": {"server": "lookup", "tool": "read"},
                    "handler": {
                        "type": "builtin_rule",
                        "decision": "modify",
                        "modifications": {"result": {"value": "policy-safe-value"}},
                    },
                }
            ]
        )
        runtime = self._runtime(
            model, mcp=mcp, required_server="lookup", policy=policy
        )
        task = self._task("使用可靠性工具读取一个稳定值")

        await runtime.run_task(task["id"])

        self.assertEqual(model.tool_results, [{"value": "policy-safe-value"}])
        checkpoint = self.state.latest_checkpoint(
            self.state.list_runs(task_id=task["id"])[0]["id"],
            include_state=True,
        )
        cached = list(checkpoint["state"]["completed_tools"].values())
        self.assertEqual(cached, [{"value": "policy-safe-value"}])

    async def test_artifact_created_deny_blocks_result_from_being_published(self) -> None:
        artifact = {
            "id": "art_policy_denied",
            "name": "unsafe.md",
            "kind": "markdown",
            "download_url": "/api/artifacts/art_policy_denied/download",
        }
        mcp = CountingMcpGateway(
            server_id="lookup",
            tool_name="read",
            result={"value": "raw-value", "artifact": artifact},
        )
        model = ToolCallingModelGateway("lookup__read", {"key": "stable-key"})
        policy = PolicyEngine(
            [
                {
                    "id": "block-artifact",
                    "event": "artifact.created",
                    "scope": "organization",
                    "handler": {
                        "type": "builtin_rule",
                        "decision": "deny",
                        "reason": "组织策略拒绝发布该产物",
                    },
                }
            ]
        )
        runtime = self._runtime(
            model, mcp=mcp, required_server="lookup", policy=policy
        )
        task = self._task("使用可靠性工具读取一个稳定值")

        await runtime.run_task(task["id"])

        stored_task = db.query_one("SELECT * FROM tasks WHERE id = ?", (task["id"],))
        self.assertEqual(stored_task["status"], "failed")
        self.assertEqual(db.json_loads(stored_task["artifacts_json"], []), [])
        self.assertFalse(
            db.query_one(
                "SELECT 1 FROM task_events WHERE task_id = ? AND type = 'tool_result'",
                (task["id"],),
            )
        )
        self.assertTrue(
            db.query_one(
                "SELECT 1 FROM task_events WHERE task_id = ? AND type = 'tool_blocked'",
                (task["id"],),
            )
        )

    async def test_tool_after_approval_runs_approval_requested_hook_and_resumes(self) -> None:
        mcp = BlockingResultMcpGateway(
            server_id="lookup", tool_name="read", result={"value": "approved-value"}
        )
        model = ToolCallingModelGateway("lookup__read", {"key": "stable-key"})
        policy = PolicyEngine(
            [
                {
                    "id": "review-result",
                    "event": "tool.after",
                    "scope": "organization",
                    "match": {"server": "lookup", "tool": "read"},
                    "handler": {
                        "type": "builtin_rule",
                        "decision": "require_approval",
                        "reason": "工具结果需要人工复核",
                    },
                },
                {
                    "id": "explain-approval",
                    "event": "approval.requested",
                    "scope": "organization",
                    "match": {
                        "conditions": [
                            {
                                "path": "approval.event",
                                "op": "eq",
                                "value": "tool.after",
                            }
                        ]
                    },
                    "handler": {
                        "type": "builtin_rule",
                        "decision": "modify",
                        "modifications": {
                            "approval": {
                                "title": "复核工具返回值",
                                "message": "请确认该工具返回值可以交给模型。",
                            }
                        },
                    },
                },
            ]
        )
        runtime = self._runtime(
            model, mcp=mcp, required_server="lookup", policy=policy
        )
        task = self._task("使用可靠性工具读取一个稳定值")
        worker = asyncio.create_task(runtime.run_task(task["id"]))
        run: dict[str, Any] | None = None

        try:
            await asyncio.wait_for(mcp.returning.wait(), timeout=2)
            for _ in range(200):
                stored_task = db.query_one(
                    "SELECT * FROM tasks WHERE id = ?", (task["id"],)
                )
                if stored_task["status"] == "waiting_approval":
                    break
                await asyncio.sleep(0.01)
            self.assertEqual(
                stored_task["status"],
                "waiting_approval",
                db.json_loads(stored_task["result_json"], {}),
            )
            result = db.json_loads(stored_task["result_json"], {})
            self.assertEqual(result["policy_event"], "tool.after")
            self.assertEqual(
                result["approval_request"]["message"],
                "请确认该工具返回值可以交给模型。",
            )
            self.assertEqual(
                result["approval_request"]["tool"],
                {"server": "lookup", "name": "read"},
            )
            self.assertEqual(
                result["approval_request"]["policy"]["event"], "tool.after"
            )
            approval_events = db.query_all(
                "SELECT type, title, content FROM task_events WHERE task_id = ? AND type = 'approval_required'",
                (task["id"],),
            )
            self.assertEqual(approval_events[-1]["title"], "复核工具返回值")
            run = self.state.list_runs(task_id=task["id"])[0]
            command = self.state.enqueue_command(
                task["id"],
                "approval",
                run_id=run["id"],
                payload={"approved": True},
            )
            await asyncio.wait_for(worker, timeout=3)
        finally:
            if not worker.done():
                worker.cancel()
                await asyncio.gather(worker, return_exceptions=True)

        self.assertEqual(
            db.query_one("SELECT status FROM tasks WHERE id = ?", (task["id"],))[
                "status"
            ],
            "completed",
        )
        self.assertEqual(self.state.get_command(command["id"])["status"], "completed")
        audit_events = [
            db.json_loads(item["data_json"], {})
            for item in db.query_all(
                "SELECT data_json FROM task_events WHERE task_id = ? AND type = 'policy_decision' ORDER BY id",
                (task["id"],),
            )
        ]
        self.assertTrue(
            any(item.get("event") == "approval.requested" for item in audit_events)
        )

    async def test_policy_approval_request_and_decision_roll_back_as_whole(self) -> None:
        task = self._task("验证策略审批原子状态")
        run = self.state.begin_run(task["id"], activate_task_projection=True)
        node = self.state.create_node(run["id"], "execute", "执行受控操作")
        self.state.start_node(node["id"])
        approval_id = "policy_approval_atomic_rollback"
        request = {
            "pending_action": "policy_approval",
            "policy_event": "tool.before",
            "summary": "请确认受控操作。",
        }
        event_data = {"action": "policy_approval", "event": "tool.before"}
        db.execute(
            """
            CREATE TRIGGER fail_policy_request_event
            BEFORE INSERT ON task_events
            WHEN NEW.type = 'approval_required'
            BEGIN
              SELECT RAISE(ABORT, 'request event failure');
            END
            """
        )

        with self.assertRaises(sqlite3.IntegrityError):
            self.state.commit_policy_approval_request(
                task_id=task["id"],
                run_id=run["id"],
                approval_id=approval_id,
                result=request,
                title="等待审批",
                content="请确认受控操作。",
                data=event_data,
            )

        self.assertEqual(
            (db.query_one("SELECT status FROM tasks WHERE id = ?", (task["id"],)) or {})[
                "status"
            ],
            "running",
        )
        self.assertEqual((self.state.get_run(run["id"]) or {})["status"], "running")
        self.assertEqual((self.state.get_node(node["id"]) or {})["output"], {})
        db.execute("DROP TRIGGER fail_policy_request_event")

        waiting = self.state.commit_policy_approval_request(
            task_id=task["id"],
            run_id=run["id"],
            approval_id=approval_id,
            result=request,
            title="等待审批",
            content="请确认受控操作。",
            data=event_data,
        )
        repeated = self.state.commit_policy_approval_request(
            task_id=task["id"],
            run_id=run["id"],
            approval_id=approval_id,
            result=request,
            title="等待审批",
            content="请确认受控操作。",
            data=event_data,
        )
        self.assertFalse(waiting["idempotent"])
        self.assertTrue(repeated["idempotent"])
        self.assertEqual(
            db.query_one(
                "SELECT COUNT(*) AS total FROM task_events "
                "WHERE task_id = ? AND type = 'approval_required'",
                (task["id"],),
            )["total"],
            1,
        )
        waiting_node = self.state.get_node(node["id"]) or {}
        self.assertEqual(waiting_node.get("output", {}).get("approval_status"), "waiting")

        command = self.state.enqueue_command(
            task["id"],
            "approval",
            run_id=run["id"],
            payload={"approval_id": approval_id, "approved": True},
        )
        db.execute(
            """
            CREATE TRIGGER fail_policy_decision_event
            BEFORE INSERT ON task_events
            WHEN NEW.type = 'approval'
            BEGIN
              SELECT RAISE(ABORT, 'decision event failure');
            END
            """
        )
        with self.assertRaises(sqlite3.IntegrityError):
            self.state.commit_policy_approval_decision(
                task_id=task["id"],
                run_id=run["id"],
                approval_id=approval_id,
                worker_id="worker-atomic",
            )
        self.assertEqual((self.state.get_command(command["id"]) or {})["status"], "queued")
        self.assertEqual(
            (db.query_one("SELECT status FROM tasks WHERE id = ?", (task["id"],)) or {})[
                "status"
            ],
            "waiting_approval",
        )
        self.assertEqual(
            (self.state.get_run(run["id"]) or {})["status"], "waiting_approval"
        )
        self.assertIsNone(
            self.state.get_policy_approval_decision(task["id"], approval_id)
        )
        db.execute("DROP TRIGGER fail_policy_decision_event")

        decision = self.state.commit_policy_approval_decision(
            task_id=task["id"],
            run_id=run["id"],
            approval_id=approval_id,
            worker_id="worker-atomic",
        )
        self.assertTrue(decision["approved"])
        self.assertEqual((self.state.get_command(command["id"]) or {})["status"], "completed")
        self.assertEqual(
            (db.query_one("SELECT status FROM tasks WHERE id = ?", (task["id"],)) or {})[
                "status"
            ],
            "running",
        )
        self.assertEqual((self.state.get_run(run["id"]) or {})["status"], "running")

    async def test_policy_decision_preserves_runtime_input_and_first_decision_wins(self) -> None:
        for approved in (True, False):
            with self.subTest(approved=approved):
                task = self._task(f"审批决定与运行输入竞争 approved={approved}")
                run = self.state.begin_run(
                    task["id"], activate_task_projection=True
                )
                approval_id = f"policy_approval_decision_{approved}"
                self.state.commit_policy_approval_request(
                    task_id=task["id"],
                    run_id=run["id"],
                    approval_id=approval_id,
                    result={
                        "pending_action": "policy_approval",
                        "policy_event": "tool.before",
                    },
                    title="等待审批",
                    content="请确认。",
                    data={"event": "tool.before"},
                )
                approval = self.state.enqueue_command(
                    task["id"],
                    "approval",
                    run_id=run["id"],
                    payload={
                        "approval_id": approval_id,
                        "approved": approved,
                    },
                    deduplicate=True,
                )
                with self.assertRaises(PublicationConflict):
                    self.state.enqueue_command(
                        task["id"],
                        "approval",
                        run_id=run["id"],
                        payload={
                            "approval_id": approval_id,
                            "approved": not approved,
                        },
                        deduplicate=True,
                    )
                message = self.state.enqueue_command(
                    task["id"],
                    "message",
                    run_id=run["id"],
                    payload={"message": "审批期间补充的新要求"},
                )
                cancel = self.state.enqueue_command(
                    task["id"],
                    "cancel",
                    run_id=run["id"],
                    payload={"reason": "审批期间请求取消"},
                )

                decision = self.state.commit_policy_approval_decision(
                    task_id=task["id"],
                    run_id=run["id"],
                    approval_id=approval_id,
                    worker_id="worker-decision",
                )

                self.assertEqual(decision["approved"], approved)
                self.assertEqual((self.state.get_command(approval["id"]) or {})["status"], "completed")
                self.assertEqual((self.state.get_command(message["id"]) or {})["status"], "queued")
                self.assertEqual((self.state.get_command(cancel["id"]) or {})["status"], "queued")
                stored_run = self.state.get_run(run["id"]) or {}
                self.assertEqual(stored_run.get("status"), "running")
                self.assertEqual(stored_run.get("accepted_generation"), 2)
                self.assertEqual(stored_run.get("applied_generation"), 0)
                proof = self.state.get_policy_approval_decision(
                    task["id"], approval_id
                )
                self.assertEqual((proof or {}).get("approved"), approved)
                repeated = self.state.commit_policy_approval_decision(
                    task_id=task["id"],
                    run_id=run["id"],
                    approval_id=approval_id,
                    worker_id="worker-retry",
                )
                self.assertTrue(repeated["idempotent"])
                self.assertEqual(repeated["approved"], approved)
                with self.assertRaises(PublicationConflict):
                    self.state.enqueue_command(
                        task["id"],
                        "approval",
                        run_id=run["id"],
                        payload={
                            "approval_id": approval_id,
                            "approved": not approved,
                        },
                    )

    async def test_message_at_policy_decision_prevents_old_tool_dispatch(self) -> None:
        state = EnqueueMessageAtPolicyDecisionState()
        self.state = state
        mcp = CountingMcpGateway(
            server_id="lookup",
            tool_name="read",
            result={"value": "must-not-be-produced"},
        )
        model = ApprovalThenReplacementModel()
        policy = PolicyEngine(
            [
                {
                    "id": "approve-old-tool-before-dispatch",
                    "event": "tool.before",
                    "scope": "organization",
                    "match": {"server": "lookup", "tool": "read"},
                    "handler": {
                        "type": "builtin_rule",
                        "decision": "require_approval",
                        "reason": "旧工具操作需要用户确认",
                    },
                }
            ]
        )
        runtime = AgentRuntime(
            ApprovalSteeringSkillRegistry(),
            mcp,
            model,
            task_state=state,
            policy_engine=policy,
        )
        task = self._task("OLD_APPROVED_TOOL：执行一次需要审批的查询")
        worker = asyncio.create_task(runtime.run_task(task["id"]))
        stored: dict[str, Any] = {}
        for _ in range(300):
            stored = db.query_one(
                "SELECT status, result_json FROM tasks WHERE id = ?",
                (task["id"],),
            ) or {}
            if stored.get("status") == "waiting_approval":
                break
            await asyncio.sleep(0.01)
        self.assertEqual(stored.get("status"), "waiting_approval")
        run = state.list_runs(task_id=task["id"])[0]
        approval_id = db.json_loads(
            stored.get("result_json"), {}
        )["policy_approval_id"]
        approval = state.enqueue_command(
            task["id"],
            "approval",
            run_id=run["id"],
            payload={"approval_id": approval_id, "approved": True},
        )

        try:
            await asyncio.wait_for(worker, timeout=5)
        finally:
            if not worker.done():
                worker.cancel()
                await asyncio.gather(worker, return_exceptions=True)

        injected = state.injected_command
        self.assertIsNotNone(injected)
        self.assertEqual(mcp.calls, [])
        self.assertEqual(model.solve_calls, 2)
        self.assertEqual(state.get_command(approval["id"])["status"], "completed")
        self.assertEqual(state.get_command(injected["id"])["status"], "completed")
        stored_task = db.query_one(
            "SELECT * FROM tasks WHERE id = ?", (task["id"],)
        )
        stored_run = state.get_run(run["id"])
        self.assertEqual(stored_task["status"], "completed")
        self.assertEqual(stored_run["status"], "completed")
        self.assertEqual(
            stored_run["accepted_generation"], stored_run["applied_generation"]
        )
        answers = db.query_all(
            "SELECT content FROM task_events WHERE task_id = ? "
            "AND type = 'answer' ORDER BY id",
            (task["id"],),
        )
        self.assertEqual(
            [item["content"] for item in answers],
            ["已按新的安全只读目标给出结论。"],
        )
        state.assert_terminal_clean(task_id=task["id"], run_id=run["id"])

    async def test_message_alone_supersedes_old_policy_wait_before_replanning(
        self,
    ) -> None:
        mcp = CountingMcpGateway(
            server_id="lookup",
            tool_name="read",
            result={"value": "must-not-be-produced"},
        )
        model = ApprovalThenReplacementModel()
        policy = PolicyEngine(
            [
                {
                    "id": "old-tool-waits-for-approval",
                    "event": "tool.before",
                    "scope": "organization",
                    "match": {"server": "lookup", "tool": "read"},
                    "handler": {
                        "type": "builtin_rule",
                        "decision": "require_approval",
                        "reason": "旧工具操作需要用户确认",
                    },
                }
            ]
        )
        runtime = AgentRuntime(
            ApprovalSteeringSkillRegistry(),
            mcp,
            model,
            task_state=self.state,
            policy_engine=policy,
        )
        task = self._task("OLD_APPROVED_TOOL：等待审批后执行查询")
        worker = asyncio.create_task(runtime.run_task(task["id"]))
        stored: dict[str, Any] = {}
        for _ in range(300):
            stored = db.query_one(
                "SELECT status FROM tasks WHERE id = ?", (task["id"],)
            ) or {}
            if stored.get("status") == "waiting_approval":
                break
            await asyncio.sleep(0.01)
        self.assertEqual(stored.get("status"), "waiting_approval")
        run = self.state.list_runs(task_id=task["id"])[0]
        message = self.state.enqueue_command(
            task["id"],
            "message",
            run_id=run["id"],
            payload={
                "message": (
                    "取消之前任务，新的目标：只输出安全只读结论，"
                    "不要调用任何外部工具"
                )
            },
        )

        try:
            await asyncio.wait_for(worker, timeout=5)
        finally:
            if not worker.done():
                worker.cancel()
                await asyncio.gather(worker, return_exceptions=True)

        self.assertEqual(mcp.calls, [])
        self.assertEqual(model.solve_calls, 2)
        self.assertEqual(self.state.get_command(message["id"])["status"], "completed")
        stored_task = db.query_one(
            "SELECT * FROM tasks WHERE id = ?", (task["id"],)
        )
        stored_run = self.state.get_run(run["id"])
        self.assertEqual(stored_task["status"], "completed")
        self.assertEqual(stored_run["status"], "completed")
        self.assertNotIn("pending_policy_approval", stored_run["metadata"])
        supersessions = stored_run["metadata"]["policy_approval_supersessions"]
        self.assertEqual(len(supersessions), 1)
        proof = next(iter(supersessions.values()))
        self.assertTrue(proof["superseded"])
        self.assertEqual(proof["command_ids"], [message["id"]])
        self.assertEqual(
            stored_run["accepted_generation"], stored_run["applied_generation"]
        )
        self.assertEqual(
            db.query_one(
                "SELECT COUNT(*) AS total FROM task_events "
                "WHERE task_id = ? AND type = 'approval' "
                "AND title = '旧审批请求已失效'",
                (task["id"],),
            )["total"],
            1,
        )
        self.state.assert_terminal_clean(task_id=task["id"], run_id=run["id"])

    async def test_committed_policy_decision_survives_immediate_worker_restart(self) -> None:
        state = InterruptAfterPolicyDecisionState()
        mcp = CountingMcpGateway(
            server_id="lookup",
            tool_name="read",
            result={"value": "restart-safe"},
        )
        model = ToolCallingModelGateway("lookup__read", {"key": "stable-key"})
        policy = PolicyEngine(
            [
                {
                    "id": "approve-before-read",
                    "event": "tool.before",
                    "scope": "organization",
                    "match": {"server": "lookup", "tool": "read"},
                    "handler": {
                        "type": "builtin_rule",
                        "decision": "require_approval",
                        "reason": "读取前需要确认",
                    },
                }
            ]
        )
        runtime = AgentRuntime(
            StubSkillRegistry(required_server="lookup"),
            mcp,
            model,
            task_state=state,
            policy_engine=policy,
        )
        task = self._task("读取重启边界值")
        worker = asyncio.create_task(runtime.run_task(task["id"]))
        stored: dict[str, Any] = {}
        for _ in range(300):
            stored = db.query_one(
                "SELECT status FROM tasks WHERE id = ?", (task["id"],)
            ) or {}
            if stored.get("status") == "waiting_approval":
                break
            await asyncio.sleep(0.01)
        self.assertEqual(stored.get("status"), "waiting_approval")
        old_run = state.list_runs(task_id=task["id"])[0]
        waiting_row = db.query_one(
            "SELECT result_json FROM tasks WHERE id = ?", (task["id"],)
        ) or {}
        approval_id = db.json_loads(
            waiting_row.get("result_json"), {}
        )["policy_approval_id"]
        command = state.enqueue_command(
            task["id"],
            "approval",
            run_id=old_run["id"],
            payload={"approval_id": approval_id, "approved": True},
        )

        with self.assertRaises(asyncio.CancelledError):
            await worker

        self.assertEqual(
            (db.query_one("SELECT status FROM tasks WHERE id = ?", (task["id"],)) or {})[
                "status"
            ],
            "running",
        )
        self.assertEqual((state.get_run(old_run["id"]) or {})["status"], "running")
        self.assertEqual((state.get_command(command["id"]) or {})["status"], "completed")
        self.assertTrue(
            (state.get_policy_approval_decision(task["id"], approval_id) or {}).get(
                "approved"
            )
        )
        self.assertEqual(mcp.calls, [])

        checkpoint = state.latest_checkpoint(old_run["id"], include_state=False)
        state.finish_run(
            old_run["id"],
            status="failed",
            error={"message": "模拟服务重启", "error_type": "ServiceRestart"},
        )
        recovered = state.create_run(
            task["id"],
            resumed_from_checkpoint_id=(checkpoint or {}).get("id"),
            metadata={"recovered_after_restart": True},
        )
        db.update_task_status(task["id"], "queued")
        recovered_runtime = AgentRuntime(
            StubSkillRegistry(required_server="lookup"),
            mcp,
            model,
            task_state=TaskStateService(db.get_conn),
            policy_engine=policy,
        )

        await recovered_runtime.run_task(task["id"], run_id=recovered["id"])

        self.assertEqual(len(mcp.calls), 1)
        self.assertEqual(
            db.query_one(
                "SELECT COUNT(*) AS total FROM task_events "
                "WHERE task_id = ? AND type = 'approval_required'",
                (task["id"],),
            )["total"],
            1,
        )

    async def test_resumed_run_reuses_checkpointed_tool_result_without_second_mcp_call(self) -> None:
        mcp = CountingMcpGateway(
            server_id="lookup", tool_name="read", result={"value": "checkpoint-cache"}
        )
        model = ToolCallingModelGateway("lookup__read", {"key": "stable-key"})
        runtime = self._runtime(model, mcp=mcp, required_server="lookup")
        task = self._task("使用可靠性工具读取一个稳定值")

        await runtime.run_task(task["id"])

        first_run = self.state.list_runs(task_id=task["id"])[0]
        checkpoint = self.state.latest_checkpoint(first_run["id"], include_state=True)
        self.assertEqual(first_run["status"], "completed")
        self.assertIsNotNone(checkpoint)
        self.assertTrue(checkpoint["state"].get("completed_tools"))
        self.assertEqual(len(mcp.calls), 1)

        queued_second = self.state.create_run(
            task["id"], resumed_from_checkpoint_id=checkpoint["id"]
        )
        await runtime.run_task(task["id"], run_id=queued_second["id"])

        second_run = self.state.get_run(queued_second["id"])
        self.assertEqual(second_run["status"], "completed")
        self.assertEqual(
            second_run["resumed_from_checkpoint_id"], checkpoint["id"]
        )
        self.assertEqual(len(model.tool_results), 2)
        self.assertEqual(model.tool_results[0], model.tool_results[1])
        self.assertEqual(len(mcp.calls), 1, "恢复执行不应重复调用具有相同指纹的 MCP")
        self.assertTrue(
            db.query_one(
                "SELECT 1 FROM task_events WHERE task_id = ? AND type = 'tool_reused'",
                (task["id"],),
            )
        )


if __name__ == "__main__":
    unittest.main()
