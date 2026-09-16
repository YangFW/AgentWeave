from __future__ import annotations

import asyncio
import os
import sqlite3
import tempfile
import time
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from app import db
from app import main as main_module
from app.services.agent_runtime import create_task_record
from app.services.task_state import TaskStateService


class RestartCompletionModel:
    async def resolve_intent(self, message, history, model_config_id):
        return {
            "standalone_request": message,
            "intent": "analysis",
            "parameters": {},
            "missing_information": [],
            "is_follow_up": bool(history),
            "source": "restart-e2e",
        }

    async def solve_with_tools(
        self,
        prompt,
        system_prompt,
        model_config_id,
        tools,
        invoke,
        max_steps=8,
        on_delta=None,
        history=None,
    ):
        answer = "服务恢复后已按当前目标完成任务。"
        if on_delta is not None:
            emitted = on_delta(answer)
            if asyncio.iscoroutine(emitted):
                await emitted
        return answer


class RestartRecoveryTests(unittest.TestCase):
    """Verify that application startup durably recovers an interrupted run."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_db_path = db.DB_PATH
        self.original_app_db_path = os.environ.get("APP_DB_PATH")
        self.db_path = Path(self.temp_dir.name) / "restart-recovery.db"
        os.environ["APP_DB_PATH"] = str(self.db_path)
        db.DB_PATH = self.db_path
        db.init_db()
        self.state = TaskStateService(db.get_conn)

    def tearDown(self) -> None:
        db.DB_PATH = self.original_db_path
        if self.original_app_db_path is None:
            os.environ.pop("APP_DB_PATH", None)
        else:
            os.environ["APP_DB_PATH"] = self.original_app_db_path
        self.temp_dir.cleanup()

    def _running_task(self, message: str) -> tuple[dict, dict]:
        task = create_task_record(message, "general-agent")
        db.update_task_status(task["id"], "running")
        run = self.state.begin_run(task["id"])
        return task, run

    def test_api_restart_does_not_recover_worker_owned_running_attempt(self):
        task, run = self._running_task("Worker 正在执行")
        self.state.update_run_metadata(run["id"], {"dispatch_backend": "redis"})
        for _ in range(2):
            recovered = main_module._recover_interrupted_runs()
            self.assertEqual(recovered, [])
            self.assertEqual(self.state.get_run(run["id"])["status"], "running")
            self.assertEqual(db.query_one("SELECT status FROM tasks WHERE id=?", (task["id"],))["status"], "running")
            self.assertEqual(len(self.state.list_runs(task_id=task["id"])), 1)

    def test_worker_recovery_persists_new_dispatch_in_same_transaction(self):
        task, run = self._running_task("Worker 中断")
        self.state.update_run_metadata(run["id"], {"dispatch_backend": "redis"})
        first = self.state.recover_interrupted_attempt(run["id"])
        second = self.state.recover_interrupted_attempt(run["id"])
        self.assertEqual(first["run"]["id"], second["run"]["id"])
        self.assertEqual(first["run"]["metadata"]["dispatch_backend"], "redis")
        pending = db.query_all("SELECT run_id FROM dispatch_outbox WHERE delivered=0")
        self.assertEqual(pending, [{"run_id": first["run"]["id"]}])

    def test_worker_policy_decision_is_not_consumed_by_api_restart(self):
        task, run = self._running_task("等待 Policy 审批")
        self.state.update_run_metadata(run['id'], {'dispatch_backend': 'redis'})
        self._policy_wait(task, run, 'policy-worker-test')
        command = self.state.enqueue_command(task['id'], 'approval', run_id=run['id'], payload={'approved': True, 'approval_id': 'policy-worker-test'})
        self.assertEqual(db.query_one('SELECT command_id FROM approval_dispatch')['command_id'], command['id'])
        self.assertEqual(main_module._prepare_waiting_approval_recovery(), [])
        self.assertEqual(self.state.get_run(run['id'])['status'], 'waiting_approval')
        decision = self.state.commit_policy_approval_decision(task_id=task['id'], run_id=run['id'], approval_id='policy-worker-test', worker_id='worker-test')
        self.assertTrue(decision['approved'])
        recovery = self.state.recover_interrupted_attempt(run['id'])
        self.assertEqual(recovery['run']['metadata']['dispatch_backend'], 'redis')
        self.assertIn('policy_approval_decisions', recovery['run']['metadata'])
        self.assertIsNotNone(db.query_one('SELECT run_id FROM dispatch_outbox WHERE run_id=?', (recovery['run']['id'],)))

    def _policy_wait(
        self,
        task: dict,
        run: dict,
        approval_id: str,
    ) -> None:
        self.state.commit_policy_approval_request(
            task_id=task["id"],
            run_id=run["id"],
            approval_id=approval_id,
            result={
                "pending_action": "policy_approval",
                "policy_approval_id": approval_id,
                "policy_event": "tool.before",
                "summary": "需要确认外部工具调用",
            },
            title="工具调用需要审批",
            content="是否允许调用测试工具？",
            data={"action": "policy_approval", "event": "tool.before"},
        )

    def _startup_patches(self):
        return (
            patch.object(main_module.loop_scheduler, "start", return_value=None),
            patch.object(main_module.loop_scheduler, "stop", new_callable=AsyncMock),
            patch.object(main_module.skill_registry, "load_builtin_skills"),
            patch.object(main_module.mcp_gateway, "seed_builtin_servers"),
            patch.object(main_module, "seed_agents"),
            patch.object(main_module, "_reload_policy_rules"),
        )

    def test_startup_recovers_running_attempt_from_latest_safe_checkpoint_once(self) -> None:
        task = create_task_record(
            "从安全检查点恢复中断任务",
            "general-agent",
            conversation_id="conv_restart_recovery",
        )
        task_id = str(task["id"])
        db.update_task_status(task_id, "running")

        old_run = self.state.begin_run(task_id)
        safe_node = self.state.create_node(
            old_run["id"],
            "understand",
            "确认目标与执行边界",
            kind="step",
        )
        self.state.start_node(safe_node["id"])
        self.state.finish_node(
            safe_node["id"],
            output={"goal_confirmed": True},
            metadata={"safe_boundary": True},
        )
        checkpoint = self.state.create_checkpoint(
            old_run["id"],
            {
                "cursor": "after-understand",
                "completed_nodes": [safe_node["id"]],
            },
            node_id=safe_node["id"],
            reason="目标确认完成，可安全恢复",
            metadata={"safe": True},
        )
        interrupted_node = self.state.create_node(
            old_run["id"],
            "execute",
            "调用执行工具",
            kind="mcp",
        )
        self.state.start_node(interrupted_node["id"])

        with (
            patch.object(main_module.loop_scheduler, "start", return_value=None),
            patch.object(main_module.loop_scheduler, "stop", new_callable=AsyncMock),
            patch.object(main_module, "_schedule_runtime") as schedule_runtime,
            patch.object(main_module.skill_registry, "load_builtin_skills"),
            patch.object(main_module.mcp_gateway, "seed_builtin_servers"),
            patch.object(main_module, "seed_agents"),
            patch.object(main_module, "_reload_policy_rules"),
        ):
            with TestClient(main_module.app) as client:
                response = client.get("/api/health")
                self.assertEqual(response.status_code, 200, response.text)

                runs_after_startup = sorted(
                    self.state.list_runs(task_id=task_id),
                    key=lambda item: item["attempt"],
                )
                self.assertEqual(len(runs_after_startup), 2)
                recovered_run = runs_after_startup[1]
                schedule_runtime.assert_called_once_with(task_id, recovered_run["id"])

                old_run_after = self.state.get_run(old_run["id"])
                self.assertIsNotNone(old_run_after)
                self.assertEqual(old_run_after["status"], "failed")
                self.assertEqual(old_run_after["error"]["error_type"], "ServiceRestart")
                self.assertTrue(old_run_after["metadata"]["interrupted"])

                safe_node_after = self.state.get_node(safe_node["id"])
                self.assertIsNotNone(safe_node_after)
                self.assertEqual(safe_node_after["status"], "completed")
                interrupted_node_after = self.state.get_node(interrupted_node["id"])
                self.assertIsNotNone(interrupted_node_after)
                self.assertEqual(interrupted_node_after["status"], "failed")
                self.assertEqual(
                    interrupted_node_after["error"]["error_type"],
                    "ServiceRestart",
                )
                self.assertTrue(interrupted_node_after["metadata"]["interrupted"])
                self.assertNotEqual(interrupted_node_after["status"], "running")

                self.assertEqual(recovered_run["attempt"], 2)
                self.assertEqual(recovered_run["status"], "queued")
                self.assertEqual(
                    recovered_run["resumed_from_checkpoint_id"], checkpoint["id"]
                )
                self.assertTrue(recovered_run["metadata"]["recovered_after_restart"])
                self.assertEqual(
                    recovered_run["metadata"]["previous_run_id"], old_run["id"]
                )
                task_after = db.query_one("SELECT status FROM tasks WHERE id = ?", (task_id,))
                self.assertIsNotNone(task_after)
                self.assertEqual(task_after["status"], "queued")

                recovery_events = db.query_all(
                    "SELECT * FROM task_events WHERE task_id = ? AND type = 'recovery_scheduled'",
                    (task_id,),
                )
                self.assertEqual(len(recovery_events), 1)

                recovered_again = main_module._recover_interrupted_runs()
                runs_after_second_recovery = sorted(
                    self.state.list_runs(task_id=task_id),
                    key=lambda item: item["attempt"],
                )
                self.assertEqual(len(runs_after_second_recovery), 2)
                self.assertEqual(
                    [item["id"] for item in runs_after_second_recovery],
                    [old_run["id"], recovered_run["id"]],
                )
                self.assertEqual(
                    [item["id"] for item in recovered_again if item["task_id"] == task_id],
                    [recovered_run["id"]],
                )
                self.assertEqual(
                    len(
                        db.query_all(
                            "SELECT id FROM task_events WHERE task_id = ? AND type = 'recovery_scheduled'",
                            (task_id,),
                        )
                    ),
                    1,
                )

    def test_undecided_policy_wait_survives_restart_without_duplicate_prompt(self) -> None:
        task, run = self._running_task("等待用户决定后再调用工具")
        approval_id = "policy_approval_restart_undecided"
        self._policy_wait(task, run, approval_id)

        with ExitStack() as stack:
            for startup_patch in self._startup_patches():
                stack.enter_context(startup_patch)
            schedule_runtime = stack.enter_context(
                patch.object(main_module, "_schedule_runtime")
            )
            resume_approval = stack.enter_context(
                patch.object(
                    main_module,
                    "_resume_after_approval_safely",
                    new_callable=AsyncMock,
                )
            )
            with TestClient(main_module.app) as client:
                self.assertEqual(client.get("/api/health").status_code, 200)

        task_after = db.query_one(
            "SELECT status, result_json FROM tasks WHERE id = ?", (task["id"],)
        )
        run_after = self.state.get_run(run["id"])
        self.assertEqual(task_after["status"], "waiting_approval")
        self.assertEqual(run_after["status"], "waiting_approval")
        self.assertEqual(
            db.json_loads(task_after["result_json"], {})["policy_approval_id"],
            approval_id,
        )
        self.assertEqual(len(self.state.list_runs(task_id=task["id"])), 1)
        self.assertEqual(
            len(
                db.query_all(
                    "SELECT id FROM task_events WHERE task_id = ? "
                    "AND type = 'approval_required'",
                    (task["id"],),
                )
            ),
            1,
        )
        self.assertFalse(
            db.query_all(
                "SELECT id FROM task_events WHERE task_id = ? "
                "AND type = 'recovery_scheduled'",
                (task["id"],),
            )
        )
        schedule_runtime.assert_not_called()
        resume_approval.assert_not_awaited()

    def test_queued_policy_decision_becomes_proof_then_transfers_message(self) -> None:
        task, run = self._running_task("审批后调用工具，同时接受新的补充要求")
        checkpoint = self.state.create_checkpoint(
            run["id"],
            {"phase": "before_tool", "goal_spec_ref": {"spec_hash": "goal-a"}},
            reason="工具调用前安全边界",
        )
        approval_id = "policy_approval_restart_decided"
        self._policy_wait(task, run, approval_id)
        approval = self.state.enqueue_command(
            task["id"],
            "approval",
            run_id=run["id"],
            payload={
                "approved": True,
                "note": "允许本次调用",
                "approval_id": approval_id,
            },
            priority=90,
            command_id="tcmd_restart_policy_decision",
        )
        message = self.state.enqueue_command(
            task["id"],
            "message",
            run_id=run["id"],
            payload={"message": "改为只读取，不要执行写入"},
            priority=20,
            command_id="tcmd_restart_message",
        )

        with ExitStack() as stack:
            for startup_patch in self._startup_patches():
                stack.enter_context(startup_patch)
            schedule_runtime = stack.enter_context(
                patch.object(main_module, "_schedule_runtime")
            )
            resume_approval = stack.enter_context(
                patch.object(
                    main_module,
                    "_resume_after_approval_safely",
                    new_callable=AsyncMock,
                )
            )
            with TestClient(main_module.app) as client:
                self.assertEqual(client.get("/api/health").status_code, 200)

        runs = sorted(
            self.state.list_runs(task_id=task["id"]),
            key=lambda item: item["attempt"],
        )
        self.assertEqual(len(runs), 2)
        old_after, recovered = runs
        self.assertEqual(old_after["id"], run["id"])
        self.assertEqual(old_after["status"], "failed")
        self.assertEqual(recovered["status"], "queued")
        self.assertEqual(recovered["resumed_from_checkpoint_id"], checkpoint["id"])
        self.assertTrue(recovered["metadata"]["recovery_pending_runtime_input"])
        self.assertEqual(
            recovered["metadata"]["recovery_pending_command_types"], ["message"]
        )
        proof = recovered["metadata"]["policy_approval_decisions"][approval_id]
        self.assertTrue(proof["approved"])
        self.assertEqual(proof["command_id"], approval["id"])
        self.assertEqual(self.state.get_command(approval["id"])["status"], "completed")
        transferred = self.state.get_command(message["id"])
        self.assertEqual(transferred["status"], "queued")
        self.assertEqual(transferred["run_id"], recovered["id"])
        self.assertEqual(
            transferred["intake_generation"], message["intake_generation"]
        )
        self.assertEqual(recovered["accepted_generation"], 1)
        self.assertEqual(recovered["applied_generation"], 0)
        activation = recovered["metadata"]["recovery_activation_result"]
        self.assertEqual(
            activation["policy_approval_decisions"][approval_id]["proof_hash"],
            proof["proof_hash"],
        )
        schedule_runtime.assert_called_once_with(
            task["id"], recovered["id"], activation_result=activation
        )
        resume_approval.assert_not_awaited()
        self.assertEqual(
            len(
                db.query_all(
                    "SELECT id FROM task_events WHERE task_id = ? "
                    "AND type = 'approval_required'",
                    (task["id"],),
                )
            ),
            1,
        )
        self.assertEqual(
            len(
                db.query_all(
                    "SELECT id FROM task_events WHERE task_id = ? AND type = 'approval'",
                    (task["id"],),
                )
            ),
            1,
        )
        self.assertEqual(main_module._prepare_waiting_approval_recovery(), [])
        recovered_again = main_module._recover_interrupted_runs()
        self.assertEqual(
            [item["id"] for item in recovered_again if item["task_id"] == task["id"]],
            [recovered["id"]],
        )
        self.assertEqual(len(self.state.list_runs(task_id=task["id"])), 2)

    def test_pending_cancel_wins_over_policy_decision_on_restart(self) -> None:
        task, run = self._running_task("审批期间取消任务")
        approval_id = "policy_approval_restart_cancel"
        self._policy_wait(task, run, approval_id)
        approval = self.state.enqueue_command(
            task["id"],
            "approval",
            run_id=run["id"],
            payload={"approved": True, "note": "允许", "approval_id": approval_id},
            priority=90,
        )
        cancel = self.state.request_cancel(
            task["id"],
            run_id=run["id"],
            reason="用户在重启前取消",
            requested_by="user",
        )

        continuations = main_module._prepare_waiting_approval_recovery()
        recovered = main_module._recover_interrupted_runs()

        self.assertEqual(continuations, [])
        self.assertFalse(
            [item for item in recovered if item["task_id"] == task["id"]]
        )
        self.assertEqual(
            db.query_one("SELECT status FROM tasks WHERE id = ?", (task["id"],))[
                "status"
            ],
            "cancelled",
        )
        self.assertEqual(self.state.get_run(run["id"])["status"], "cancelled")
        self.assertEqual(self.state.get_command(cancel["id"])["status"], "completed")
        self.assertEqual(self.state.get_command(approval["id"])["status"], "cancelled")
        self.assertIsNone(
            self.state.get_policy_approval_decision(task["id"], approval_id)
        )

    def test_recommendation_queued_and_committed_decisions_are_rescheduled(self) -> None:
        undecided_task, undecided_run = self._running_task(
            "等待确认是否安装推荐 Skill"
        )
        self.state.commit_skill_recommendation_request(
            task_id=undecided_task["id"],
            run_id=undecided_run["id"],
            approval_id="skill_recommendation_restart_undecided",
            recommendation_id="markdown-generator",
            result={},
            title="推荐安装 Markdown Skill",
            content="确认后再继续",
            data={"action": "install_recommended_skill"},
        )

        queued_task, queued_run = self._running_task("安装推荐 Skill 后继续")
        queued_approval_id = "skill_recommendation_restart_queued"
        self.state.commit_skill_recommendation_request(
            task_id=queued_task["id"],
            run_id=queued_run["id"],
            approval_id=queued_approval_id,
            recommendation_id="docx-generator",
            result={},
            title="推荐安装文档 Skill",
            content="安装后继续生成文档",
            data={"action": "install_recommended_skill"},
        )
        queued_command = self.state.enqueue_command(
            queued_task["id"],
            "approval",
            run_id=queued_run["id"],
            payload={
                "approved": True,
                "note": "安装并继续",
                "approval_id": queued_approval_id,
            },
            priority=90,
        )

        committed_task, committed_run = self._running_task("跳过推荐 Skill 后继续")
        committed_approval_id = "skill_recommendation_restart_committed"
        self.state.commit_skill_recommendation_request(
            task_id=committed_task["id"],
            run_id=committed_run["id"],
            approval_id=committed_approval_id,
            recommendation_id="ppt-generator",
            result={},
            title="推荐安装演示文稿 Skill",
            content="是否安装推荐能力",
            data={"action": "install_recommended_skill"},
        )
        committed_command = self.state.enqueue_command(
            committed_task["id"],
            "approval",
            run_id=committed_run["id"],
            payload={
                "approved": False,
                "note": "暂不安装",
                "approval_id": committed_approval_id,
            },
            priority=90,
        )
        committed_result = db.json_loads(
            db.query_one(
                "SELECT result_json FROM tasks WHERE id = ?", (committed_task["id"],)
            )["result_json"],
            {},
        )
        self.state.commit_skill_recommendation_decision(
            task_id=committed_task["id"],
            run_id=committed_run["id"],
            command_id=committed_command["id"],
            approval_id=committed_approval_id,
            recommendation_id="ppt-generator",
            approved=False,
            note="暂不安装",
            result=committed_result,
            events=[
                {
                    "type": "approval",
                    "title": "已跳过 Skill 安装",
                    "content": "按现有能力继续",
                }
            ],
        )

        with ExitStack() as stack:
            for startup_patch in self._startup_patches():
                stack.enter_context(startup_patch)
            schedule_runtime = stack.enter_context(
                patch.object(main_module, "_schedule_runtime")
            )
            resume_approval = stack.enter_context(
                patch.object(
                    main_module,
                    "_resume_after_approval_safely",
                    new_callable=AsyncMock,
                )
            )
            with TestClient(main_module.app) as client:
                self.assertEqual(client.get("/api/health").status_code, 200)

        schedule_runtime.assert_not_called()
        self.assertEqual(resume_approval.await_count, 2)
        resume_approval.assert_any_await(
            queued_task["id"], True, "安装并继续", queued_command["id"]
        )
        resume_approval.assert_any_await(
            committed_task["id"],
            False,
            "暂不安装",
            committed_command["id"],
        )
        self.assertEqual(
            self.state.get_run(queued_run["id"])["status"], "waiting_approval"
        )
        self.assertEqual(
            self.state.get_run(undecided_run["id"])["status"],
            "waiting_approval",
        )
        self.assertEqual(
            len(
                self.state.list_commands(
                    task_id=undecided_task["id"], command_types=["approval"]
                )
            ),
            0,
        )
        self.assertEqual(
            self.state.get_run(committed_run["id"])["status"], "waiting_approval"
        )
        self.assertEqual(
            self.state.get_command(queued_command["id"])["status"], "queued"
        )
        self.assertEqual(
            self.state.get_command(committed_command["id"])["status"], "completed"
        )

    def test_startup_replays_committed_generic_supersede_and_finishes_message(
        self,
    ) -> None:
        task, run = self._running_task("执行一次需要确认的外部写入")
        self.state.transition_run(run["id"], "waiting_approval")
        db.update_task_status(
            task["id"],
            "waiting_approval",
            result={
                "pending_action": "external_write",
                "summary": "请确认是否执行外部写入。",
            },
        )
        message = self.state.enqueue_command(
            task["id"],
            "message",
            run_id=run["id"],
            payload={"message": "取消之前任务，新的目标：只做只读审计"},
        )
        approval = self.state.enqueue_command(
            task["id"],
            "approval",
            run_id=run["id"],
            payload={
                "approved": False,
                "note": "不允许写入，继续只读审计",
            },
            priority=90,
        )

        with patch.object(
            main_module.runtime,
            "run_task",
            new_callable=AsyncMock,
            side_effect=asyncio.CancelledError,
        ):
            with self.assertRaises(asyncio.CancelledError):
                asyncio.run(
                    main_module.runtime.resume_after_approval(
                        task["id"],
                        False,
                        "不允许写入，继续只读审计",
                        command_id=approval["id"],
                    )
                )

        self.assertEqual(self.state.get_command(approval["id"])["status"], "completed")
        self.assertTrue(self.state.get_command(approval["id"])["result"]["superseded"])
        self.assertEqual(self.state.get_command(message["id"])["status"], "queued")
        self.assertEqual(
            db.query_one("SELECT status FROM tasks WHERE id = ?", (task["id"],))[
                "status"
            ],
            "waiting_approval",
        )

        main_module.seed_agents()
        with ExitStack() as stack:
            for startup_patch in self._startup_patches():
                stack.enter_context(startup_patch)
            stack.enter_context(
                patch.object(
                    main_module.runtime,
                    "model_gateway",
                    RestartCompletionModel(),
                )
            )
            with TestClient(main_module.app) as client:
                self.assertEqual(client.get("/api/health").status_code, 200)
                deadline = time.monotonic() + 5
                status = ""
                while time.monotonic() < deadline:
                    status = str(
                        db.query_one(
                            "SELECT status FROM tasks WHERE id = ?", (task["id"],)
                        )["status"]
                    )
                    if status in {"completed", "failed", "cancelled"}:
                        break
                    time.sleep(0.01)

        self.assertEqual(status, "completed")
        self.assertEqual(self.state.get_command(message["id"])["status"], "completed")
        self.assertEqual(self.state.get_command(approval["id"])["status"], "completed")
        self.assertEqual(len(self.state.list_runs(task_id=task["id"])), 1)
        self.assertEqual(
            len(
                db.query_all(
                    "SELECT id FROM task_events "
                    "WHERE task_id = ? AND type = 'approval_required'",
                    (task["id"],),
                )
            ),
            0,
        )
        self.state.assert_terminal_clean(task_id=task["id"], run_id=run["id"])

    def test_startup_replays_committed_skill_install_without_duplicate_effect(
        self,
    ) -> None:
        task, run = self._running_task("请给 WorkBuddy 写一份 PRD")
        approval_id = "skill_recommendation_restart_real_e2e"
        self.state.commit_skill_recommendation_request(
            task_id=task["id"],
            run_id=run["id"],
            approval_id=approval_id,
            recommendation_id="product_requirement_document",
            result={},
            title="推荐安装 PRD Skill",
            content="安装后继续原任务",
            data={"action": "install_recommended_skill"},
        )
        approval = self.state.enqueue_command(
            task["id"],
            "approval",
            run_id=run["id"],
            payload={
                "approved": True,
                "note": "安装并继续",
                "approval_id": approval_id,
            },
            priority=90,
        )

        with patch.object(
            main_module.runtime,
            "run_task",
            new_callable=AsyncMock,
            side_effect=asyncio.CancelledError,
        ):
            with self.assertRaises(asyncio.CancelledError):
                asyncio.run(
                    main_module.runtime.resume_after_approval(
                        task["id"],
                        True,
                        "安装并继续",
                        command_id=approval["id"],
                    )
                )

        installed_before = main_module.skill_registry.get_skill(
            "product_requirement_document"
        )
        self.assertIsNotNone(installed_before)
        install_events_before = db.query_all(
            "SELECT id FROM task_events WHERE task_id = ? AND type = 'install'",
            (task["id"],),
        )
        self.assertEqual(len(install_events_before), 1)
        self.assertEqual(self.state.get_command(approval["id"])["status"], "completed")
        self.assertEqual(
            db.query_one("SELECT status FROM tasks WHERE id = ?", (task["id"],))[
                "status"
            ],
            "waiting_approval",
        )

        main_module.seed_agents()
        with ExitStack() as stack:
            for startup_patch in self._startup_patches():
                stack.enter_context(startup_patch)
            stack.enter_context(
                patch.object(
                    main_module.runtime,
                    "model_gateway",
                    RestartCompletionModel(),
                )
            )
            with TestClient(main_module.app) as client:
                self.assertEqual(client.get("/api/health").status_code, 200)
                deadline = time.monotonic() + 5
                status = ""
                while time.monotonic() < deadline:
                    status = str(
                        db.query_one(
                            "SELECT status FROM tasks WHERE id = ?", (task["id"],)
                        )["status"]
                    )
                    if status in {"completed", "failed", "cancelled"}:
                        break
                    time.sleep(0.01)

        self.assertEqual(status, "completed")
        installed_after = main_module.skill_registry.get_skill(
            "product_requirement_document"
        )
        self.assertEqual(installed_after["content"], installed_before["content"])
        self.assertEqual(
            len(
                db.query_all(
                    "SELECT id FROM task_events "
                    "WHERE task_id = ? AND type = 'install'",
                    (task["id"],),
                )
            ),
            1,
        )
        self.assertEqual(
            len(
                db.query_all(
                    "SELECT id FROM task_events "
                    "WHERE task_id = ? AND type = 'approval_required'",
                    (task["id"],),
                )
            ),
            1,
        )
        self.assertEqual(self.state.get_command(approval["id"])["status"], "completed")
        self.state.assert_terminal_clean(task_id=task["id"], run_id=run["id"])

    def test_recovery_rolls_back_run_command_and_task_when_event_insert_fails(self) -> None:
        task, run = self._running_task("验证重启恢复事务回滚")
        node = self.state.create_node(run["id"], "execute", "正在执行")
        self.state.start_node(node["id"])
        message = self.state.enqueue_command(
            task["id"],
            "message",
            run_id=run["id"],
            payload={"message": "重启后仍需处理"},
        )
        db.execute(
            """
            CREATE TRIGGER fail_restart_recovery_event
            BEFORE INSERT ON task_events
            WHEN NEW.type = 'recovery_scheduled'
            BEGIN
                SELECT RAISE(ABORT, 'injected recovery event failure');
            END
            """
        )

        with self.assertRaises(sqlite3.IntegrityError):
            self.state.recover_interrupted_attempt(run["id"])

        self.assertEqual(self.state.get_run(run["id"])["status"], "running")
        self.assertEqual(len(self.state.list_runs(task_id=task["id"])), 1)
        self.assertEqual(self.state.get_node(node["id"])["status"], "running")
        message_after = self.state.get_command(message["id"])
        self.assertEqual(message_after["status"], "queued")
        self.assertEqual(message_after["run_id"], run["id"])
        self.assertEqual(
            db.query_one("SELECT status FROM tasks WHERE id = ?", (task["id"],))[
                "status"
            ],
            "running",
        )


if __name__ == "__main__":
    unittest.main()
