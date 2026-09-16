from __future__ import annotations

import tempfile
import unittest
from unittest.mock import AsyncMock, patch
import os
from app.services import auth_service
from pathlib import Path

from app import db
from app.services.agent_runtime import AgentRuntime, create_task_record
from app.services.mcp_gateway import McpGateway
from app.services.model_gateway import ModelGateway
from app.services.skill_registry import SkillRegistry


class ChatInstallTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.original_db_path = db.DB_PATH
        self.temp_dir = tempfile.TemporaryDirectory()
        db.DB_PATH = Path(self.temp_dir.name) / "test.db"
        db.init_db()

    async def asyncTearDown(self) -> None:
        db.DB_PATH = self.original_db_path
        self.temp_dir.cleanup()

    async def test_legacy_mutating_install_callbacks_are_rejected(self) -> None:
        async def unsafe_install(url: str):
            return {"id": "already-mutated"}

        with self.assertRaisesRegex(ValueError, "must use skill_url_loader"):
            AgentRuntime(
                SkillRegistry(),
                McpGateway(),
                ModelGateway(),
                skill_url_installer=unsafe_install,
            )

    async def test_member_cannot_install_platform_components_through_chat(self):
        with patch.dict(os.environ, {'APP_AUTH_ENABLED':'true', 'APP_ADMIN_PASSWORD':'', 'APP_USER_PASSWORD':''}):
            auth_service.init_schema()
            member = auth_service.create_user('member', 'example-password')
            loader = AsyncMock()
            runtime = AgentRuntime(SkillRegistry(), McpGateway(), ModelGateway(), skill_url_loader=loader, mcp_url_loader=loader)
            for message in ('安装 Skill https://example.com/SKILL.md', '安装 MCP https://example.com/mcp.json'):
                task = create_task_record(message, 'general-agent', user_id=member['id'])
                with self.assertRaises(PermissionError):
                    await runtime._try_platform_command(task)
            loader.assert_not_awaited()
            self.assertFalse(runtime._can_manage_platform(task))

    async def test_role_revoked_during_download_prevents_transactional_install(self):
        with patch.dict(os.environ, {'APP_AUTH_ENABLED':'true', 'APP_ADMIN_PASSWORD':'', 'APP_USER_PASSWORD':''}):
            auth_service.init_schema()
            admin = auth_service.create_user('admin-test','example-password',role='admin')
            async def download(url):
                db.execute("UPDATE users SET role='user' WHERE id=?", (admin['id'],))
                return {'files':{'SKILL.md':b'---\nid: revoked_skill\nname: Revoked\ndescription: Test\n---\nTest'},'fallback_id':'revoked_skill'}
            registry = SkillRegistry()
            runtime = AgentRuntime(registry,McpGateway(),ModelGateway(),skill_url_loader=download)
            task = create_task_record('安装 Skill https://example.com/SKILL.md','general-agent',user_id=admin['id'])
            await runtime.run_task(task['id'])
            self.assertIsNone(registry.get_skill('revoked_skill'))
            self.assertEqual(db.query_one('SELECT status FROM tasks WHERE id=?',(task['id'],))['status'],'failed')

    async def test_chat_installs_skill_from_market_url(self) -> None:
        seen: list[str] = []

        async def load(url: str):
            seen.append(url)
            return {
                "files": {
                    "SKILL.md": (
                        b"---\nid: demo_skill\nname: Demo Skill\nversion: 1.0.0\n"
                        b"---\nUse this test skill.\n"
                    ),
                    "references/guide.md": b"# Guide\n",
                },
                "fallback_id": "demo_skill",
            }

        registry = SkillRegistry()
        runtime = AgentRuntime(
            registry,
            McpGateway(),
            ModelGateway(),
            skill_url_loader=load,
        )
        task = create_task_record("安装 Skill https://example.com/demo/SKILL.md。", "general-agent")
        await runtime.run_task(task["id"])
        self.assertEqual(seen, ["https://example.com/demo/SKILL.md"])
        stored = db.query_one("SELECT status, result_json FROM tasks WHERE id = ?", (task["id"],))
        self.assertEqual(stored["status"], "completed")
        self.assertTrue(db.json_loads(stored["result_json"], {})["installed"])
        run = runtime.task_state.list_runs(task_id=task["id"])[0]
        self.assertEqual(run["status"], "completed")
        self.assertEqual(run["intake_state"], "closed")
        self.assertEqual(registry.get_skill("demo_skill")["file_count"], 2)
        runtime.task_state.assert_terminal_clean(
            task_id=task["id"], run_id=run["id"]
        )

    async def test_chat_installs_mcp_from_market_url(self) -> None:
        seen: list[str] = []

        async def load(url: str):
            seen.append(url)
            return [{"id": "demo-mcp", "name": "Demo MCP"}]

        gateway = McpGateway()
        runtime = AgentRuntime(
            SkillRegistry(),
            gateway,
            ModelGateway(),
            mcp_url_loader=load,
        )
        task = create_task_record("安装 MCP https://example.com/mcp.json", "general-agent")
        await runtime.run_task(task["id"])
        self.assertEqual(seen, ["https://example.com/mcp.json"])
        stored = db.query_one("SELECT status, result_json FROM tasks WHERE id = ?", (task["id"],))
        self.assertEqual(stored["status"], "completed")
        self.assertEqual(db.json_loads(stored["result_json"], {})["type"], "mcp")
        run = runtime.task_state.list_runs(task_id=task["id"])[0]
        self.assertEqual(run["status"], "completed")
        self.assertEqual(run["intake_state"], "closed")
        self.assertEqual(gateway.get_server("demo-mcp")["name"], "Demo MCP")
        runtime.task_state.assert_terminal_clean(
            task_id=task["id"], run_id=run["id"]
        )

    async def test_inline_mcp_json_with_base_url_is_not_treated_as_market_url(self) -> None:
        seen: list[str] = []

        async def load(url: str):
            seen.append(url)
            return []

        runtime = AgentRuntime(
            SkillRegistry(),
            McpGateway(),
            ModelGateway(),
            mcp_url_loader=load,
        )
        task = create_task_record(
            '安装 MCP\n```json\n{"mcpServers":{"inline-http":{"base_url":"https://example.invalid"}}}\n```',
            "general-agent",
        )
        await runtime.run_task(task["id"])
        self.assertEqual(seen, [])
        stored = db.query_one("SELECT status, result_json FROM tasks WHERE id = ?", (task["id"],))
        result = db.json_loads(stored["result_json"], {})
        self.assertEqual(stored["status"], "completed")
        self.assertEqual(result["mcp_servers"][0]["id"], "inline-http")
        run = runtime.task_state.list_runs(task_id=task["id"])[0]
        self.assertEqual(run["status"], "completed")
        self.assertEqual(run["intake_state"], "closed")
        runtime.task_state.assert_terminal_clean(
            task_id=task["id"], run_id=run["id"]
        )


if __name__ == "__main__":
    unittest.main()
