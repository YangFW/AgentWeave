from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from app import db
from app import main as main_module


class PolicyApiTests(unittest.TestCase):
    """HTTP acceptance coverage for persistent, hot-reloaded policy rules."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_db_path = db.DB_PATH
        self.original_app_db_path = os.environ.get("APP_DB_PATH")
        self.original_env_rules = os.environ.get("APP_POLICY_RULES_JSON")
        self.original_policy_rules = list(main_module.policy_engine._rules)
        self.db_path = Path(self.temp_dir.name) / "policy-api.db"

        os.environ["APP_DB_PATH"] = str(self.db_path)
        os.environ["APP_POLICY_RULES_JSON"] = ""
        db.DB_PATH = self.db_path
        db.init_db()

        self.scheduler_start_patch = patch.object(
            main_module.loop_scheduler, "start", return_value=None
        )
        self.scheduler_stop_patch = patch.object(
            main_module.loop_scheduler, "stop", new_callable=AsyncMock
        )
        self.scheduler_start_patch.start()
        self.scheduler_stop_patch.start()
        self.client_context = TestClient(main_module.app)
        self.client = self.client_context.__enter__()

    def tearDown(self) -> None:
        self.client_context.__exit__(None, None, None)
        self.scheduler_stop_patch.stop()
        self.scheduler_start_patch.stop()
        main_module.policy_engine.set_rules(self.original_policy_rules)
        db.DB_PATH = self.original_db_path
        if self.original_app_db_path is None:
            os.environ.pop("APP_DB_PATH", None)
        else:
            os.environ["APP_DB_PATH"] = self.original_app_db_path
        if self.original_env_rules is None:
            os.environ.pop("APP_POLICY_RULES_JSON", None)
        else:
            os.environ["APP_POLICY_RULES_JSON"] = self.original_env_rules
        self.temp_dir.cleanup()

    def _create_rule(self, payload: dict) -> dict:
        response = self.client.post("/api/policies", json=payload)
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    @staticmethod
    def _evaluate_tool(*, server: str = "filesystem", user_id: str = "user-42"):
        return asyncio.run(
            main_module.policy_engine.evaluate(
                "tool.before",
                {
                    "user_id": user_id,
                    "tool": {
                        "server": server,
                        "name": "write_file",
                        "arguments": {"path": "report.md"},
                    },
                },
            )
        )

    def test_crud_redaction_and_engine_hot_reload(self) -> None:
        secret = "policy-api-secret-must-not-leak"
        created = self._create_rule(
            {
                "id": "block-filesystem",
                "name": "Block filesystem writes",
                "event": "tool.before",
                "scope": "organization",
                "priority": 100,
                "match": {"server": "filesystem"},
                "handler": {
                    "type": "builtin_rule",
                    "decision": "deny",
                    "reason": "Filesystem writes are disabled",
                    "metadata": {
                        "api_key": secret,
                        "classification": "internal",
                    },
                },
            }
        )

        self.assertEqual(created["handler_type"], "builtin_rule")
        self.assertEqual(created["handler"]["decision"], "deny")
        self.assertEqual(created["handler"]["metadata"]["api_key"], "[REDACTED]")
        self.assertNotIn(secret, str(created))
        self.assertEqual(self._evaluate_tool().outcome, "deny")

        listed_response = self.client.get("/api/policies")
        self.assertEqual(listed_response.status_code, 200, listed_response.text)
        listed = listed_response.json()
        self.assertEqual([item["id"] for item in listed], ["block-filesystem"])
        self.assertEqual(listed[0]["handler"]["metadata"]["api_key"], "[REDACTED]")
        self.assertNotIn(secret, listed_response.text)

        read_response = self.client.get("/api/policies/block-filesystem")
        self.assertEqual(read_response.status_code, 200, read_response.text)
        self.assertEqual(
            read_response.json()["handler"]["metadata"]["api_key"], "[REDACTED]"
        )
        self.assertNotIn(secret, read_response.text)

        update_response = self.client.put(
            "/api/policies/block-filesystem",
            json={
                "name": "Approve filesystem writes",
                "handler": {
                    "type": "builtin_rule",
                    "decision": "require_approval",
                    "reason": "A reviewer must approve filesystem writes",
                },
            },
        )
        self.assertEqual(update_response.status_code, 200, update_response.text)
        self.assertEqual(update_response.json()["name"], "Approve filesystem writes")
        self.assertEqual(self._evaluate_tool().outcome, "require_approval")

        disable_response = self.client.put(
            "/api/policies/block-filesystem", json={"enabled": False}
        )
        self.assertEqual(disable_response.status_code, 200, disable_response.text)
        self.assertFalse(disable_response.json()["enabled"])
        disabled_evaluation = self._evaluate_tool()
        self.assertEqual(disabled_evaluation.outcome, "allow")
        self.assertEqual(disabled_evaluation.rules_considered, 0)
        self.assertNotIn(
            "block-filesystem",
            [item["id"] for item in main_module.policy_engine.list_rules()],
        )

        delete_response = self.client.delete("/api/policies/block-filesystem")
        self.assertEqual(delete_response.status_code, 200, delete_response.text)
        self.assertEqual(delete_response.json(), {"ok": True, "id": "block-filesystem"})
        self.assertEqual(self.client.get("/api/policies/block-filesystem").status_code, 404)
        self.assertEqual(self.client.get("/api/policies").json(), [])
        self.assertNotIn(
            "block-filesystem",
            [item["id"] for item in main_module.policy_engine.list_rules()],
        )

    def test_rejects_shell_and_python_handlers_without_persisting_them(self) -> None:
        for handler_type in ("shell", "python"):
            with self.subTest(handler_type=handler_type):
                response = self.client.post(
                    "/api/policies",
                    json={
                        "id": f"unsafe-{handler_type}",
                        "event": "tool.before",
                        "handler": {
                            "type": handler_type,
                            "command": "print('unsafe')",
                        },
                    },
                )
                self.assertEqual(response.status_code, 400, response.text)
                self.assertIn("forbidden", response.json()["detail"].lower())

        self.assertEqual(self.client.get("/api/policies").json(), [])
        stored = db.query_one("SELECT COUNT(*) AS count FROM policy_rules")
        self.assertEqual(stored["count"], 0)

    def test_organization_deny_cannot_be_overridden_by_user_allow(self) -> None:
        common_match = {"server": "filesystem", "tool": "write_file"}
        self._create_rule(
            {
                "id": "organization-deny",
                "event": "tool.before",
                "scope": "organization",
                "priority": 1,
                "match": common_match,
                "handler": {
                    "type": "builtin_rule",
                    "decision": "deny",
                    "reason": "Organization policy blocks writes",
                },
            }
        )
        self._create_rule(
            {
                "id": "user-allow",
                "event": "tool.before",
                "scope": "user",
                "scope_id": "user-42",
                "priority": 10_000,
                "match": common_match,
                "handler": {
                    "type": "builtin_rule",
                    "decision": "allow",
                    "reason": "User policy permits writes",
                },
            }
        )

        context = {
            "user_id": "user-42",
            "tool": {
                "server": "filesystem",
                "name": "write_file",
                "arguments": {"path": "report.md"},
            },
        }
        evaluation = asyncio.run(
            main_module.runtime._evaluate_policy(
                "tool.before", context, enforce=False
            )
        )

        self.assertEqual(evaluation.outcome, "deny")
        self.assertEqual(evaluation.rules_matched, 2)
        decisions = {item.rule_id: item for item in evaluation.decisions}
        self.assertEqual(decisions["organization-deny"].decision, "deny")
        self.assertEqual(decisions["user-allow"].decision, "allow")
        with self.assertRaisesRegex(RuntimeError, "Organization policy blocks writes"):
            asyncio.run(
                main_module.runtime._evaluate_policy(
                    "tool.before", context, enforce=True
                )
            )


if __name__ == "__main__":
    unittest.main()
