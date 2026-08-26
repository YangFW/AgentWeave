from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException

from app import db
from app.main import create_model, list_models, test_model, update_model
from app.schemas import ModelConfigCreate, ModelConfigUpdate
from app.services.secret_store import secret_store


class ModelConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        self.original_db_path = db.DB_PATH
        self.original_key_file = secret_store.key_file
        self.original_outbound = os.environ.get("APP_ALLOW_OUTBOUND_NETWORK")
        self.original_env_key = os.environ.get("DEMO_API_KEY")
        self.temp_dir = tempfile.TemporaryDirectory()
        db.DB_PATH = Path(self.temp_dir.name) / "test.db"
        secret_store.key_file = Path(self.temp_dir.name) / ".secret_key"
        os.environ.pop("APP_ALLOW_OUTBOUND_NETWORK", None)
        os.environ.pop("DEMO_API_KEY", None)
        db.init_db()

    def tearDown(self) -> None:
        db.DB_PATH = self.original_db_path
        secret_store.key_file = self.original_key_file
        if self.original_outbound is None:
            os.environ.pop("APP_ALLOW_OUTBOUND_NETWORK", None)
        else:
            os.environ["APP_ALLOW_OUTBOUND_NETWORK"] = self.original_outbound
        if self.original_env_key is None:
            os.environ.pop("DEMO_API_KEY", None)
        else:
            os.environ["DEMO_API_KEY"] = self.original_env_key
        self.temp_dir.cleanup()

    def test_direct_key_is_encrypted_and_model_is_listed_without_secret(self) -> None:
        created = create_model(ModelConfigCreate(
            id="direct-model",
            name="Direct Model",
            provider="openai_compatible",
            model="demo-model",
            base_url="https://example.invalid/v1",
            api_key_mode="direct",
            api_key="secret-value",
        ))
        self.assertTrue(created["has_api_key"])
        self.assertEqual(created["api_key_mode"], "direct")
        self.assertNotIn("api_key_ciphertext", created)
        self.assertEqual(created["api_key_env"], "")
        self.assertEqual(created["capabilities"]["protocol"], "openai_chat_completions")
        self.assertTrue(created["capabilities"]["streaming"])
        self.assertTrue(created["capabilities"]["tool_calling"])
        self.assertIn(created["readiness"]["state"], {"ready", "needs_config"})
        listed = next(item for item in list_models() if item["id"] == "direct-model")
        self.assertTrue(listed["has_api_key"])
        self.assertIn("capabilities", listed)
        self.assertIn("readiness", listed)
        self.assertEqual(listed["last_test"]["status"], "untested")
        stored = db.query_one("SELECT api_key_ciphertext FROM model_configs WHERE id = ?", ("direct-model",))
        self.assertNotIn("secret-value", stored["api_key_ciphertext"])

    def test_env_model_is_saved_and_can_switch_to_direct_key(self) -> None:
        create_model(ModelConfigCreate(
            id="env-model",
            name="Env Model",
            model="demo-model",
            base_url="https://example.invalid/v1",
            api_key_mode="env",
            api_key_env="DEMO_API_KEY",
        ))
        listed = next(item for item in list_models() if item["id"] == "env-model")
        self.assertEqual(listed["readiness"]["state"], "needs_config")
        self.assertIn("DEMO_API_KEY", listed["readiness"]["detail"])
        updated = update_model("env-model", ModelConfigUpdate(api_key_mode="direct", api_key="new-secret"))
        self.assertEqual(updated["api_key_mode"], "direct")
        self.assertEqual(updated["api_key_env"], "")

    def test_env_mode_rejects_invalid_variable_name(self) -> None:
        with self.assertRaises(HTTPException):
            create_model(ModelConfigCreate(
                id="bad-env",
                name="Bad Env",
                model="demo-model",
                api_key_mode="env",
                api_key_env="sk-not-an-env-name",
            ))

    def test_model_test_result_is_persisted_without_exposing_secret(self) -> None:
        create_model(ModelConfigCreate(
            id="smoke-model",
            name="Smoke Model",
            provider="openai_compatible",
            model="demo-model",
            base_url="https://example.invalid/v1",
            api_key_mode="direct",
            api_key="secret-value",
        ))

        with patch("app.main.model_gateway.summarize", new=AsyncMock(return_value="OK")):
            result = asyncio.run(test_model("smoke-model"))

        self.assertTrue(result["ok"])
        self.assertEqual(result["model"]["last_test"]["status"], "pass")
        listed = next(item for item in list_models() if item["id"] == "smoke-model")
        self.assertEqual(listed["last_test"]["status"], "pass")
        self.assertEqual(listed["last_test"]["message"], "OK")
        self.assertNotIn("api_key_ciphertext", listed)

    def test_failed_model_test_is_persisted_for_visible_troubleshooting(self) -> None:
        create_model(ModelConfigCreate(
            id="failing-model",
            name="Failing Model",
            provider="openai_compatible",
            model="demo-model",
            base_url="https://example.invalid/v1",
            api_key_mode="direct",
            api_key="secret-value",
        ))

        with patch("app.main.model_gateway.summarize", new=AsyncMock(side_effect=RuntimeError("连接失败"))):
            with self.assertRaises(HTTPException):
                asyncio.run(test_model("failing-model"))

        listed = next(item for item in list_models() if item["id"] == "failing-model")
        self.assertEqual(listed["last_test"]["status"], "fail")
        self.assertIn("连接失败", listed["last_test"]["message"])
        self.assertNotIn("api_key_ciphertext", listed)


if __name__ == "__main__":
    unittest.main()
