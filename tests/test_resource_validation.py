from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from app import db
from app import main as main_module


class ResourceValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_db_path = db.DB_PATH
        self.original_app_db_path = os.environ.get("APP_DB_PATH")
        self.db_path = Path(self.temp_dir.name) / "validation.db"
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

    def test_resource_ids_reject_route_breaking_characters(self) -> None:
        payloads = (
            ("/api/skills", {"id": "a/b", "name": "Bad", "description": "", "content": "# Bad"}),
            ("/api/mcp", {"id": "a/b", "name": "Bad", "kind": "http"}),
            ("/api/agents", {"id": "a/b", "name": "Bad"}),
            ("/api/models", {"id": "a/b", "name": "Bad", "model": "demo", "api_key_env": "DEMO_KEY"}),
        )
        for endpoint, payload in payloads:
            with self.subTest(endpoint=endpoint):
                self.assertEqual(self.client.post(endpoint, json=payload).status_code, 422)

    def test_unknown_mcp_kind_and_model_provider_are_rejected(self) -> None:
        self.assertEqual(
            self.client.post("/api/mcp", json={"id": "bad-kind", "name": "Bad", "kind": "mystery"}).status_code,
            422,
        )
        self.assertEqual(
            self.client.post(
                "/api/models",
                json={"id": "bad-provider", "name": "Bad", "provider": "mystery", "model": "demo", "api_key_env": "DEMO_KEY"},
            ).status_code,
            422,
        )

    def test_mcp_import_uses_the_same_validation(self) -> None:
        response = self.client.post(
            "/api/mcp/import",
            files={"file": ("mcp.json", json.dumps({"id": "a/b", "name": "Bad", "kind": "http"}), "application/json")},
        )
        self.assertEqual(response.status_code, 400, response.text)
        self.assertIsNone(db.query_one("SELECT id FROM mcp_servers WHERE id = ?", ("a/b",)))

    def test_agent_and_task_reject_missing_bindings(self) -> None:
        agent = self.client.post(
            "/api/agents",
            json={"id": "broken-agent", "name": "Broken", "skills": ["missing-skill"]},
        )
        self.assertEqual(agent.status_code, 400, agent.text)
        task = self.client.post("/api/tasks", json={"message": "hello", "agent_id": "missing-agent"})
        self.assertEqual(task.status_code, 400, task.text)

    def test_model_base_url_must_be_http_or_https(self) -> None:
        response = self.client.post(
            "/api/models",
            json={
                "id": "bad-url",
                "name": "Bad URL",
                "model": "demo",
                "base_url": "file:///etc/passwd",
                "api_key_env": "DEMO_KEY",
            },
        )
        self.assertEqual(response.status_code, 422, response.text)


if __name__ == "__main__":
    unittest.main()
