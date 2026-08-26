from __future__ import annotations

import copy
import hashlib
import inspect
import tempfile
import unittest
from pathlib import Path
from typing import Any, Awaitable, Callable

from app import db
from app.services.agent_runtime import AgentRuntime, create_task_record
from app.services.policy_engine import PolicyEngine
from app.services.task_state import TaskStateService


async def _emit_delta(
    callback: Callable[[str], Awaitable[None] | None] | None,
    text: str,
) -> None:
    if callback is None:
        return
    pending = callback(text)
    if inspect.isawaitable(pending):
        await pending


class ResumeContractSkillRegistry:
    """Stable one-Skill registry used to isolate resume contract tests."""

    def __init__(self) -> None:
        self.skill = {
            "id": "resume_contract_skill",
            "name": "恢复合同测试 Skill",
            "description": "通过固定 MCP 读取一个恢复测试值。",
            "version": "1.0.0",
            "content": "必须调用 resume-store.read，并只回答当前恢复测试目标。",
            "enabled": True,
            "required_mcps": ["resume-store"],
        }

    def list_skills(self, enabled_only: bool = False) -> list[dict[str, Any]]:
        return [dict(self.skill)]

    def score_skills(
        self, message: str, allowed_ids: list[str] | None = None
    ) -> list[dict[str, Any]]:
        if allowed_ids and self.skill["id"] not in allowed_ids:
            return []
        return [{"skill": dict(self.skill), "score": 10.0}]

    def get_skill(self, skill_id: str) -> dict[str, Any] | None:
        return dict(self.skill) if skill_id == self.skill["id"] else None

    def runtime_content(self, skill_id: str, max_chars: int = 16000) -> str:
        if skill_id != self.skill["id"]:
            return ""
        return str(self.skill["content"])[:max_chars]


class MutableSchemaMcpGateway:
    """MCP whose advertised schema can change between the original and resumed run."""

    def __init__(self) -> None:
        self.schema_revision = 1
        self.calls: list[dict[str, Any]] = []

    def list_tools(self) -> list[dict[str, Any]]:
        properties: dict[str, Any] = {"key": {"type": "string"}}
        if self.schema_revision >= 2:
            # An optional property is still a contract change: exact Schema
            # identity, rather than argument compatibility, controls replay.
            properties["trace_id"] = {"type": "string"}
        return [
            {
                "server_id": "resume-store",
                "name": "read",
                "description": "读取固定恢复测试值",
                "effect": "read",
                "server_kind": "builtin",
                "input_schema": {
                    "type": "object",
                    "properties": properties,
                    "required": ["key"],
                    "additionalProperties": False,
                },
            }
        ]

    async def invoke_tool(
        self,
        server_id: str,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        task_id: str = "",
    ) -> dict[str, Any]:
        self.calls.append(
            {
                "server_id": server_id,
                "tool_name": tool_name,
                "arguments": dict(arguments),
                "task_id": task_id,
                "schema_revision": self.schema_revision,
            }
        )
        return {
            "value": f"stable-value-v{self.schema_revision}",
            "source": "resume-contract-fixture",
        }


class ToolCallingResumeModel:
    def __init__(self) -> None:
        self.results: list[dict[str, Any]] = []

    async def resolve_intent(
        self,
        message: str,
        history: list[dict[str, str]],
        model_config_id: str,
    ) -> dict[str, Any]:
        return {
            "standalone_request": message,
            "intent": "read_value",
            "parameters": {"key": "stable-key"},
            "missing_information": [],
            "is_follow_up": False,
            "source": "resume_contract_fixture",
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
        result = await invoke("resume-store__read", {"key": "stable-key"})
        self.results.append(dict(result))
        answer = f"已按当前目标读取并核验：{result['value']}。"
        await _emit_delta(on_delta, answer)
        return answer


class NoToolResumeModel(ToolCallingResumeModel):
    """Produces a plausible candidate while intentionally omitting the bound tool."""

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
        answer = "候选声称已经读取，但本轮没有调用已绑定工具。"
        await _emit_delta(on_delta, answer)
        return answer


class RuntimeResumeContractTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.original_db_path = db.DB_PATH
        self.temp_dir = tempfile.TemporaryDirectory()
        db.DB_PATH = Path(self.temp_dir.name) / "runtime-resume-contract.db"
        db.init_db()
        self.state = TaskStateService(db.get_conn)
        self.registry = ResumeContractSkillRegistry()

    async def asyncTearDown(self) -> None:
        db.DB_PATH = self.original_db_path
        self.temp_dir.cleanup()

    def _runtime(
        self,
        model: ToolCallingResumeModel,
        gateway: MutableSchemaMcpGateway,
    ) -> AgentRuntime:
        return AgentRuntime(
            self.registry,
            gateway,
            model,
            task_state=self.state,
            policy_engine=PolicyEngine(),
        )

    @staticmethod
    def _new_task() -> dict[str, Any]:
        return create_task_record(
            "使用恢复合同工具读取 stable-key，并给出可验证结论",
            "general-agent",
            conversation_id="conv_resume_contract",
        )

    @staticmethod
    def _event_count(task_id: str, event_type: str) -> int:
        row = db.query_one(
            "SELECT COUNT(*) AS total FROM task_events WHERE task_id = ? AND type = ?",
            (task_id, event_type),
        )
        return int((row or {}).get("total") or 0)

    async def _complete_original_run(
        self,
        *,
        gateway: MutableSchemaMcpGateway | None = None,
        model: ToolCallingResumeModel | None = None,
    ) -> tuple[
        dict[str, Any],
        AgentRuntime,
        MutableSchemaMcpGateway,
        ToolCallingResumeModel,
        dict[str, Any],
        dict[str, Any],
        dict[str, Any],
    ]:
        gateway = gateway or MutableSchemaMcpGateway()
        model = model or ToolCallingResumeModel()
        runtime = self._runtime(model, gateway)
        task = self._new_task()

        await runtime.run_task(task["id"])

        first_run = self.state.list_runs(task_id=task["id"])[0]
        self.assertEqual(first_run["status"], "completed")
        self.assertEqual(len(gateway.calls), 1)
        checkpoint = self.state.latest_checkpoint(
            first_run["id"], include_state=True
        )
        self.assertIsNotNone(checkpoint)
        assert checkpoint is not None
        goal = self.state.latest_goal_spec(run_id=first_run["id"])
        self.assertIsNotNone(goal)
        assert goal is not None
        self.assertEqual(
            checkpoint["state"]["goal_spec_ref"],
            {
                "id": goal["id"],
                "goal_id": goal["spec"]["goal_id"],
                "version": goal["version"],
                "spec_hash": goal["spec_hash"],
            },
        )
        return task, runtime, gateway, model, first_run, checkpoint, goal

    async def _resume(
        self,
        task_id: str,
        runtime: AgentRuntime,
        checkpoint_id: str,
    ) -> dict[str, Any]:
        queued = self.state.create_run(
            task_id, resumed_from_checkpoint_id=checkpoint_id
        )
        await runtime.run_task(task_id, run_id=queued["id"])
        resumed = self.state.get_run(queued["id"])
        self.assertIsNotNone(resumed)
        assert resumed is not None
        return resumed

    async def test_resume_preserves_exact_goal_spec_identity_and_run_projection(
        self,
    ) -> None:
        (
            task,
            runtime,
            _gateway,
            _model,
            first_run,
            checkpoint,
            original_goal,
        ) = await self._complete_original_run()
        original_lineage = {
            item["version"]: (item["id"], item["spec_hash"])
            for item in self.state.list_goal_specs(run_id=first_run["id"])
        }

        resumed = await self._resume(task["id"], runtime, checkpoint["id"])

        self.assertEqual(resumed["status"], "completed")
        resumed_goal = self.state.latest_goal_spec(run_id=resumed["id"])
        self.assertIsNotNone(resumed_goal)
        assert resumed_goal is not None
        self.assertEqual(
            (resumed_goal["id"], resumed_goal["version"], resumed_goal["spec_hash"]),
            (
                original_goal["id"],
                original_goal["version"],
                original_goal["spec_hash"],
            ),
        )
        resumed_lineage = {
            item["version"]: (item["id"], item["spec_hash"])
            for item in self.state.list_goal_specs(run_id=resumed["id"])
        }
        self.assertEqual(resumed_lineage, original_lineage)
        self.assertEqual(
            {
                key: resumed["metadata"].get(key)
                for key in ("goal_spec_id", "goal_spec_version", "goal_spec_hash")
            },
            {
                "goal_spec_id": original_goal["id"],
                "goal_spec_version": original_goal["version"],
                "goal_spec_hash": original_goal["spec_hash"],
            },
            "恢复运行必须显式投影活动 GoalSpec 的原始身份，不能只靠隐式关联",
        )

    async def test_tampered_checkpoint_goal_or_reference_fails_closed_without_answer(
        self,
    ) -> None:
        for tamper_kind in ("goal_spec", "goal_spec_ref"):
            with self.subTest(tamper_kind=tamper_kind):
                (
                    task,
                    runtime,
                    gateway,
                    _model,
                    first_run,
                    checkpoint,
                    _goal,
                ) = await self._complete_original_run()
                answer_count = self._event_count(task["id"], "answer")
                state = copy.deepcopy(checkpoint["state"])
                if tamper_kind == "goal_spec":
                    state["goal_spec"]["objective"]["statement"] = (
                        "检查点中被篡改的目标"
                    )
                else:
                    state["goal_spec_ref"]["spec_hash"] = "0" * 64
                tampered = self.state.create_checkpoint(
                    first_run["id"],
                    state,
                    reason=f"tampered-{tamper_kind}",
                )

                resumed = await self._resume(task["id"], runtime, tampered["id"])

                self.assertEqual(resumed["status"], "failed")
                self.assertEqual(
                    self._event_count(task["id"], "answer"), answer_count
                )
                self.assertEqual(len(gateway.calls), 1)
                self.assertEqual(
                    self.state.list_verifications(run_id=resumed["id"]), []
                )
                task_row = db.query_one(
                    "SELECT status FROM tasks WHERE id = ?", (task["id"],)
                ) or {}
                self.assertEqual(task_row.get("status"), "failed")
                self.state.assert_terminal_clean(
                    task_id=task["id"], run_id=resumed["id"]
                )

    async def test_mcp_schema_change_before_resume_fails_without_cache_reuse(
        self,
    ) -> None:
        (
            task,
            runtime,
            gateway,
            _model,
            _first_run,
            checkpoint,
            _goal,
        ) = await self._complete_original_run()
        answer_count = self._event_count(task["id"], "answer")
        reused_count = self._event_count(task["id"], "tool_reused")
        gateway.schema_revision = 2

        resumed = await self._resume(task["id"], runtime, checkpoint["id"])

        self.assertEqual(resumed["status"], "failed")
        self.assertEqual(len(gateway.calls), 1)
        self.assertEqual(
            self._event_count(task["id"], "tool_reused"), reused_count
        )
        self.assertEqual(self._event_count(task["id"], "answer"), answer_count)
        error_event = db.query_one(
            "SELECT content FROM task_events WHERE task_id = ? AND type = 'error' ORDER BY id DESC LIMIT 1",
            (task["id"],),
        )
        self.assertIn("Schema 已变化", str((error_event or {}).get("content") or ""))

    async def test_stale_checkpoint_cache_falls_back_to_current_goal_effect_journal(
        self,
    ) -> None:
        (
            task,
            runtime,
            gateway,
            _model,
            first_run,
            checkpoint,
            goal,
        ) = await self._complete_original_run()
        current_hash = str(goal["spec_hash"])
        stale_hash = hashlib.sha256(b"superseded-goal").hexdigest()
        state = copy.deepcopy(checkpoint["state"])
        current_cache = dict(
            state["completed_tools_by_goal_hash"].pop(current_hash)
        )
        fingerprint = next(iter(current_cache))
        state["completed_tools_by_goal_hash"] = {stale_hash: current_cache}
        state["completed_tools"] = current_cache
        state["completed_tools_goal_hash"] = stale_hash
        for evidence in state.get("tool_evidence", []):
            evidence["goal_spec_ref"]["spec_hash"] = stale_hash
        stale_checkpoint = self.state.create_checkpoint(
            first_run["id"], state, reason="stale-cache-from-other-goal"
        )

        resumed = await self._resume(task["id"], runtime, stale_checkpoint["id"])

        self.assertEqual(resumed["status"], "completed")
        # The tampered checkpoint cache is not trusted.  The independently
        # persisted effect belongs to the still-current GoalSpec, however, so
        # restart recovery must reuse it instead of repeating the side effect.
        self.assertEqual(len(gateway.calls), 1)
        self.assertEqual(self._event_count(task["id"], "tool_reused"), 0)
        self.assertEqual(
            self._event_count(task["id"], "tool_effect_reused"), 1
        )
        latest = self.state.latest_checkpoint(resumed["id"], include_state=True)
        self.assertIsNotNone(latest)
        assert latest is not None
        latest_state = latest["state"]
        self.assertEqual(latest_state["completed_tools_goal_hash"], current_hash)
        self.assertIn(
            fingerprint,
            latest_state["completed_tools_by_goal_hash"][current_hash],
        )
        current_evidence = [
            item
            for item in latest_state.get("tool_evidence", [])
            if item.get("fingerprint") == fingerprint
        ]
        self.assertEqual(len(current_evidence), 1)
        self.assertEqual(
            current_evidence[0]["goal_spec_ref"]["spec_hash"], current_hash
        )

    async def test_evidence_from_another_goal_hash_cannot_pass_final_verification(
        self,
    ) -> None:
        (
            task,
            _runtime,
            gateway,
            _model,
            first_run,
            checkpoint,
            goal,
        ) = await self._complete_original_run()
        stale_hash = hashlib.sha256(b"different-goal-evidence").hexdigest()
        state = copy.deepcopy(checkpoint["state"])
        state["completed_tools_by_goal_hash"] = {}
        state["completed_tools"] = {}
        state["completed_tools_goal_hash"] = stale_hash
        for evidence in state.get("tool_evidence", []):
            evidence["goal_spec_ref"] = {
                **dict(evidence.get("goal_spec_ref") or {}),
                "spec_hash": stale_hash,
            }
        stale_checkpoint = self.state.create_checkpoint(
            first_run["id"], state, reason="stale-evidence-from-other-goal"
        )
        no_tool_runtime = self._runtime(NoToolResumeModel(), gateway)
        answer_count = self._event_count(task["id"], "answer")

        resumed = await self._resume(
            task["id"], no_tool_runtime, stale_checkpoint["id"]
        )

        self.assertEqual(resumed["status"], "failed")
        self.assertEqual(len(gateway.calls), 1)
        self.assertEqual(self._event_count(task["id"], "answer"), answer_count)
        reports = self.state.list_verifications(run_id=resumed["id"])
        self.assertEqual(len(reports), 1)
        self.assertEqual(reports[0]["status"], "failed")
        self.assertEqual(
            reports[0]["goal_spec_id"], goal["id"],
            "失败报告仍必须绑定恢复的原始 GoalSpec",
        )

    async def test_resumed_plan_and_nodes_use_goal_version_namespace(self) -> None:
        (
            task,
            runtime,
            _gateway,
            _model,
            _first_run,
            checkpoint,
            goal,
        ) = await self._complete_original_run()

        resumed = await self._resume(task["id"], runtime, checkpoint["id"])

        self.assertEqual(resumed["status"], "completed")
        expected_plan_id = (
            f"plan_{goal['spec']['goal_id']}_v{goal['version']}"
        )
        self.assertEqual(resumed["metadata"].get("plan_id"), expected_plan_id)
        nodes = self.state.list_nodes(resumed["id"])
        self.assertTrue(nodes)
        self.assertTrue(
            all(
                node["metadata"].get("plan_id") == expected_plan_id
                and node["node_key"].startswith(f"{expected_plan_id}:")
                for node in nodes
            ),
            nodes,
        )
        logical_ids = {
            str(node["metadata"].get("logical_id") or "") for node in nodes
        }
        self.assertTrue(
            {"understand", "prepare", "execute", "validate"}.issubset(
                logical_ids
            )
        )
        latest = self.state.latest_checkpoint(resumed["id"], include_state=True)
        self.assertIsNotNone(latest)
        assert latest is not None
        self.assertEqual(latest["state"].get("plan_id"), expected_plan_id)
        self.assertEqual(
            latest["state"].get("goal_spec_ref"),
            {
                "id": goal["id"],
                "goal_id": goal["spec"]["goal_id"],
                "version": goal["version"],
                "spec_hash": goal["spec_hash"],
            },
        )


if __name__ == "__main__":
    unittest.main()
