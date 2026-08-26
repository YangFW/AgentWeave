from __future__ import annotations

import asyncio
import inspect
import tempfile
import unittest
from pathlib import Path
from typing import Any, Awaitable, Callable

from app import db
from app.services.agent_runtime import AgentRuntime, create_task_record
from app.services.mcp_gateway import McpGateway, ToolError
from app.services.policy_engine import PolicyEngine
from app.services.task_state import TaskStateService


class PermissionSkillRegistry:
    def __init__(self, server_id: str) -> None:
        self.server_id = server_id
        self.skill = {
            "id": f"permission_{server_id}",
            "name": "权限闭环测试 Skill",
            "description": "只用于运行时权限隔离测试。",
            "content": "仅调用当前任务要求且已授权的测试工具。",
            "enabled": True,
            "required_mcps": [server_id] if server_id else [],
        }

    def list_skills(self, enabled_only: bool = False) -> list[dict[str, Any]]:
        return [dict(self.skill)]

    def score_skills(
        self, message: str, allowed_ids: list[str] | None = None
    ) -> list[dict[str, Any]]:
        if allowed_ids is not None and self.skill["id"] not in allowed_ids:
            return []
        return [{"skill": dict(self.skill), "score": 10.0}]

    def get_skill(self, skill_id: str) -> dict[str, Any] | None:
        return dict(self.skill) if skill_id == self.skill["id"] else None

    def runtime_content(self, skill_id: str, max_chars: int = 16000) -> str:
        return self.skill["content"] if skill_id == self.skill["id"] else ""


class RecordingMcpGateway:
    def __init__(self, definitions: list[dict[str, Any]]) -> None:
        self.definitions = {
            (str(item["server_id"]), str(item["name"])): dict(item)
            for item in definitions
        }
        self.calls: list[dict[str, Any]] = []
        self.delays: dict[tuple[str, str], float] = {}
        self.failures: dict[tuple[str, str, str], str] = {}
        self.cancelled: list[tuple[str, str, str]] = []

    def list_tools(self) -> list[dict[str, Any]]:
        return [dict(item) for item in self.definitions.values()]

    def get_tool_definition(
        self, server_id: str, tool_name: str
    ) -> dict[str, Any] | None:
        value = self.definitions.get((server_id, tool_name))
        return dict(value) if value else None

    async def invoke_tool(
        self,
        server_id: str,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        task_id: str = "",
    ) -> dict[str, Any]:
        call = {
            "server_id": server_id,
            "tool_name": tool_name,
            "arguments": dict(arguments),
            "task_id": task_id,
        }
        self.calls.append(call)
        try:
            delay = self.delays.get((server_id, tool_name), 0.0)
            if delay:
                await asyncio.sleep(delay)
        except asyncio.CancelledError:
            self.cancelled.append((server_id, tool_name, task_id))
            raise
        key = str(arguments.get("key") or "")
        failure = self.failures.get((server_id, tool_name, key))
        if failure:
            raise ToolError(failure)
        if server_id == "weather" and tool_name == "forecast":
            return {
                "city": str(arguments.get("city") or "宁波"),
                "region": "中国 浙江 宁波",
                "date": "2026-08-12",
                "day": arguments.get("day", "tomorrow"),
                "condition": "晴",
                "temperature_max_c": 31,
                "temperature_min_c": 24,
                "precipitation_probability_max_percent": 10,
                "precipitation_sum_mm": 0,
                "wind_speed_max_kmh": 12,
                "wind_gusts_max_kmh": 18,
                "provider": "fixture",
                "source": "https://example.test/weather",
            }
        return {"value": f"{server_id}.{tool_name}:{key or 'ok'}"}


async def _delta(
    callback: Callable[[str], Awaitable[None] | None] | None, text: str
) -> None:
    if callback is None:
        return
    pending = callback(text)
    if inspect.isawaitable(pending):
        await pending


class ScriptModelGateway:
    def __init__(
        self,
        actions: list[tuple[str, dict[str, Any]]] | None = None,
        *,
        catch_tool_errors: bool = False,
        delay_before_actions: float = 0.0,
    ) -> None:
        self.actions = actions or []
        self.catch_tool_errors = catch_tool_errors
        self.delay_before_actions = max(0.0, float(delay_before_actions))
        self.results: list[dict[str, Any]] = []
        self.errors: list[str] = []
        self.offered_tools: list[list[str]] = []

    async def resolve_intent(
        self, message: str, history: list[dict[str, str]], model_config_id: str
    ) -> dict[str, Any]:
        weather = "天气" in message and "不要" not in message
        return {
            "standalone_request": message,
            "intent": "weather_query" if weather else "general",
            "parameters": {"city": "宁波", "day": "tomorrow"} if weather else {},
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
        self.offered_tools.append(
            [f"{item['server_id']}__{item['name']}" for item in tools]
        )
        if self.delay_before_actions:
            await asyncio.sleep(self.delay_before_actions)
        for qualified_name, arguments in self.actions:
            try:
                self.results.append(await invoke(qualified_name, dict(arguments)))
            except ToolError as exc:
                self.errors.append(str(exc))
                if not self.catch_tool_errors:
                    raise
        answer = "权限闭环测试已完成，并保留了可核验的工具结果。"
        await _delta(on_delta, answer)
        return answer


class ParallelMemberModelGateway(ScriptModelGateway):
    def __init__(self) -> None:
        super().__init__()
        self.waiting = 0
        self.barrier = asyncio.Event()

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
        action = "lab__read" if "MEMBER_READ" in prompt else "lab__write"
        self.offered_tools.append(
            [f"{item['server_id']}__{item['name']}" for item in tools]
        )
        self.waiting += 1
        if self.waiting == 2:
            self.barrier.set()
        await asyncio.wait_for(self.barrier.wait(), timeout=1)
        result = await invoke(action, {"key": action.rsplit("__", 1)[-1]})
        self.results.append(result)
        answer = f"独立专家结果：{result['value']}，权限没有与其他成员混用。"
        await _delta(on_delta, answer)
        return answer


class AgentPermissionRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.original_db_path = db.DB_PATH
        self.temp_dir = tempfile.TemporaryDirectory()
        db.DB_PATH = Path(self.temp_dir.name) / "permissions-runtime.db"
        db.init_db()
        self.state = TaskStateService(db.get_conn)

    async def asyncTearDown(self) -> None:
        db.DB_PATH = self.original_db_path
        self.temp_dir.cleanup()

    def _insert_agent(
        self,
        agent_id: str,
        permissions: dict[str, Any],
        *,
        servers: list[str],
    ) -> None:
        now = db.utc_now()
        db.execute(
            """INSERT INTO agents(
                   id, name, description, model, system_prompt, skills_json,
                   mcp_servers_json, permissions_json, created_at, updated_at
               ) VALUES (?, ?, '', 'deterministic', '', ?, ?, ?, ?, ?)""",
            (
                agent_id,
                agent_id,
                db.json_dumps([f"permission_{servers[0]}"] if servers else []),
                db.json_dumps(servers),
                db.json_dumps(permissions),
                now,
                now,
            ),
        )

    def _runtime(
        self,
        server_id: str,
        gateway: RecordingMcpGateway,
        model: Any,
        *,
        policy: PolicyEngine | None = None,
    ) -> AgentRuntime:
        return AgentRuntime(
            PermissionSkillRegistry(server_id),
            gateway,  # type: ignore[arg-type]
            model,
            task_state=self.state,
            policy_engine=policy or PolicyEngine(),
        )

    @staticmethod
    def _definition(
        server_id: str,
        name: str,
        *,
        kind: str = "builtin",
        effect: str = "read",
        annotations: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return {
            "server_id": server_id,
            "server_kind": kind,
            "name": name,
            "description": "权限测试工具",
            "input_schema": {"type": "object", "properties": {"key": {"type": "string"}}},
            "effect": effect,
            "annotations": annotations or {},
        }

    def _task(
        self,
        message: str,
        agent_id: str,
        *,
        executor_type: str = "agent",
        model_id: str | None = None,
    ) -> dict[str, Any]:
        return create_task_record(
            message,
            agent_id,
            conversation_id=f"conv_{agent_id}_{len(message)}",
            executor_type=executor_type,
            model_id=model_id,
        )

    def _latest_state(self, task_id: str) -> dict[str, Any]:
        run = self.state.list_runs(task_id=task_id)[-1]
        checkpoint = self.state.latest_checkpoint(run["id"], include_state=True)
        self.assertIsNotNone(checkpoint)
        return checkpoint["state"]

    async def test_weather_shortcut_and_model_tool_both_use_unified_guard(self) -> None:
        definitions = [
            self._definition("weather", "forecast"),
            self._definition("lab", "read"),
        ]
        gateway = RecordingMcpGateway(definitions)
        self._insert_agent(
            "weather-denied",
            {"allowed_tools": ["forecast"], "denied_tools": ["forecast"]},
            servers=["weather"],
        )
        weather_model = ScriptModelGateway()
        weather_task = self._task("请查询宁波明天天气", "weather-denied")

        await self._runtime("", gateway, weather_model).run_task(weather_task["id"])

        self.assertFalse(any(call["server_id"] == "weather" for call in gateway.calls))
        blocked = db.query_one(
            "SELECT data_json FROM task_events WHERE task_id = ? AND type = 'tool_blocked' ORDER BY id LIMIT 1",
            (weather_task["id"],),
        )
        self.assertEqual(db.json_loads(blocked["data_json"], {})["reason"], "tool_denied")

        self._insert_agent(
            "model-allowed", {"allowed_tools": ["read"]}, servers=["lab"]
        )
        model = ScriptModelGateway([("lab__read", {"key": "model"})])
        model_task = self._task("调用权限测试工具读取模型数据", "model-allowed")
        await self._runtime("lab", gateway, model).run_task(model_task["id"])

        self.assertEqual(
            [(call["server_id"], call["tool_name"]) for call in gateway.calls],
            [("lab", "read")],
        )
        self.assertEqual(
            db.query_one("SELECT status FROM tasks WHERE id = ?", (model_task["id"],))["status"],
            "completed",
        )

    async def test_configured_model_weather_uses_normal_tool_planning(self) -> None:
        gateway = RecordingMcpGateway([self._definition("weather", "forecast")])
        self._insert_agent(
            "weather-model",
            {"allowed_tools": ["forecast"]},
            servers=["weather"],
        )
        model = ScriptModelGateway(
            [("weather__forecast", {"city": "宁波", "day": "tomorrow"})]
        )
        task = self._task(
            "请查询宁波明天天气",
            "weather-model",
            model_id="configured-model",
        )

        await self._runtime("weather", gateway, model).run_task(task["id"])

        self.assertEqual(model.offered_tools, [["weather__forecast"]])
        self.assertEqual(
            [(call["server_id"], call["tool_name"]) for call in gateway.calls],
            [("weather", "forecast")],
        )
        self.assertTrue(
            db.query_one(
                "SELECT 1 FROM task_events WHERE task_id = ? AND type = 'model'",
                (task["id"],),
            )
        )

    async def test_document_request_with_weather_words_never_offers_weather_tool(self) -> None:
        gateway = RecordingMcpGateway(
            [
                self._definition("weather", "forecast"),
                self._definition("report", "generate_document", effect="write"),
            ]
        )
        self._insert_agent(
            "document-weather-guard",
            {"allowed_tools": ["forecast", "generate_document"]},
            servers=["weather", "report"],
        )
        model = ScriptModelGateway()
        task = self._task(
            "把行程整理成 Word 文档，保留天气不好时的室内备选",
            "document-weather-guard",
            model_id="configured-model",
        )

        await self._runtime("", gateway, model).run_task(task["id"])

        self.assertTrue(model.offered_tools)
        self.assertNotIn("weather__forecast", model.offered_tools[0])
        self.assertFalse(any(call["server_id"] == "weather" for call in gateway.calls))

    async def test_explicit_deny_wins_over_allow_and_policy_can_still_tighten(self) -> None:
        gateway = RecordingMcpGateway([self._definition("lab", "read")])
        self._insert_agent(
            "deny-wins",
            {"allowed_tools": ["read"], "denied_tools": ["lab.read"]},
            servers=["lab"],
        )
        model = ScriptModelGateway([("lab__read", {"key": "blocked"})])
        task = self._task("调用权限测试工具读取禁止项", "deny-wins")
        await self._runtime("lab", gateway, model).run_task(task["id"])

        self.assertEqual(gateway.calls, [])
        self.assertEqual(
            db.query_one("SELECT status FROM tasks WHERE id = ?", (task["id"],))["status"],
            "failed",
        )
        run = self.state.list_runs(task_id=task["id"])[0]
        self.assertEqual(run["metadata"]["effective_permissions"]["allowed_tools"], ["read"])
        self.assertEqual(run["metadata"]["effective_permissions"]["denied_tools"], ["lab.read"])

        self._insert_agent(
            "policy-tighten", {"allowed_tools": ["read"]}, servers=["lab"]
        )
        policy = PolicyEngine(
            [
                {
                    "id": "deny-lab-read",
                    "name": "策略继续收紧",
                    "event": "tool.before",
                    "scope": "organization",
                    "priority": 100,
                    "match": {"server": "lab", "tool": "read"},
                    "handler": {
                        "type": "builtin_rule",
                        "decision": "deny",
                        "reason": "组织策略禁止本次读取",
                    },
                }
            ]
        )
        policy_model = ScriptModelGateway([("lab__read", {"key": "policy"})])
        policy_task = self._task("调用权限测试工具读取策略项", "policy-tighten")
        await self._runtime("lab", gateway, policy_model, policy=policy).run_task(
            policy_task["id"]
        )
        self.assertEqual(gateway.calls, [])

    async def test_read_only_trusts_builtin_effect_and_remote_annotations_fail_closed(self) -> None:
        gateway = RecordingMcpGateway(
            [
                self._definition("builtin-lab", "read", effect="read"),
                self._definition("builtin-lab", "write", effect="write"),
                self._definition(
                    "remote-safe",
                    "fetch",
                    kind="mcp_http",
                    effect="read",
                    annotations={"readOnlyHint": True},
                ),
                self._definition(
                    "remote-unknown",
                    "fetch",
                    kind="mcp_http",
                    effect="read",
                    annotations={},
                ),
            ]
        )
        cases = [
            ("builtin-read", "builtin-lab", "read", True),
            ("builtin-write", "builtin-lab", "write", False),
            ("remote-read", "remote-safe", "fetch", True),
            ("remote-unknown", "remote-unknown", "fetch", False),
        ]
        for agent_id, server_id, tool_name, should_run in cases:
            with self.subTest(agent_id=agent_id):
                self._insert_agent(
                    agent_id,
                    {"allowed_tools": [tool_name], "read_only": True},
                    servers=[server_id],
                )
                model = ScriptModelGateway([(f"{server_id}__{tool_name}", {"key": agent_id})])
                task = self._task(f"调用权限测试工具 {agent_id}", agent_id)
                calls_before = len(gateway.calls)
                await self._runtime(server_id, gateway, model).run_task(task["id"])
                self.assertEqual(len(gateway.calls) - calls_before, 1 if should_run else 0)
                status = db.query_one("SELECT status FROM tasks WHERE id = ?", (task["id"],))["status"]
                self.assertEqual(status, "completed" if should_run else "failed")

    def _insert_member_permissions(
        self, task_id: str, member_id: str, permissions: dict[str, Any]
    ) -> None:
        now = db.utc_now()
        db.execute(
            """INSERT INTO team_member_runs(
                   id, team_run_id, member_id, child_task_id, attempt, status,
                   output_json, error_json, started_at, finished_at, created_at,
                   updated_at, conversation_id, permissions_json, input_json
               ) VALUES (?, 'team_parallel', ?, ?, 1, 'queued', '{}', '{}', '', '', ?, ?, ?, ?, '{}')""",
            (
                f"member_run_{member_id}",
                member_id,
                task_id,
                now,
                now,
                f"conv_{member_id}",
                db.json_dumps(permissions),
            ),
        )

    async def test_parallel_team_members_use_independent_permission_snapshots(self) -> None:
        gateway = RecordingMcpGateway(
            [
                self._definition("lab", "read", effect="read"),
                self._definition("lab", "write", effect="write"),
            ]
        )
        gateway.delays[("lab", "read")] = 0.03
        gateway.delays[("lab", "write")] = 0.03
        self._insert_agent("shared-expert", {}, servers=["lab"])
        read_task = self._task(
            "MEMBER_READ：独立读取证据", "shared-expert", executor_type="team_member"
        )
        write_task = self._task(
            "MEMBER_WRITE：独立写入测试结果", "shared-expert", executor_type="team_member"
        )
        self._insert_member_permissions(
            read_task["id"], "reader", {"allowed_tools": ["read"]}
        )
        self._insert_member_permissions(
            write_task["id"], "writer", {"allowed_tools": ["write"]}
        )
        model = ParallelMemberModelGateway()
        runtime = self._runtime("lab", gateway, model)

        await asyncio.gather(
            runtime.run_task(read_task["id"]), runtime.run_task(write_task["id"])
        )

        self.assertEqual(
            {(call["task_id"], call["tool_name"]) for call in gateway.calls},
            {(read_task["id"], "read"), (write_task["id"], "write")},
        )
        for task in (read_task, write_task):
            self.assertEqual(
                db.query_one("SELECT status FROM tasks WHERE id = ?", (task["id"],))["status"],
                "completed",
            )
        read_permissions = self.state.list_runs(task_id=read_task["id"])[0]["metadata"]["effective_permissions"]
        write_permissions = self.state.list_runs(task_id=write_task["id"])[0]["metadata"]["effective_permissions"]
        self.assertEqual(read_permissions["allowed_tools"], ["read"])
        self.assertEqual(write_permissions["allowed_tools"], ["write"])

    async def test_max_calls_cache_reuse_and_recovery_keep_durable_snapshot(self) -> None:
        gateway = RecordingMcpGateway([self._definition("lab", "read")])
        self._insert_agent(
            "recoverable",
            {"allowed_tools": ["read"], "max_tool_calls": 1},
            servers=["lab"],
        )
        model = ScriptModelGateway([("lab__read", {"key": "stable"})])
        runtime = self._runtime("lab", gateway, model)
        task = self._task("调用权限测试工具并保存可恢复结果", "recoverable")
        await runtime.run_task(task["id"])

        first_run = self.state.list_runs(task_id=task["id"])[0]
        checkpoint = self.state.latest_checkpoint(first_run["id"], include_state=True)
        self.assertEqual(checkpoint["state"]["tool_calls_used"], 1)
        self.assertEqual(len(gateway.calls), 1)

        # Configuration changes affect new runs, but a checkpoint-resume must
        # keep the already-audited snapshot and reuse the completed result.
        db.execute(
            "UPDATE agents SET permissions_json = ? WHERE id = 'recoverable'",
            (db.json_dumps({"allowed_tools": [], "denied_tools": ["read"], "max_tool_calls": 0}),),
        )
        second = self.state.create_run(
            task["id"], resumed_from_checkpoint_id=checkpoint["id"]
        )
        await runtime.run_task(task["id"], run_id=second["id"])

        self.assertEqual(len(gateway.calls), 1)
        restored = self.state.get_run(second["id"])
        self.assertEqual(restored["status"], "completed")
        self.assertEqual(restored["metadata"]["effective_permissions"]["allowed_tools"], ["read"])
        self.assertEqual(
            self.state.latest_checkpoint(second["id"], include_state=True)["state"]["tool_calls_used"],
            1,
        )
        self.assertTrue(
            db.query_one(
                "SELECT 1 FROM task_events WHERE task_id = ? AND type = 'tool_reused'",
                (task["id"],),
            )
        )

    async def test_real_tool_failure_stops_task_before_model_can_continue(self) -> None:
        gateway = RecordingMcpGateway([self._definition("lab", "read")])
        gateway.failures[("lab", "read", "first")] = "fixture real failure"
        self._insert_agent(
            "failure-budget",
            {"allowed_tools": ["read"], "max_tool_calls": 1},
            servers=["lab"],
        )
        model = ScriptModelGateway(
            [("lab__read", {"key": "first"}), ("lab__read", {"key": "second"})],
            catch_tool_errors=True,
        )
        task = self._task("连续调用权限测试工具验证失败计次", "failure-budget")
        await self._runtime("lab", gateway, model).run_task(task["id"])

        self.assertEqual(len(gateway.calls), 1)
        # A custom adapter may catch the exception internally, but the runtime
        # keeps the first failure fatal, blocks every later real invocation,
        # and refuses to publish the adapter's final answer.
        self.assertEqual(len(model.errors), 2)
        self.assertIn("fixture real failure", model.errors[0])
        self.assertIn("fixture real failure", model.errors[1])
        self.assertEqual(
            db.query_one("SELECT status FROM tasks WHERE id = ?", (task["id"],))["status"],
            "failed",
        )
        self.assertFalse(
            db.query_one(
                "SELECT 1 FROM task_events WHERE task_id = ? AND type = 'answer'",
                (task["id"],),
            )
        )
        state = self._latest_state(task["id"])
        self.assertEqual(state["tool_calls_used"], 1)
        self.assertEqual(state["last_failed_tool"]["name"], "read")

    async def test_timeout_applies_remaining_runtime_budget_and_cancels_tool(self) -> None:
        gateway = RecordingMcpGateway([self._definition("lab", "read")])
        gateway.delays[("lab", "read")] = 0.5
        self._insert_agent(
            "timeout-agent",
            {"allowed_tools": ["read"], "timeout_seconds": 0.15},
            servers=["lab"],
        )
        # Model/planning latency must not consume the tool execution budget.
        # This also keeps the assertion deterministic under asyncio debug mode.
        model = ScriptModelGateway(
            [("lab__read", {"key": "slow"})], delay_before_actions=0.2
        )
        task = self._task("调用权限测试工具验证剩余时限", "timeout-agent")
        await self._runtime("lab", gateway, model).run_task(task["id"])

        self.assertEqual(len(gateway.calls), 1)
        self.assertEqual(gateway.cancelled, [("lab", "read", task["id"])])
        self.assertEqual(
            db.query_one("SELECT status FROM tasks WHERE id = ?", (task["id"],))["status"],
            "failed",
        )
        state = self._latest_state(task["id"])
        self.assertEqual(state["tool_calls_used"], 1)
        self.assertGreaterEqual(state["permission_elapsed_seconds"], 0.14)

    async def test_timeout_budget_is_cumulative_across_real_tool_calls(self) -> None:
        gateway = RecordingMcpGateway([self._definition("lab", "read")])
        gateway.delays[("lab", "read")] = 0.08
        self._insert_agent(
            "cumulative-timeout-agent",
            {"allowed_tools": ["read"], "timeout_seconds": 0.15},
            servers=["lab"],
        )
        model = ScriptModelGateway(
            [
                ("lab__read", {"key": "first"}),
                ("lab__read", {"key": "second"}),
            ]
        )
        task = self._task(
            "连续调用权限测试工具验证累计执行时限", "cumulative-timeout-agent"
        )
        await self._runtime("lab", gateway, model).run_task(task["id"])

        self.assertEqual(len(gateway.calls), 2)
        self.assertEqual(gateway.cancelled, [("lab", "read", task["id"])])
        self.assertEqual(
            db.query_one("SELECT status FROM tasks WHERE id = ?", (task["id"],))["status"],
            "failed",
        )
        state = self._latest_state(task["id"])
        self.assertEqual(state["tool_calls_used"], 2)
        self.assertGreaterEqual(state["permission_elapsed_seconds"], 0.14)


class McpCapabilityMetadataTests(unittest.TestCase):
    def test_builtin_effects_and_remote_annotations_are_preserved(self) -> None:
        from app.services.mcp_gateway import BUILTIN_SERVERS

        definitions = {
            (server["id"], tool["name"]): tool
            for server in BUILTIN_SERVERS
            for tool in server["tools"]
        }
        self.assertEqual(definitions[("weather", "forecast")]["effect"], "read")
        self.assertEqual(definitions[("report", "generate_document")]["effect"], "write")

        class Annotation:
            def model_dump(self, **kwargs: Any) -> dict[str, Any]:
                return {"readOnlyHint": True, "destructiveHint": False}

        class Tool:
            name = "remote_read"
            description = "remote"
            inputSchema: dict[str, Any] = {}
            annotations = Annotation()

        class Result:
            tools = [Tool()]

        normalized = McpGateway()._normalize_mcp_tools(Result())
        self.assertEqual(
            normalized[0]["annotations"],
            {"readOnlyHint": True, "destructiveHint": False},
        )


if __name__ == "__main__":
    unittest.main()
