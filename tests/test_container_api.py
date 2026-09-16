from __future__ import annotations

import tempfile
import unittest
from unittest.mock import AsyncMock, patch
from pathlib import Path

from fastapi.testclient import TestClient

from app import db
from app import main as main_module
from app.seed import seed_agents


class ContainerApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test_api.db"
        self.orig_db_path = db.DB_PATH
        db.DB_PATH = self.db_path
        db.init_db()
        main_module.task_state.init_schema()
        seed_agents()
        self.runtime_patch = patch.object(main_module.runtime, "run_task", new_callable=AsyncMock)
        self.mock_run_task = self.runtime_patch.start()
        self.client = TestClient(main_module.app)

    def tearDown(self) -> None:
        self.runtime_patch.stop()
        db.DB_PATH = self.orig_db_path
        self.temp_dir.cleanup()

    def test_create_task_with_container_engine(self) -> None:
        resp = self.client.post(
            "/api/tasks",
            json={
                "message": "echo 'Testing API container engine' > /workspace/artifacts/test.txt",
                "agent_id": "general-agent",
                "workspace": "api-test-ws",
                "execution_engine": "container",
            },
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertIn("id", data)
        task_id = data["id"]

        task_row = db.query_one("SELECT * FROM tasks WHERE id = ?", (task_id,))
        self.assertIsNotNone(task_row)
        self.assertEqual(task_row["execution_engine"], "container")


if __name__ == "__main__":
    unittest.main()
