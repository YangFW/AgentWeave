from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from app import db
from app import main as main_module


class ConversationSummaryApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_db_path = db.DB_PATH
        self.original_app_db_path = os.environ.get("APP_DB_PATH")
        self.db_path = Path(self.temp_dir.name) / "conversation-summary-api.db"
        os.environ["APP_DB_PATH"] = str(self.db_path)
        db.DB_PATH = self.db_path
        db.init_db()

        self.patches = (
            patch.object(main_module.loop_scheduler, "start", return_value=None),
            patch.object(main_module.loop_scheduler, "stop", new_callable=AsyncMock),
            patch.object(main_module.skill_registry, "load_builtin_skills", return_value=None),
            patch.object(main_module.mcp_gateway, "seed_builtin_servers", return_value=None),
            patch.object(main_module, "seed_agents", return_value=None),
            patch.object(main_module, "_recover_interrupted_runs", return_value=[]),
        )
        for active_patch in self.patches:
            active_patch.start()
        self.client_context = TestClient(main_module.app)
        self.client = self.client_context.__enter__()

    def tearDown(self) -> None:
        self.client_context.__exit__(None, None, None)
        for active_patch in reversed(self.patches):
            active_patch.stop()
        db.DB_PATH = self.original_db_path
        if self.original_app_db_path is None:
            os.environ.pop("APP_DB_PATH", None)
        else:
            os.environ["APP_DB_PATH"] = self.original_app_db_path
        self.temp_dir.cleanup()

    @staticmethod
    def _scope(*, user_id: str = "alice", workspace_id: str = "workspace-a") -> dict[str, str]:
        return {
            "organization_id": "org-a",
            "workspace_id": workspace_id,
            "user_id": user_id,
        }

    def test_summary_crud_is_visible_versioned_and_scope_bound(self) -> None:
        conversation_id = "conv-summary-api"
        created = self.client.put(
            f"/api/conversation-summaries/{conversation_id}",
            params=self._scope(),
            json={
                "summary": "用户正在整理平台验收材料。",
                "preserved_constraints": ["必须使用中文", "不要调用天气工具"],
                "through_task_id": "task-1",
                "model_id": "deterministic-compactor",
            },
        )
        self.assertEqual(created.status_code, 200, created.text)
        self.assertEqual(created.json()["version"], 1)
        self.assertEqual(created.json()["conversation_id"], conversation_id)

        listed = self.client.get("/api/conversation-summaries", params=self._scope())
        self.assertEqual(listed.status_code, 200, listed.text)
        self.assertEqual([item["conversation_id"] for item in listed.json()], [conversation_id])

        fetched = self.client.get(
            f"/api/conversation-summaries/{conversation_id}", params=self._scope()
        )
        self.assertEqual(fetched.status_code, 200, fetched.text)
        self.assertEqual(fetched.json(), created.json())

        updated = self.client.put(
            f"/api/conversation-summaries/{conversation_id}",
            params=self._scope(),
            json={
                "summary": "用户正在整理平台验收材料，并要求生成 Word。",
                "preserved_constraints": ["必须使用中文", "最终输出 Word"],
                "through_task_id": "task-2",
                "model_id": "manual-editor",
            },
        )
        self.assertEqual(updated.status_code, 200, updated.text)
        self.assertEqual(updated.json()["version"], 2)
        self.assertEqual(updated.json()["created_at"], created.json()["created_at"])

        invisible = self.client.get(
            f"/api/conversation-summaries/{conversation_id}",
            params=self._scope(user_id="bob"),
        )
        self.assertEqual(invisible.status_code, 404, invisible.text)
        conflict = self.client.put(
            f"/api/conversation-summaries/{conversation_id}",
            params=self._scope(user_id="bob"),
            json={"summary": "Bob 不能覆盖 Alice 的摘要。"},
        )
        self.assertEqual(conflict.status_code, 409, conflict.text)

        deleted = self.client.delete(
            f"/api/conversation-summaries/{conversation_id}", params=self._scope()
        )
        self.assertEqual(deleted.status_code, 200, deleted.text)
        self.assertEqual(deleted.json(), {"conversation_id": conversation_id, "deleted": True})
        self.assertEqual(
            self.client.get(
                f"/api/conversation-summaries/{conversation_id}", params=self._scope()
            ).status_code,
            404,
        )

    def test_summary_listing_isolates_workspaces_and_users(self) -> None:
        service = main_module.runtime.conversation_summary_service
        service.upsert(
            {**self._scope(), "conversation_id": "conv-alice-a"},
            summary="Alice A",
        )
        service.upsert(
            {**self._scope(workspace_id="workspace-b"), "conversation_id": "conv-alice-b"},
            summary="Alice B",
        )
        service.upsert(
            {**self._scope(user_id="bob"), "conversation_id": "conv-bob-a"},
            summary="Bob A",
        )

        alice_a = self.client.get(
            "/api/conversation-summaries", params=self._scope()
        ).json()
        alice_b = self.client.get(
            "/api/conversation-summaries", params=self._scope(workspace_id="workspace-b")
        ).json()
        bob_a = self.client.get(
            "/api/conversation-summaries", params=self._scope(user_id="bob")
        ).json()
        self.assertEqual([item["conversation_id"] for item in alice_a], ["conv-alice-a"])
        self.assertEqual([item["conversation_id"] for item in alice_b], ["conv-alice-b"])
        self.assertEqual([item["conversation_id"] for item in bob_a], ["conv-bob-a"])


if __name__ == "__main__":
    unittest.main()
