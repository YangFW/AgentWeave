from __future__ import annotations

import asyncio
import inspect
import os
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any, Awaitable, Callable
from unittest.mock import ANY, AsyncMock, patch

from fastapi.testclient import TestClient

from app import db
from app import main as main_module
from app.builtin_skill_catalog import get_builtin_skill
from app.schemas import ApprovalRequest
from app.services.agent_runtime import AgentRuntime, create_task_record
from app.services import mcp_gateway as mcp_module
from app.services.skill_registry import SkillRegistry
from app.services.task_state import PublicationConflict, TaskStateService


class NoToolsGateway:
    def list_tools(self) -> list[dict[str, Any]]:
        return []


class CompletionModel:
    def __init__(self, *, blocking: bool = False) -> None:
        self.answer = "WorkBuddy 的 PRD 已完成，包含产品目标、用户故事和可验证的验收标准。"
        self.blocking = blocking
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.prompts: list[str] = []

    async def resolve_intent(
        self, message: str, history: list[dict[str, str]], model_config_id: str
    ) -> dict[str, Any]:
        return {
            "standalone_request": message,
            "intent": "product_requirement",
            "parameters": {},
            "missing_information": [],
            "is_follow_up": False,
            "source": "direct",
        }

    async def solve_with_tools(
        self,
        prompt: str,
        system_prompt: str,
        model_config_id: str,
        tools: list[dict[str, Any]],
        invoke: Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]],
        max_steps: int = 8,
        on_delta: Callable[[str], Awaitable[None] | None] | None = None,
        history: list[dict[str, str]] | None = None,
    ) -> str:
        self.prompts.append(prompt)
        self.started.set()
        if self.blocking:
            await self.release.wait()
        if on_delta:
            pending = on_delta(self.answer)
            if inspect.isawaitable(pending):
                await pending
        return self.answer


class SkillRecommendationRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.original_db_path = db.DB_PATH
        self.temp_dir = tempfile.TemporaryDirectory()
        db.DB_PATH = Path(self.temp_dir.name) / "skill-recommendation.db"
        db.init_db()
        self.state = TaskStateService(db.get_conn)
        self.registry = SkillRegistry()
        self.registry.load_builtin_skills()

    async def asyncTearDown(self) -> None:
        db.DB_PATH = self.original_db_path
        self.temp_dir.cleanup()

    async def _waiting_task(self, runtime: AgentRuntime) -> tuple[dict[str, Any], dict[str, Any]]:
        task = create_task_record(
            "请给 WorkBuddy 写一份 PRD，包含产品目标、用户故事和验收标准，直接回答即可。",
            "general-agent",
            conversation_id="conv_skill_recommendation",
        )
        await runtime.run_task(task["id"])
        stored = db.query_one("SELECT * FROM tasks WHERE id = ?", (task["id"],))
        runs = self.state.list_runs(task_id=task["id"])
        self.assertEqual(stored["status"], "waiting_approval")
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0]["status"], "waiting_approval")
        self.assertIsNone(self.registry.get_skill("product_requirement_document"))
        return task, runs[0]

    async def test_approval_installs_immediately_and_completes_original_task_in_same_run(self) -> None:
        model = CompletionModel(blocking=True)
        runtime = AgentRuntime(
            self.registry,
            NoToolsGateway(),
            model,
            task_state=self.state,
        )
        task, original_run = await self._waiting_task(runtime)

        continuation = asyncio.create_task(
            runtime.resume_after_approval(task["id"], True, "确认安装并继续")
        )
        try:
            await asyncio.wait_for(model.started.wait(), timeout=5)
            installed = self.registry.get_skill("product_requirement_document")
            self.assertIsNotNone(installed)
            self.assertTrue(installed["enabled"])
            in_progress = db.query_one("SELECT status FROM tasks WHERE id = ?", (task["id"],))
            self.assertEqual(in_progress["status"], "running")
            active_runs = self.state.list_runs(task_id=task["id"])
            self.assertEqual(len(active_runs), 1)
            self.assertEqual(active_runs[0]["id"], original_run["id"])
            model.release.set()
            await asyncio.wait_for(continuation, timeout=5)
        finally:
            if not continuation.done():
                continuation.cancel()
                await asyncio.gather(continuation, return_exceptions=True)

        stored = db.query_one("SELECT * FROM tasks WHERE id = ?", (task["id"],))
        final_run = self.state.list_runs(task_id=task["id"])
        self.assertEqual(stored["status"], "completed")
        self.assertEqual(len(final_run), 1)
        self.assertEqual(final_run[0]["id"], original_run["id"])
        self.assertEqual(final_run[0]["status"], "completed")
        self.assertIn("### Skill: 产品需求文档 PRD Skill", model.prompts[0])
        events = db.query_all(
            "SELECT type, content FROM task_events WHERE task_id = ? ORDER BY id",
            (task["id"],),
        )
        self.assertEqual(sum(item["type"] == "approval_required" for item in events), 1)
        self.assertEqual(sum(item["type"] == "install" for item in events), 1)
        self.assertTrue(any(item["type"] == "answer" for item in events))
        self.assertFalse(any("重新发送" in item["content"] for item in events))

    async def test_rejection_keeps_environment_unchanged_but_completes_original_task(self) -> None:
        model = CompletionModel()
        runtime = AgentRuntime(
            self.registry,
            NoToolsGateway(),
            model,
            task_state=self.state,
        )
        task, original_run = await self._waiting_task(runtime)

        await runtime.resume_after_approval(task["id"], False, "暂不安装，继续完成")

        stored = db.query_one("SELECT * FROM tasks WHERE id = ?", (task["id"],))
        final_runs = self.state.list_runs(task_id=task["id"])
        self.assertEqual(stored["status"], "completed")
        self.assertIsNone(self.registry.get_skill("product_requirement_document"))
        self.assertEqual(len(final_runs), 1)
        self.assertEqual(final_runs[0]["id"], original_run["id"])
        self.assertEqual(final_runs[0]["status"], "completed")
        decision = final_runs[0]["metadata"]["skill_recommendation_decision"]
        self.assertEqual(
            decision["recommendation_id"], "product_requirement_document"
        )
        self.assertEqual(decision["decision"], "rejected")
        self.assertFalse(decision["approved"])
        self.assertFalse(decision["superseded"])
        self.assertTrue(decision["proof_hash"])
        self.assertEqual(len(model.prompts), 1)
        self.assertNotIn("### Skill: 产品需求文档 PRD Skill", model.prompts[0])
        events = db.query_all(
            "SELECT type, content FROM task_events WHERE task_id = ? ORDER BY id",
            (task["id"],),
        )
        self.assertEqual(sum(item["type"] == "approval_required" for item in events), 1)
        self.assertEqual(sum(item["type"] == "install" for item in events), 0)
        self.assertTrue(any(item["type"] == "answer" for item in events))

    async def test_direct_pptx_configuration_is_a_platform_command(self) -> None:
        """The chat command must configure the bundled generator without a model call."""

        model = CompletionModel()
        runtime = AgentRuntime(
            self.registry,
            NoToolsGateway(),
            model,
            task_state=self.state,
        )
        original_base_dir = mcp_module.BASE_DIR
        try:
            mcp_module.BASE_DIR = Path(self.temp_dir.name)
            with patch.dict(os.environ, {"APP_PPTX_GENERATOR": ""}, clear=False):
                task = create_task_record(
                    "直接给我配置好PPT工具",
                    "general-agent",
                    conversation_id="conv_pptx_configuration",
                )
                await runtime.run_task(task["id"])
        finally:
            mcp_module.BASE_DIR = original_base_dir

        stored = db.query_one("SELECT * FROM tasks WHERE id = ?", (task["id"],))
        self.assertEqual(stored["status"], "completed")
        self.assertEqual(model.prompts, [])
        answer = db.query_one(
            "SELECT content FROM task_events WHERE task_id = ? AND type = 'answer'",
            (task["id"],),
        )
        self.assertIn("已按你的要求配置好 PPT 工具", answer["content"])
        self.assertIn("python-pptx", db.json_loads(stored["result_json"]) ["generator"])
        self.assertEqual(
            (Path(self.temp_dir.name) / ".env.local").read_text(encoding="utf-8").strip(),
            'APP_PPTX_GENERATOR="python"',
        )

    async def test_pptx_generation_request_is_not_misclassified_as_configuration(self) -> None:
        self.assertFalse(
            AgentRuntime._looks_like_presentation_configuration(
                "关于上面的信息，帮我整理成一份ppt"
            )
        )
        self.assertFalse(
            AgentRuntime._looks_like_presentation_configuration(
                "如果 PPTX 能力已配置再生成 PPTX，并提供预览和下载"
            )
        )
        self.assertFalse(
            AgentRuntime._looks_like_presentation_configuration(
                "配置好 PPT 工具后，再生成一份旅行计划 PPT"
            )
        )
        self.assertTrue(
            AgentRuntime._looks_like_presentation_configuration(
                "直接给我配置好PPT工具"
            )
        )

    async def test_one_decision_does_not_chain_another_matching_recommendation(self) -> None:
        model = CompletionModel()
        runtime = AgentRuntime(
            self.registry,
            NoToolsGateway(),
            model,
            task_state=self.state,
        )
        task = create_task_record(
            "请用 Mermaid 流程图表达 WorkBuddy PRD 的用户故事和验收标准。",
            "general-agent",
        )

        await runtime.run_task(task["id"])
        waiting = db.query_one("SELECT * FROM tasks WHERE id = ?", (task["id"],))
        waiting_result = db.json_loads(waiting["result_json"], {})
        self.assertEqual(waiting["status"], "waiting_approval")
        self.assertEqual(waiting_result["recommendation_id"], "mermaid_diagram")

        await runtime.resume_after_approval(task["id"], False, "不安装，直接继续")

        stored = db.query_one("SELECT status FROM tasks WHERE id = ?", (task["id"],))
        self.assertEqual(stored["status"], "completed")
        self.assertIsNone(self.registry.get_skill("mermaid_diagram"))
        self.assertIsNone(self.registry.get_skill("product_requirement_document"))
        self.assertEqual(len(model.prompts), 1)
        approval_count = db.query_one(
            "SELECT COUNT(*) AS count FROM task_events WHERE task_id = ? AND type = 'approval_required'",
            (task["id"],),
        )
        self.assertEqual(approval_count["count"], 1)

    async def test_recommendation_resume_activates_task_projection_with_run_claim(
        self,
    ) -> None:
        model = CompletionModel()
        runtime = AgentRuntime(
            self.registry,
            NoToolsGateway(),
            model,
            task_state=self.state,
        )
        task, original_run = await self._waiting_task(runtime)
        original_update = db.update_task_status

        def reject_separate_running_projection(
            task_id: str,
            status: str,
            result: dict[str, Any] | None = None,
            artifacts: list[dict[str, Any]] | None = None,
        ) -> None:
            if task_id == task["id"] and status == "running":
                raise AssertionError(
                    "approval resume must activate Task and Run in one TaskState transaction"
                )
            original_update(task_id, status, result=result, artifacts=artifacts)

        with patch.object(
            db,
            "update_task_status",
            side_effect=reject_separate_running_projection,
        ):
            await runtime.resume_after_approval(task["id"], False, "跳过安装并继续")

        stored = db.query_one("SELECT status FROM tasks WHERE id = ?", (task["id"],)) or {}
        resumed_run = self.state.get_run(original_run["id"]) or {}
        self.assertEqual(stored.get("status"), "completed")
        self.assertEqual(resumed_run.get("status"), "completed")
        self.state.assert_terminal_clean(
            task_id=task["id"], run_id=original_run["id"]
        )

    async def test_recommendation_request_rolls_back_event_task_and_run_together(
        self,
    ) -> None:
        task = create_task_record("请写一份 PRD", "general-agent")
        run = self.state.begin_run(
            task["id"], activate_task_projection=True
        )
        connection = db.get_conn()
        try:
            connection.execute(
                """
                CREATE TRIGGER fail_recommendation_task_projection
                BEFORE UPDATE OF status ON tasks
                WHEN NEW.id = OLD.id AND NEW.status = 'waiting_approval'
                BEGIN
                    SELECT RAISE(ABORT, 'injected recommendation failure');
                END
                """
            )
            connection.commit()
        finally:
            connection.close()

        with self.assertRaises(sqlite3.IntegrityError):
            self.state.commit_skill_recommendation_request(
                task_id=task["id"],
                run_id=run["id"],
                approval_id="skill_recommendation_atomic_request",
                recommendation_id="product_requirement_document",
                result={},
                title="安装内置 Skill",
                content="是否安装后继续任务？",
                data={"action": "install_recommended_skill"},
            )

        stored = db.query_one(
            "SELECT status FROM tasks WHERE id = ?", (task["id"],)
        ) or {}
        self.assertEqual(stored.get("status"), "running")
        self.assertEqual(self.state.get_run(run["id"])["status"], "running")
        self.assertEqual(
            db.query_one(
                "SELECT COUNT(*) AS count FROM task_events WHERE task_id = ?",
                (task["id"],),
            )["count"],
            0,
        )
        self.assertNotIn(
            "pending_skill_recommendation",
            self.state.get_run(run["id"])["metadata"],
        )

    async def test_install_and_decision_roll_back_as_one_transaction(self) -> None:
        runtime = AgentRuntime(
            self.registry,
            NoToolsGateway(),
            CompletionModel(),
            task_state=self.state,
        )
        task, original_run = await self._waiting_task(runtime)
        before = db.query_one(
            "SELECT COUNT(*) AS count FROM task_events WHERE task_id = ?",
            (task["id"],),
        )["count"]
        real_install = self.registry.install_content_in_transaction

        def install_then_fail(*args: Any, **kwargs: Any) -> dict[str, Any]:
            real_install(*args, **kwargs)
            raise RuntimeError("injected failure after registry write")

        with (
            patch.object(
                self.registry,
                "install_content_in_transaction",
                side_effect=install_then_fail,
            ),
            self.assertRaisesRegex(
                RuntimeError, "injected failure after registry write"
            ),
        ):
            await runtime.resume_after_approval(
                task["id"], True, "安装并继续"
            )

        self.assertIsNone(
            self.registry.get_skill("product_requirement_document")
        )
        stored = db.query_one(
            "SELECT status FROM tasks WHERE id = ?", (task["id"],)
        ) or {}
        self.assertEqual(stored.get("status"), "waiting_approval")
        self.assertEqual(
            self.state.get_run(original_run["id"])["status"],
            "waiting_approval",
        )
        commands = self.state.list_commands(
            task_id=task["id"], command_types=["approval"]
        )
        self.assertEqual(len(commands), 1)
        self.assertEqual(commands[0]["status"], "queued")
        after = db.query_one(
            "SELECT COUNT(*) AS count FROM task_events WHERE task_id = ?",
            (task["id"],),
        )["count"]
        self.assertEqual(after, before)

    async def test_approval_is_bound_to_the_recommended_package_hash(self) -> None:
        runtime = AgentRuntime(
            self.registry,
            NoToolsGateway(),
            CompletionModel(),
            task_state=self.state,
        )
        task, original_run = await self._waiting_task(runtime)
        pending = self.state.get_run(original_run["id"])["metadata"][
            "pending_skill_recommendation"
        ]
        fingerprint = pending["recommendation_fingerprint"]
        self.assertEqual(fingerprint["schema"], "builtin-skill-package/1.0")
        self.assertTrue(fingerprint["package_hash"])
        changed = dict(get_builtin_skill("product_requirement_document") or {})
        changed["content"] = str(changed["content"]) + "\n审批等待期间发生变化。\n"

        with (
            patch(
                "app.services.agent_runtime.get_builtin_skill",
                return_value=changed,
            ),
            self.assertRaisesRegex(PublicationConflict, "内容在审批期间发生变化"),
        ):
            await runtime.resume_after_approval(task["id"], True, "安装并继续")

        self.assertIsNone(self.registry.get_skill("product_requirement_document"))
        self.assertEqual(
            db.query_one("SELECT status FROM tasks WHERE id = ?", (task["id"],))[
                "status"
            ],
            "waiting_approval",
        )
        approval = self.state.list_commands(
            task_id=task["id"], command_types=["approval"]
        )[0]
        self.assertEqual(approval["status"], "queued")

    async def test_approval_never_overwrites_a_different_skill_installed_while_waiting(
        self,
    ) -> None:
        runtime = AgentRuntime(
            self.registry,
            NoToolsGateway(),
            CompletionModel(),
            task_state=self.state,
        )
        task, _ = await self._waiting_task(runtime)
        manual_content = """---
id: product_requirement_document
name: 用户手动安装的 PRD Skill
description: 审批等待期间由用户安装的不同版本。
version: 9.9.9
---
# 用户版本

不得被旧推荐静默覆盖。
"""
        self.registry.install_content(manual_content)

        with self.assertRaisesRegex(PublicationConflict, "必须单独确认覆盖"):
            await runtime.resume_after_approval(task["id"], True, "安装并继续")

        installed = self.registry.get_skill("product_requirement_document") or {}
        self.assertEqual(installed.get("version"), "9.9.9")
        self.assertEqual(installed.get("content"), manual_content)
        self.assertEqual(
            db.query_one("SELECT status FROM tasks WHERE id = ?", (task["id"],))[
                "status"
            ],
            "waiting_approval",
        )

    async def test_message_winning_approval_does_not_install_stale_skill(
        self,
    ) -> None:
        model = CompletionModel()
        runtime = AgentRuntime(
            self.registry,
            NoToolsGateway(),
            model,
            task_state=self.state,
        )
        task, original_run = await self._waiting_task(runtime)
        message_command = self.state.enqueue_command(
            task["id"],
            "message",
            run_id=original_run["id"],
            payload={"message": "改成简短说明，不需要安装 PRD Skill。"},
        )

        await runtime.resume_after_approval(task["id"], True, "安装并继续")

        self.assertIsNone(
            self.registry.get_skill("product_requirement_document")
        )
        stored = db.query_one(
            "SELECT status FROM tasks WHERE id = ?", (task["id"],)
        ) or {}
        self.assertEqual(stored.get("status"), "completed")
        self.assertEqual(
            self.state.get_command(message_command["id"])["status"],
            "completed",
        )
        approval = self.state.list_commands(
            task_id=task["id"], command_types=["approval"]
        )[0]
        self.assertEqual(approval["status"], "completed")
        self.assertTrue(approval["result"]["superseded"])
        self.assertNotIn("产品需求文档 PRD Skill", model.prompts[-1])
        self.state.assert_terminal_clean(
            task_id=task["id"], run_id=original_run["id"]
        )

    async def test_cancel_winning_approval_never_installs_skill(self) -> None:
        runtime = AgentRuntime(
            self.registry,
            NoToolsGateway(),
            CompletionModel(),
            task_state=self.state,
        )
        task, original_run = await self._waiting_task(runtime)
        self.state.request_cancel(
            task["id"], run_id=original_run["id"], reason="用户取消"
        )

        await runtime.resume_after_approval(task["id"], True, "安装并继续")

        self.assertIsNone(
            self.registry.get_skill("product_requirement_document")
        )
        stored = db.query_one(
            "SELECT status FROM tasks WHERE id = ?", (task["id"],)
        ) or {}
        self.assertEqual(stored.get("status"), "cancelled")
        self.assertEqual(
            self.state.get_run(original_run["id"])["status"], "cancelled"
        )
        approval = self.state.list_commands(
            task_id=task["id"], command_types=["approval"]
        )[0]
        self.assertEqual(approval["status"], "completed")
        self.assertTrue(approval["result"]["superseded"])
        self.state.assert_terminal_clean(
            task_id=task["id"], run_id=original_run["id"]
        )

    async def test_message_still_wins_when_recommended_catalog_entry_disappears(
        self,
    ) -> None:
        model = CompletionModel()
        runtime = AgentRuntime(
            self.registry,
            NoToolsGateway(),
            model,
            task_state=self.state,
        )
        task, original_run = await self._waiting_task(runtime)
        message = self.state.enqueue_command(
            task["id"],
            "message",
            run_id=original_run["id"],
            payload={"message": "改为只给一段简短说明，不安装任何 Skill。"},
        )

        with patch(
            "app.services.agent_runtime.get_builtin_skill", return_value=None
        ) as catalog_lookup:
            await runtime.resume_after_approval(task["id"], True, "安装并继续")

        catalog_lookup.assert_not_called()
        self.assertIsNone(self.registry.get_skill("product_requirement_document"))
        self.assertEqual(
            db.query_one("SELECT status FROM tasks WHERE id = ?", (task["id"],))[
                "status"
            ],
            "completed",
        )
        self.assertEqual(self.state.get_command(message["id"])["status"], "completed")
        approval = self.state.list_commands(
            task_id=task["id"], command_types=["approval"]
        )[0]
        self.assertTrue(approval["result"]["superseded"])

    async def test_cancel_still_wins_when_recommended_catalog_entry_disappears(
        self,
    ) -> None:
        runtime = AgentRuntime(
            self.registry,
            NoToolsGateway(),
            CompletionModel(),
            task_state=self.state,
        )
        task, original_run = await self._waiting_task(runtime)
        cancel = self.state.request_cancel(
            task["id"], run_id=original_run["id"], reason="用户取消"
        )

        with patch(
            "app.services.agent_runtime.get_builtin_skill", return_value=None
        ) as catalog_lookup:
            await runtime.resume_after_approval(task["id"], True, "安装并继续")

        catalog_lookup.assert_not_called()
        self.assertIsNone(self.registry.get_skill("product_requirement_document"))
        self.assertEqual(
            db.query_one("SELECT status FROM tasks WHERE id = ?", (task["id"],))[
                "status"
            ],
            "cancelled",
        )
        self.assertEqual(self.state.get_command(cancel["id"])["status"], "completed")
        approval = self.state.list_commands(
            task_id=task["id"], command_types=["approval"]
        )[0]
        self.assertTrue(approval["result"]["superseded"])

    async def test_real_approval_api_continuation_accepts_durable_command_id(
        self,
    ) -> None:
        runtime = AgentRuntime(
            self.registry,
            NoToolsGateway(),
            CompletionModel(),
            task_state=self.state,
        )
        task, original_run = await self._waiting_task(runtime)
        before = set(main_module._runtime_tasks)

        with patch.object(main_module, "runtime", runtime):
            response = await main_module.approve_task(
                task["id"],
                ApprovalRequest(approved=True, note="通过 API 安装并继续"),
            )
            spawned = [
                item for item in main_module._runtime_tasks if item not in before
            ]
            self.assertEqual(len(spawned), 1)
            await asyncio.wait_for(
                asyncio.gather(*spawned, return_exceptions=False), timeout=5
            )

        stored = db.query_one(
            "SELECT status FROM tasks WHERE id = ?", (task["id"],)
        ) or {}
        self.assertEqual(response["ok"], True)
        self.assertFalse(response["duplicate"])
        self.assertEqual(stored.get("status"), "completed")
        self.assertIsNotNone(
            self.registry.get_skill("product_requirement_document")
        )
        approval = self.state.get_command(response["command"]["id"])
        self.assertEqual(approval["status"], "completed")
        self.assertEqual(
            approval["result"]["recommendation_id"],
            "product_requirement_document",
        )
        self.state.assert_terminal_clean(
            task_id=task["id"], run_id=original_run["id"]
        )


class SkillRecommendationApprovalApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_db_path = db.DB_PATH
        self.original_app_db_path = os.environ.get("APP_DB_PATH")
        self.db_path = Path(self.temp_dir.name) / "skill-recommendation-api.db"
        os.environ["APP_DB_PATH"] = str(self.db_path)
        db.DB_PATH = self.db_path
        db.init_db()
        self.state = TaskStateService(db.get_conn)

        self.resume_patch = patch.object(
            main_module.runtime, "resume_after_approval", new_callable=AsyncMock
        )
        self.scheduler_start_patch = patch.object(
            main_module.loop_scheduler, "start", return_value=None
        )
        self.scheduler_stop_patch = patch.object(
            main_module.loop_scheduler, "stop", new_callable=AsyncMock
        )
        self.resume = self.resume_patch.start()
        self.scheduler_start_patch.start()
        self.scheduler_stop_patch.start()
        self.client_context = TestClient(main_module.app)
        self.client = self.client_context.__enter__()

    def tearDown(self) -> None:
        self.client_context.__exit__(None, None, None)
        self.scheduler_stop_patch.stop()
        self.scheduler_start_patch.stop()
        self.resume_patch.stop()
        db.DB_PATH = self.original_db_path
        if self.original_app_db_path is None:
            os.environ.pop("APP_DB_PATH", None)
        else:
            os.environ["APP_DB_PATH"] = self.original_app_db_path
        self.temp_dir.cleanup()

    def _waiting_task(self) -> str:
        task = create_task_record("请生成 PRD", "general-agent")
        run = self.state.begin_run(task["id"])
        self.state.transition_run(run["id"], "waiting_approval")
        db.update_task_status(
            task["id"],
            "waiting_approval",
            result={
                "pending_action": "install_recommended_skill",
                "recommendation_id": "product_requirement_document",
                "skill_recommendation_approval_id": (
                    "skill_recommendation_api_test"
                ),
            },
        )
        return task["id"]

    def _waiting_policy_task(self) -> str:
        task = create_task_record("执行需要审批的策略动作", "general-agent")
        run = self.state.begin_run(
            task["id"], activate_task_projection=True
        )
        self.state.commit_policy_approval_request(
            task_id=task["id"],
            run_id=run["id"],
            approval_id="policy_approval_api_test",
            result={
                "pending_action": "policy_approval",
                "policy_approval_id": "policy_approval_api_test",
                "summary": "需要审批策略动作",
            },
            title="策略要求审批",
            content="需要审批策略动作",
            data={"action": "policy_approval"},
        )
        return task["id"]

    def test_approval_endpoint_dispatches_both_skill_recommendation_decisions(self) -> None:
        task_ids = [self._waiting_task(), self._waiting_task()]

        approved = self.client.post(
            f"/api/tasks/{task_ids[0]}/approve",
            json={"approved": True, "note": "安装并继续"},
        )
        rejected = self.client.post(
            f"/api/tasks/{task_ids[1]}/approve",
            json={"approved": False, "note": "跳过并继续"},
        )

        self.assertEqual(approved.status_code, 200, approved.text)
        self.assertEqual(rejected.status_code, 200, rejected.text)
        deadline = time.monotonic() + 2
        while self.resume.await_count < 2 and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(self.resume.await_count, 2)
        self.resume.assert_any_await(
            task_ids[0], True, "安装并继续", command_id=ANY
        )
        self.resume.assert_any_await(
            task_ids[1], False, "跳过并继续", command_id=ANY
        )

    def test_recommendation_approval_is_one_durable_decision(self) -> None:
        task_id = self._waiting_task()

        first = self.client.post(
            f"/api/tasks/{task_id}/approve",
            json={"approved": True, "note": "安装并继续"},
        )
        duplicate = self.client.post(
            f"/api/tasks/{task_id}/approve",
            json={"approved": True, "note": "安装并继续"},
        )
        conflict = self.client.post(
            f"/api/tasks/{task_id}/approve",
            json={"approved": False, "note": "跳过并继续"},
        )

        self.assertEqual(first.status_code, 200, first.text)
        self.assertFalse(first.json()["duplicate"])
        self.assertEqual(duplicate.status_code, 200, duplicate.text)
        self.assertTrue(duplicate.json()["duplicate"])
        self.assertEqual(conflict.status_code, 409, conflict.text)
        deadline = time.monotonic() + 2
        while self.resume.await_count < 1 and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(self.resume.await_count, 1)
        self.resume.assert_awaited_once_with(
            task_id, True, "安装并继续", command_id=ANY
        )
        commands = self.state.list_commands(
            task_id=task_id,
            command_types=["approval"],
        )
        self.assertEqual(len(commands), 1)
        self.assertEqual(commands[0]["payload"]["approved"], True)
        self.assertEqual(commands[0]["payload"]["note"], "安装并继续")
        self.assertTrue(commands[0]["payload"]["decision_request_id"])
        self.assertEqual(commands[0]["status"], "queued")

    def test_policy_approval_retries_and_conflicts_share_the_same_decision(self) -> None:
        task_id = self._waiting_policy_task()

        first = self.client.post(
            f"/api/tasks/{task_id}/approve",
            json={"approved": False, "note": "不允许执行"},
        )
        duplicate = self.client.post(
            f"/api/tasks/{task_id}/approve",
            json={"approved": False, "note": "不允许执行"},
        )
        conflict = self.client.post(
            f"/api/tasks/{task_id}/approve",
            json={"approved": True, "note": "改为允许"},
        )

        self.assertEqual(first.status_code, 200, first.text)
        self.assertFalse(first.json()["duplicate"])
        self.assertEqual(duplicate.status_code, 200, duplicate.text)
        self.assertTrue(duplicate.json()["duplicate"])
        self.assertEqual(conflict.status_code, 409, conflict.text)
        commands = self.state.list_commands(
            task_id=task_id,
            command_types=["approval"],
        )
        self.assertEqual(len(commands), 1)
        self.assertEqual(commands[0]["status"], "queued")
        self.assertEqual(commands[0]["payload"]["approved"], False)
        self.assertEqual(commands[0]["payload"]["note"], "不允许执行")
        self.assertTrue(commands[0]["payload"]["decision_request_id"])
        self.resume.assert_not_awaited()

    def test_approval_background_failure_is_visible_and_fails_the_task(self) -> None:
        task_id = self._waiting_task()
        self.resume.side_effect = RuntimeError("injected approval continuation failure")

        response = self.client.post(
            f"/api/tasks/{task_id}/approve",
            json={"approved": True, "note": "安装并继续"},
        )

        self.assertEqual(response.status_code, 200, response.text)
        deadline = time.monotonic() + 2
        stored: dict[str, Any] = {}
        while time.monotonic() < deadline:
            stored = db.query_one(
                "SELECT status FROM tasks WHERE id = ?", (task_id,)
            ) or {}
            if stored.get("status") == "failed":
                break
            time.sleep(0.01)
        self.assertEqual(stored.get("status"), "failed")
        run = self.state.list_runs(task_id=task_id)[0]
        self.assertEqual(run["status"], "failed")
        error_events = db.query_all(
            "SELECT content FROM task_events WHERE task_id = ? AND type = 'error'",
            (task_id,),
        )
        self.assertTrue(error_events)
        self.assertIn("审批后续处理失败", error_events[-1]["content"])

    def test_non_waiting_task_rejects_approval_without_persisting_a_decision(self) -> None:
        task = create_task_record("普通运行任务", "general-agent")
        self.state.begin_run(task["id"])

        response = self.client.post(
            f"/api/tasks/{task['id']}/approve",
            json={"approved": True, "note": "不应接受"},
        )

        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(
            self.state.list_commands(
                task_id=task["id"], command_types=["approval"]
            ),
            [],
        )

    def test_completed_approval_retry_is_idempotent_but_conflict_is_rejected(
        self,
    ) -> None:
        task_id = self._waiting_task()
        first = self.client.post(
            f"/api/tasks/{task_id}/approve",
            json={"approved": True, "note": "安装并继续"},
        )
        self.assertEqual(first.status_code, 200, first.text)
        command = self.state.list_commands(
            task_id=task_id, command_types=["approval"]
        )[0]
        claimed = self.state.claim_command(
            "test-completion",
            task_id=task_id,
            run_id=str(command["run_id"]),
            command_types=["approval"],
        )
        self.assertEqual(claimed["id"], command["id"])
        self.state.complete_command(
            command["id"],
            result={
                "action": "install_recommended_skill",
                "approved": True,
                "superseded": False,
            },
        )
        run = self.state.list_runs(task_id=task_id)[0]
        db.update_task_status(task_id, "completed")
        self.state.finish_run(run["id"], status="completed", result={"ok": True})

        duplicate = self.client.post(
            f"/api/tasks/{task_id}/approve",
            json={"approved": True, "note": "安装并继续"},
        )
        conflict = self.client.post(
            f"/api/tasks/{task_id}/approve",
            json={"approved": False, "note": "改为拒绝"},
        )

        self.assertEqual(duplicate.status_code, 200, duplicate.text)
        self.assertTrue(duplicate.json()["duplicate"])
        self.assertEqual(conflict.status_code, 409, conflict.text)

    def test_completed_waiting_decision_retry_redispatches_continuation(self) -> None:
        task_id = self._waiting_task()
        first = self.client.post(
            f"/api/tasks/{task_id}/approve",
            json={"approved": True, "note": "安装并继续"},
        )
        self.assertEqual(first.status_code, 200, first.text)
        deadline = time.monotonic() + 2
        while self.resume.await_count < 1 and time.monotonic() < deadline:
            time.sleep(0.01)
        command = self.state.list_commands(
            task_id=task_id, command_types=["approval"]
        )[0]
        claimed = self.state.claim_command(
            "test-completion",
            task_id=task_id,
            run_id=str(command["run_id"]),
            command_types=["approval"],
        )
        self.assertEqual(claimed["id"], command["id"])
        self.state.complete_command(
            command["id"],
            result={
                "action": "install_recommended_skill",
                "approved": True,
                "superseded": False,
            },
        )
        self.resume.reset_mock()

        retry = self.client.post(
            f"/api/tasks/{task_id}/approve",
            json={"approved": True, "note": "安装并继续"},
        )

        self.assertEqual(retry.status_code, 200, retry.text)
        self.assertTrue(retry.json()["duplicate"])
        deadline = time.monotonic() + 2
        while self.resume.await_count < 1 and time.monotonic() < deadline:
            time.sleep(0.01)
        self.resume.assert_awaited_once_with(
            task_id,
            True,
            "安装并继续",
            command_id=command["id"],
        )


if __name__ == "__main__":
    unittest.main()
