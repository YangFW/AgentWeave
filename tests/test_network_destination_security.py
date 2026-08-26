from __future__ import annotations

import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from app.services.mcp_gateway import McpGateway, ToolError
from app.services.model_gateway import ModelGateway


def _dns_result(address: str):
    family = 10 if ":" in address else 2
    return [(family, 1, 6, "", (address, 443))]


class ModelDestinationSecurityTests(unittest.TestCase):
    def test_public_model_host_is_allowed_without_an_allowlist(self) -> None:
        with patch.dict(os.environ, {}, clear=False), patch(
            "app.services.network_policy.socket.getaddrinfo",
            return_value=_dns_result("93.184.216.34"),
        ):
            os.environ.pop("APP_MODEL_HOST_ALLOWLIST", None)
            url = ModelGateway._validated_base_url(
                {"base_url": "https://model.example.test/v1"}
            )
        self.assertEqual(url, "https://model.example.test/v1")

    def test_public_model_http_requires_an_explicit_allowlist_entry(self) -> None:
        with patch.dict(os.environ, {}, clear=False), patch(
            "app.services.network_policy.socket.getaddrinfo",
            return_value=_dns_result("93.184.216.34"),
        ):
            os.environ.pop("APP_MODEL_HOST_ALLOWLIST", None)
            with self.assertRaisesRegex(RuntimeError, "HTTPS"):
                ModelGateway._validated_base_url(
                    {"base_url": "http://model.example.test/v1"}
                )

    def test_model_host_rejects_local_private_and_metadata_destinations(self) -> None:
        cases = (
            ("http://localhost:11434/v1", "93.184.216.34"),
            ("https://model.example.test/v1", "10.20.30.40"),
            ("https://model.example.test/v1", "224.0.0.1"),
            ("http://169.254.169.254/latest", "169.254.169.254"),
            ("http://metadata.google.internal/computeMetadata/v1", "93.184.216.34"),
        )
        for url, address in cases:
            with self.subTest(url=url), patch.dict(os.environ, {}, clear=False), patch(
                "app.services.network_policy.socket.getaddrinfo",
                return_value=_dns_result(address),
            ):
                os.environ.pop("APP_MODEL_HOST_ALLOWLIST", None)
                with self.assertRaises(RuntimeError):
                    ModelGateway._validated_base_url({"base_url": url})

    def test_model_allowlist_is_exact_and_can_explicitly_enable_a_local_model(self) -> None:
        with patch.dict(
            os.environ,
            {"APP_MODEL_HOST_ALLOWLIST": "model.example.test"},
            clear=False,
        ), patch(
            "app.services.network_policy.socket.getaddrinfo",
            return_value=_dns_result("93.184.216.34"),
        ):
            with self.assertRaisesRegex(RuntimeError, "APP_MODEL_HOST_ALLOWLIST"):
                ModelGateway._validated_base_url(
                    {"base_url": "https://sub.model.example.test/v1"}
                )

        with patch.dict(
            os.environ,
            {"APP_MODEL_HOST_ALLOWLIST": "localhost"},
            clear=False,
        ), patch(
            "app.services.network_policy.socket.getaddrinfo",
            return_value=_dns_result("127.0.0.1"),
        ):
            self.assertEqual(
                ModelGateway._validated_base_url(
                    {"base_url": "http://localhost:11434/v1"}
                ),
                "http://localhost:11434/v1",
            )

    def test_model_url_rejects_non_http_credentials_query_and_fragment(self) -> None:
        for url in (
            "file:///tmp/model",
            "https://user:secret@model.example.test/v1",
            "https://model.example.test/v1?token=secret",
            "https://model.example.test/v1#fragment",
        ):
            with self.subTest(url=url), patch.dict(
                os.environ,
                {"APP_MODEL_HOST_ALLOWLIST": "model.example.test"},
                clear=False,
            ):
                with self.assertRaises(RuntimeError):
                    ModelGateway._validated_base_url({"base_url": url})


class RedirectPolicyTests(unittest.IsolatedAsyncioTestCase):
    async def test_model_client_disables_redirects(self) -> None:
        row = {
            "id": "online-model",
            "provider": "openai_compatible",
            "model": "test-model",
            "base_url": "https://model.example.test/v1",
            "config_json": "{}",
            "api_key_ciphertext": "",
            "api_key_env": "MODEL_TEST_API_KEY",
        }
        response = MagicMock()
        response.raise_for_status.return_value = None
        response.json.return_value = {
            "choices": [{"message": {"content": "ok"}}]
        }
        client = AsyncMock()
        client.post.return_value = response
        context = MagicMock()
        context.__aenter__ = AsyncMock(return_value=client)
        context.__aexit__ = AsyncMock(return_value=False)
        with patch.dict(
            os.environ,
            {
                "APP_ALLOW_OUTBOUND_NETWORK": "true",
                "APP_MODEL_HOST_ALLOWLIST": "model.example.test",
                "MODEL_TEST_API_KEY": "test-key",
            },
            clear=False,
        ), patch(
            "app.services.network_policy.socket.getaddrinfo",
            return_value=_dns_result("93.184.216.34"),
        ), patch(
            "app.services.model_gateway.db.query_one", return_value=row
        ), patch(
            "app.services.model_gateway.httpx.AsyncClient", return_value=context
        ) as async_client:
            self.assertEqual(
                await ModelGateway().summarize(
                    "hello", model_config_id="online-model"
                ),
                "ok",
            )
        self.assertFalse(async_client.call_args.kwargs["follow_redirects"])

    async def test_http_tool_client_disables_redirects(self) -> None:
        response = MagicMock()
        response.raise_for_status.return_value = None
        response.json.return_value = {"ok": True}
        client = AsyncMock()
        client.get.return_value = response
        context = MagicMock()
        context.__aenter__ = AsyncMock(return_value=client)
        context.__aexit__ = AsyncMock(return_value=False)
        server = {
            "config": {"base_url": "https://tool.example.test"},
            "tools": [{"name": "lookup", "method": "GET", "path": "/lookup"}],
        }
        with patch.dict(
            os.environ,
            {
                "APP_ALLOW_OUTBOUND_NETWORK": "true",
                "APP_ALLOW_HTTP_TOOLS": "true",
                "APP_REMOTE_HOST_ALLOWLIST": "tool.example.test",
            },
            clear=False,
        ), patch(
            "app.services.network_policy.socket.getaddrinfo",
            return_value=_dns_result("93.184.216.34"),
        ), patch(
            "app.services.mcp_gateway.httpx.AsyncClient", return_value=context
        ) as async_client:
            result = await McpGateway()._invoke_http_tool(
                server, "lookup", {"id": "one"}
            )
        self.assertEqual(result, {"ok": True})
        self.assertFalse(async_client.call_args.kwargs["follow_redirects"])


class RemoteDestinationSecurityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.gateway = McpGateway()

    def test_remote_mcp_and_http_require_a_nonempty_allowlist(self) -> None:
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("APP_REMOTE_HOST_ALLOWLIST", None)
            with self.assertRaisesRegex(ToolError, "非空.*APP_REMOTE_HOST_ALLOWLIST"):
                self.gateway._validate_remote_url("https://mcp.example.test/mcp")

    def test_remote_allowlist_is_exact_and_still_rejects_non_public_dns(self) -> None:
        with patch.dict(
            os.environ,
            {"APP_REMOTE_HOST_ALLOWLIST": "mcp.example.test"},
            clear=False,
        ):
            with self.assertRaisesRegex(ToolError, "APP_REMOTE_HOST_ALLOWLIST"):
                self.gateway._validate_remote_url("https://evil.mcp.example.test/mcp")
            with patch(
                "app.services.network_policy.socket.getaddrinfo",
                return_value=_dns_result("192.168.10.4"),
            ):
                with self.assertRaisesRegex(ToolError, "非公网 IP"):
                    self.gateway._validate_remote_url("https://mcp.example.test/mcp")

    def test_remote_allowlisted_public_host_is_accepted(self) -> None:
        with patch.dict(
            os.environ,
            {"APP_REMOTE_HOST_ALLOWLIST": "mcp.example.test"},
            clear=False,
        ), patch(
            "app.services.network_policy.socket.getaddrinfo",
            return_value=_dns_result("93.184.216.34"),
        ):
            self.assertEqual(
                self.gateway._validate_remote_url("https://mcp.example.test/mcp"),
                "https://mcp.example.test/mcp",
            )


class StdioCommandSecurityTests(unittest.TestCase):
    def test_stdio_requires_a_nonempty_allowlist(self) -> None:
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("APP_STDIO_COMMAND_ALLOWLIST", None)
            with self.assertRaisesRegex(ToolError, "非空.*APP_STDIO_COMMAND_ALLOWLIST"):
                McpGateway._resolve_stdio_command("python3")

    def test_bare_command_requires_an_exact_bare_entry_and_uses_which(self) -> None:
        executable = str(Path(sys.executable).resolve())
        with patch.dict(
            os.environ,
            {"APP_STDIO_COMMAND_ALLOWLIST": "python3"},
            clear=False,
        ), patch("app.services.mcp_gateway.shutil.which", return_value=executable) as which:
            self.assertEqual(
                McpGateway._resolve_stdio_command("python3"), executable
            )
        which.assert_called_once_with("python3")

        with patch.dict(
            os.environ,
            {"APP_STDIO_COMMAND_ALLOWLIST": executable},
            clear=False,
        ):
            with self.assertRaisesRegex(ToolError, "裸命令不在"):
                McpGateway._resolve_stdio_command(Path(executable).name)

    def test_absolute_command_only_matches_an_exact_resolved_absolute_entry(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            allowed = Path(temp_dir) / "allowed-tool"
            lookalike = Path(temp_dir) / "nested" / "allowed-tool"
            lookalike.parent.mkdir()
            allowed.write_text("#!/bin/sh\n", encoding="utf-8")
            lookalike.write_text("#!/bin/sh\n", encoding="utf-8")
            allowed.chmod(allowed.stat().st_mode | stat.S_IXUSR)
            lookalike.chmod(lookalike.stat().st_mode | stat.S_IXUSR)

            with patch.dict(
                os.environ,
                {"APP_STDIO_COMMAND_ALLOWLIST": str(allowed)},
                clear=False,
            ):
                self.assertEqual(
                    McpGateway._resolve_stdio_command(str(allowed)),
                    str(allowed.resolve()),
                )
                with self.assertRaisesRegex(ToolError, "绝对路径不在"):
                    McpGateway._resolve_stdio_command(str(lookalike))

    def test_absolute_path_cannot_match_a_bare_entry_by_basename(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            executable = Path(temp_dir) / "node"
            executable.write_text("#!/bin/sh\n", encoding="utf-8")
            executable.chmod(executable.stat().st_mode | stat.S_IXUSR)
            with patch.dict(
                os.environ,
                {"APP_STDIO_COMMAND_ALLOWLIST": "node"},
                clear=False,
            ):
                with self.assertRaisesRegex(ToolError, "绝对路径不在"):
                    McpGateway._resolve_stdio_command(str(executable))


if __name__ == "__main__":
    unittest.main()
