from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from app import db
from app import main as main_module
from app.services.agent_runtime import AgentRuntime
from app.services.context_service import ExecutionScope
from app.services.expert_team_service import ExpertTeamService
from app.services.mcp_gateway import McpGateway
from app.services.model_gateway import ModelGateway
from app.services.policy_engine import PolicyEngine
from app.services.skill_registry import SkillRegistry
from app.services.task_state import TaskStateService


def _insert_agent(
    agent_id: str,
    *,
    organization_id: str = "org-a",
    workspace_id: str = "workspace-a",
    owner_user_id: str = "alice",
    visibility: str = "organization",
) -> None:
    now = db.utc_now()
    db.execute(
        """
        INSERT INTO agents(
            id, name, description, model, system_prompt, skills_json,
            mcp_servers_json, permissions_json, organization_id, workspace_id,
            owner_user_id, visibility, created_at, updated_at
        ) VALUES (?, ?, '', 'deterministic', '', '[]', '[]', '{}', ?, ?, ?, ?, ?, ?)
        """,
        (
            agent_id,
            agent_id,
            organization_id,
            workspace_id,
            owner_user_id,
            visibility,
            now,
            now,
        ),
    )


class RecordingRuntime:
    """A deterministic worker that exposes a real concurrency barrier."""

    def __init__(
        self,
        task_state: TaskStateService,
        *,
        fail_first: set[str] | None = None,
        supervisor_summary: str = "SUPERVISOR_SUMMARY::all-members；结论：存在风险。",
        supervisor_artifacts: list[dict[str, Any]] | None = None,
    ) -> None:
        self.task_state = task_state
        self.fail_first = set(fail_first or set())
        self.supervisor_summary = supervisor_summary
        self.supervisor_artifacts = list(supervisor_artifacts or [])
        self.invocations: dict[str, int] = {}
        self.active_members = 0
        self.max_active_members = 0
        self.parallel_barrier = asyncio.Event()
        self.member_prompts: dict[str, list[str]] = {}
        self.member_conversations: dict[str, list[str]] = {}
        self.supervisor_prompts: list[str] = []

    async def resolve_task_goal(self, task: dict[str, Any]) -> dict[str, Any]:
        return {
            "standalone_request": str(task.get("message") or ""),
            "intent": "expert_team_test",
            "parameters": {},
            "missing_information": [],
            "is_follow_up": False,
            "source": "test_fixture",
        }

    async def run_task(self, task_id: str, *, run_id: str | None = None) -> None:
        task = db.query_one("SELECT * FROM tasks WHERE id = ?", (task_id,)) or {}
        agent_id = str(task.get("agent_id") or "")
        self.invocations[agent_id] = self.invocations.get(agent_id, 0) + 1
        state_run = self.task_state.begin_run(task_id, run_id=run_id)
        message = str(task.get("message") or "")
        if message.startswith("你是专家团中的独立成员"):
            self.member_prompts.setdefault(agent_id, []).append(message)
            self.member_conversations.setdefault(agent_id, []).append(
                str(task.get("conversation_id") or "")
            )
            self.active_members += 1
            self.max_active_members = max(self.max_active_members, self.active_members)
            if self.active_members >= 2:
                self.parallel_barrier.set()
            # If orchestration is accidentally sequential, this fails instead
            # of merely producing timing data that could be misinterpreted.
            await asyncio.wait_for(self.parallel_barrier.wait(), timeout=0.5)
            await asyncio.sleep(0.02)
            self.active_members -= 1
            should_fail = (
                agent_id in self.fail_first and self.invocations[agent_id] == 1
            )
            if should_fail:
                db.update_task_status(task_id, "failed", result={"error": f"{agent_id} fixture failure"})
                self.task_state.finish_run(
                    state_run["id"], status="failed", error={"message": "fixture failure"}
                )
                return
            summary = f"MEMBER_OUTPUT::{agent_id}"
        else:
            self.supervisor_prompts.append(message)
            summary = self.supervisor_summary
        db.insert_event(task_id, "answer", "测试回答", summary)
        db.update_task_status(
            task_id,
            "completed",
            result={"summary": summary},
            artifacts=self.supervisor_artifacts if not message.startswith("你是专家团中的独立成员") else [],
        )
        self.task_state.finish_run(state_run["id"], result={"summary": summary})


class ExpertTeamServiceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_db_path = db.DB_PATH
        self.original_env = os.environ.get("APP_DB_PATH")
        db.DB_PATH = Path(self.temp_dir.name) / "expert-team.db"
        os.environ["APP_DB_PATH"] = str(db.DB_PATH)
        db.init_db()
        self.task_state = TaskStateService()
        for agent_id in ("supervisor", "researcher", "reviewer"):
            _insert_agent(agent_id)
        self.scope = ExecutionScope(
            organization_id="org-a", workspace_id="workspace-a", user_id="alice"
        )

    def tearDown(self) -> None:
        db.DB_PATH = self.original_db_path
        if self.original_env is None:
            os.environ.pop("APP_DB_PATH", None)
        else:
            os.environ["APP_DB_PATH"] = self.original_env
        self.temp_dir.cleanup()

    def _create_team(self, runtime: RecordingRuntime) -> ExpertTeamService:
        service = ExpertTeamService(runtime, task_state=self.task_state)  # type: ignore[arg-type]
        service.create_team(
            {
                "id": "risk-council",
                "name": "风险专家团",
                "description": "并行研究与复核",
                "supervisor_agent_id": "supervisor",
                "aggregation_prompt": "保留证据，指出分歧并形成结论。",
                "acceptance": ["包含结论", "包含风险"],
                "budget": {"timeout_seconds": 30},
                "organization_id": "org-a",
                "workspace_id": "workspace-a",
                "owner_user_id": "alice",
                "visibility": "private",
                "permissions": {"read_only": True, "max_tool_calls": 6},
                "members": [
                    {
                        "id": "research-member",
                        "agent_id": "researcher",
                        "role": "研究员",
                        "member_prompt": "独立收集事实证据。",
                        "position": 1,
                        "permissions": {"allowed_tools": ["search", "read"], "max_tool_calls": 4},
                    },
                    {
                        "id": "review-member",
                        "agent_id": "reviewer",
                        "role": "复核员",
                        "member_prompt": "独立识别风险与反例。",
                        "position": 2,
                        "permissions": {"allowed_tools": ["read"], "max_tool_calls": 2},
                    },
                ],
                "enabled": True,
            }
        )
        return service

    def test_template_installation_persists_scope_and_restrictive_permissions(self) -> None:
        runtime = RecordingRuntime(self.task_state)
        service = ExpertTeamService(runtime, task_state=self.task_state)  # type: ignore[arg-type]
        template = service.create_template(
            {
                "id": "audit-expert",
                "name": "审计专家",
                "manifest": {
                    "system_prompt": "只基于证据审计。",
                    "skills": ["general_task"],
                    "mcp_servers": ["filesystem"],
                    "permissions": {
                        "allowed_tools": ["read", "search"],
                        "max_tool_calls": 10,
                    },
                },
                "organization_id": "org-a",
                "workspace_id": "workspace-a",
                "owner_user_id": "alice",
                "visibility": "private",
                "permissions": {"allowed_tools": ["read"], "read_only": True},
                "enabled": True,
            }
        )
        self.assertEqual(template["organization_id"], "org-a")
        self.assertEqual(service.list_templates(self.scope)[0]["id"], "audit-expert")
        bob = ExecutionScope(
            organization_id="org-a", workspace_id="workspace-a", user_id="bob"
        )
        self.assertEqual(service.list_templates(bob), [])

        installed = service.install_template(
            "audit-expert",
            self.scope,
            {
                "installation_id": "install-audit",
                "agent_id": "installed-auditor",
                "visibility": "private",
                "permissions": {"allowed_tools": ["read", "write"], "max_tool_calls": 3},
                "overrides": {"name": "项目审计专家"},
            },
        )
        self.assertEqual(installed["agent"]["name"], "项目审计专家")
        self.assertEqual(installed["permissions"]["allowed_tools"], ["read"])
        self.assertEqual(installed["permissions"]["max_tool_calls"], 3)
        self.assertTrue(installed["permissions"]["read_only"])
        self.assertEqual(service.list_installations(bob), [])
        self.assertEqual(service.list_installations(self.scope)[0]["agent_id"], "installed-auditor")

    async def test_two_members_really_overlap_use_isolated_contexts_and_supervisor_aggregates(self) -> None:
        runtime = RecordingRuntime(self.task_state)
        service = self._create_team(runtime)
        parent, parent_run, team_run = service.create_task_and_run(
            "risk-council", self.scope, message="评估供应链方案的主要风险"
        )
        self.assertEqual(
            service.queued_runs_for_recovery(),
            [{"id": team_run["id"], "parent_task_id": parent["id"]}],
        )
        await service.run_team(team_run["id"])
        self.assertEqual(service.queued_runs_for_recovery(), [])

        completed = service.get_team_run(team_run["id"], self.scope) or {}
        self.assertEqual(completed["status"], "completed", completed)
        self.assertTrue(completed["result"]["validation"]["passed"])
        self.assertEqual(
            [item["status"] for item in completed["result"]["validation"]["criteria"]],
            ["passed", "passed", "passed"],
        )
        self.assertEqual(runtime.max_active_members, 2)
        self.assertEqual(len(completed["member_runs"]), 2)
        reviewer_permissions = next(
            item["permissions"]
            for item in completed["member_runs"]
            if item["member_id"] == "review-member"
        )
        self.assertTrue(reviewer_permissions["read_only"])
        self.assertEqual(reviewer_permissions["max_tool_calls"], 2)
        self.assertEqual(reviewer_permissions["allowed_tools"], ["read"])
        conversations = {
            item["conversation_id"] for item in completed["member_runs"]
        }
        self.assertEqual(len(conversations), 2)
        for prompts in runtime.member_prompts.values():
            self.assertNotIn("MEMBER_OUTPUT::", prompts[0])
            self.assertIn("共同目标：评估供应链方案的主要风险", prompts[0])
            self.assertIn("正文控制在 1600 个中文字符以内", prompts[0])
        self.assertEqual(len(runtime.supervisor_prompts), 1)
        self.assertIn("MEMBER_OUTPUT::researcher", runtime.supervisor_prompts[0])
        self.assertIn("MEMBER_OUTPUT::reviewer", runtime.supervisor_prompts[0])
        self.assertIn("最终答复控制在 2400 个中文字符以内", runtime.supervisor_prompts[0])
        parent_row = db.query_one("SELECT * FROM tasks WHERE id = ?", (parent["id"],)) or {}
        self.assertEqual(parent_row["status"], "completed")
        self.task_state.assert_terminal_clean(
            task_id=parent["id"], run_id=parent_run["id"]
        )
        self.assertEqual(
            db.json_loads(parent_row["result_json"], {})["summary"],
            "SUPERVISOR_SUMMARY::all-members；结论：存在风险。",
        )
        parent_nodes = self.task_state.list_nodes(completed["parent_run_id"])
        self.assertEqual([node["kind"] for node in parent_nodes], ["agent", "agent", "agent"])
        self.assertTrue(all(node["status"] == "completed" for node in parent_nodes))

    async def test_structured_acceptance_passes_only_after_all_checks_succeed(self) -> None:
        runtime = RecordingRuntime(
            self.task_state,
            supervisor_summary="结论：当前方案存在供应链风险，建议上线前完成复核并留存证据。",
            supervisor_artifacts=[
                {
                    "id": "review-markdown",
                    "name": "review.md",
                    "kind": "markdown",
                    "download_url": "/api/artifacts/review-markdown/download",
                }
            ],
        )
        service = self._create_team(runtime)
        service.update_team(
            "risk-council",
            self.scope,
            {
                "acceptance": [
                    {"id": "length", "min_chars": 20},
                    {
                        "id": "keywords",
                        "type": "required_keywords",
                        "value": ["结论", "风险", "建议"],
                    },
                    {
                        "id": "artifact",
                        "type": "requires_artifact",
                        "value": True,
                        "artifact_kinds": ["md"],
                    },
                ]
            },
        )
        parent, parent_run, team_run = service.create_task_and_run(
            "risk-council", self.scope, message="评审供应链方案并交付 Markdown 报告"
        )

        await service.run_team(team_run["id"])

        completed = service.get_team_run(team_run["id"], self.scope) or {}
        self.assertEqual(completed["status"], "completed", completed)
        validation = completed["result"]["validation"]
        self.assertTrue(validation["passed"])
        self.assertEqual(
            [item["status"] for item in validation["criteria"]],
            ["passed", "passed", "passed", "passed"],
        )
        self.assertEqual(validation["artifact_count"], 1)
        parent_row = db.query_one("SELECT * FROM tasks WHERE id = ?", (parent["id"],)) or {}
        self.assertEqual(parent_row["status"], "completed")
        self.assertEqual(self.task_state.get_run(parent_run["id"])["status"], "completed")
        output_check = db.query_one(
            "SELECT data_json FROM task_events WHERE task_id = ? AND type = 'output_check'",
            (parent["id"],),
        ) or {}
        self.assertTrue(db.json_loads(output_check.get("data_json"), {})["passed"])

    async def test_failed_acceptance_fails_team_parent_task_and_validation_node(self) -> None:
        runtime = RecordingRuntime(
            self.task_state,
            supervisor_summary="结论：方案可行。",
        )
        service = self._create_team(runtime)
        service.update_team(
            "risk-council",
            self.scope,
            {
                "acceptance": [
                    {"id": "length", "type": "min_chars", "value": 80},
                    {"id": "keywords", "required_keywords": ["结论", "风险"]},
                    {"id": "artifact", "requires_artifact": True},
                ]
            },
        )
        parent, parent_run, team_run = service.create_task_and_run(
            "risk-council", self.scope, message="评审方案并生成产物"
        )

        await service.run_team(team_run["id"])

        failed = service.get_team_run(team_run["id"], self.scope) or {}
        self.assertEqual(failed["status"], "failed", failed)
        self.assertEqual(failed["error"]["error_type"], "acceptance_failed")
        validation = failed["result"]["validation"]
        self.assertFalse(validation["passed"])
        failed_ids = {
            item["id"] for item in validation["criteria"] if item["status"] == "failed"
        }
        self.assertEqual(failed_ids, {"length", "keywords", "artifact"})
        parent_row = db.query_one("SELECT * FROM tasks WHERE id = ?", (parent["id"],)) or {}
        self.assertEqual(parent_row["status"], "failed")
        self.assertEqual(self.task_state.get_run(parent_run["id"])["status"], "failed")
        self.task_state.assert_terminal_clean(
            task_id=parent["id"], run_id=parent_run["id"]
        )
        parent_nodes = self.task_state.list_nodes(parent_run["id"])
        self.assertEqual(parent_nodes[-1]["node_key"], "supervisor:aggregate")
        self.assertEqual(parent_nodes[-1]["status"], "failed")
        events = db.query_all(
            "SELECT type, data_json FROM task_events WHERE task_id = ? ORDER BY id",
            (parent["id"],),
        )
        event_types = [item["type"] for item in events]
        self.assertIn("output_check", event_types)
        self.assertIn("team_acceptance_failed", event_types)
        self.assertNotIn("team_completed", event_types)
        output_check = next(item for item in events if item["type"] == "output_check")
        self.assertFalse(db.json_loads(output_check["data_json"], {})["passed"])

    async def test_failed_member_retry_does_not_rerun_successful_member(self) -> None:
        runtime = RecordingRuntime(self.task_state, fail_first={"reviewer"})
        service = self._create_team(runtime)
        parent, parent_run, team_run = service.create_task_and_run(
            "risk-council", self.scope, message="复核投标方案"
        )
        await service.run_team(team_run["id"])
        partial = service.get_team_run(team_run["id"], self.scope) or {}
        self.assertEqual(partial["status"], "partial_failed", partial)
        self.assertEqual(partial["result"]["goal_snapshot"], "复核投标方案")
        self.assertEqual(
            partial["result"]["intent_resolution"]["standalone_request"],
            "复核投标方案",
        )
        self.assertEqual(runtime.invocations.get("researcher"), 1)
        self.assertEqual(runtime.invocations.get("reviewer"), 1)
        self.assertNotIn("supervisor", runtime.invocations)
        self.task_state.assert_terminal_clean(
            task_id=parent["id"], run_id=parent_run["id"]
        )
        failed = next(item for item in partial["member_runs"] if item["status"] == "failed")
        successful_id = next(
            item["id"] for item in partial["member_runs"] if item["status"] == "completed"
        )

        await service.retry_member(team_run["id"], failed["id"], self.scope)
        completed = service.get_team_run(team_run["id"], self.scope) or {}
        self.assertEqual(completed["status"], "completed", completed)
        self.assertEqual(runtime.invocations.get("researcher"), 1)
        self.assertEqual(runtime.invocations.get("reviewer"), 2)
        self.assertEqual(runtime.invocations.get("supervisor"), 1)
        researcher_runs = [
            item for item in completed["member_runs"] if item["member_id"] == "research-member"
        ]
        reviewer_runs = [
            item for item in completed["member_runs"] if item["member_id"] == "review-member"
        ]
        self.assertEqual([item["id"] for item in researcher_runs], [successful_id])
        self.assertEqual([item["attempt"] for item in reviewer_runs], [1, 2])
        self.assertEqual([item["status"] for item in reviewer_runs], ["failed", "completed"])
        parent_runs = self.task_state.list_runs(task_id=parent["id"])
        self.assertEqual(len(parent_runs), 2)
        retry_parent_run = next(item for item in parent_runs if item["attempt"] == 2)
        retry_nodes = self.task_state.list_nodes(retry_parent_run["id"])
        self.assertEqual(len(retry_nodes), 2)
        self.assertIn("retry", retry_nodes[0]["node_key"])
        self.assertEqual(retry_nodes[1]["node_key"], "supervisor:aggregate")

    async def test_resolved_conversation_goal_is_frozen_for_all_experts_and_supervisor(self) -> None:
        runtime = RecordingRuntime(self.task_state)
        runtime.resolve_task_goal = AsyncMock(
            return_value={
                "standalone_request": "把前面对平台模式的可靠性与交互评审整理成 P0/P1/P2 建议",
                "intent": "general",
                "parameters": {},
                "missing_information": [],
                "is_follow_up": True,
                "source": "conversation_model",
            }
        )
        service = self._create_team(runtime)
        _, _, team_run = service.create_task_and_run(
            "risk-council", self.scope, message="把前面的结果整理一下"
        )

        await service.run_team(team_run["id"])

        completed = service.get_team_run(team_run["id"], self.scope) or {}
        resolved_goal = "把前面对平台模式的可靠性与交互评审整理成 P0/P1/P2 建议"
        self.assertEqual(completed["result"]["goal_snapshot"], resolved_goal)
        self.assertTrue(completed["result"]["intent_resolution"]["is_follow_up"])
        for member_run in completed["member_runs"]:
            self.assertEqual(member_run["input"]["goal"], resolved_goal)
        for prompts in runtime.member_prompts.values():
            self.assertIn(f"共同目标：{resolved_goal}", prompts[0])
            self.assertNotIn("共同目标：把前面的结果整理一下", prompts[0])
        self.assertIn(f"共同目标：{resolved_goal}", runtime.supervisor_prompts[0])

    async def test_missing_information_is_requested_before_any_expert_starts(self) -> None:
        runtime = RecordingRuntime(self.task_state)
        runtime.resolve_task_goal = AsyncMock(
            return_value={
                "standalone_request": "评审待发布方案",
                "intent": "review",
                "parameters": {},
                "missing_information": ["待评审方案的范围或材料"],
                "is_follow_up": False,
                "source": "conversation_model",
            }
        )
        service = self._create_team(runtime)
        parent, parent_run, team_run = service.create_task_and_run(
            "risk-council", self.scope, message="请专家评审一下"
        )

        await service.run_team(team_run["id"])

        completed = service.get_team_run(team_run["id"], self.scope) or {}
        self.assertEqual(completed["status"], "completed")
        self.assertTrue(completed["result"]["needs_clarification"])
        self.assertEqual(runtime.invocations, {})
        event_types = [
            item["type"]
            for item in db.query_all(
                "SELECT type FROM task_events WHERE task_id = ? ORDER BY id", (parent["id"],)
            )
        ]
        self.assertIn("clarification", event_types)
        self.assertIn("answer", event_types)
        self.assertNotIn("team_parallel_start", event_types)
        parent_row = db.query_one("SELECT * FROM tasks WHERE id = ?", (parent["id"],)) or {}
        self.assertEqual(parent_row["status"], "completed")
        self.task_state.assert_terminal_clean(
            task_id=parent["id"], run_id=parent_run["id"]
        )

    async def test_runtime_message_preempts_stale_team_candidate_and_reexecutes_all_roles(self) -> None:
        runtime = RecordingRuntime(self.task_state)
        service = self._create_team(runtime)
        parent, parent_run, team_run = service.create_task_and_run(
            "risk-council", self.scope, message="评估旧方案"
        )
        original_validate = service._validate_team_acceptance
        injected = False

        def validate_with_message(*args: Any, **kwargs: Any) -> dict[str, Any]:
            nonlocal injected
            if not injected:
                injected = True
                self.task_state.enqueue_command(
                    parent["id"],
                    "message",
                    run_id=parent_run["id"],
                    payload={"message": "补充要求：必须比较新方案并说明迁移风险"},
                    priority=20,
                )
            return original_validate(*args, **kwargs)

        with patch.object(
            service, "_validate_team_acceptance", side_effect=validate_with_message
        ):
            await service.run_team(team_run["id"])

        completed = service.get_team_run(team_run["id"], self.scope) or {}
        self.assertEqual(completed["status"], "completed", completed)
        self.assertEqual(runtime.invocations.get("researcher"), 2)
        self.assertEqual(runtime.invocations.get("reviewer"), 2)
        self.assertEqual(runtime.invocations.get("supervisor"), 2)
        self.assertIn("评估旧方案", completed["result"]["goal_snapshot"])
        self.assertIn("必须比较新方案", completed["result"]["goal_snapshot"])
        command = self.task_state.list_commands(
            task_id=parent["id"], command_types=["message"]
        )[0]
        self.assertEqual(command["status"], "completed")
        self.assertTrue(command["result"]["applied"])
        final_run = self.task_state.assert_terminal_clean(
            task_id=parent["id"], run_id=parent_run["id"]
        )
        self.assertEqual(final_run["accepted_generation"], 1)
        self.assertEqual(final_run["applied_generation"], 1)
        steering_events = db.query_all(
            "SELECT * FROM task_events WHERE task_id = ? AND type = 'steering'",
            (parent["id"],),
        )
        self.assertEqual(len(steering_events), 1)

    async def test_cancel_preempts_team_success_in_the_atomic_publication_window(self) -> None:
        runtime = RecordingRuntime(self.task_state)
        service = self._create_team(runtime)
        parent, parent_run, team_run = service.create_task_and_run(
            "risk-council", self.scope, message="评估可取消方案"
        )
        original_validate = service._validate_team_acceptance
        injected = False

        def validate_with_cancel(*args: Any, **kwargs: Any) -> dict[str, Any]:
            nonlocal injected
            if not injected:
                injected = True
                self.task_state.request_cancel(
                    parent["id"],
                    run_id=parent_run["id"],
                    reason="用户在最终发布前取消",
                    requested_by="test",
                )
            return original_validate(*args, **kwargs)

        with patch.object(
            service, "_validate_team_acceptance", side_effect=validate_with_cancel
        ):
            await service.run_team(team_run["id"])

        cancelled = service.get_team_run(team_run["id"], self.scope) or {}
        self.assertEqual(cancelled["status"], "cancelled", cancelled)
        parent_row = db.query_one(
            "SELECT status FROM tasks WHERE id = ?", (parent["id"],)
        ) or {}
        self.assertEqual(parent_row.get("status"), "cancelled")
        final_run = self.task_state.assert_terminal_clean(
            task_id=parent["id"], run_id=parent_run["id"]
        )
        self.assertEqual(final_run["status"], "cancelled")
        cancel_command = self.task_state.list_commands(
            task_id=parent["id"], command_types=["cancel"]
        )[0]
        self.assertEqual(cancel_command["status"], "completed")
        event_types = [
            row["type"]
            for row in db.query_all(
                "SELECT type FROM task_events WHERE task_id = ? ORDER BY id",
                (parent["id"],),
            )
        ]
        self.assertIn("cancelled", event_types)
        self.assertNotIn("team_completed", event_types)
        self.assertNotIn("answer", event_types)

    def test_all_four_team_terminal_paths_roll_back_as_one_unit_on_event_failure(self) -> None:
        runtime = RecordingRuntime(self.task_state)
        service = self._create_team(runtime)
        matrix = [
            (
                "success", "team_completed", "completed", "completed",
                {"summary": "ok"}, {},
            ),
            (
                "clarification",
                "clarification",
                "completed",
                "completed",
                {"summary": "need input", "needs_clarification": True},
                {},
            ),
            (
                "partial",
                "team_partial_failed",
                "partial_failed",
                "failed",
                {"summary": "partial"},
                {"message": "partial", "error_type": "team_partial_failed"},
            ),
            (
                "acceptance",
                "team_acceptance_failed",
                "failed",
                "failed",
                {"summary": "rejected"},
                {"message": "rejected", "error_type": "acceptance_failed"},
            ),
        ]
        for label, event_type, team_status, task_status, result, error in matrix:
            with self.subTest(path=label):
                parent, parent_run, team_run = service.create_task_and_run(
                    "risk-council", self.scope, message=f"atomic-{label}"
                )
                service._begin_parent_run(team_run, trigger="atomic_test")
                before = {
                    "team": db.query_one(
                        "SELECT * FROM team_runs WHERE id = ?", (team_run["id"],)
                    ),
                    "task": db.query_one(
                        "SELECT * FROM tasks WHERE id = ?", (parent["id"],)
                    ),
                    "run": db.query_one(
                        "SELECT * FROM task_runs WHERE id = ?", (parent_run["id"],)
                    ),
                    "events": db.query_all(
                        "SELECT * FROM task_events WHERE task_id = ? ORDER BY id",
                        (parent["id"],),
                    ),
                }
                trigger = f"fail_team_atomic_{label}"
                db.execute(
                    f"""
                    CREATE TRIGGER {trigger}
                    BEFORE INSERT ON task_events
                    WHEN NEW.type = '{event_type}'
                    BEGIN
                        SELECT RAISE(ABORT, 'injected expert terminal event failure');
                    END
                    """
                )
                try:
                    with self.assertRaises(sqlite3.IntegrityError):
                        service._commit_team_terminal(
                            team_run_id=team_run["id"],
                            parent_run_id=parent_run["id"],
                            team_status=team_status,
                            task_status=task_status,
                            result=result,
                            error=error,
                            artifacts=[],
                            events=[
                                {
                                    "type": event_type,
                                    "title": label,
                                    "content": "fault injection",
                                }
                            ],
                        )
                finally:
                    db.execute(f"DROP TRIGGER IF EXISTS {trigger}")
                after = {
                    "team": db.query_one(
                        "SELECT * FROM team_runs WHERE id = ?", (team_run["id"],)
                    ),
                    "task": db.query_one(
                        "SELECT * FROM tasks WHERE id = ?", (parent["id"],)
                    ),
                    "run": db.query_one(
                        "SELECT * FROM task_runs WHERE id = ?", (parent_run["id"],)
                    ),
                    "events": db.query_all(
                        "SELECT * FROM task_events WHERE task_id = ? ORDER BY id",
                        (parent["id"],),
                    ),
                }
                self.assertEqual(after, before)

    async def test_missing_team_is_closed_once_and_never_stolen_across_two_restarts(self) -> None:
        runtime = RecordingRuntime(self.task_state)
        service = self._create_team(runtime)
        parent, parent_run, team_run = service.create_task_and_run(
            "risk-council", self.scope, message="团队删除前已提交的任务"
        )
        db.execute("DELETE FROM agent_team_members WHERE team_id = 'risk-council'")
        db.execute("DELETE FROM agent_teams WHERE id = 'risk-council'")

        first_team_queue = service.reconcile_interrupted_orchestrated_runs()
        self.assertEqual(
            first_team_queue,
            [{"id": team_run["id"], "parent_task_id": parent["id"]}],
        )
        self.assertEqual(main_module._recover_interrupted_runs(), [])
        await service.run_team(team_run["id"])

        failed_team = db.query_one(
            "SELECT status FROM team_runs WHERE id = ?", (team_run["id"],)
        ) or {}
        self.assertEqual(failed_team.get("status"), "failed")
        self.task_state.assert_terminal_clean(
            task_id=parent["id"], run_id=parent_run["id"]
        )

        second_team_queue = service.reconcile_interrupted_orchestrated_runs()
        self.assertEqual(second_team_queue, [])
        self.assertEqual(main_module._recover_interrupted_runs(), [])
        self.assertEqual(runtime.invocations, {})

    def test_interrupted_active_team_and_orphan_member_converge_across_restarts(self) -> None:
        runtime = RecordingRuntime(self.task_state)
        service = self._create_team(runtime)
        parent, parent_run, created_team_run = service.create_task_and_run(
            "risk-council", self.scope, message="执行中平台重启"
        )
        parent_run = service._begin_parent_run(
            created_team_run, trigger="interrupted_test"
        )
        team_run = service.get_team_run(created_team_run["id"], self.scope) or {}
        team = service.get_team("risk-council", self.scope) or {}
        db.execute(
            "UPDATE team_runs SET status = 'running', started_at = ?, updated_at = ? WHERE id = ?",
            (db.utc_now(), db.utc_now(), team_run["id"]),
        )
        member_run, child_run_id = service._create_member_attempt(
            team_run, team, team["members"][0]
        )

        self.assertEqual(service.reconcile_interrupted_orchestrated_runs(), [])

        failed_team = service.get_team_run(team_run["id"], self.scope) or {}
        self.assertEqual(failed_team["status"], "failed")
        self.task_state.assert_terminal_clean(
            task_id=parent["id"], run_id=parent_run["id"]
        )
        child_task_id = member_run["child_task_id"]
        self.task_state.assert_terminal_clean(
            task_id=child_task_id, run_id=child_run_id
        )
        member_after = db.query_one(
            "SELECT status FROM team_member_runs WHERE id = ?", (member_run["id"],)
        ) or {}
        self.assertEqual(member_after.get("status"), "failed")

        self.assertEqual(service.reconcile_interrupted_orchestrated_runs(), [])
        self.assertEqual(main_module._recover_interrupted_runs(), [])

    def test_team_children_and_metadata_quarantine_never_enter_generic_recovery(self) -> None:
        member = db.query_one("SELECT * FROM tasks LIMIT 1")
        self.assertIsNone(member)
        from app.services.agent_runtime import create_task_record

        child = create_task_record(
            "orphan member",
            "researcher",
            self.scope.workspace_id,
            organization_id=self.scope.organization_id,
            user_id=self.scope.user_id,
            executor_type="team_member",
            executor_id="researcher",
        )
        child_run = self.task_state.create_run(
            child["id"], metadata={"team_run_id": "missing", "role": "member"}
        )
        quarantined = create_task_record(
            "metadata-owned supervisor",
            "supervisor",
            self.scope.workspace_id,
            organization_id=self.scope.organization_id,
            user_id=self.scope.user_id,
        )
        quarantined_run = self.task_state.create_run(
            quarantined["id"],
            metadata={"executor_type": "team_supervisor", "executor_id": "supervisor"},
        )

        recovered = main_module._recover_interrupted_runs()

        recovered_ids = {row["id"] for row in recovered}
        self.assertNotIn(child_run["id"], recovered_ids)
        self.assertNotIn(quarantined_run["id"], recovered_ids)
        self.assertEqual(self.task_state.get_run(child_run["id"])["status"], "queued")
        self.assertEqual(
            self.task_state.get_run(quarantined_run["id"])["status"], "queued"
        )
        self.assertEqual(
            ExpertTeamService(
                RecordingRuntime(self.task_state), task_state=self.task_state
            ).reconcile_interrupted_orchestrated_runs(),
            [],
        )
        self.task_state.assert_terminal_clean(
            task_id=child["id"], run_id=child_run["id"]
        )
        # Contradictory ownership is terminally quarantined.  Checking only
        # the original run id is insufficient: generic recovery must not have
        # created an ordinary sibling attempt for the same Task.
        quarantined_runs = self.task_state.list_runs(
            task_id=quarantined["id"], limit=100
        )
        self.assertEqual(
            [(item["id"], item["status"]) for item in quarantined_runs],
            [(quarantined_run["id"], "failed")],
        )
        self.assertEqual(
            (db.query_one(
                "SELECT status FROM tasks WHERE id = ?", (quarantined["id"],)
            ) or {}).get("status"),
            "failed",
        )

    def test_agent_task_with_team_metadata_and_extension_fails_closed_once(self) -> None:
        from app.services.agent_runtime import create_task_record

        runtime = RecordingRuntime(self.task_state)
        service = self._create_team(runtime)
        task = create_task_record(
            "contradictory owner",
            "supervisor",
            self.scope.workspace_id,
            organization_id=self.scope.organization_id,
            user_id=self.scope.user_id,
        )
        run = self.task_state.create_run(
            task["id"],
            metadata={"executor_type": "team", "executor_id": "risk-council"},
        )
        now = db.utc_now()
        team_run_id = "xrun_ownership_mismatch"
        db.execute(
            """
            INSERT INTO team_runs(
                id, team_id, parent_task_id, status, result_json, error_json,
                started_at, finished_at, created_at, updated_at,
                organization_id, workspace_id, user_id, parent_run_id
            ) VALUES (?, 'risk-council', ?, 'queued', '{}', '{}', '', '', ?, ?,
                      ?, ?, ?, ?)
            """,
            (
                team_run_id,
                task["id"],
                now,
                now,
                self.scope.organization_id,
                self.scope.workspace_id,
                self.scope.user_id,
                run["id"],
            ),
        )

        self.assertEqual(main_module._recover_interrupted_runs(), [])
        self.assertEqual(service.reconcile_interrupted_orchestrated_runs(), [])

        all_runs = self.task_state.list_runs(task_id=task["id"], limit=100)
        self.assertEqual(
            [(item["id"], item["status"]) for item in all_runs],
            [(run["id"], "failed")],
        )
        self.assertEqual(
            (db.query_one(
                "SELECT status FROM team_runs WHERE id = ?", (team_run_id,)
            ) or {}).get("status"),
            "failed",
        )
        event_count = int(
            (db.query_one(
                "SELECT COUNT(*) AS total FROM task_events "
                "WHERE task_id = ? AND type = 'error' "
                "AND data_json LIKE '%ExecutorOwnershipMismatch%'",
                (task["id"],),
            ) or {}).get("total")
            or 0
        )
        self.assertEqual(event_count, 1)

        self.assertEqual(service.reconcile_interrupted_orchestrated_runs(), [])
        self.assertEqual(main_module._recover_interrupted_runs(), [])
        self.assertEqual(len(self.task_state.list_runs(task_id=task["id"])), 1)
        self.assertEqual(
            int((db.query_one(
                "SELECT COUNT(*) AS total FROM task_events "
                "WHERE task_id = ? AND type = 'error' "
                "AND data_json LIKE '%ExecutorOwnershipMismatch%'",
                (task["id"],),
            ) or {}).get("total") or 0),
            1,
        )

    def test_task_only_team_submission_is_repaired_atomically_without_duplicates(self) -> None:
        from app.services.agent_runtime import create_task_record

        runtime = RecordingRuntime(self.task_state)
        service = self._create_team(runtime)
        task = create_task_record(
            "task-only team residue",
            "supervisor",
            self.scope.workspace_id,
            organization_id=self.scope.organization_id,
            user_id=self.scope.user_id,
            executor_type="team",
            executor_id="risk-council",
        )

        first = service.reconcile_interrupted_orchestrated_runs()
        second = service.reconcile_interrupted_orchestrated_runs()

        self.assertEqual(len(first), 1)
        self.assertEqual(first, second)
        runs = self.task_state.list_runs(task_id=task["id"], limit=100)
        team_runs = db.query_all(
            "SELECT * FROM team_runs WHERE parent_task_id = ?", (task["id"],)
        )
        self.assertEqual(len(runs), 1)
        self.assertEqual(len(team_runs), 1)
        self.assertEqual(team_runs[0]["parent_run_id"], runs[0]["id"])
        self.assertEqual(first[0]["id"], team_runs[0]["id"])
        self.assertEqual(
            runs[0]["metadata"].get("executor_type"), "team"
        )
        self.assertEqual(
            runs[0]["metadata"].get("executor_id"), "risk-council"
        )
        self.assertEqual(main_module._recover_interrupted_runs(), [])

    def test_task_only_missing_team_fails_atomically_and_is_restart_idempotent(self) -> None:
        from app.services.agent_runtime import create_task_record

        service = ExpertTeamService(
            RecordingRuntime(self.task_state), task_state=self.task_state
        )
        task = create_task_record(
            "missing team residue",
            "supervisor",
            self.scope.workspace_id,
            organization_id=self.scope.organization_id,
            user_id=self.scope.user_id,
            executor_type="team",
            executor_id="deleted-team",
        )

        self.assertEqual(service.reconcile_interrupted_orchestrated_runs(), [])
        first_task = db.query_one("SELECT * FROM tasks WHERE id = ?", (task["id"],)) or {}
        first_events = db.query_all(
            "SELECT * FROM task_events WHERE task_id = ?", (task["id"],)
        )
        self.assertEqual(first_task.get("status"), "failed")
        self.assertEqual(self.task_state.list_runs(task_id=task["id"]), [])
        self.assertEqual(
            db.query_all(
                "SELECT * FROM team_runs WHERE parent_task_id = ?", (task["id"],)
            ),
            [],
        )
        self.assertEqual(len(first_events), 1)
        self.assertEqual(
            db.json_loads(first_events[0]["data_json"], {}).get("error_type"),
            "MissingExpertTeam",
        )

        self.assertEqual(service.reconcile_interrupted_orchestrated_runs(), [])
        self.assertEqual(
            db.query_one("SELECT * FROM tasks WHERE id = ?", (task["id"],)),
            first_task,
        )
        self.assertEqual(
            db.query_all("SELECT * FROM task_events WHERE task_id = ?", (task["id"],)),
            first_events,
        )

    def test_terminal_core_projection_closes_active_team_extension_without_overwrite(self) -> None:
        runtime = RecordingRuntime(self.task_state)
        service = self._create_team(runtime)
        parent, parent_run, team_run = service.create_task_and_run(
            "risk-council", self.scope, message="already public"
        )
        now = db.utc_now()
        public_result = {"summary": "PUBLIC_RESULT", "verification_id": "verify-fixed"}
        public_artifacts = [{"id": "art_public", "name": "public.md"}]
        db.execute(
            "UPDATE tasks SET status = 'completed', result_json = ?, artifacts_json = ?, updated_at = ? WHERE id = ?",
            (
                db.json_dumps(public_result),
                db.json_dumps(public_artifacts),
                now,
                parent["id"],
            ),
        )
        db.execute(
            """
            UPDATE task_runs
            SET status = 'completed', result_json = ?, intake_state = 'closed',
                intake_closed_at = ?, started_at = ?, finished_at = ?, updated_at = ?
            WHERE id = ?
            """,
            (db.json_dumps(public_result), now, now, now, now, parent_run["id"]),
        )
        db.execute(
            "UPDATE team_runs SET status = 'running', started_at = ?, finished_at = '', updated_at = ? WHERE id = ?",
            (now, now, team_run["id"]),
        )
        before_task = db.query_one("SELECT * FROM tasks WHERE id = ?", (parent["id"],))
        before_run = db.query_one(
            "SELECT * FROM task_runs WHERE id = ?", (parent_run["id"],)
        )

        self.assertEqual(service.reconcile_interrupted_orchestrated_runs(), [])

        self.assertEqual(
            db.query_one("SELECT * FROM tasks WHERE id = ?", (parent["id"],)),
            before_task,
        )
        self.assertEqual(
            db.query_one("SELECT * FROM task_runs WHERE id = ?", (parent_run["id"],)),
            before_run,
        )
        self.assertEqual(
            (db.query_one(
                "SELECT status FROM team_runs WHERE id = ?", (team_run["id"],)
            ) or {}).get("status"),
            "completed",
        )
        self.assertEqual(service.reconcile_interrupted_orchestrated_runs(), [])

    def test_team_submission_create_rolls_back_task_run_and_events_on_extension_failure(self) -> None:
        runtime = RecordingRuntime(self.task_state)
        service = self._create_team(runtime)
        db.execute(
            """
            CREATE TRIGGER fail_team_submission_insert
            BEFORE INSERT ON team_runs
            BEGIN
                SELECT RAISE(ABORT, 'injected team submission failure');
            END
            """
        )
        try:
            with self.assertRaises(sqlite3.IntegrityError):
                service.create_task_and_run(
                    "risk-council", self.scope, message="atomic submission"
                )
        finally:
            db.execute("DROP TRIGGER IF EXISTS fail_team_submission_insert")

        self.assertEqual(db.query_all("SELECT * FROM tasks"), [])
        self.assertEqual(db.query_all("SELECT * FROM task_runs"), [])
        self.assertEqual(db.query_all("SELECT * FROM team_runs"), [])
        self.assertEqual(db.query_all("SELECT * FROM task_events"), [])

    def test_orphan_member_failure_and_extension_update_rollback_together(self) -> None:
        runtime = RecordingRuntime(self.task_state)
        service = self._create_team(runtime)
        _, _, team_run = service.create_task_and_run(
            "risk-council", self.scope, message="orphan child atomic failure"
        )
        team = service.get_team("risk-council", self.scope) or {}
        member_run, child_run_id = service._create_member_attempt(
            team_run, team, team["members"][0]
        )
        child_task_id = member_run["child_task_id"]
        db.execute(
            f"""
            CREATE TRIGGER fail_member_extension_update
            BEFORE UPDATE ON team_member_runs
            WHEN OLD.id = '{member_run['id']}' AND NEW.status = 'failed'
            BEGIN
                SELECT RAISE(ABORT, 'injected member extension failure');
            END
            """
        )
        try:
            with self.assertRaises(sqlite3.IntegrityError):
                service.reconcile_interrupted_orchestrated_runs()
        finally:
            db.execute("DROP TRIGGER IF EXISTS fail_member_extension_update")

        self.assertEqual(
            (db.query_one(
                "SELECT status FROM tasks WHERE id = ?", (child_task_id,)
            ) or {}).get("status"),
            "queued",
        )
        self.assertEqual(self.task_state.get_run(child_run_id)["status"], "queued")
        self.assertEqual(
            (db.query_one(
                "SELECT status FROM team_member_runs WHERE id = ?", (member_run["id"],)
            ) or {}).get("status"),
            "queued",
        )

        service.reconcile_interrupted_orchestrated_runs()
        self.task_state.assert_terminal_clean(
            task_id=child_task_id, run_id=child_run_id
        )
        self.assertEqual(
            (db.query_one(
                "SELECT status FROM team_member_runs WHERE id = ?", (member_run["id"],)
            ) or {}).get("status"),
            "failed",
        )

    async def test_member_retry_activation_rolls_back_parent_run_on_team_update_failure(self) -> None:
        runtime = RecordingRuntime(self.task_state, fail_first={"researcher"})
        service = self._create_team(runtime)
        parent, _, team_run = service.create_task_and_run(
            "risk-council", self.scope, message="retry activation atomicity"
        )
        await service.run_team(team_run["id"])
        failed_member = next(
            item
            for item in service.get_team_run(team_run["id"], self.scope)["member_runs"]
            if item["status"] == "failed"
        )
        before_runs = self.task_state.list_runs(task_id=parent["id"], limit=100)
        before_task = db.query_one("SELECT * FROM tasks WHERE id = ?", (parent["id"],))
        before_team = db.query_one(
            "SELECT * FROM team_runs WHERE id = ?", (team_run["id"],)
        )
        db.execute(
            f"""
            CREATE TRIGGER fail_retry_team_activation
            BEFORE UPDATE ON team_runs
            WHEN OLD.id = '{team_run['id']}' AND NEW.status = 'running'
            BEGIN
                SELECT RAISE(ABORT, 'injected retry activation failure');
            END
            """
        )
        try:
            with self.assertRaises(sqlite3.IntegrityError):
                await service.retry_member(
                    team_run["id"], failed_member["id"], self.scope
                )
        finally:
            db.execute("DROP TRIGGER IF EXISTS fail_retry_team_activation")

        self.assertEqual(
            self.task_state.list_runs(task_id=parent["id"], limit=100), before_runs
        )
        self.assertEqual(
            db.query_one("SELECT * FROM tasks WHERE id = ?", (parent["id"],)),
            before_task,
        )
        self.assertEqual(
            db.query_one("SELECT * FROM team_runs WHERE id = ?", (team_run["id"],)),
            before_team,
        )

    def test_team_parent_run_metadata_mismatch_is_failed_not_dispatched(self) -> None:
        runtime = RecordingRuntime(self.task_state)
        service = self._create_team(runtime)
        parent, parent_run, team_run = service.create_task_and_run(
            "risk-council", self.scope, message="ownership mismatch"
        )
        self.task_state.update_run_metadata(
            parent_run["id"],
            {"executor_type": "team_supervisor", "executor_id": "wrong-owner"},
            merge=False,
        )

        self.assertEqual(service.queued_runs_for_recovery(), [])
        self.assertEqual(main_module._recover_interrupted_runs(), [])
        self.assertEqual(service.reconcile_interrupted_orchestrated_runs(), [])

        failed_team = db.query_one(
            "SELECT status FROM team_runs WHERE id = ?", (team_run["id"],)
        ) or {}
        self.assertEqual(failed_team.get("status"), "failed")
        self.task_state.assert_terminal_clean(
            task_id=parent["id"], run_id=parent_run["id"]
        )
        event = db.query_one(
            """SELECT data_json FROM task_events
               WHERE task_id = ? AND type = 'error' ORDER BY id DESC LIMIT 1""",
            (parent["id"],),
        ) or {}
        self.assertEqual(
            db.json_loads(event.get("data_json"), {}).get("error_type"),
            "ExecutorOwnershipMismatch",
        )

    async def test_real_agent_runtime_completes_parallel_team_and_supervisor(self) -> None:
        registry = SkillRegistry()
        registry.load_builtin_skills()
        mcp = McpGateway()
        mcp.seed_builtin_servers()
        runtime = AgentRuntime(
            registry,
            mcp,
            ModelGateway(),
            task_state=self.task_state,
            policy_engine=PolicyEngine(),
        )
        service = ExpertTeamService(runtime, task_state=self.task_state)
        service.create_team(
            {
                "id": "real-runtime-team",
                "name": "真实运行时专家团",
                "supervisor_agent_id": "supervisor",
                "organization_id": "org-a",
                "workspace_id": "workspace-a",
                "owner_user_id": "alice",
                "visibility": "private",
                "members": [
                    {
                        "id": "real-research",
                        "agent_id": "researcher",
                        "role": "方案分析",
                        "member_prompt": "分析方案 A 的优势和风险。",
                    },
                    {
                        "id": "real-review",
                        "agent_id": "reviewer",
                        "role": "独立复核",
                        "member_prompt": "分析方案 B 并提出反例。",
                    },
                ],
            }
        )
        previous_delay = os.environ.get("APP_DETERMINISTIC_STREAM_DELAY_MS")
        os.environ["APP_DETERMINISTIC_STREAM_DELAY_MS"] = "0"
        try:
            parent, _, team_run = service.create_task_and_run(
                "real-runtime-team",
                self.scope,
                message="比较方案 A 与方案 B，给出风险可控的选择建议",
            )
            await service.run_team(team_run["id"])
        finally:
            if previous_delay is None:
                os.environ.pop("APP_DETERMINISTIC_STREAM_DELAY_MS", None)
            else:
                os.environ["APP_DETERMINISTIC_STREAM_DELAY_MS"] = previous_delay
        completed = service.get_team_run(team_run["id"], self.scope) or {}
        self.assertEqual(completed["status"], "completed", completed)
        self.assertEqual(len(completed["member_runs"]), 2)
        self.assertTrue(all(item["status"] == "completed" for item in completed["member_runs"]))
        self.assertTrue(completed["supervisor_child_task_id"])
        parent_row = db.query_one("SELECT * FROM tasks WHERE id = ?", (parent["id"],)) or {}
        self.assertEqual(parent_row["status"], "completed")
        event_types = {
            item["type"]
            for item in db.query_all(
                "SELECT type FROM task_events WHERE task_id = ?", (parent["id"],)
            )
        }
        self.assertTrue(
            {"team_parallel_start", "team_aggregating", "team_completed"}.issubset(event_types)
        )


class ExpertTeamApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_db_path = db.DB_PATH
        self.original_env = os.environ.get("APP_DB_PATH")
        db.DB_PATH = Path(self.temp_dir.name) / "expert-team-api.db"
        os.environ["APP_DB_PATH"] = str(db.DB_PATH)
        db.init_db()
        self.scheduler_start = patch.object(main_module.loop_scheduler, "start", return_value=None)
        self.scheduler_stop = patch.object(main_module.loop_scheduler, "stop", new_callable=AsyncMock)
        self.skill_seed = patch.object(main_module.skill_registry, "load_builtin_skills", return_value=None)
        self.mcp_seed = patch.object(main_module.mcp_gateway, "seed_builtin_servers", return_value=None)
        self.agent_seed = patch.object(main_module, "seed_agents", return_value=None)
        self.recovery = patch.object(main_module, "_recover_interrupted_runs", return_value=[])
        for item in (
            self.scheduler_start,
            self.scheduler_stop,
            self.skill_seed,
            self.mcp_seed,
            self.agent_seed,
            self.recovery,
        ):
            item.start()
        self.client_context = TestClient(main_module.app)
        self.client = self.client_context.__enter__()
        for agent_id in ("supervisor", "researcher", "reviewer"):
            _insert_agent(agent_id)

    def tearDown(self) -> None:
        self.client_context.__exit__(None, None, None)
        for item in reversed(
            (
                self.scheduler_start,
                self.scheduler_stop,
                self.skill_seed,
                self.mcp_seed,
                self.agent_seed,
                self.recovery,
            )
        ):
            item.stop()
        db.DB_PATH = self.original_db_path
        if self.original_env is None:
            os.environ.pop("APP_DB_PATH", None)
        else:
            os.environ["APP_DB_PATH"] = self.original_env
        self.temp_dir.cleanup()

    @staticmethod
    def _scope_params() -> dict[str, str]:
        return {
            "organization_id": "org-a",
            "workspace_id": "workspace-a",
            "user_id": "alice",
        }

    def test_template_install_team_and_async_run_api_contract(self) -> None:
        created = self.client.post(
            "/api/expert-templates",
            json={
                "id": "api-expert",
                "name": "API 专家",
                "manifest": {"model": "deterministic", "permissions": {"read_only": True}},
                "organization_id": "org-a",
                "workspace_id": "workspace-a",
                "owner_user_id": "alice",
                "visibility": "private",
            },
        )
        self.assertEqual(created.status_code, 201, created.text)
        invisible = self.client.get(
            "/api/expert-templates",
            params={**self._scope_params(), "user_id": "bob"},
        )
        self.assertEqual(invisible.json(), [])
        installed = self.client.post(
            "/api/expert-templates/api-expert/install",
            json={
                **self._scope_params(),
                "installation_id": "api-install",
                "agent_id": "api-installed-agent",
                "visibility": "private",
            },
        )
        self.assertEqual(installed.status_code, 201, installed.text)
        self.assertEqual(installed.json()["agent_id"], "api-installed-agent")

        team = self.client.post(
            "/api/expert-teams",
            json={
                "id": "api-team",
                "name": "API 专家团",
                "supervisor_agent_id": "supervisor",
                "aggregation_prompt": "PRIVATE_AGGREGATION_PROMPT",
                "permissions": {"private_policy": "PRIVATE_TEAM_PERMISSION"},
                "organization_id": "org-a",
                "workspace_id": "workspace-a",
                "owner_user_id": "alice",
                "visibility": "private",
                "members": [
                    {
                        "id": "api-member-a",
                        "agent_id": "researcher",
                        "role": "研究",
                        "member_prompt": "PRIVATE_MEMBER_PROMPT",
                        "permissions": {"private_member_policy": "PRIVATE_MEMBER_PERMISSION"},
                    },
                    {"id": "api-member-b", "agent_id": "reviewer", "role": "复核"},
                ],
            },
        )
        self.assertEqual(team.status_code, 201, team.text)
        self.assertEqual(len(team.json()["members"]), 2)
        with patch.object(main_module, "_schedule_team_run") as schedule:
            run = self.client.post(
                "/api/expert-teams/api-team/runs",
                json={**self._scope_params(), "message": "并行评估 API 方案"},
            )
        self.assertEqual(run.status_code, 202, run.text)
        body = run.json()
        self.assertTrue(body["accepted"])
        self.assertEqual(body["task"]["executor_type"], "team")
        self.assertEqual(body["task"]["executor_id"], "api-team")
        self.assertEqual(body["team_run"]["status"], "queued")
        schedule.assert_called_once_with(body["team_run"]["id"])
        fetched = self.client.get(
            f"/api/expert-team-runs/{body['team_run']['id']}", params=self._scope_params()
        )
        self.assertEqual(fetched.status_code, 200, fetched.text)
        denied = self.client.get(
            f"/api/expert-team-runs/{body['team_run']['id']}",
            params={**self._scope_params(), "user_id": "bob"},
        )
        self.assertEqual(denied.status_code, 404)
        with patch.object(main_module, "_schedule_team_run") as automatic_schedule:
            automatic = self.client.post(
                "/api/tasks",
                json={
                    "message": "请由专家并行评估这个任务",
                    "executor_type": "team",
                    "organization_id": "org-a",
                    "workspace": "workspace-a",
                    "user_id": "alice",
                },
            )
        self.assertEqual(automatic.status_code, 200, automatic.text)
        automatic_body = automatic.json()
        self.assertEqual(automatic_body["executor_id"], "api-team")
        self.assertEqual(automatic_body["expert_selection"]["selection_mode"], "automatic")
        self.assertEqual(automatic_body["expert_selection"]["team_id"], "api-team")
        self.assertEqual(
            [item["role"] for item in automatic_body["expert_selection"]["members"]],
            ["研究", "复核"],
        )
        automatic_schedule.assert_called_once_with(automatic_body["team_run"]["id"])
        event = db.query_one(
            "SELECT * FROM task_events WHERE task_id = ? AND type = 'expert_selection'",
            (automatic_body["id"],),
        ) or {}
        event_data = db.json_loads(event.get("data_json"), {})
        self.assertEqual(event_data, automatic_body["expert_selection"])
        public_json = json.dumps(event_data, ensure_ascii=False)
        for private_value in (
            "PRIVATE_AGGREGATION_PROMPT",
            "PRIVATE_TEAM_PERMISSION",
            "PRIVATE_MEMBER_PROMPT",
            "PRIVATE_MEMBER_PERMISSION",
        ):
            self.assertNotIn(private_value, public_json)
        self.assertNotIn("permissions", public_json)
        self.assertNotIn("member_prompt", public_json)
        self.assertNotIn("aggregation_prompt", public_json)
        with patch.object(main_module, "_schedule_team_run") as task_schedule:
            via_task_api = self.client.post(
                "/api/tasks",
                json={
                    "message": "从统一任务入口运行专家团",
                    "executor_type": "team",
                    "executor_id": "api-team",
                    "agent_id": "general-agent",
                    "organization_id": "org-a",
                    "workspace": "workspace-a",
                    "user_id": "alice",
                },
            )
        self.assertEqual(via_task_api.status_code, 200, via_task_api.text)
        task_body = via_task_api.json()
        self.assertEqual(task_body["agent_id"], "supervisor")
        self.assertEqual(task_body["executor_id"], "api-team")
        self.assertEqual(task_body["expert_selection"]["selection_mode"], "manual")
        self.assertEqual(task_body["team_run"]["status"], "queued")
        task_schedule.assert_called_once_with(task_body["team_run"]["id"])

    def test_automatic_team_selection_prefers_semantically_matching_team(self) -> None:
        teams = [
            {
                "id": "security-team",
                "name": "安全合规专家团",
                "description": "评估权限隔离、数据安全、审计和合规风险",
                "members": [
                    {"id": "security-a", "agent_id": "researcher", "role": "安全审计"},
                    {"id": "security-b", "agent_id": "reviewer", "role": "权限复核"},
                ],
            },
            {
                "id": "growth-team",
                "name": "增长体验专家团",
                "description": "评估市场增长、转化和用户体验",
                "members": [
                    {"id": "growth-a", "agent_id": "researcher", "role": "增长分析"},
                    {"id": "growth-b", "agent_id": "reviewer", "role": "体验评审"},
                ],
            },
        ]
        for item in teams:
            created = self.client.post(
                "/api/expert-teams",
                json={
                    **item,
                    "supervisor_agent_id": "supervisor",
                    "organization_id": "org-a",
                    "workspace_id": "workspace-a",
                    "owner_user_id": "alice",
                    "visibility": "private",
                },
            )
            self.assertEqual(created.status_code, 201, created.text)
        with patch.object(main_module, "_schedule_team_run") as schedule:
            response = self.client.post(
                "/api/tasks",
                json={
                    "message": "请评估系统权限隔离和数据安全的合规风险",
                    "executor_type": "team",
                    "organization_id": "org-a",
                    "workspace": "workspace-a",
                    "user_id": "alice",
                },
            )
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["executor_id"], "security-team")
        self.assertEqual(body["expert_selection"]["team_id"], "security-team")
        self.assertTrue(body["expert_selection"]["matched_terms"])
        self.assertNotIn("confidence", body["expert_selection"])
        schedule.assert_called_once_with(body["team_run"]["id"])

    def test_automatic_team_selection_without_available_team_is_friendly(self) -> None:
        response = self.client.post(
            "/api/tasks",
            json={
                "message": "请自动组织专家评审方案",
                "executor_type": "team",
                "organization_id": "org-a",
                "workspace": "workspace-a",
                "user_id": "alice",
            },
        )
        self.assertEqual(response.status_code, 404, response.text)
        self.assertIn("当前没有可用专家团", response.json()["detail"])

    def test_live_run_endpoint_has_an_event_loop_for_background_team(self) -> None:
        created = self.client.post(
            "/api/expert-teams",
            json={
                "id": "live-schedule-team",
                "name": "真实调度专家团",
                "supervisor_agent_id": "supervisor",
                "organization_id": "org-a",
                "workspace_id": "workspace-a",
                "owner_user_id": "alice",
                "visibility": "private",
                "members": [
                    {"id": "live-member-a", "agent_id": "researcher", "role": "研究"},
                    {"id": "live-member-b", "agent_id": "reviewer", "role": "复核"},
                ],
            },
        )
        self.assertEqual(created.status_code, 201, created.text)
        with patch.object(
            main_module.expert_team_service,
            "run_team",
            new=AsyncMock(return_value=None),
        ) as runner:
            response = self.client.post(
                "/api/expert-teams/live-schedule-team/runs",
                json={**self._scope_params(), "message": "验证真实异步调度入口"},
            )
            for _ in range(20):
                if runner.await_count:
                    break
                time.sleep(0.01)
        self.assertEqual(response.status_code, 202, response.text)
        self.assertTrue(response.json()["accepted"])
        self.assertEqual(runner.await_count, 1)
        body = response.json()
        self.assertEqual(
            main_module._recover_interrupted_runs(
                preserve_task_ids={body["task"]["id"]}
            ),
            [],
        )
        preserved_run = main_module.task_state.get_run(body["run"]["id"])
        self.assertEqual(preserved_run["status"], "queued")


if __name__ == "__main__":
    unittest.main()
