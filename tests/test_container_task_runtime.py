from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

from app import db
from app.services.agent_runtime import AgentRuntime, create_task_record
from app.services.mcp_gateway import McpGateway
from app.services.model_gateway import ModelGateway
from app.services.skill_registry import SkillRegistry
from app.services.task_state import TaskStateService


class ContainerTaskRuntimeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test_platform.db"
        self.orig_db_path = db.DB_PATH
        db.DB_PATH = self.db_path
        db.init_db()
        self.task_state = TaskStateService(db.get_conn)
        self.task_state.init_schema()
        self.runtime = AgentRuntime(
            SkillRegistry(),
            McpGateway(),
            ModelGateway(),
            task_state=self.task_state,
        )

    def tearDown(self) -> None:
        db.DB_PATH = self.orig_db_path
        self.temp_dir.cleanup()

    async def test_container_engine_task_lifecycle(self) -> None:
        task = create_task_record(
            message="echo 'AgentNexus Container Sandbox Works!' > /workspace/artifacts/result.txt",
            agent_id="general-agent",
            workspace="sandbox-test-ws",
            execution_engine="container",
        )
        task_id = task["id"]

        await self.runtime.run_task(task_id)

        updated_task = db.query_one("SELECT * FROM tasks WHERE id = ?", (task_id,))
        self.assertIsNotNone(updated_task)
        self.assertEqual(updated_task["status"], "completed")
        self.assertEqual(updated_task["execution_engine"], "container")

        artifacts = db.json_loads(updated_task["artifacts_json"], [])
        self.assertTrue(any(a["name"] == "result.txt" for a in artifacts))

        events = db.query_all("SELECT type, title FROM task_events WHERE task_id = ?", (task_id,))
        event_types = [e["type"] for e in events]
        self.assertIn("stage_started", event_types)
        self.assertIn("stage_completed", event_types)
        self.assertIn("task_completed", event_types)

    async def test_container_engine_failure_handling(self) -> None:
        task = create_task_record(
            message="exit 42",
            agent_id="general-agent",
            workspace="sandbox-fail-ws",
            execution_engine="container",
        )
        task_id = task["id"]

        await self.runtime.run_task(task_id)

        updated_task = db.query_one("SELECT * FROM tasks WHERE id = ?", (task_id,))
        self.assertEqual(updated_task["status"], "failed")
        events = db.query_all("SELECT type, title FROM task_events WHERE task_id = ?", (task_id,))
        event_types = [e["type"] for e in events]
        self.assertIn("stage_failed", event_types)
        self.assertIn("task_failed", event_types)


if __name__ == "__main__":
    unittest.main()
