from __future__ import annotations

import inspect
import os
import tempfile
import unittest
from pathlib import Path
from typing import Any, Awaitable, Callable
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from app import db
from app import main as main_module
from app.services.agent_runtime import AgentRuntime, create_task_record
from app.services.context_service import ContextService, ExecutionScope
from app.services.policy_engine import PolicyEngine
from app.services.task_state import TaskStateService


class MemoryApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_db_path = db.DB_PATH
        self.original_app_db_path = os.environ.get("APP_DB_PATH")
        self.db_path = Path(self.temp_dir.name) / "memory-api.db"
        os.environ["APP_DB_PATH"] = str(self.db_path)
        db.DB_PATH = self.db_path
        db.init_db()

        self.scheduler_start_patch = patch.object(
            main_module.loop_scheduler, "start", return_value=None
        )
        self.scheduler_stop_patch = patch.object(
            main_module.loop_scheduler, "stop", new_callable=AsyncMock
        )
        self.skill_seed_patch = patch.object(
            main_module.skill_registry, "load_builtin_skills", return_value=None
        )
        self.mcp_seed_patch = patch.object(
            main_module.mcp_gateway, "seed_builtin_servers", return_value=None
        )
        self.agent_seed_patch = patch.object(main_module, "seed_agents", return_value=None)
        self.recovery_patch = patch.object(
            main_module, "_recover_interrupted_runs", return_value=[]
        )
        for active_patch in (
            self.scheduler_start_patch,
            self.scheduler_stop_patch,
            self.skill_seed_patch,
            self.mcp_seed_patch,
            self.agent_seed_patch,
            self.recovery_patch,
        ):
            active_patch.start()
        self.client_context = TestClient(main_module.app)
        self.client = self.client_context.__enter__()

    def tearDown(self) -> None:
        self.client_context.__exit__(None, None, None)
        for active_patch in reversed(
            (
                self.scheduler_start_patch,
                self.scheduler_stop_patch,
                self.skill_seed_patch,
                self.mcp_seed_patch,
                self.agent_seed_patch,
                self.recovery_patch,
            )
        ):
            active_patch.stop()
        db.DB_PATH = self.original_db_path
        if self.original_app_db_path is None:
            os.environ.pop("APP_DB_PATH", None)
        else:
            os.environ["APP_DB_PATH"] = self.original_app_db_path
        self.temp_dir.cleanup()

    @staticmethod
    def _scope(
        *, user_id: str = "alice", workspace_id: str = "workspace-a"
    ) -> dict[str, str]:
        return {
            "organization_id": "org-a",
            "workspace_id": workspace_id,
            "user_id": user_id,
            "agent_id": "general-agent",
            "conversation_id": f"conversation-{user_id}-{workspace_id}",
        }

    def _create_memory(
        self,
        *,
        user_id: str = "alice",
        workspace_id: str = "workspace-a",
        scope_type: str = "user",
        title: str = "回答风格",
        content: str = "默认使用简体中文回答",
    ) -> dict[str, Any]:
        response = self.client.post(
            "/api/memories",
            json={
                **self._scope(user_id=user_id, workspace_id=workspace_id),
                "scope_type": scope_type,
                "kind": "preference",
                "title": title,
                "content": content,
                "tags": ["语言", "输出"],
                "trust_level": 90,
            },
        )
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    def test_memory_api_crud_enable_disable_revisions_and_delete(self) -> None:
        scope = self._scope()
        created = self._create_memory()
        memory_id = created["id"]
        self.assertEqual(created["scope_type"], "user")
        self.assertEqual(created["scope_id"], "alice")
        self.assertTrue(created["enabled"])

        listed = self.client.get("/api/memories", params=scope)
        self.assertEqual(listed.status_code, 200, listed.text)
        self.assertEqual([item["id"] for item in listed.json()], [memory_id])
        fetched = self.client.get(f"/api/memories/{memory_id}", params=scope)
        self.assertEqual(fetched.status_code, 200, fetched.text)
        self.assertEqual(fetched.json(), created)

        updated = self.client.put(
            f"/api/memories/{memory_id}",
            params=scope,
            json={
                "title": "更新后的回答风格",
                "content": "使用简体中文，并先给结论",
                "tags": ["语言", "结论"],
                "reason": "api-test-update",
            },
        )
        self.assertEqual(updated.status_code, 200, updated.text)
        self.assertEqual(updated.json()["content"], "使用简体中文，并先给结论")

        disabled = self.client.post(
            f"/api/memories/{memory_id}/disable", params=scope
        )
        self.assertEqual(disabled.status_code, 200, disabled.text)
        self.assertFalse(disabled.json()["enabled"])
        effective_while_disabled = self.client.get(
            "/api/context/effective", params=scope
        )
        self.assertEqual(effective_while_disabled.status_code, 200)
        self.assertNotIn(memory_id, effective_while_disabled.json()["used_memory_ids"])

        enabled = self.client.post(
            f"/api/memories/{memory_id}/enable", params=scope
        )
        self.assertEqual(enabled.status_code, 200, enabled.text)
        self.assertTrue(enabled.json()["enabled"])
        effective_after_enable = self.client.get(
            "/api/context/effective", params=scope
        )
        self.assertIn(memory_id, effective_after_enable.json()["used_memory_ids"])

        revisions = self.client.get(
            f"/api/memories/{memory_id}/revisions", params=scope
        )
        self.assertEqual(revisions.status_code, 200, revisions.text)
        self.assertEqual(
            [item["reason"] for item in revisions.json()],
            ["created", "api-test-update", "disabled", "enabled"],
        )
        self.assertEqual(
            [item["revision"] for item in revisions.json()], [1, 2, 3, 4]
        )

        deleted = self.client.delete(f"/api/memories/{memory_id}", params=scope)
        self.assertEqual(deleted.status_code, 200, deleted.text)
        self.assertEqual(deleted.json(), {"id": memory_id, "deleted": True})
        self.assertEqual(
            self.client.get(f"/api/memories/{memory_id}", params=scope).status_code,
            404,
        )
        revisions_after_delete = self.client.get(
            f"/api/memories/{memory_id}/revisions", params=scope
        )
        self.assertEqual(revisions_after_delete.status_code, 200)
        self.assertEqual(
            [item["reason"] for item in revisions_after_delete.json()],
            ["created", "api-test-update", "disabled", "enabled", "api_delete"],
        )

    def test_memory_api_isolates_users_and_workspaces(self) -> None:
        alice_user = self._create_memory(
            user_id="alice",
            workspace_id="workspace-a",
            title="Alice 偏好",
            content="Alice 默认要简洁回答",
        )
        bob_user = self._create_memory(
            user_id="bob",
            workspace_id="workspace-a",
            title="Bob 偏好",
            content="Bob 默认要详细回答",
        )
        shared_workspace = self._create_memory(
            user_id="alice",
            workspace_id="workspace-a",
            scope_type="workspace",
            title="工作区术语",
            content="统一使用“智能体平台”这个术语",
        )

        alice_visible = self.client.get(
            "/api/memories", params=self._scope(user_id="alice", workspace_id="workspace-a")
        ).json()
        bob_visible = self.client.get(
            "/api/memories", params=self._scope(user_id="bob", workspace_id="workspace-a")
        ).json()
        other_workspace_visible = self.client.get(
            "/api/memories", params=self._scope(user_id="alice", workspace_id="workspace-b")
        ).json()

        self.assertEqual(
            {item["id"] for item in alice_visible},
            {alice_user["id"], shared_workspace["id"]},
        )
        self.assertEqual(
            {item["id"] for item in bob_visible},
            {bob_user["id"], shared_workspace["id"]},
        )
        self.assertEqual(other_workspace_visible, [])
        self.assertEqual(
            self.client.get(
                f"/api/memories/{alice_user['id']}",
                params=self._scope(user_id="bob", workspace_id="workspace-a"),
            ).status_code,
            404,
        )
        self.assertEqual(
            self.client.get(
                f"/api/memories/{shared_workspace['id']}",
                params=self._scope(user_id="alice", workspace_id="workspace-b"),
            ).status_code,
            404,
        )


class StubSkillRegistry:
    skill = {
        "id": "general_task",
        "name": "通用任务 Skill",
        "description": "用于记忆运行时隔离测试。",
        "content": "忠实完成用户当前任务。",
        "enabled": True,
        "required_mcps": [],
    }

    def list_skills(self, enabled_only: bool = False) -> list[dict[str, Any]]:
        return [dict(self.skill)]

    def score_skills(
        self, message: str, allowed_ids: list[str] | None = None
    ) -> list[dict[str, Any]]:
        return [{"skill": dict(self.skill), "score": 10.0}]

    def get_skill(self, skill_id: str) -> dict[str, Any] | None:
        return dict(self.skill) if skill_id == self.skill["id"] else None

    def runtime_content(self, skill_id: str, max_chars: int = 16000) -> str:
        return str(self.skill["content"])[:max_chars]


class StubMcpGateway:
    def list_tools(self) -> list[dict[str, Any]]:
        return []


class RecordingModelGateway:
    def __init__(self) -> None:
        self.intent_calls: list[dict[str, Any]] = []
        self.solve_calls: list[dict[str, Any]] = []

    async def resolve_intent(
        self, message: str, history: list[dict[str, str]], model_config_id: str
    ) -> dict[str, Any]:
        self.intent_calls.append(
            {
                "message": message,
                "history": [dict(item) for item in history],
                "model_id": model_config_id,
            }
        )
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
        self.solve_calls.append(
            {
                "prompt": prompt,
                "system_prompt": system_prompt,
                "history": [dict(item) for item in history or []],
                "tools": [dict(item) for item in tools],
            }
        )
        answer = "普通任务已经完成。"
        if on_delta:
            pending = on_delta(answer)
            if inspect.isawaitable(pending):
                await pending
        return answer


class MemoryRuntimeIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_db_path = db.DB_PATH
        db.DB_PATH = Path(self.temp_dir.name) / "memory-runtime.db"
        db.init_db()
        self.task_state = TaskStateService(db.get_conn)
        self.context_service = ContextService(db.get_conn)

    async def asyncTearDown(self) -> None:
        db.DB_PATH = self.original_db_path
        self.temp_dir.cleanup()

    def _runtime(self, model: RecordingModelGateway) -> AgentRuntime:
        return AgentRuntime(
            StubSkillRegistry(),
            StubMcpGateway(),
            model,
            task_state=self.task_state,
            policy_engine=PolicyEngine(),
            context_service=self.context_service,
        )

    @staticmethod
    def _task(
        message: str,
        *,
        user_id: str,
        workspace: str,
        conversation_id: str,
    ) -> dict[str, Any]:
        return create_task_record(
            message,
            "general-agent",
            workspace,
            conversation_id=conversation_id,
            organization_id="org-memory-runtime",
            user_id=user_id,
        )

    @staticmethod
    def _event_payloads(task_id: str, event_type: str) -> list[dict[str, Any]]:
        return [
            db.json_loads(row["data_json"], {})
            for row in db.query_all(
                "SELECT data_json FROM task_events WHERE task_id = ? AND type = ? ORDER BY id",
                (task_id, event_type),
            )
        ]

    async def test_chat_remember_injects_across_conversations_isolates_and_forget_stops_it(self) -> None:
        memory_content = "以后普通回答都使用简体中文，并在结尾注明验收完成"
        remember_model = RecordingModelGateway()
        runtime = self._runtime(remember_model)
        remember_task = self._task(
            f"记住：{memory_content}",
            user_id="alice",
            workspace="workspace-a",
            conversation_id="conversation-remember",
        )

        await runtime.run_task(remember_task["id"])

        stored_remember_task = db.query_one(
            "SELECT status FROM tasks WHERE id = ?", (remember_task["id"],)
        )
        saved_rows = db.query_all("SELECT * FROM memory_entries")
        self.assertEqual(stored_remember_task["status"], "completed")
        remember_run = runtime.task_state.list_runs(
            task_id=remember_task["id"]
        )[0]
        self.assertEqual(remember_run["status"], "completed")
        self.assertEqual(remember_run["intake_state"], "closed")
        runtime.task_state.assert_terminal_clean(
            task_id=remember_task["id"], run_id=remember_run["id"]
        )
        self.assertEqual(len(saved_rows), 1)
        memory_id = saved_rows[0]["id"]
        self.assertEqual(saved_rows[0]["organization_id"], "org-memory-runtime")
        self.assertEqual(saved_rows[0]["workspace_id"], "workspace-a")
        self.assertEqual(saved_rows[0]["user_id"], "alice")
        self.assertEqual(saved_rows[0]["scope_type"], "user")
        self.assertEqual(saved_rows[0]["content"], memory_content)
        self.assertEqual(saved_rows[0]["source_ref"], remember_task["id"])
        self.assertEqual(len(self._event_payloads(remember_task["id"], "memory_saved")), 1)
        scope = ExecutionScope(
            "org-memory-runtime",
            "workspace-a",
            "alice",
            "general-agent",
            "conversation-remember",
        )
        self.assertEqual(
            [item["reason"] for item in self.context_service.list_revisions(memory_id, scope)],
            ["explicit_remember"],
        )
        self.assertEqual(remember_model.solve_calls, [])

        alice_model = RecordingModelGateway()
        alice_task = self._task(
            "请概括可靠任务内核的价值",
            user_id="alice",
            workspace="workspace-a",
            conversation_id="conversation-alice-new",
        )
        await self._runtime(alice_model).run_task(alice_task["id"])

        memory_events = self._event_payloads(alice_task["id"], "memory")
        self.assertEqual(len(memory_events), 1)
        self.assertEqual(memory_events[0]["memory_ids"], [memory_id])
        self.assertEqual(memory_events[0]["scopes"], ["user"])
        plans = self._event_payloads(alice_task["id"], "plan")
        final_plan = plans[-1]["plan"]
        prepare = next(item for item in final_plan["nodes"] if item["id"] == "prepare")
        self.assertEqual(
            [(item["id"], item["kind"]) for item in prepare["children"]],
            [("memory:effective", "memory")],
        )
        memory_progress = [
            item for item in self._event_payloads(alice_task["id"], "plan_progress")
            if item.get("child_id") == "memory:effective"
        ]
        self.assertEqual(len(memory_progress), 1)
        self.assertEqual(memory_progress[0]["status"], "completed")
        self.assertEqual(len(alice_model.intent_calls), 1)
        self.assertIn(
            memory_content,
            "\n".join(item["content"] for item in alice_model.intent_calls[0]["history"]),
        )
        self.assertEqual(len(alice_model.solve_calls), 1)
        self.assertIn(memory_content, alice_model.solve_calls[0]["prompt"])
        self.assertIn("长期记忆", alice_model.solve_calls[0]["system_prompt"])
        self.assertEqual(
            db.query_one("SELECT status FROM tasks WHERE id = ?", (alice_task["id"],))[
                "status"
            ],
            "completed",
        )

        bob_model = RecordingModelGateway()
        bob_task = self._task(
            "请概括可靠任务内核的价值",
            user_id="bob",
            workspace="workspace-a",
            conversation_id="conversation-bob-new",
        )
        await self._runtime(bob_model).run_task(bob_task["id"])
        self.assertEqual(self._event_payloads(bob_task["id"], "memory"), [])
        self.assertNotIn(
            memory_content,
            "\n".join(item["content"] for item in bob_model.intent_calls[0]["history"]),
        )
        self.assertNotIn(memory_content, bob_model.solve_calls[0]["prompt"])

        other_workspace_model = RecordingModelGateway()
        other_workspace_task = self._task(
            "请概括可靠任务内核的价值",
            user_id="alice",
            workspace="workspace-b",
            conversation_id="conversation-alice-other-workspace",
        )
        await self._runtime(other_workspace_model).run_task(other_workspace_task["id"])
        self.assertEqual(
            self._event_payloads(other_workspace_task["id"], "memory"), []
        )
        self.assertNotIn(memory_content, other_workspace_model.solve_calls[0]["prompt"])

        forget_model = RecordingModelGateway()
        forget_task = self._task(
            f"忘记：{memory_content}",
            user_id="alice",
            workspace="workspace-a",
            conversation_id="conversation-forget",
        )
        await self._runtime(forget_model).run_task(forget_task["id"])
        deleted_payloads = self._event_payloads(forget_task["id"], "memory_deleted")
        self.assertEqual(len(deleted_payloads), 1)
        self.assertEqual(deleted_payloads[0]["memory_ids"], [memory_id])
        self.assertEqual(db.query_all("SELECT * FROM memory_entries"), [])
        self.assertEqual(
            [item["reason"] for item in self.context_service.list_revisions(memory_id, scope)],
            ["explicit_remember", "explicit_forget"],
        )

        after_forget_model = RecordingModelGateway()
        after_forget_task = self._task(
            "再次概括可靠任务内核的价值",
            user_id="alice",
            workspace="workspace-a",
            conversation_id="conversation-after-forget",
        )
        await self._runtime(after_forget_model).run_task(after_forget_task["id"])
        self.assertEqual(
            self._event_payloads(after_forget_task["id"], "memory"), []
        )
        self.assertNotIn(memory_content, after_forget_model.solve_calls[0]["prompt"])
        self.assertNotIn(
            memory_content,
            "\n".join(
                item["content"] for item in after_forget_model.intent_calls[0]["history"]
            ),
        )

    async def test_compound_remember_request_continues_through_normal_runtime(self) -> None:
        model = RecordingModelGateway()
        runtime = self._runtime(model)
        self.context_service.remember(
            ExecutionScope(
                "org-memory-runtime", "workspace-a", "alice", "", ""
            ),
            "本次验收代号是旧代号-000",
            title="旧验收代号",
            source_ref="older-task",
            created_by="alice",
        )
        message = (
            "请记住本次验收代号是蓝鲸-731。"
            "请用两句话说明模型连接已经通过测试。"
        )
        task = self._task(
            message,
            user_id="alice",
            workspace="workspace-a",
            conversation_id="conversation-compound-remember",
        )

        await runtime.run_task(task["id"])

        memories = db.query_all("SELECT * FROM memory_entries")
        self.assertEqual(len(memories), 2)
        current_memory = next(item for item in memories if item["source_ref"] == task["id"])
        self.assertEqual(current_memory["content"], "本次验收代号是蓝鲸-731")
        self.assertEqual(len(self._event_payloads(task["id"], "memory_saved")), 1)
        self.assertEqual(len(model.intent_calls), 1)
        self.assertEqual(len(model.solve_calls), 1)
        self.assertIn("蓝鲸-731", model.solve_calls[0]["prompt"])
        self.assertNotIn(
            "旧代号-000",
            "\n".join(item["content"] for item in model.intent_calls[0]["history"]),
        )
        self.assertNotIn("旧代号-000", model.solve_calls[0]["prompt"])
        self.assertNotIn("请记住", model.intent_calls[0]["message"])
        self.assertNotIn("请记住", model.solve_calls[0]["prompt"])
        self.assertEqual(
            db.query_one("SELECT status FROM tasks WHERE id = ?", (task["id"],))["status"],
            "completed",
        )

        await runtime.run_task(task["id"])
        self.assertEqual(len(db.query_all("SELECT * FROM memory_entries")), 2)


if __name__ == "__main__":
    unittest.main()
