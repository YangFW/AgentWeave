from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from app import db
from app.services.container_agent_runner import (
    ContainerAgentRunner,
    ContainerRunnerConfig,
    default_container_runner,
)
from app.services.workspace_path_manager import WorkspacePathManager


class ContainerAgentRunnerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.base_dir = Path(self.temp_dir.name)
        self.path_manager = WorkspacePathManager(base_dir=self.base_dir)
        self.config = ContainerRunnerConfig(
            image_name="agentnexus-runner:latest",
            cpu_limit="1.5",
            memory_limit="2g",
        )
        self.runner = ContainerAgentRunner(
            config=self.config,
            path_manager=self.path_manager,
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_build_engine_command(self) -> None:
        codex_cmd = self.runner.build_engine_command("codex", "write a hello world")
        self.assertEqual(codex_cmd[:4], ["codex", "exec", "--skip-git-repo-check", "--dangerously-bypass-approvals-and-sandbox"])
        self.assertEqual(codex_cmd[-1], "write a hello world")

        claude_cmd = self.runner.build_engine_command("claude", "fix the test")
        self.assertEqual(claude_cmd[:3], ["claude", "-p", "fix the test"])
        self.assertIn("--dangerously-skip-permissions", claude_cmd)

        custom_cmd = self.runner.build_engine_command("command", "ls -la")
        self.assertEqual(custom_cmd, ["/bin/bash", "-c", "ls -la"])

        override_cmd = self.runner.build_engine_command("codex", "unused", command_override=["python3", "main.py"])
        self.assertEqual(override_cmd, ["python3", "main.py"])

    def test_build_docker_run_args(self) -> None:
        paths = self.path_manager.get_paths("org-test", "user-test", "ws-test")
        docker_args = self.runner.build_docker_run_args(
            container_name="test-cont",
            paths=paths,
            env_vars={"OPENAI_API_KEY": "sk-mock-key"},
            engine_cmd=["echo", "hi"],
        )

        self.assertIn("docker", docker_args)
        self.assertIn("run", docker_args)
        self.assertIn("--name", docker_args)
        self.assertIn("test-cont", docker_args)
        self.assertIn(f"--user={os.getuid()}:{os.getgid()}", docker_args)
        self.assertIn("--cpus=1.5", docker_args)
        self.assertIn("--memory=2g", docker_args)
        self.assertIn("-e", docker_args)
        self.assertIn("OPENAI_API_KEY=sk-mock-key", docker_args)
        self.assertIn(f"{paths.code_dir}:/workspace:rw", docker_args)
        self.assertIn("echo", docker_args)
        self.assertIn("hi", docker_args)

    async def test_live_docker_command_execution(self) -> None:
        docker_ok = await self.runner.check_docker_available()
        if not docker_ok:
            self.skipTest("Docker daemon not available")

        res = await self.runner.execute_task(
            task_id="test-run-cmd",
            run_id="run-cmd-1",
            prompt="echo 'isolated container success' && echo 'output-data' > /workspace/artifacts/out.txt",
            engine="command",
            organization_id="test-org",
            user_id="test-user",
            workspace_id="test-ws",
        )

        self.assertEqual(res.status, "completed")
        self.assertEqual(res.exit_code, 0)
        self.assertIn("isolated container success", res.stdout)
        self.assertTrue(any(a["name"] == "out.txt" for a in res.artifacts))

    async def test_cancellation_stops_container(self) -> None:
        docker_ok = await self.runner.check_docker_available()
        if not docker_ok:
            self.skipTest("Docker daemon not available")

        cancelled = False

        def check_cancel() -> bool:
            return cancelled

        async def cancel_later() -> None:
            await asyncio.sleep(0.5)
            nonlocal cancelled
            cancelled = True

        cancel_task = asyncio.create_task(cancel_later())

        res = await self.runner.execute_task(
            task_id="test-run-cancel",
            run_id="run-cancel-1",
            prompt="sleep 10",
            engine="command",
            organization_id="test-org",
            user_id="test-user",
            workspace_id="test-ws",
            is_cancel_requested=check_cancel,
        )

        await cancel_task
        self.assertEqual(res.status, "cancelled")

    def test_saved_engine_config_is_injected(self) -> None:
        from app import db
        from app.services.execution_engine_service import update_engine
        from app.services.secret_store import secret_store

        original_db_path = db.DB_PATH
        original_key_file = secret_store.key_file
        db.DB_PATH = self.base_dir / "runner.db"
        secret_store.key_file = self.base_dir / ".secret_key"
        try:
            db.init_db()
            update_engine("codex", {
                "api_key_mode": "direct",
                "api_key": "injected-from-admin",
                "base_url": "https://models.example.invalid/v1",
                "model": "demo-model",
            })
            command = self.runner.build_engine_command("codex", "write tests")
            self.assertIn("-m", command)
            self.assertEqual(command[command.index("-m") + 1], "demo-model")
            self.assertEqual(command[-1], "write tests")
            from app.services.execution_engine_service import resolve_runtime_env
            env = resolve_runtime_env("codex")
            self.assertEqual(env["OPENAI_API_KEY"], "injected-from-admin")
            self.assertEqual(env["OPENAI_BASE_URL"], "https://models.example.invalid/v1")
        finally:
            db.DB_PATH = original_db_path
            secret_store.key_file = original_key_file



if __name__ == "__main__":
    unittest.main()
