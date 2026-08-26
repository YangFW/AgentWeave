from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from app import db
from app import main as main_module


class MarketplaceApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_db_path = db.DB_PATH
        self.original_app_db_path = os.environ.get("APP_DB_PATH")
        self.db_path = Path(self.temp_dir.name) / "marketplace-api.db"
        os.environ["APP_DB_PATH"] = str(self.db_path)
        db.DB_PATH = self.db_path
        db.init_db()

        self.patches = [
            patch.object(main_module.loop_scheduler, "start", return_value=None),
            patch.object(main_module.loop_scheduler, "stop", new_callable=AsyncMock),
            patch.object(main_module.skill_registry, "load_builtin_skills", return_value=None),
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

    def test_marketplace_lists_recommendations(self) -> None:
        response = self.client.get("/api/marketplace")
        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertIn("skills", payload)
        self.assertIn("mcp_servers", payload)
        skill_ids = {item["id"] for item in payload["skills"]}
        mcp_ids = {item["id"] for item in payload["mcp_servers"]}
        self.assertTrue({"mermaid_diagram", "product_requirement_document"}.issubset(skill_ids))
        self.assertTrue({"weather", "web-search", "spreadsheet", "report"}.issubset(mcp_ids))
        mermaid = next(item for item in payload["skills"] if item["id"] == "mermaid_diagram")
        self.assertEqual(mermaid["install_plan"]["method"], "builtin_catalog")
        self.assertFalse(mermaid["install_plan"]["permissions"]["runs_local_process"])
        self.assertIn("技能中心", mermaid["install_plan"]["post_install"])

        web_search = next(item for item in payload["mcp_servers"] if item["id"] == "web-search")
        self.assertEqual(web_search["install_plan"]["method"], "builtin_mcp")
        self.assertTrue(web_search["install_plan"]["permissions"]["uses_network"])
        self.assertIn("search", web_search["install_plan"]["tools"])

        report = next(item for item in payload["mcp_servers"] if item["id"] == "report")
        self.assertTrue(report["install_plan"]["permissions"]["writes_artifacts"])
        self.assertIn("工具接入", report["install_plan"]["post_install"])

    def test_marketplace_install_and_enable_are_idempotent(self) -> None:
        installed_skill = self.client.post("/api/marketplace/skills/mermaid_diagram/install")
        self.assertEqual(installed_skill.status_code, 200, installed_skill.text)
        self.assertEqual(installed_skill.json()["id"], "mermaid_diagram")
        self.assertTrue(installed_skill.json()["enabled"])

        stored_skill = self.client.get("/api/skills/mermaid_diagram")
        self.assertEqual(stored_skill.status_code, 200, stored_skill.text)
        self.assertTrue(stored_skill.json()["enabled"])

        db.execute("UPDATE mcp_servers SET enabled = 0 WHERE id = 'weather'")
        disabled = self.client.get("/api/mcp/weather")
        self.assertFalse(disabled.json()["enabled"])

        enabled = self.client.post("/api/marketplace/mcp/weather/enable")
        self.assertEqual(enabled.status_code, 200, enabled.text)
        self.assertTrue(enabled.json()["enabled"])


if __name__ == "__main__":
    unittest.main()
