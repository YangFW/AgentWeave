from __future__ import annotations

import unittest

from app.services.goal_spec_service import compile_draft, finalize
from app.services.runtime_contract_service import (
    ContractViolation,
    RuntimeContractService,
)


class _Skills:
    def runtime_content(self, skill_id: str, max_chars: int = 16000) -> str:
        return f"stable instructions for {skill_id}"


class _Tools:
    def __init__(self) -> None:
        self.definitions = {
            ("lab", "read"): {
                "server_id": "lab",
                "name": "read",
                "description": "Read a city record",
                "effect": "read",
                "input_schema": {
                    "type": "object",
                    "properties": {"city": {"type": "string"}},
                    "required": ["city"],
                    "additionalProperties": False,
                },
            },
            ("lab", "write"): {
                "server_id": "lab",
                "name": "write",
                "description": "Write a city record",
                "effect": "write",
                "input_schema": {
                    "type": "object",
                    "properties": {"city": {"type": "string"}},
                    "required": ["city"],
                },
            },
        }

    def get_tool_definition(self, server_id: str, tool_name: str):
        value = self.definitions.get((server_id, tool_name))
        return dict(value) if value else None

    def list_tools(self):
        return [dict(item) for item in self.definitions.values()]


class RuntimeContractServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tools = _Tools()
        self.service = RuntimeContractService(_Skills(), self.tools)
        draft = compile_draft(
            task_id="task-contract",
            objective={"statement": "读取宁波记录", "intent": "lookup"},
            inputs=[
                {
                    "key": "city",
                    "label": "城市",
                    "value": "宁波",
                    "status": "provided",
                    "provenance": [
                        {
                            "source_type": "user_message",
                            "source_id": "task-contract",
                        }
                    ],
                }
            ],
        )
        bindings = self.service.snapshot_bindings(
            skills=[{"id": "lookup", "name": "Lookup", "version": "1.2.3"}],
            tools=[("lab", "read")],
            argument_constraints={
                ("lab", "read"): [
                    {
                        "argument_path": "city",
                        "operator": "equals",
                        "source_input_key": "city",
                    }
                ]
            },
        )
        self.goal = finalize(draft, capability_bindings=bindings)

    def test_snapshot_freezes_skill_content_and_exact_tool_schema(self) -> None:
        self.assertEqual(self.goal.capability_bindings.skills[0].version, "1.2.3")
        self.assertEqual(len(self.goal.capability_bindings.skills[0].content_hash), 64)
        self.assertEqual(self.goal.capability_bindings.tools[0].qualified_name, "lab.read")
        self.assertEqual(
            self.goal.capability_bindings.tools[0].argument_constraints[0].source_input_key,
            "city",
        )
        self.service.validate_tool_call(self.goal, "lab", "read", {"city": "宁波"})

    def test_same_server_wrong_tool_is_rejected(self) -> None:
        with self.assertRaisesRegex(ContractViolation, "未授权"):
            self.service.validate_tool_call(self.goal, "lab", "write", {"city": "宁波"})

    def test_json_schema_and_goal_parameter_are_both_enforced(self) -> None:
        with self.assertRaisesRegex(ContractViolation, "不符合 Schema"):
            self.service.validate_tool_call(self.goal, "lab", "read", {"city": 42})
        with self.assertRaisesRegex(ContractViolation, "与已确认目标不一致"):
            self.service.validate_tool_call(self.goal, "lab", "read", {"city": "上海"})
        with self.assertRaisesRegex(ContractViolation, "不符合 Schema"):
            self.service.validate_tool_call(
                self.goal, "lab", "read", {"city": "宁波", "extra": True}
            )

    def test_schema_change_fails_closed(self) -> None:
        self.tools.definitions[("lab", "read")]["input_schema"]["properties"][
            "limit"
        ] = {"type": "integer"}
        with self.assertRaisesRegex(ContractViolation, "Schema 已变化"):
            self.service.validate_tool_call(self.goal, "lab", "read", {"city": "宁波"})

    def test_invalid_schema_cannot_be_snapshotted(self) -> None:
        self.tools.definitions[("lab", "read")]["input_schema"] = {
            "type": "definitely-not-a-json-schema-type"
        }
        with self.assertRaisesRegex(ContractViolation, "Schema 无效"):
            self.service.snapshot_bindings(skills=[], tools=[("lab", "read")])


if __name__ == "__main__":
    unittest.main()
