from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app import db
from app.seed import seed_agents
from app.services.model_defaults import configured_default_model_id
from app.services.workspace_service import WorkspaceService


class ModelDefaultSelectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.original_db_path = db.DB_PATH
        self.temp_dir = tempfile.TemporaryDirectory()
        db.DB_PATH = Path(self.temp_dir.name) / "model-defaults.db"
        db.init_db()

    def tearDown(self) -> None:
        db.DB_PATH = self.original_db_path
        self.temp_dir.cleanup()

    def _insert_model(self, model_id: str, *, status: str = "", credential: str = "") -> None:
        now = db.utc_now()
        db.execute(
            """
            INSERT INTO model_configs(
                id, name, provider, model, base_url, api_key_env,
                api_key_ciphertext, enabled, last_test_status, created_at, updated_at
            ) VALUES (?, ?, 'openai_compatible', ?, 'https://example.invalid/v1', '', ?, 1, ?, ?, ?)
            """,
            (model_id, model_id, model_id, credential, status, now, now),
        )

    def test_unconfigured_install_keeps_offline_fallback(self) -> None:
        self.assertEqual(configured_default_model_id(), "deterministic")

    def test_seeded_agent_uses_tested_configured_model(self) -> None:
        self._insert_model("online-model", status="pass", credential="encrypted")

        seed_agents()

        general = db.query_one("SELECT model FROM agents WHERE id = 'general-agent'")
        self.assertEqual(general["model"], "online-model")

    def test_new_default_workspace_uses_tested_configured_model(self) -> None:
        self._insert_model("online-model", status="pass", credential="encrypted")

        service = WorkspaceService()

        workspace = service.get_workspace("default", {"organization_id": "local-org", "user_id": "local-user"})
        self.assertIsNotNone(workspace)
        self.assertEqual(workspace["default_model_id"], "online-model")


if __name__ == "__main__":
    unittest.main()
