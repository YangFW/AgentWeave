from __future__ import annotations

import unittest

from app.services.mcp_gateway import McpGateway


class McpTimeoutTests(unittest.TestCase):
    def setUp(self) -> None:
        self.gateway = McpGateway()

    def test_stdio_timeout_defaults_to_sixty_seconds(self) -> None:
        self.assertEqual(self.gateway._stdio_timeout_seconds({"config": {}}), 60.0)

    def test_stdio_timeout_is_bounded(self) -> None:
        self.assertEqual(self.gateway._stdio_timeout_seconds({"config": {"timeout": 1}}), 5.0)
        self.assertEqual(self.gateway._stdio_timeout_seconds({"config": {"timeout": 999}}), 300.0)

    def test_invalid_stdio_timeout_uses_default(self) -> None:
        self.assertEqual(self.gateway._stdio_timeout_seconds({"config": {"timeout": "bad"}}), 60.0)


if __name__ == "__main__":
    unittest.main()
