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
from app.services.knowledge_base_service import KnowledgeBaseService
from app.services.policy_engine import PolicyEngine
from app.services.task_state import TaskStateService


class KnowledgeBaseApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.temp_path = Path(self.temp_dir.name)
        self.original_db_path = db.DB_PATH
        self.original_app_db_path = os.environ.get("APP_DB_PATH")
        self.original_upload_dir = main_module.UPLOAD_DIR
        self.db_path = self.temp_path / "knowledge-api.db"
        os.environ["APP_DB_PATH"] = str(self.db_path)
        db.DB_PATH = self.db_path
        main_module.UPLOAD_DIR = self.temp_path / "uploads"
        db.init_db()

        self.patches = [
            patch.object(main_module.loop_scheduler, "start", return_value=None),
            patch.object(main_module.loop_scheduler, "stop", new_callable=AsyncMock),
            patch.object(main_module.skill_registry, "load_builtin_skills", return_value=None),
            patch.object(main_module.mcp_gateway, "seed_builtin_servers", return_value=None),
            patch.object(main_module, "seed_agents", return_value=None),
            patch.object(main_module, "_recover_interrupted_runs", return_value=[]),
        ]
        for item in self.patches:
            item.start()
        self.client_context = TestClient(main_module.app)
        self.client = self.client_context.__enter__()

    def tearDown(self) -> None:
        self.client_context.__exit__(None, None, None)
        for item in reversed(self.patches):
            item.stop()
        main_module.UPLOAD_DIR = self.original_upload_dir
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
            "organization_id": "org-kb",
            "workspace_id": workspace_id,
            "user_id": user_id,
        }

    def _create_base(
        self,
        *,
        user_id: str = "alice",
        workspace_id: str = "workspace-a",
        visibility: str = "workspace",
        name: str = "项目资料库",
    ) -> dict[str, Any]:
        response = self.client.post(
            "/api/knowledge-bases",
            json={
                **self._scope(user_id=user_id, workspace_id=workspace_id),
                "name": name,
                "description": "用于平台知识库测试",
                "visibility": visibility,
                "enabled": True,
            },
        )
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    def _upload_text(self, name: str, content: str) -> dict[str, Any]:
        response = self.client.post(
            "/api/uploads",
            files={"file": (name, content.encode("utf-8"), "text/markdown")},
        )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def test_crud_upload_index_search_and_do_not_expose_file_path(self) -> None:
        scope = self._scope()
        base = self._create_base()
        updated = self.client.put(
            f"/api/knowledge-bases/{base['id']}",
            params=scope,
            json={
                "name": "更新后的项目资料库",
                "description": "更新描述",
                "visibility": "workspace",
                "enabled": True,
            },
        )
        self.assertEqual(updated.status_code, 200, updated.text)
        self.assertEqual(updated.json()["name"], "更新后的项目资料库")

        uploaded = self._upload_text(
            "agentnexus.md",
            "AgentNexus 的知识库能力会把项目资料索引为可检索上下文，并保留来源片段。",
        )

        indexed = self.client.post(
            f"/api/knowledge-bases/{base['id']}/documents/upload",
            params=scope,
            json={"upload_id": uploaded["id"]},
        )
        self.assertEqual(indexed.status_code, 201, indexed.text)
        document = indexed.json()
        self.assertEqual(document["name"], "agentnexus.md")
        self.assertEqual(document["chunk_count"], 1)
        self.assertNotIn(str(main_module.UPLOAD_DIR), indexed.text)

        documents = self.client.get(
            f"/api/knowledge-bases/{base['id']}/documents", params=scope
        )
        self.assertEqual(documents.status_code, 200, documents.text)
        self.assertEqual([item["id"] for item in documents.json()], [document["id"]])
        self.assertNotIn(str(main_module.UPLOAD_DIR), documents.text)

        search = self.client.get(
            "/api/knowledge/search",
            params={**scope, "base_id": base["id"], "q": "AgentNexus 来源片段", "limit": 5},
        )
        self.assertEqual(search.status_code, 200, search.text)
        body = search.json()
        self.assertEqual(body["used_knowledge_base_ids"], [base["id"]])
        self.assertEqual(body["matches"][0]["document_name"], "agentnexus.md")
        self.assertIn("可检索上下文", body["matches"][0]["content"])
        self.assertNotIn(str(main_module.UPLOAD_DIR), search.text)

        deleted = self.client.delete(f"/api/knowledge-bases/{base['id']}", params=scope)
        self.assertEqual(deleted.status_code, 200, deleted.text)
        self.assertEqual(deleted.json(), {"id": base["id"], "deleted": True})
        self.assertEqual(
            self.client.get(f"/api/knowledge-bases/{base['id']}", params=scope).status_code,
            404,
        )
        self.assertEqual(
            self.client.get("/api/knowledge/search", params={**scope, "q": "AgentNexus"}).json()["matches"],
            [],
        )

    def test_visibility_isolates_private_and_workspace_knowledge_bases(self) -> None:
        private_base = self._create_base(visibility="private", name="Alice 私有资料")
        workspace_base = self._create_base(visibility="workspace", name="工作区资料")

        alice_visible = self.client.get(
            "/api/knowledge-bases", params=self._scope(user_id="alice", workspace_id="workspace-a")
        )
        bob_visible = self.client.get(
            "/api/knowledge-bases", params=self._scope(user_id="bob", workspace_id="workspace-a")
        )
        other_workspace_visible = self.client.get(
            "/api/knowledge-bases", params=self._scope(user_id="alice", workspace_id="workspace-b")
        )

        self.assertEqual(alice_visible.status_code, 200, alice_visible.text)
        self.assertEqual(
            {item["id"] for item in alice_visible.json()},
            {private_base["id"], workspace_base["id"]},
        )
        self.assertEqual(
            {item["id"] for item in bob_visible.json()},
            {workspace_base["id"]},
        )
        self.assertEqual(other_workspace_visible.json(), [])

        forbidden = self.client.get(
            f"/api/knowledge-bases/{private_base['id']}",
            params=self._scope(user_id="bob", workspace_id="workspace-a"),
        )
        self.assertEqual(forbidden.status_code, 404, forbidden.text)


class StubSkillRegistry:
    skill = {
        "id": "general_task",
        "name": "通用任务 Skill",
        "description": "用于知识库运行时注入测试。",
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
        answer = "已根据知识库资料完成回答。"
        if on_delta:
            pending = on_delta(answer)
            if inspect.isawaitable(pending):
                await pending
        return answer


class KnowledgeRuntimeIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.temp_path = Path(self.temp_dir.name)
        self.original_db_path = db.DB_PATH
        db.DB_PATH = self.temp_path / "knowledge-runtime.db"
        db.init_db()
        self.task_state = TaskStateService(db.get_conn)
        self.context_service = ContextService(db.get_conn)
        self.knowledge_service = KnowledgeBaseService(db.get_conn)

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
            knowledge_service=self.knowledge_service,
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

    async def test_runtime_retrieves_knowledge_emits_event_and_adds_plan_child(self) -> None:
        scope = ExecutionScope(
            "org-kb-runtime",
            "workspace-a",
            "alice",
            "general-agent",
            "conversation-kb-runtime",
        )
        base = self.knowledge_service.create_base(
            scope,
            name="平台资料库",
            description="运行时注入测试",
            visibility="workspace",
        )
        source_path = self.temp_path / "platform.md"
        source_path.write_text(
            "AgentNexus 知识库会在任务执行前检索项目资料，并要求回答保留文档名和片段编号。",
            encoding="utf-8",
        )
        self.knowledge_service.index_upload(
            base["id"],
            scope,
            upload={
                "id": "upl_runtime_kb",
                "name": "platform.md",
                "content_type": "text/markdown",
                "path": str(source_path),
            },
        )

        model = RecordingModelGateway()
        task = create_task_record(
            "请说明 AgentNexus 知识库如何使用来源片段",
            "general-agent",
            "workspace-a",
            conversation_id="conversation-kb-runtime",
            organization_id="org-kb-runtime",
            user_id="alice",
        )

        await self._runtime(model).run_task(task["id"])

        knowledge_events = self._event_payloads(task["id"], "knowledge")
        self.assertEqual(len(knowledge_events), 1)
        self.assertEqual(knowledge_events[0]["knowledge_base_ids"], [base["id"]])
        self.assertEqual(knowledge_events[0]["matches"][0]["document_name"], "platform.md")
        self.assertNotIn(str(source_path), str(knowledge_events))

        plans = self._event_payloads(task["id"], "plan")
        final_plan = plans[-1]["plan"]
        prepare = next(item for item in final_plan["nodes"] if item["id"] == "prepare")
        self.assertIn(
            ("knowledge:retrieval", "knowledge"),
            [(item["id"], item["kind"]) for item in prepare["children"]],
        )
        knowledge_progress = [
            item for item in self._event_payloads(task["id"], "plan_progress")
            if item.get("child_id") == "knowledge:retrieval"
        ]
        self.assertEqual(len(knowledge_progress), 1)
        self.assertEqual(knowledge_progress[0]["status"], "completed")
        self.assertEqual(len(model.solve_calls), 1)
        self.assertIn("platform.md", model.solve_calls[0]["prompt"])
        self.assertIn("片段", model.solve_calls[0]["prompt"])


if __name__ == "__main__":
    unittest.main()
