from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app import db
from app.schemas import ExecutionEngineUpdate
from app.services import auth_service
from app.services.execution_engine_service import (
    ENGINE_CREDENTIAL_ENV_KEYS,
    list_engines,
    resolve_runtime_env,
    update_engine,
)
from app.services.secret_store import secret_store


class ExecutionEngineConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_db_path = db.DB_PATH
        self.original_key_file = secret_store.key_file
        self.env_patch = patch.dict(os.environ, {}, clear=False)
        self.env_patch.start()
        for key in ENGINE_CREDENTIAL_ENV_KEYS:
            os.environ.pop(key, None)
        db.DB_PATH = Path(self.temp_dir.name) / "engines.db"
        secret_store.key_file = Path(self.temp_dir.name) / ".secret_key"
        db.init_db()

    def tearDown(self) -> None:
        self.env_patch.stop()
        db.DB_PATH = self.original_db_path
        secret_store.key_file = self.original_key_file
        self.temp_dir.cleanup()

    def test_seeded_engines_list_without_secrets(self) -> None:
        engines = list_engines()
        ids = {item["id"] for item in engines}
        self.assertEqual(ids, {"codex", "claude"})
        for item in engines:
            self.assertNotIn("api_key_ciphertext", item)
            self.assertNotIn("api_key", item)
            self.assertIn("has_api_key", item)
            self.assertIn("readiness", item)
            self.assertFalse(item["has_api_key"])

    def test_direct_key_is_encrypted_and_preferred_over_host_env(self) -> None:
        os.environ["OPENAI_API_KEY"] = "host-key"
        os.environ["CODEX_API_KEY"] = "host-codex"
        updated = update_engine(
            "codex",
            ExecutionEngineUpdate(
                api_key_mode="direct",
                api_key="admin-secret",
                base_url="https://ai.example.invalid/v1",
                enabled=True,
                model="gpt-test",
            ).model_dump(exclude_unset=True),
        )
        self.assertTrue(updated["has_api_key"])
        self.assertEqual(updated["api_key_mode"], "direct")
        self.assertEqual(updated["base_url"], "https://ai.example.invalid/v1")
        self.assertEqual(updated["config"]["model"], "gpt-test")
        stored = db.query_one("SELECT api_key_ciphertext FROM execution_engines WHERE id = ?", ("codex",))
        self.assertNotIn("admin-secret", stored["api_key_ciphertext"])
        env = resolve_runtime_env("codex")
        self.assertEqual(env["OPENAI_API_KEY"], "admin-secret")
        self.assertEqual(env["CODEX_API_KEY"], "admin-secret")
        self.assertEqual(env["OPENAI_BASE_URL"], "https://ai.example.invalid/v1")
        self.assertEqual(env["OPENAI_API_BASE"], "https://ai.example.invalid/v1")

    def test_env_mode_copies_named_variable(self) -> None:
        os.environ["CUSTOM_CODEX_KEY"] = "from-custom-env"
        update_engine(
            "codex",
            ExecutionEngineUpdate(api_key_mode="env", api_key_env="CUSTOM_CODEX_KEY").model_dump(exclude_unset=True),
        )
        env = resolve_runtime_env("codex")
        self.assertEqual(env["OPENAI_API_KEY"], "from-custom-env")
        self.assertEqual(env["CODEX_API_KEY"], "from-custom-env")

    def test_disabled_engine_is_reported(self) -> None:
        updated = update_engine("claude", ExecutionEngineUpdate(enabled=False).model_dump(exclude_unset=True))
        self.assertFalse(updated["enabled"])
        self.assertEqual(updated["readiness"]["state"], "off")


class ExecutionEngineApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_db_path = db.DB_PATH
        self.original_key_file = secret_store.key_file
        db.DB_PATH = Path(self.temp_dir.name) / "engines-api.db"
        secret_store.key_file = Path(self.temp_dir.name) / ".secret_key"
        db.init_db()
        auth_service.init_schema()
        auth_service.create_user("admin", "admin123456", "admin")
        self.client = TestClient(__import__("app.main", fromlist=["app"]).app)
        self.client.post("/api/auth/login", json={"username": "admin", "password": "admin123456"})

    def tearDown(self) -> None:
        self.client.close()
        db.DB_PATH = self.original_db_path
        secret_store.key_file = self.original_key_file
        self.temp_dir.cleanup()

    def test_public_list_and_update_without_auth(self) -> None:
        listed = self.client.get("/api/execution-engines")
        self.assertEqual(listed.status_code, 200, listed.text)
        payload = listed.json()
        self.assertTrue(payload)
        self.assertNotIn("api_key_ciphertext", listed.text)
        updated = self.client.put(
            "/api/execution-engines/claude",
            json={"api_key_mode": "direct", "api_key": "claude-secret", "enabled": True},
        )
        self.assertEqual(updated.status_code, 200, updated.text)
        body = updated.json()
        self.assertTrue(body["has_api_key"])
        self.assertNotIn("claude-secret", updated.text)
        self.assertNotIn("api_key_ciphertext", body)

    def test_discover_models_api(self) -> None:
        # Test with invalid url raises 400
        resp = self.client.post("/api/models/discover", json={
            "base_url": "https://invalid.domain.example.test",
            "api_key": "test-key",
            "api_key_mode": "direct",
        })
        self.assertEqual(resp.status_code, 400)



class ExecutionEngineAuthTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_db_path = db.DB_PATH
        self.original_key_file = secret_store.key_file
        db.DB_PATH = Path(self.temp_dir.name) / "engines-auth.db"
        secret_store.key_file = Path(self.temp_dir.name) / ".secret_key"
        self.env = patch.dict(os.environ, {
            "APP_AUTH_ENABLED": "true",
            "APP_ADMIN_USERNAME": "admin",
            "APP_ADMIN_PASSWORD": "secret-admin-1",
            "APP_USER_PASSWORD": "",
        })
        self.env.start()
        db.init_db()
        auth_service.init_schema()
        auth_service.create_user("member", "example-test-password", "user")
        self.admin = TestClient(__import__("app.main", fromlist=["app"]).app)
        self.member = TestClient(__import__("app.main", fromlist=["app"]).app)

    def tearDown(self) -> None:
        self.admin.close()
        self.member.close()
        self.env.stop()
        db.DB_PATH = self.original_db_path
        secret_store.key_file = self.original_key_file
        self.temp_dir.cleanup()

    def test_member_cannot_update_but_can_list(self) -> None:
        login = self.member.post("/api/auth/login", json={"username": "member", "password": "example-test-password"})
        self.assertEqual(login.status_code, 200, login.text)
        listed = self.member.get("/api/execution-engines")
        self.assertEqual(listed.status_code, 200, listed.text)
        denied = self.member.put(
            "/api/execution-engines/codex",
            json={"api_key_mode": "direct", "api_key": "stolen"},
        )
        self.assertEqual(denied.status_code, 403)

    def test_admin_can_update(self) -> None:
        login = self.admin.post("/api/auth/login", json={"username": "admin", "password": "secret-admin-1"})
        self.assertEqual(login.status_code, 200, login.text)
        updated = self.admin.put(
            "/api/execution-engines/codex",
            json={"api_key_mode": "env", "api_key_env": "OPENAI_API_KEY", "enabled": True},
        )
        self.assertEqual(updated.status_code, 200, updated.text)
        self.assertEqual(updated.json()["api_key_env"], "OPENAI_API_KEY")

    def test_copy_credentials_from_when_creating_model(self) -> None:
        login = self.admin.post("/api/auth/login", json={"username": "admin", "password": "secret-admin-1"})
        self.assertEqual(login.status_code, 200)
        # Create base model
        res1 = self.admin.post("/api/models", json={
            "id": "base-model",
            "name": "Base Model",
            "provider": "openai_compatible",
            "model": "base-1",
            "base_url": "https://api.example.com/v1",
            "api_key_mode": "direct",
            "api_key": "sk-secret-key-12345",
            "enabled": True,
        })
        self.assertEqual(res1.status_code, 200)

        # Clone credentials to new model
        res2 = self.admin.post("/api/models", json={
            "id": "cloned-model",
            "name": "Cloned Model",
            "provider": "openai_compatible",
            "model": "cloned-2",
            "copy_credentials_from": "base-model",
            "enabled": True,
        })
        self.assertEqual(res2.status_code, 200)
        self.assertTrue(res2.json()["has_api_key"])

    def test_codex_command_with_resume_session(self) -> None:
        from app.services.container_agent_runner import default_container_runner
        cmd_new = default_container_runner.build_engine_command("codex", "hello")
        self.assertIn("exec", cmd_new)
        self.assertNotIn("resume", cmd_new)

        cmd_resume = default_container_runner.build_engine_command(
            "codex", "hello again", resume_session_id="01a0a810-test-uuid"
        )
        self.assertIn("resume", cmd_resume)
        self.assertIn("01a0a810-test-uuid", cmd_resume)
