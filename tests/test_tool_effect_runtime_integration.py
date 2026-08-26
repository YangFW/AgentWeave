from __future__ import annotations

import asyncio
import hashlib
import inspect
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Awaitable, Callable
from unittest.mock import patch

from app import db
from app.services import agent_runtime as runtime_module
from app.services import mcp_gateway as mcp_module
from app.services.agent_runtime import AgentRuntime, create_task_record
from app.services.mcp_gateway import McpGateway
from app.services.policy_engine import PolicyEngine
from app.services.task_state import TaskStateService


async def _send_delta(
    callback: Callable[[str], Awaitable[None] | None] | None,
    value: str,
) -> None:
    if callback is None:
        return
    pending = callback(value)
    if inspect.isawaitable(pending):
        await pending


class RuntimeSkillRegistry:
    """Deterministic registry used to expose one exact test capability."""

    def __init__(self, *, skill_id: str, required_server: str = "") -> None:
        self.skill = {
            "id": skill_id,
            "name": f"{skill_id} Skill",
            "description": "用于持久副作用重启集成测试。",
            "content": "严格执行当前目标；只调用已确认的工具。",
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

    def runtime_content(self, skill_id: str, max_chars: int = 16000) -> str:
        if skill_id != self.skill["id"]:
            return ""
        return str(self.skill["content"])[:max_chars]


class StaticModelGateway:
    def __init__(self, answer: str = "已生成并校验用户要求的文件。") -> None:
        self.answer = answer
        self.solve_calls = 0

    async def resolve_intent(
        self,
        message: str,
        history: list[dict[str, str]],
        model_config_id: str,
    ) -> dict[str, Any]:
        return {
            "standalone_request": message,
            "intent": "general",
            "parameters": {},
            "missing_information": [],
            "is_follow_up": bool(history),
            "source": "runtime-effect-integration",
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
        await _send_delta(on_delta, self.answer)
        return self.answer


class ToolCallingModelGateway(StaticModelGateway):
    def __init__(
        self,
        qualified_tool_name: str,
        arguments: dict[str, Any],
        *,
        cancel_after_first_result: bool = False,
    ) -> None:
        super().__init__("工具写入已完成并通过校验。")
        self.qualified_tool_name = qualified_tool_name
        self.arguments = dict(arguments)
        self.cancel_after_first_result = cancel_after_first_result
        self.results: list[dict[str, Any]] = []

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
        result = await invoke(self.qualified_tool_name, dict(self.arguments))
        self.results.append(result)
        if self.cancel_after_first_result and self.solve_calls == 1:
            # The tool result and its tool_completed checkpoint are durable,
            # but the candidate has not reached final publication.
            raise asyncio.CancelledError
        await _send_delta(on_delta, self.answer)
        return self.answer


class CrashAfterExternalWriteGateway:
    """A non-idempotent remote sink that crashes after committing once."""

    def __init__(self) -> None:
        self.external_effect_count = 0
        self.calls: list[dict[str, Any]] = []
        self.crash_once = True

    def list_tools(self) -> list[dict[str, Any]]:
        return [
            {
                "server_id": "ledger",
                "server_name": "External Ledger",
                "server_kind": "http",
                "name": "append_entry",
                "description": "向外部账本追加一条不可撤销记录。",
                "input_schema": {
                    "type": "object",
                    "properties": {"payload": {"type": "string"}},
                    "required": ["payload"],
                    "additionalProperties": False,
                },
            }
        ]

    def get_tool_definition(
        self, server_id: str, tool_name: str
    ) -> dict[str, Any] | None:
        return next(
            (
                dict(item)
                for item in self.list_tools()
                if item["server_id"] == server_id and item["name"] == tool_name
            ),
            None,
        )

    async def invoke_tool(
        self,
        server_id: str,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        task_id: str = "",
        idempotency_key: str = "",
        tool_effect_id: str = "",
    ) -> dict[str, Any]:
        self.external_effect_count += 1
        self.calls.append(
            {
                "server_id": server_id,
                "tool_name": tool_name,
                "arguments": dict(arguments),
                "task_id": task_id,
                "idempotency_key": idempotency_key,
                "tool_effect_id": tool_effect_id,
            }
        )
        if self.crash_once:
            self.crash_once = False
            raise asyncio.CancelledError
        return {
            "value": f"external-entry-{self.external_effect_count}",
            "external_ref": f"entry-{self.external_effect_count}",
        }


class CrashAfterArtifactGateway(McpGateway):
    """Run the real generator, then lose the process before journal success."""

    def __init__(self) -> None:
        self.generator_calls = 0
        self.crash_once = True

    async def invoke_tool(
        self,
        server_id: str,
        tool_name: str,
        arguments: dict[str, Any],
        task_id: str | None = None,
        *,
        idempotency_key: str = "",
        tool_effect_id: str = "",
    ) -> dict[str, Any]:
        if server_id == "report" and tool_name == "generate_document":
            self.generator_calls += 1
        result = await super().invoke_tool(
            server_id,
            tool_name,
            arguments,
            task_id=task_id,
            idempotency_key=idempotency_key,
            tool_effect_id=tool_effect_id,
        )
        if (
            self.crash_once
            and server_id == "report"
            and tool_name == "generate_document"
        ):
            self.crash_once = False
            raise asyncio.CancelledError
        return result


class CountingArtifactGateway(McpGateway):
    """Count real gateway dispatches for the succeeded-before-publish window."""

    def __init__(self) -> None:
        self.generator_calls = 0

    async def invoke_tool(
        self,
        server_id: str,
        tool_name: str,
        arguments: dict[str, Any],
        task_id: str | None = None,
        *,
        idempotency_key: str = "",
        tool_effect_id: str = "",
    ) -> dict[str, Any]:
        if server_id == "report" and tool_name == "generate_document":
            self.generator_calls += 1
        return await super().invoke_tool(
            server_id,
            tool_name,
            arguments,
            task_id=task_id,
            idempotency_key=idempotency_key,
            tool_effect_id=tool_effect_id,
        )


class FakeHttpResponse:
    text = ""

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, Any]:
        return {"ok": True}


class CapturingHttpClient:
    init_headers: list[dict[str, str]] = []
    request_headers: list[dict[str, str]] = []

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.__class__.init_headers.append(dict(kwargs.get("headers") or {}))

    async def __aenter__(self) -> "CapturingHttpClient":
        return self

    async def __aexit__(self, *args: Any) -> None:
        return None

    async def get(self, url: str, **kwargs: Any) -> FakeHttpResponse:
        self.__class__.request_headers.append(dict(kwargs.get("headers") or {}))
        return FakeHttpResponse()

    async def request(
        self, method: str, url: str, **kwargs: Any
    ) -> FakeHttpResponse:
        self.__class__.request_headers.append(dict(kwargs.get("headers") or {}))
        return FakeHttpResponse()


class FakeStreamableHttpContext:
    async def __aenter__(self) -> tuple[str, str, None]:
        return "read", "write", None

    async def __aexit__(self, *args: Any) -> None:
        return None


class CapturingMcpSession:
    tool_calls: list[tuple[str, dict[str, Any]]] = []

    def __init__(self, read: Any, write: Any) -> None:
        self.read = read
        self.write = write

    async def __aenter__(self) -> "CapturingMcpSession":
        return self

    async def __aexit__(self, *args: Any) -> None:
        return None

    async def initialize(self) -> None:
        return None

    async def call_tool(
        self, tool_name: str, arguments: dict[str, Any]
    ) -> SimpleNamespace:
        self.__class__.tool_calls.append((tool_name, dict(arguments)))
        return SimpleNamespace(
            content=[], isError=False, structuredContent={"ok": True}
        )


class ToolEffectRuntimeIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_db_path = db.DB_PATH
        self.db_path = Path(self.temp_dir.name) / "runtime-effects.db"
        self.artifact_dir = Path(self.temp_dir.name) / "artifacts"
        db.DB_PATH = self.db_path
        db.init_db()
        self.state = TaskStateService(db.get_conn)
        self.artifact_patch = patch.multiple(
            "app.services.mcp_gateway",
            ARTIFACT_DIR=self.artifact_dir,
        )
        self.artifact_patch.start()
        self.runtime_artifact_patch = patch.object(
            runtime_module, "ARTIFACT_DIR", self.artifact_dir
        )
        self.runtime_artifact_patch.start()

    async def asyncTearDown(self) -> None:
        self.runtime_artifact_patch.stop()
        self.artifact_patch.stop()
        db.DB_PATH = self.original_db_path
        self.temp_dir.cleanup()

    @staticmethod
    def _task(message: str) -> dict[str, Any]:
        return create_task_record(
            message,
            "general-agent",
            conversation_id="conv_tool_effect_runtime",
        )

    async def _wait_for_task_status(
        self,
        task_id: str,
        expected: str,
        *,
        timeout: float = 5.0,
    ) -> dict[str, Any]:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        latest: dict[str, Any] = {}
        while loop.time() < deadline:
            latest = db.query_one(
                "SELECT * FROM tasks WHERE id = ?", (task_id,)
            ) or {}
            if latest.get("status") == expected:
                return latest
            await asyncio.sleep(0.01)
        self.fail(
            f"task {task_id} did not reach {expected}; latest={latest.get('status')}"
        )

    def _recover(self, old_run_id: str) -> dict[str, Any]:
        recovered = self.state.recover_interrupted_attempt(old_run_id)
        self.assertTrue(recovered["recovered"])
        return recovered["run"]

    async def _run_non_idempotent_recovery(self, *, approved: bool) -> None:
        gateway = CrashAfterExternalWriteGateway()
        model = ToolCallingModelGateway(
            "ledger__append_entry", {"payload": "stable-payload"}
        )
        registry = RuntimeSkillRegistry(
            skill_id="ledger_writer", required_server="ledger"
        )
        runtime = AgentRuntime(
            registry,
            gateway,
            model,
            task_state=self.state,
            policy_engine=PolicyEngine(),
        )
        task = self._task("向外部审计账本追加 stable-payload 记录")

        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(runtime.run_task(task["id"]), timeout=5)

        old_run = self.state.list_runs(task_id=task["id"])[0]
        self.assertEqual(gateway.external_effect_count, 1)
        self.assertEqual(old_run["status"], "running")
        effects = db.query_all(
            "SELECT * FROM tool_effects WHERE task_id = ?", (task["id"],)
        )
        self.assertEqual(len(effects), 1)
        self.assertEqual(effects[0]["state"], "unknown")
        self.assertEqual(effects[0]["effect_kind"], "non_idempotent_write")
        checkpoints = self.state.list_checkpoints(
            run_id=old_run["id"], include_state=True
        )
        self.assertFalse(
            any(
                item["state"].get("phase") == "tool_completed"
                or bool(item["state"].get("completed_tools"))
                for item in checkpoints
            )
        )

        recovered_run = self._recover(old_run["id"])
        recovered_runtime = AgentRuntime(
            registry,
            gateway,
            model,
            task_state=TaskStateService(db.get_conn),
            policy_engine=PolicyEngine(),
        )
        worker = asyncio.create_task(
            recovered_runtime.run_task(task["id"], run_id=recovered_run["id"])
        )
        waiting = await self._wait_for_task_status(
            task["id"], "waiting_approval"
        )
        self.assertEqual(gateway.external_effect_count, 1)
        waiting_result = db.json_loads(waiting.get("result_json"), {})
        approval_id = str(waiting_result.get("policy_approval_id") or "")
        self.assertTrue(approval_id)
        self.state.enqueue_command(
            task["id"],
            "approval",
            run_id=recovered_run["id"],
            payload={"approval_id": approval_id, "approved": approved},
            priority=90,
        )
        try:
            await asyncio.wait_for(worker, timeout=5)
        finally:
            if not worker.done():
                worker.cancel()
                await asyncio.gather(worker, return_exceptions=True)

        stored_task = db.query_one(
            "SELECT * FROM tasks WHERE id = ?", (task["id"],)
        ) or {}
        stored_run = self.state.get_run(recovered_run["id"]) or {}
        if approved:
            self.assertEqual(gateway.external_effect_count, 2)
            self.assertEqual(stored_task.get("status"), "completed")
            self.assertEqual(stored_run.get("status"), "completed")
            effect = db.query_one(
                "SELECT * FROM tool_effects WHERE task_id = ?", (task["id"],)
            ) or {}
            self.assertEqual(effect.get("state"), "succeeded")
            self.assertEqual(effect.get("attempt_count"), 2)
            self.assertEqual(len(gateway.calls), 2)
            self.assertEqual(
                gateway.calls[0]["idempotency_key"],
                gateway.calls[1]["idempotency_key"],
            )
            self.assertEqual(
                gateway.calls[0]["tool_effect_id"],
                gateway.calls[1]["tool_effect_id"],
            )
            self.assertTrue(gateway.calls[0]["idempotency_key"])
            self.assertTrue(gateway.calls[0]["tool_effect_id"])
        else:
            self.assertEqual(gateway.external_effect_count, 1)
            self.assertEqual(stored_task.get("status"), "failed")
            self.assertEqual(stored_run.get("status"), "failed")
            effect = db.query_one(
                "SELECT * FROM tool_effects WHERE task_id = ?", (task["id"],)
            ) or {}
            self.assertEqual(effect.get("state"), "unknown")
            self.assertEqual(effect.get("attempt_count"), 1)
            answer_count = db.query_one(
                "SELECT COUNT(*) AS total FROM task_events "
                "WHERE task_id = ? AND type = 'answer'",
                (task["id"],),
            ) or {}
            self.assertEqual(answer_count.get("total"), 0)

    async def test_unknown_non_idempotent_write_requires_approval_before_retry(
        self,
    ) -> None:
        await self._run_non_idempotent_recovery(approved=True)

    async def test_unknown_non_idempotent_write_rejection_never_retries(self) -> None:
        await self._run_non_idempotent_recovery(approved=False)

    async def test_artifact_created_before_journal_success_is_adopted_exactly_once(
        self,
    ) -> None:
        gateway = CrashAfterArtifactGateway()
        gateway.seed_builtin_servers()
        model = StaticModelGateway("这是只生成一次的 HTML 文档正文。")
        registry = RuntimeSkillRegistry(skill_id="general_task")
        runtime = AgentRuntime(
            registry,
            gateway,
            model,
            task_state=self.state,
            policy_engine=PolicyEngine(),
        )
        task = self._task("生成一个 HTML 文件，标题为重启一致性报告")

        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(runtime.run_task(task["id"]), timeout=5)

        old_run = self.state.list_runs(task_id=task["id"])[0]
        before = db.query_all(
            "SELECT * FROM artifacts WHERE task_id = ?", (task["id"],)
        )
        self.assertEqual(gateway.generator_calls, 1)
        self.assertEqual(len(before), 1)
        original = before[0]
        original_path = self.artifact_dir / original["relative_path"]
        self.assertTrue(original_path.is_file())
        original_bytes = original_path.read_bytes()
        self.assertEqual(
            hashlib.sha256(original_bytes).hexdigest(), original["sha256"]
        )
        effect = db.query_one(
            "SELECT * FROM tool_effects WHERE task_id = ?", (task["id"],)
        ) or {}
        self.assertEqual(effect.get("state"), "unknown")
        self.assertEqual(effect.get("artifact_id"), "")

        recovered_run = self._recover(old_run["id"])
        recovered_runtime = AgentRuntime(
            registry,
            gateway,
            model,
            task_state=TaskStateService(db.get_conn),
            policy_engine=PolicyEngine(),
        )
        await asyncio.wait_for(
            recovered_runtime.run_task(task["id"], run_id=recovered_run["id"]),
            timeout=5,
        )

        after = db.query_all(
            "SELECT * FROM artifacts WHERE task_id = ?", (task["id"],)
        )
        self.assertEqual(gateway.generator_calls, 1)
        self.assertEqual(len(after), 1)
        self.assertEqual(after[0]["id"], original["id"])
        self.assertEqual(after[0]["relative_path"], original["relative_path"])
        self.assertEqual(after[0]["run_id"], recovered_run["id"])
        self.assertEqual(after[0]["delivery_status"], "published")
        self.assertEqual(original_path.read_bytes(), original_bytes)
        self.assertEqual(
            len([item for item in self.artifact_dir.rglob("*") if item.is_file()]),
            1,
        )
        stored_effect = db.query_one(
            "SELECT * FROM tool_effects WHERE task_id = ?", (task["id"],)
        ) or {}
        self.assertEqual(stored_effect.get("state"), "succeeded")
        self.assertEqual(stored_effect.get("artifact_id"), original["id"])
        self.assertEqual(stored_effect.get("artifact_sha256"), original["sha256"])
        answer = db.query_one(
            "SELECT content FROM task_events WHERE task_id = ? "
            "AND type = 'answer' ORDER BY id DESC LIMIT 1",
            (task["id"],),
        ) or {}
        self.assertIn(
            f"/api/artifacts/{original['id']}/download",
            str(answer.get("content") or ""),
        )

    async def test_succeeded_artifact_checkpoint_before_publication_is_reused_once(
        self,
    ) -> None:
        gateway = CountingArtifactGateway()
        gateway.seed_builtin_servers()
        arguments = {
            "title": "Checkpoint Window",
            "content": "The durable artifact must not be cloned.",
            "format": "html",
            "filename": "checkpoint-window.html",
        }
        model = ToolCallingModelGateway(
            "report__generate_document",
            arguments,
            cancel_after_first_result=True,
        )
        registry = RuntimeSkillRegistry(skill_id="general_task")
        runtime = AgentRuntime(
            registry,
            gateway,
            model,
            task_state=self.state,
            policy_engine=PolicyEngine(),
        )
        task = self._task("生成一个 HTML 文件并提供下载")

        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(runtime.run_task(task["id"]), timeout=5)

        old_run = self.state.list_runs(task_id=task["id"])[0]
        before = db.query_all(
            "SELECT * FROM artifacts WHERE task_id = ?", (task["id"],)
        )
        self.assertEqual(gateway.generator_calls, 1)
        self.assertEqual(len(before), 1)
        original = before[0]
        effect = db.query_one(
            "SELECT * FROM tool_effects WHERE task_id = ?", (task["id"],)
        ) or {}
        self.assertEqual(effect.get("state"), "succeeded")
        self.assertEqual(effect.get("artifact_id"), original["id"])
        checkpoints = self.state.list_checkpoints(
            run_id=old_run["id"], include_state=True
        )
        self.assertTrue(
            any(
                item["state"].get("phase") == "tool_completed"
                and bool(item["state"].get("completed_tools"))
                for item in checkpoints
            )
        )
        source_path = self.artifact_dir / original["relative_path"]
        source_bytes = source_path.read_bytes()

        recovered_run = self._recover(old_run["id"])
        recovered_runtime = AgentRuntime(
            registry,
            gateway,
            model,
            task_state=TaskStateService(db.get_conn),
            policy_engine=PolicyEngine(),
        )
        await asyncio.wait_for(
            recovered_runtime.run_task(task["id"], run_id=recovered_run["id"]),
            timeout=5,
        )

        after = db.query_all(
            "SELECT * FROM artifacts WHERE task_id = ?", (task["id"],)
        )
        self.assertEqual(gateway.generator_calls, 1)
        self.assertEqual(len(after), 1)
        self.assertEqual(after[0]["id"], original["id"])
        self.assertEqual(after[0]["run_id"], recovered_run["id"])
        self.assertEqual(after[0]["delivery_status"], "published")
        self.assertEqual(source_path.read_bytes(), source_bytes)
        self.assertEqual(
            len([item for item in self.artifact_dir.rglob("*") if item.is_file()]),
            1,
        )

    async def test_http_gateway_forwards_one_stable_transport_header(self) -> None:
        gateway = McpGateway()
        gateway.create_server(
            {
                "id": "http-effects",
                "name": "HTTP Effects",
                "kind": "http",
                "description": "",
                "enabled": True,
                "config": {
                    "base_url": "https://effects.example.test",
                    "headers": {
                        "Authorization": "Bearer fixture",
                        "idempotency-key": "unstable-caller-value",
                    },
                },
                "tools": [
                    {
                        "name": "write",
                        "description": "write",
                        "path": "/write",
                        "method": "POST",
                        "input_schema": {"type": "object"},
                    }
                ],
            }
        )
        CapturingHttpClient.init_headers.clear()
        CapturingHttpClient.request_headers.clear()
        key = "agentnexus-runtime-stable-key"

        with (
            patch.object(mcp_module, "require_outbound_network"),
            patch.object(mcp_module, "env_flag", return_value=True),
            patch.object(
                gateway, "_validate_remote_url", side_effect=lambda value: value
            ),
            patch.object(mcp_module.httpx, "AsyncClient", CapturingHttpClient),
        ):
            result = await gateway.invoke_tool(
                "http-effects",
                "write",
                {"value": 1},
                idempotency_key=key,
            )

        self.assertEqual(result, {"ok": True})
        self.assertEqual(len(CapturingHttpClient.request_headers), 1)
        headers = CapturingHttpClient.request_headers[0]
        self.assertEqual(headers["Idempotency-Key"], key)
        self.assertEqual(headers["Authorization"], "Bearer fixture")
        self.assertEqual(
            [name for name in headers if name.lower() == "idempotency-key"],
            ["Idempotency-Key"],
        )

    async def test_streamable_mcp_forwards_transport_and_schema_safe_argument(
        self,
    ) -> None:
        import mcp
        from mcp.client import streamable_http as streamable_module

        gateway = McpGateway()
        gateway.create_server(
            {
                "id": "remote-effects",
                "name": "Remote Effects",
                "kind": "mcp_http",
                "description": "",
                "enabled": True,
                "config": {
                    "url": "https://mcp.example.test/rpc",
                    "headers": {"idempotency-key": "unstable"},
                },
                "tools": [
                    {
                        "name": "declared",
                        "description": "declared idempotency argument",
                        "input_schema": {
                            "type": "object",
                            "properties": {
                                "value": {"type": "string"},
                                "idempotencyKey": {"type": "string"},
                            },
                            "additionalProperties": False,
                        },
                    },
                    {
                        "name": "undeclared",
                        "description": "no idempotency argument",
                        "input_schema": {
                            "type": "object",
                            "properties": {"value": {"type": "string"}},
                            "additionalProperties": False,
                        },
                    },
                ],
            }
        )
        CapturingHttpClient.init_headers.clear()
        CapturingMcpSession.tool_calls.clear()
        stream_urls: list[str] = []

        def fake_streamable_http_client(
            url: str, *, http_client: Any
        ) -> FakeStreamableHttpContext:
            stream_urls.append(url)
            self.assertIsInstance(http_client, CapturingHttpClient)
            return FakeStreamableHttpContext()

        key = "agentnexus-mcp-stable-key"
        with (
            patch.object(mcp_module, "require_outbound_network"),
            patch.object(mcp_module, "env_flag", return_value=True),
            patch.object(
                gateway, "_validate_remote_url", side_effect=lambda value: value
            ),
            patch.object(mcp_module.httpx, "AsyncClient", CapturingHttpClient),
            patch.object(mcp, "ClientSession", CapturingMcpSession),
            patch.object(
                streamable_module,
                "streamable_http_client",
                side_effect=fake_streamable_http_client,
            ),
        ):
            await gateway.invoke_tool(
                "remote-effects",
                "declared",
                {"value": "one"},
                idempotency_key=key,
            )
            await gateway.invoke_tool(
                "remote-effects",
                "undeclared",
                {"value": "two"},
                idempotency_key=key,
            )

        self.assertEqual(
            stream_urls,
            ["https://mcp.example.test/rpc", "https://mcp.example.test/rpc"],
        )
        self.assertEqual(len(CapturingHttpClient.init_headers), 2)
        for headers in CapturingHttpClient.init_headers:
            self.assertEqual(headers["Idempotency-Key"], key)
            self.assertEqual(
                [name for name in headers if name.lower() == "idempotency-key"],
                ["Idempotency-Key"],
            )
        self.assertEqual(
            CapturingMcpSession.tool_calls,
            [
                ("declared", {"value": "one", "idempotencyKey": key}),
                ("undeclared", {"value": "two"}),
            ],
        )


if __name__ == "__main__":
    unittest.main()
