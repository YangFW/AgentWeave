from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from app import db
from app import main as main_module
from app.services.agent_runtime import create_task_record


class WorkspaceApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_db_path = db.DB_PATH
        self.original_app_db_path = os.environ.get("APP_DB_PATH")
        self.db_path = Path(self.temp_dir.name) / "workspace-api.db"
        os.environ["APP_DB_PATH"] = str(self.db_path)
        db.DB_PATH = self.db_path
        db.init_db()

        self.patches = [
            patch.object(main_module.loop_scheduler, "start", return_value=None),
            patch.object(main_module.loop_scheduler, "stop", new_callable=AsyncMock),
            patch.object(main_module.skill_registry, "load_builtin_skills", return_value=None),
            patch.object(main_module.mcp_gateway, "seed_builtin_servers", return_value=None),
            patch.object(main_module, "seed_agents", return_value=None),
            patch.object(main_module, "_recover_interrupted_runs", return_value=[]),
            patch.object(main_module.runtime, "run_task", new_callable=AsyncMock),
        ]
        for item in self.patches:
            item.start()
        self.client_context = TestClient(main_module.app)
        self.client = self.client_context.__enter__()

    def tearDown(self) -> None:
        self.client_context.__exit__(None, None, None)
        for item in reversed(self.patches):
            item.stop()
        db.DB_PATH = self.original_db_path
        if self.original_app_db_path is None:
            os.environ.pop("APP_DB_PATH", None)
        else:
            os.environ["APP_DB_PATH"] = self.original_app_db_path
        self.temp_dir.cleanup()

    def test_default_workspace_exists_and_cannot_be_deleted(self) -> None:
        listed = self.client.get("/api/workspaces")
        self.assertEqual(listed.status_code, 200, listed.text)
        default = next(item for item in listed.json() if item["id"] == "default")
        self.assertEqual(default["name"], "默认项目")
        self.assertTrue(default["enabled"])

        deleted = self.client.delete("/api/workspaces/default")

        self.assertEqual(deleted.status_code, 400, deleted.text)
        self.assertIn("默认项目不能删除", deleted.text)

    def test_workspace_crud_soft_delete_and_task_filtering(self) -> None:
        created = self.client.post(
            "/api/workspaces",
            json={
                "id": "project-alpha",
                "name": "Alpha 项目",
                "description": "项目级隔离测试",
                "organization_id": "local-org",
                "user_id": "alice",
                "default_agent_id": "general-agent",
                "default_model_id": "deterministic",
                "settings": {"web_search": False},
            },
        )
        self.assertEqual(created.status_code, 201, created.text)
        self.assertEqual(created.json()["settings"], {"web_search": False})

        updated = self.client.put(
            "/api/workspaces/project-alpha",
            json={
                "name": "Alpha 项目更新",
                "description": "已更新",
                "default_agent_id": "general-agent",
                "default_model_id": "deterministic",
                "settings": {"web_search": True},
                "enabled": True,
            },
        )
        self.assertEqual(updated.status_code, 200, updated.text)
        self.assertEqual(updated.json()["name"], "Alpha 项目更新")
        self.assertEqual(updated.json()["settings"], {"web_search": True})

        alpha_task = create_task_record(
            "Alpha 任务",
            "general-agent",
            workspace="project-alpha",
            organization_id="local-org",
            user_id="alice",
        )
        default_task = create_task_record(
            "Default 任务",
            "general-agent",
            workspace="default",
            organization_id="local-org",
            user_id="alice",
        )

        alpha_list = self.client.get(
            "/api/tasks",
            params={"organization_id": "local-org", "user_id": "alice", "workspace_id": "project-alpha"},
        )
        default_list = self.client.get(
            "/api/tasks",
            params={"organization_id": "local-org", "user_id": "alice", "workspace_id": "default"},
        )
        self.assertEqual(alpha_list.status_code, 200, alpha_list.text)
        self.assertEqual(default_list.status_code, 200, default_list.text)
        self.assertEqual([item["id"] for item in alpha_list.json()], [alpha_task["id"]])
        self.assertEqual([item["id"] for item in default_list.json()], [default_task["id"]])

        deleted = self.client.delete("/api/workspaces/project-alpha")
        self.assertEqual(deleted.status_code, 200, deleted.text)
        self.assertEqual(deleted.json()["soft_deleted"], True)

        active_only = self.client.get("/api/workspaces")
        self.assertNotIn("project-alpha", {item["id"] for item in active_only.json()})
        with_disabled = self.client.get("/api/workspaces", params={"include_disabled": True})
        disabled = next(item for item in with_disabled.json() if item["id"] == "project-alpha")
        self.assertFalse(disabled["enabled"])


if __name__ == "__main__":
    unittest.main()
