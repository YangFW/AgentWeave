from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from pathlib import Path

from app import db
from app.services.agent_runtime import AgentRuntime, create_task_record
from app.services.container_agent_runner import (
    ContainerAgentRunner,
    ContainerRunnerConfig,
    default_container_runner,
)
from app.services.mcp_gateway import McpGateway
from app.services.model_gateway import ModelGateway
from app.services.skill_registry import SkillRegistry
from app.services.task_state import TaskStateService
from app.services.workspace_path_manager import WorkspacePathManager, default_path_manager


class SandboxIsolationAndLifecycleTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test_sandbox.db"
        self.orig_db_path = db.DB_PATH
        db.DB_PATH = self.db_path
        db.init_db()

        self.workspaces_root = Path(self.temp_dir.name) / "workspaces"
        self.path_manager = WorkspacePathManager(base_dir=self.workspaces_root)
        self.task_state = TaskStateService(db.get_conn)
        self.task_state.init_schema()
        self.runtime = AgentRuntime(
            SkillRegistry(),
            McpGateway(),
            ModelGateway(),
            task_state=self.task_state,
        )

    def tearDown(self) -> None:
        db.DB_PATH = self.orig_db_path
        self.temp_dir.cleanup()

    async def asyncTearDown(self) -> None:
        await default_container_runner.cleanup_stale_containers(prefix="nexus-", force_all_idle_warm=True)

    async def test_multi_turn_session_continuity_in_same_project(self) -> None:
        conv_id = "conv_continuity_test"
        ws_id = "proj_accounting"

        # Turn 1: User creates a stateful file in the workspace
        task1 = create_task_record(
            message="echo 'INITIAL_BALANCE=100' > /workspace/account.env && echo 'Turn 1 done'",
            agent_id="general-agent",
            workspace=ws_id,
            conversation_id=conv_id,
            execution_engine="container",
        )
        await self.runtime.run_task(task1["id"])

        t1_row = db.query_one("SELECT status FROM tasks WHERE id = ?", (task1["id"],))
        self.assertEqual(t1_row["status"], "completed")

        # Verify account.env exists on host in that project's code directory
        p_paths = default_path_manager.get_paths("local-org", "local-user", ws_id)
        acc_file = p_paths.code_dir / "account.env"
        self.assertTrue(acc_file.exists())
        self.assertIn("INITIAL_BALANCE=100", acc_file.read_text())

        # Turn 2: Second task in the same conversation and workspace reads and modifies the file
        task2 = create_task_record(
            message="cat /workspace/account.env && echo 'BALANCE_UPDATED=200' >> /workspace/account.env",
            agent_id="general-agent",
            workspace=ws_id,
            conversation_id=conv_id,
            execution_engine="container",
        )
        await self.runtime.run_task(task2["id"])

        t2_row = db.query_one("SELECT status, result_json FROM tasks WHERE id = ?", (task2["id"],))
        self.assertEqual(t2_row["status"], "completed")
        t2_res = db.json_loads(t2_row["result_json"])
        self.assertIn("INITIAL_BALANCE=100", t2_res.get("stdout", ""))

        # Verify host file now has both lines
        updated_content = acc_file.read_text()
        self.assertIn("INITIAL_BALANCE=100", updated_content)
        self.assertIn("BALANCE_UPDATED=200", updated_content)

    async def test_cross_customer_and_cross_project_isolation(self) -> None:
        # Customer A in Workspace A
        cust_a_paths = default_path_manager.get_paths("org_alpha", "user_alice", "project_secret")
        default_path_manager.ensure_workspace(cust_a_paths)
        (cust_a_paths.code_dir / "alice_confidential.key").write_text("ALICE_SUPER_SECRET_KEY_12345")

        # Customer B in Workspace B
        cust_b_paths = default_path_manager.get_paths("org_beta", "user_bob", "project_public")
        default_path_manager.ensure_workspace(cust_b_paths)

        # Run Customer B's task attempting to find or read Alice's file
        task_b = create_task_record(
            message="ls -la /workspace && test -f /workspace/alice_confidential.key && echo 'LEAKED' || echo 'NOT_FOUND'",
            agent_id="general-agent",
            organization_id="org_beta",
            user_id="user_bob",
            workspace="project_public",
            execution_engine="container",
        )
        await self.runtime.run_task(task_b["id"])

        t_b_row = db.query_one("SELECT status, result_json FROM tasks WHERE id = ?", (task_b["id"],))
        self.assertEqual(t_b_row["status"], "completed")
        t_b_res = db.json_loads(t_b_row["result_json"])
        self.assertIn("NOT_FOUND", t_b_res.get("stdout", ""))
        self.assertNotIn("LEAKED", t_b_res.get("stdout", ""))
        self.assertNotIn("ALICE_SUPER_SECRET", t_b_res.get("stdout", ""))

    async def test_container_destroyed_immediately_on_completion(self) -> None:
        task = create_task_record(
            message="echo 'testing container lifecycle'",
            agent_id="general-agent",
            workspace="cleanup_ws",
            execution_engine="container",
        )
        task_id = task["id"]
        await self.runtime.run_task(task_id)

        # Check running docker containers with name matching this task
        proc = await asyncio.create_subprocess_exec(
            "docker", "ps", "-a", "--filter", f"name=nexus-run-{task_id[:12]}", "--format", "{{.ID}}",
            stdout=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
        containers = [c for c in stdout.decode().strip().split("\n") if c]
        self.assertEqual(len(containers), 0, "Container was not destroyed after task completion!")

    async def test_container_destroyed_on_timeout(self) -> None:
        runner = ContainerAgentRunner(
            config=ContainerRunnerConfig(default_timeout=2),
        )
        res = await runner.execute_task(
            task_id="timeout-test-task",
            run_id="run-timeout-1",
            prompt="sleep 30",
            engine="command",
            workspace_id="ws_timeout",
            timeout_seconds=2,
        )
        self.assertEqual(res.status, "timeout")

        # Verify container was killed and removed
        proc = await asyncio.create_subprocess_exec(
            "docker", "ps", "-a", "--filter", "name=nexus-run-timeout-test", "--format", "{{.ID}}",
            stdout=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
        containers = [c for c in stdout.decode().strip().split("\n") if c]
        self.assertEqual(len(containers), 0, "Timed-out container was not destroyed!")

    async def test_stale_container_cleanup_sweep(self) -> None:
        # Start a detached container with nexus-run- prefix
        stale_name = "nexus-run-unit-test-stale-sweep"
        await asyncio.create_subprocess_exec(
            "docker", "run", "-d", "--name", stale_name, "node:22-bookworm-slim", "sleep", "60",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await asyncio.sleep(0.5)

        # Run sweep
        cleaned_count = await default_container_runner.cleanup_stale_containers(prefix="nexus-run-unit-test-stale")
        self.assertGreaterEqual(cleaned_count, 1)

        # Verify it is gone
        proc = await asyncio.create_subprocess_exec(
            "docker", "ps", "-a", "--filter", f"name={stale_name}", "--format", "{{.ID}}",
            stdout=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
        containers = [c for c in stdout.decode().strip().split("\n") if c]
        self.assertEqual(len(containers), 0)

    async def test_warm_container_idle_cleanup(self) -> None:
        stale_warm_name = "nexus-warm-unit-test-stale-sweep"
        await asyncio.create_subprocess_exec(
            "docker", "run", "-d", "--name", stale_warm_name, "node:22-bookworm-slim", "sleep", "60",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await asyncio.sleep(0.5)

        cleaned_count = await default_container_runner.cleanup_stale_containers(
            prefix="nexus-warm-unit-test-stale", force_all_idle_warm=True
        )
        self.assertGreaterEqual(cleaned_count, 1)

        proc = await asyncio.create_subprocess_exec(
            "docker", "ps", "-a", "--filter", f"name={stale_warm_name}", "--format", "{{.ID}}",
            stdout=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
        containers = [c for c in stdout.decode().strip().split("\n") if c]
        self.assertEqual(len(containers), 0)

    async def test_codex_and_claude_engines_present_and_executable_in_sandbox(self) -> None:
        # Test codex execution in sandbox container
        res_codex = await default_container_runner.execute_task(
            task_id="test-engine-codex",
            run_id="run-c-1",
            prompt="unused",
            engine="command",
            command_override=["codex", "--version"],
            workspace_id="ws_codex_check",
        )
        self.assertEqual(res_codex.status, "completed")
        self.assertIn("codex-cli", res_codex.stdout)

        # Test claude execution in sandbox container
        res_claude = await default_container_runner.execute_task(
            task_id="test-engine-claude",
            run_id="run-c-2",
            prompt="unused",
            engine="command",
            command_override=["claude", "--version"],
            workspace_id="ws_claude_check",
        )
        self.assertEqual(res_claude.status, "completed")
        self.assertIn("Claude Code", res_claude.stdout)


    async def test_new_dependencies_install_onto_host_project_mount(self) -> None:
        ws_id = "ws_deps_persist"
        paths = default_path_manager.get_paths("local-org", "local-user", ws_id)
        default_path_manager.ensure_workspace(paths)

        py_pkg = paths.code_dir / "vendor" / "demo_dep"
        py_pkg.mkdir(parents=True, exist_ok=True)
        (py_pkg / "demo_dep.py").write_text("VALUE = 'from-host-mount'\n", encoding="utf-8")
        (py_pkg / "setup.py").write_text(
            "from setuptools import setup\nsetup(name='demo-dep', version='1.0.0', py_modules=['demo_dep'])\n",
            encoding="utf-8",
        )

        npm_pkg = paths.code_dir / "vendor" / "demo-npm"
        npm_pkg.mkdir(parents=True, exist_ok=True)
        (npm_pkg / "package.json").write_text(
            '{"name":"demo-npm","version":"1.0.0","main":"index.js"}\n',
            encoding="utf-8",
        )
        (npm_pkg / "index.js").write_text("module.exports = 'from-host-mount';\n", encoding="utf-8")

        install = await default_container_runner.execute_task(
            task_id="test-deps-install",
            run_id="run-deps-1",
            prompt="unused",
            engine="command",
            command_override=[
                "/bin/bash",
                "-c",
                "python -m pip install --disable-pip-version-check --no-index /workspace/vendor/demo_dep "
                "&& python -c 'import demo_dep,sys; print(demo_dep.VALUE); print(sys.prefix)' "
                "&& npm install --offline --no-fund --no-audit /workspace/vendor/demo-npm "
                "&& node -e \"console.log(require('demo-npm')); console.log(require.resolve('demo-npm'))\"",
            ],
            workspace_id=ws_id,
        )
        self.assertEqual(install.status, "completed", install.stderr or install.stdout)
        self.assertIn("from-host-mount", install.stdout)
        self.assertIn("/workspace/.venv", install.stdout)

        installed_py = list(paths.code_dir.glob(".venv/lib/python*/site-packages/demo_dep.py"))
        self.assertTrue(installed_py, f"Python dependency missing under host mount {paths.code_dir}")
        npm_link = paths.code_dir / "node_modules" / "demo-npm"
        self.assertTrue(npm_link.exists(), f"npm dependency missing under host mount {npm_link}")

        reuse = await default_container_runner.execute_task(
            task_id="test-deps-reuse",
            run_id="run-deps-2",
            prompt="unused",
            engine="command",
            command_override=[
                "/bin/bash",
                "-c",
                "python -c 'import demo_dep; print(demo_dep.VALUE)' && node -e \"console.log(require('demo-npm'))\"",
            ],
            workspace_id=ws_id,
        )
        self.assertEqual(reuse.status, "completed", reuse.stderr or reuse.stdout)
        self.assertEqual(reuse.stdout.count("from-host-mount"), 2)

        other = default_path_manager.get_paths("org_other", "user_other", "ws_other")
        default_path_manager.ensure_workspace(other)
        self.assertFalse((other.code_dir / "node_modules" / "demo-npm").exists())
        self.assertFalse(any(other.code_dir.glob(".venv/lib/python*/site-packages/demo_dep.py")))


if __name__ == "__main__":
    unittest.main()
