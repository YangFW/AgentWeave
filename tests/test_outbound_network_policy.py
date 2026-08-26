from __future__ import annotations

import os
import unittest
from unittest.mock import AsyncMock, patch

from app import main as main_module
from app.services.mcp_gateway import McpGateway, ToolError
from app.services.model_gateway import ModelGateway
from app.services.network_policy import env_flag, outbound_network_enabled
from app.services.policy_engine import PolicyEngine, PolicyRule


class OutboundNetworkPolicyTests(unittest.IsolatedAsyncioTestCase):
    def _offline_environment(self):
        return patch.dict(
            os.environ,
            {
                "APP_ALLOW_OUTBOUND_NETWORK": "false",
                "APP_ALLOW_WEB_SEARCH": "true",
                "APP_ALLOW_REMOTE_MCP": "true",
                "APP_ALLOW_HTTP_TOOLS": "true",
                "APP_ALLOW_REMOTE_INSTALL": "true",
                "APP_REMOTE_INSTALL_HOST_ALLOWLIST": "github.com",
                "TAVILY_API_KEY": "test-key",
            },
            clear=False,
        )

    def test_outbound_network_is_disabled_by_default(self) -> None:
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("APP_ALLOW_OUTBOUND_NETWORK", None)
            self.assertFalse(outbound_network_enabled())
        with patch.dict(os.environ, {"APP_ALLOW_OUTBOUND_NETWORK": "on"}, clear=False):
            self.assertTrue(outbound_network_enabled())
        self.assertFalse(env_flag("APP_FLAG_THAT_DOES_NOT_EXIST"))

    async def test_weather_fails_before_opening_an_http_client(self) -> None:
        gateway = McpGateway()
        with self._offline_environment(), patch(
            "app.services.mcp_gateway.httpx.AsyncClient"
        ) as client:
            with self.assertRaisesRegex(ToolError, "天气查询.*APP_ALLOW_OUTBOUND_NETWORK=true"):
                await gateway._weather_forecast({"city": "宁波", "day": "today"})
        client.assert_not_called()

    async def test_search_and_remote_tools_cannot_bypass_the_master_switch(self) -> None:
        gateway = McpGateway()
        remote = {"config": {"url": "https://mcp.example.test/mcp"}}
        http_server = {
            "config": {"base_url": "https://api.example.test"},
            "tools": [{"name": "lookup", "method": "GET", "path": "/lookup"}],
        }
        with self._offline_environment(), patch(
            "app.services.mcp_gateway.httpx.AsyncClient"
        ) as client:
            with self.assertRaisesRegex(ToolError, "联网搜索.*APP_ALLOW_OUTBOUND_NETWORK=true"):
                await gateway._web_search({"query": "test"})
            with self.assertRaisesRegex(ToolError, "远程 MCP.*APP_ALLOW_OUTBOUND_NETWORK=true"):
                gateway._mcp_http_config(remote)
            with self.assertRaisesRegex(ToolError, "HTTP 工具调用.*APP_ALLOW_OUTBOUND_NETWORK=true"):
                await gateway._invoke_http_tool(http_server, "lookup", {"id": "1"})
        client.assert_not_called()

    async def test_http_policy_fails_closed_without_opening_a_client(self) -> None:
        rule = PolicyRule.from_dict(
            {
                "id": "remote-policy",
                "event": "tool.before",
                "match": {"server": "filesystem"},
                "handler": {
                    "type": "http",
                    "url": "https://policy.example.test/check",
                },
            }
        )
        engine = PolicyEngine(
            [rule],
            http_enabled=True,
            http_allowlist=["policy.example.test"],
        )
        with self._offline_environment(), patch(
            "app.services.policy_engine.httpx.AsyncClient"
        ) as client:
            evaluation = await engine.evaluate(
                "tool.before", {"server": "filesystem", "tool": "read_file"}
            )
        self.assertTrue(evaluation.denied)
        self.assertIn("APP_ALLOW_OUTBOUND_NETWORK=true", evaluation.summary)
        client.assert_not_called()

    async def test_online_model_fails_before_resolving_a_key_or_connecting(self) -> None:
        row = {
            "id": "online-model",
            "provider": "openai_compatible",
            "model": "test-model",
            "base_url": "https://model.example.test/v1",
            "config_json": "{}",
        }
        with self._offline_environment(), patch(
            "app.services.model_gateway.db.query_one", return_value=row
        ), patch.object(ModelGateway, "_api_key") as api_key, patch(
            "app.services.model_gateway.httpx.AsyncClient"
        ) as client:
            with self.assertRaisesRegex(RuntimeError, "在线模型调用.*APP_ALLOW_OUTBOUND_NETWORK=true"):
                await ModelGateway().summarize("hello", model_config_id="online-model")
        api_key.assert_not_called()
        client.assert_not_called()

    def test_remote_install_and_capability_status_obey_the_master_switch(self) -> None:
        with self._offline_environment():
            with self.assertRaisesRegex(ValueError, "下载链接安装.*APP_ALLOW_OUTBOUND_NETWORK=true"):
                main_module._remote_install_url("https://github.com/example/skill.zip")
            status = main_module.capabilities()
        self.assertFalse(status["outbound_network"]["enabled"])
        self.assertFalse(status["web_search"]["enabled"])
        self.assertFalse(status["remote_mcp"]["enabled"])
        self.assertFalse(status["http_tools"]["enabled"])
        self.assertFalse(status["remote_install"]["enabled"])

    def test_capability_status_requires_and_accepts_both_switches(self) -> None:
        with patch.dict(
            os.environ,
            {
                "APP_ALLOW_OUTBOUND_NETWORK": "true",
                "APP_ALLOW_WEB_SEARCH": "true",
                "APP_ALLOW_REMOTE_MCP": "true",
                "APP_ALLOW_HTTP_TOOLS": "true",
                "APP_ALLOW_REMOTE_INSTALL": "true",
                "APP_REMOTE_INSTALL_HOST_ALLOWLIST": "github.com",
            },
            clear=False,
        ), patch(
            "app.services.network_policy._resolve_host_addresses",
            return_value={__import__("ipaddress").ip_address("140.82.112.4")},
        ):
            status = main_module.capabilities()
            checked = main_module._remote_install_url(
                "https://github.com/example/skill.zip"
            )
        self.assertTrue(status["outbound_network"]["enabled"])
        self.assertTrue(status["web_search"]["enabled"])
        self.assertTrue(status["remote_mcp"]["enabled"])
        self.assertTrue(status["http_tools"]["enabled"])
        self.assertTrue(status["remote_install"]["enabled"])
        self.assertEqual(checked, "https://github.com/example/skill.zip")


if __name__ == "__main__":
    unittest.main()
