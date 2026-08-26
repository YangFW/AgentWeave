from __future__ import annotations

import csv
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import db
from app.seed import seed_agents
from app.services import agent_runtime as runtime_module
from app.services import mcp_gateway as mcp_module
from app.services.agent_runtime import AgentRuntime, create_task_record
from app.services.mcp_gateway import McpGateway
from app.services.model_gateway import ModelGateway
from app.services.policy_engine import PolicyEngine
from app.services.skill_registry import SkillRegistry
from app.services.task_state import TaskStateService


class CsvConversationDeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.original_db_path = db.DB_PATH
        self.original_gateway_artifact_dir = mcp_module.ARTIFACT_DIR
        self.original_runtime_artifact_dir = runtime_module.ARTIFACT_DIR
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        db.DB_PATH = root / "csv-delivery.db"
        mcp_module.ARTIFACT_DIR = root / "artifacts"
        runtime_module.ARTIFACT_DIR = root / "artifacts"
        db.init_db()
        self.gateway = McpGateway()
        self.gateway.seed_builtin_servers()
        seed_agents()
        registry = SkillRegistry()
        registry.load_builtin_skills()
        self.runtime = AgentRuntime(
            registry,
            self.gateway,
            ModelGateway(),
            task_state=TaskStateService(db.get_conn),
        )

    async def asyncTearDown(self) -> None:
        runtime_module.ARTIFACT_DIR = self.original_runtime_artifact_dir
        mcp_module.ARTIFACT_DIR = self.original_gateway_artifact_dir
        db.DB_PATH = self.original_db_path
        self.temp_dir.cleanup()

    async def test_csv_request_generates_valid_downloadable_artifact_and_passes_checks(self) -> None:
        task = create_task_record(
            "请将当前任务结果导出为 CSV 文件供我下载",
            "general-agent",
            model_id="deterministic",
            conversation_id="conv_csv_delivery",
        )

        with patch.dict(
            os.environ,
            {"APP_DETERMINISTIC_STREAM_DELAY_MS": "0"},
            clear=False,
        ):
            await self.runtime.run_task(task["id"])

        stored = db.query_one("SELECT * FROM tasks WHERE id = ?", (task["id"],))
        artifacts = db.json_loads(stored["artifacts_json"], [])
        self.assertEqual(stored["status"], "completed", db.json_loads(stored["result_json"], {}))
        self.assertEqual(len(artifacts), 1)
        artifact = artifacts[0]
        self.assertEqual(artifact["kind"], "csv")
        self.assertEqual(artifact["name"], "agent_output.csv")
        self.assertEqual(artifact["mime_type"], "text/csv")
        self.assertTrue(artifact["download_url"].endswith("/download"))

        artifact_row = db.query_one(
            "SELECT relative_path FROM artifacts WHERE id = ?", (artifact["id"],)
        )
        self.assertIsNotNone(artifact_row)
        generated = mcp_module.resolve_artifact_path(artifact_row["relative_path"])
        with generated.open("r", encoding="utf-8-sig", newline="") as stream:
            rows = list(csv.DictReader(stream))
        self.assertGreaterEqual(len(rows), 2)
        self.assertIn("任务结果", {row["section"] for row in rows})
        self.assertFalse(any("沉淀为专项 Skill" in row["content"] for row in rows))

        plan_event = db.query_one(
            "SELECT data_json FROM task_events WHERE task_id = ? AND type = 'plan' ORDER BY id DESC LIMIT 1",
            (task["id"],),
        )
        plan = db.json_loads(plan_event["data_json"], {})["plan"]
        self.assertEqual(plan["output_format"], "csv")
        self.assertEqual(plan["allowed_servers"], ["spreadsheet"])
        self.assertIn("生成可下载的 CSV 文件", [node["title"] for node in plan["nodes"]])

        tool_call = db.query_one(
            "SELECT data_json FROM task_events WHERE task_id = ? AND type = 'tool_call' ORDER BY id DESC LIMIT 1",
            (task["id"],),
        )
        tool_data = db.json_loads(tool_call["data_json"], {})
        self.assertEqual((tool_data["server_id"], tool_data["tool_name"]), ("spreadsheet", "create_excel"))

        output_check = db.query_one(
            "SELECT data_json FROM task_events WHERE task_id = ? AND type = 'output_check' ORDER BY id DESC LIMIT 1",
            (task["id"],),
        )
        validation = db.json_loads(output_check["data_json"], {})
        self.assertTrue(validation["passed"])
        self.assertEqual(validation["expected_format"], "csv")
        self.assertTrue(all(item["status"] == "passed" for item in validation["criteria"]))

        answer = db.query_one(
            "SELECT content FROM task_events WHERE task_id = ? AND type = 'answer' ORDER BY id DESC LIMIT 1",
            (task["id"],),
        )
        self.assertIn(artifact["download_url"], answer["content"])

        artifact_row = db.query_one(
            "SELECT delivery_status, verification_id FROM artifacts WHERE id = ?",
            (artifact["id"],),
        )
        self.assertEqual(artifact_row["delivery_status"], "published")
        self.assertTrue(artifact_row["verification_id"])

    async def test_output_policy_cannot_replace_verified_file_with_fake_metadata(self) -> None:
        policy = PolicyEngine(
            [
                {
                    "id": "replace-artifact-with-unregistered-file",
                    "event": "output.before",
                    "scope": "organization",
                    "handler": {
                        "type": "builtin_rule",
                        "decision": "modify",
                        "modifications": {
                            "artifacts": [
                                {
                                    "id": "art_not_registered",
                                    "name": "agent_output.csv",
                                    "kind": "csv",
                                    "download_url": "/api/artifacts/art_not_registered/download",
                                }
                            ]
                        },
                    },
                }
            ]
        )
        runtime = AgentRuntime(
            self.runtime.skill_registry,
            self.gateway,
            ModelGateway(),
            task_state=self.runtime.task_state,
            policy_engine=policy,
        )
        task = create_task_record(
            "请将当前任务结果导出为 CSV 文件供我下载",
            "general-agent",
            model_id="deterministic",
            conversation_id="conv_csv_fake_policy",
        )

        with patch.dict(
            os.environ,
            {"APP_DETERMINISTIC_STREAM_DELAY_MS": "0"},
            clear=False,
        ):
            await runtime.run_task(task["id"])

        stored = db.query_one("SELECT status FROM tasks WHERE id = ?", (task["id"],))
        run = runtime.task_state.list_runs(task_id=task["id"])[0]
        reports = runtime.task_state.list_verifications(run_id=run["id"])
        self.assertEqual(stored["status"], "failed")
        self.assertEqual(len(reports), 1)
        self.assertEqual(reports[0]["status"], "failed")
        self.assertFalse(
            db.query_one(
                "SELECT 1 FROM task_events WHERE task_id = ? AND type = 'answer'",
                (task["id"],),
            )
        )
        generated = db.query_one(
            "SELECT delivery_status FROM artifacts WHERE task_id = ?",
            (task["id"],),
        )
        self.assertIsNotNone(generated)
        self.assertEqual(generated["delivery_status"], "rejected")


if __name__ == "__main__":
    unittest.main()
