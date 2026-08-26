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
from app.services.mcp_gateway import MCP_SECRET_PLACEHOLDER, McpGateway, ToolError


class McpApiSecurityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_db_path = db.DB_PATH
        self.original_app_db_path = os.environ.get("APP_DB_PATH")
        self.original_policy_rules = list(main_module.policy_engine._rules)
        self.db_path = Path(self.temp_dir.name) / "mcp-api-security.db"
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
        main_module.policy_engine.set_rules([])
        self.client_context = TestClient(main_module.app)
        self.client = self.client_context.__enter__()

    def tearDown(self) -> None:
        self.client_context.__exit__(None, None, None)
        for active_patch in reversed(self.patches):
            active_patch.stop()
        main_module.policy_engine.set_rules(self.original_policy_rules)
        db.DB_PATH = self.original_db_path
        if self.original_app_db_path is None:
            os.environ.pop("APP_DB_PATH", None)
        else:
            os.environ["APP_DB_PATH"] = self.original_app_db_path
        self.temp_dir.cleanup()

    def test_crud_and_import_recursively_redact_secrets_without_losing_them(self) -> None:
        create_payload = {
            "id": "secure-remote",
            "name": "Secure Remote",
            "kind": "mcp_http",
            "config": {
                "url": "https://mcp.example.invalid/api",
                "headers": {
                    "Authorization": "Bearer top-secret",
                    "X-Trace": "trace-secret",
                },
                "timeout": 30,
                "nested": {
                    "password": "db-password",
                    "items": [{"api_key": "nested-key", "label": "visible-label"}],
                },
            },
            "tools": [],
        }
        created = self.client.post("/api/mcp", json=create_payload)
        self.assertEqual(created.status_code, 200, created.text)
        self._assert_public_secret_shape(created)

        listed = self.client.get("/api/mcp")
        self.assertEqual(listed.status_code, 200, listed.text)
        self.assertEqual(len(listed.json()), 1)
        self._assert_public_secret_shape(listed)

        fetched = self.client.get("/api/mcp/secure-remote")
        self.assertEqual(fetched.status_code, 200, fetched.text)
        public = fetched.json()
        self._assert_public_secret_shape(fetched)
        self.assertTrue(public["has_secret"])
        self.assertEqual(public["secret_placeholder"], MCP_SECRET_PLACEHOLDER)
        self.assertEqual(public["config"]["headers"]["X-Trace"], MCP_SECRET_PLACEHOLDER)
        self.assertEqual(public["config"]["timeout"], 30)

        public["name"] = "Secure Remote Updated"
        public["config"]["timeout"] = 45
        updated = self.client.put("/api/mcp/secure-remote", json=public)
        self.assertEqual(updated.status_code, 200, updated.text)
        self._assert_public_secret_shape(updated)

        stored = db.json_loads(
            db.query_one(
                "SELECT config_json FROM mcp_servers WHERE id = ?", ("secure-remote",)
            )["config_json"],
            {},
        )
        self.assertEqual(stored["headers"]["Authorization"], "Bearer top-secret")
        self.assertEqual(stored["nested"]["password"], "db-password")
        self.assertEqual(stored["nested"]["items"][0]["api_key"], "nested-key")
        self.assertEqual(stored["headers"]["X-Trace"], "trace-secret")
        self.assertEqual(stored["timeout"], 45)

        imported = self.client.post(
            "/api/mcp/import",
            files={
                "file": (
                    "mcp.json",
                    json.dumps(
                        {
                            "mcpServers": {
                                "imported-secure": {
                                    "url": "https://example.invalid/mcp",
                                    "headers": {"Cookie": "session=secret"},
                                    "env": {"SERVICE_TOKEN": "token-value"},
                                }
                            }
                        }
                    ),
                    "application/json",
                )
            },
        )
        self.assertEqual(imported.status_code, 200, imported.text)
        self._assert_public_secret_shape(imported)
        self.assertTrue(imported.json()[0]["has_secret"])

    def test_direct_invoke_allows_read_only_and_rejects_writes_or_unmarked_tools(self) -> None:
        servers = [
            {
                "id": "builtin-test",
                "name": "Builtin Test",
                "kind": "builtin",
                "tools": [
                    {"name": "read", "effect": "read", "annotations": {"readOnlyHint": True}},
                    {"name": "write", "effect": "write", "annotations": {"readOnlyHint": False}},
                ],
            },
            {
                "id": "remote-test",
                "name": "Remote Test",
                "kind": "mcp_http",
                "tools": [
                    {"name": "read", "annotations": {"readOnlyHint": True}},
                    {"name": "unknown", "annotations": {}},
                ],
            },
        ]
        for server in servers:
            created = self.client.post("/api/mcp", json=server)
            self.assertEqual(created.status_code, 200, created.text)

        with patch.object(
            main_module.mcp_gateway,
            "invoke_tool",
            new=AsyncMock(return_value={"ok": True}),
        ) as invoke:
            builtin_read = self.client.post(
                "/api/mcp/builtin-test/tools/read/invoke", json={"arguments": {"q": "x"}}
            )
            remote_read = self.client.post(
                "/api/mcp/remote-test/tools/read/invoke", json={"arguments": {"q": "y"}}
            )
            self.assertEqual(builtin_read.status_code, 200, builtin_read.text)
            self.assertEqual(remote_read.status_code, 200, remote_read.text)
            self.assertEqual(invoke.await_count, 2)

            for server_id, tool_name in (
                ("builtin-test", "write"),
                ("remote-test", "unknown"),
            ):
                blocked = self.client.post(
                    f"/api/mcp/{server_id}/tools/{tool_name}/invoke",
                    json={"arguments": {}},
                )
                self.assertEqual(blocked.status_code, 403, blocked.text)
                self.assertIn("正式对话任务", blocked.json()["detail"])
            self.assertEqual(invoke.await_count, 2)

    def test_direct_read_only_invoke_still_honors_tool_before_policy(self) -> None:
        created = self.client.post(
            "/api/mcp",
            json={
                "id": "policy-read",
                "name": "Policy Read",
                "kind": "mcp_http",
                "tools": [{"name": "lookup", "annotations": {"readOnlyHint": True}}],
            },
        )
        self.assertEqual(created.status_code, 200, created.text)
        main_module.policy_engine.set_rules(
            [
                {
                    "id": "deny-direct-read",
                    "event": "tool.before",
                    "scope": "organization",
                    "match": {"server": "policy-read", "tool": "lookup"},
                    "handler": {
                        "type": "builtin_rule",
                        "decision": "deny",
                        "reason": "Direct test disabled by policy",
                    },
                }
            ]
        )
        with patch.object(
            main_module.mcp_gateway, "invoke_tool", new=AsyncMock(return_value={"ok": True})
        ) as invoke:
            blocked = self.client.post(
                "/api/mcp/policy-read/tools/lookup/invoke", json={"arguments": {}}
            )
        self.assertEqual(blocked.status_code, 403, blocked.text)
        self.assertIn("Direct test disabled by policy", blocked.json()["detail"])
        invoke.assert_not_awaited()

    def test_remote_mcp_error_result_is_not_treated_as_success(self) -> None:
        class ErrorResult:
            isError = True
            structuredContent = None
            content = [{"type": "text", "text": "remote operation failed"}]

        with self.assertRaisesRegex(ToolError, "remote operation failed"):
            McpGateway()._normalize_mcp_result(ErrorResult())

    def _assert_public_secret_shape(self, response) -> None:
        body = response.text
        for secret in (
            "Bearer top-secret",
            "db-password",
            "nested-key",
            "session=secret",
            "token-value",
            "trace-secret",
        ):
            self.assertNotIn(secret, body)
        self.assertNotIn("config_json", body)
        self.assertNotIn("tools_json", body)


if __name__ == "__main__":
    unittest.main()
