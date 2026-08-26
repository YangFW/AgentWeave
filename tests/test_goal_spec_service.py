from __future__ import annotations

import json
import unittest

from pydantic import ValidationError

from app.services.goal_spec_service import (
    CapabilityBindings,
    GoalSpec,
    ToolBinding,
    canonical_goal_hash,
    compile_draft,
    ensure_goal_spec,
    finalize,
    public_goal_summary,
    revise_for_steering,
)


SKILL_HASH = "1" * 64
TOOL_SCHEMA_HASH = "2" * 64
REVISED_TOOL_SCHEMA_HASH = "3" * 64
ATTACHMENT_HASH = "4" * 64


def confirmed_by_user() -> dict[str, object]:
    return {
        "status": "user_confirmed",
        "mode": "user",
        "confidence": 1.0,
        "confirmation_ref": "event_user_confirmation",
    }


def bindings(schema_hash: str = TOOL_SCHEMA_HASH) -> dict[str, object]:
    return {
        "skills": [
            {
                "skill_id": "word_document",
                "version": "1.2.0",
                "content_hash": SKILL_HASH,
                "name": "Word 文档",
                "purpose": "组织可下载文档",
                "score": 9.5,
            }
        ],
        "tools": [
            {
                "server_id": "report",
                "tool_name": "generate_document",
                "schema_hash": schema_hash,
                "effect": "write",
                "purpose": "生成 DOCX 交付物",
            }
        ],
        "network_access": "none",
    }


def draft_with_all_fields():
    return compile_draft(
        task_id="task_goal_contract",
        conversation_id="conv_goal_contract",
        objective={
            "statement": "根据上传的会议记录生成 Word 纪要",
            "intent": "create_meeting_minutes",
            "in_scope": ["整理决定事项", "列出负责人"],
            "out_of_scope": ["虚构缺失日期"],
            "constraints": ["只使用附件内容"],
            "provenance": [
                {
                    "source_type": "user_message",
                    "source_id": "event_user_1",
                    "field": "message",
                    "excerpt": "内部解析记录不应进入公开摘要",
                }
            ],
        },
        inputs=[
            {
                "key": "source_notes",
                "label": "会议记录",
                "value": {"attachment_id": "upl_notes", "pages": 3},
                "required": True,
                "status": "provided",
                "provenance": [
                    {
                        "source_type": "attachment",
                        "source_id": "upl_notes",
                        "field": "extracted_text",
                        "excerpt": "敏感附件摘录不得出现在公开目标摘要",
                    }
                ],
            }
        ],
        deliverables=[
            {
                "id": "minutes_docx",
                "kind": "artifact",
                "format": "docx",
                "title": "会议纪要",
                "filename": "会议纪要.docx",
                "sections": ["决定事项", "负责人"],
                "download_required": True,
                "source_input_keys": ["source_notes"],
            }
        ],
        capability_bindings=bindings(),
        context_refs=[
            {
                "kind": "attachment",
                "ref_id": "upl_notes",
                "role": "primary_source",
                "content_hash": ATTACHMENT_HASH,
                "required": True,
                "label": "会议记录.docx",
            }
        ],
        acceptance=[
            {
                "id": "semantic_goal",
                "title": "纪要忠实对应当前会议记录",
                "kind": "semantic_match",
                "target": "answer_and_artifacts",
                "operator": "semantic_equivalent",
                "severity": "block",
                "evidence_required": True,
            },
            {
                "id": "download_docx",
                "title": "Word 文件可以下载",
                "kind": "download",
                "target": "deliverable:minutes_docx",
                "operator": "valid",
            },
        ],
    )


class GoalSpecServiceTests(unittest.TestCase):
    def test_compile_draft_covers_contract_and_uses_exact_tool_binding(self) -> None:
        spec = draft_with_all_fields()

        self.assertEqual(spec.schema_version, "1.0")
        self.assertEqual(spec.version, 1)
        self.assertEqual(spec.status, "draft")
        self.assertEqual(len(spec.spec_hash), 64)
        self.assertEqual(spec.objective.intent, "create_meeting_minutes")
        self.assertEqual(spec.inputs[0].provenance[0].source_id, "upl_notes")
        self.assertEqual(spec.deliverables[0].filename, "会议纪要.docx")
        self.assertEqual(spec.context_refs[0].content_hash, ATTACHMENT_HASH)
        self.assertEqual(spec.capability_bindings.skills[0].content_hash, SKILL_HASH)
        tool = spec.capability_bindings.tools[0]
        self.assertEqual(tool.qualified_name, "report.generate_document")
        self.assertEqual(tool.schema_hash, TOOL_SCHEMA_HASH)
        self.assertEqual(spec.acceptance[0].kind, "semantic_match")

        serialized = spec.model_dump(mode="json")
        self.assertEqual(serialized["capability_bindings"]["tools"][0]["server_id"], "report")
        self.assertEqual(serialized["capability_bindings"]["tools"][0]["tool_name"], "generate_document")
        json.dumps(serialized, ensure_ascii=False)

    def test_canonical_hash_is_stable_across_plain_dict_key_order_and_roundtrip(self) -> None:
        first = compile_draft(
            task_id="task_hash",
            objective={"statement": "生成结论", "intent": "answer"},
            inputs=[
                {
                    "key": "facts",
                    "label": "事实",
                    "value": {"b": 2, "a": 1},
                    "status": "provided",
                    "provenance": [
                        {"source_type": "user_message", "source_id": "event_1"}
                    ],
                }
            ],
        )
        second = compile_draft(
            task_id="task_hash",
            objective={"intent": "answer", "statement": "生成结论"},
            inputs=[
                {
                    "provenance": [
                        {"source_id": "event_1", "source_type": "user_message"}
                    ],
                    "status": "provided",
                    "value": {"a": 1, "b": 2},
                    "label": "事实",
                    "key": "facts",
                }
            ],
        )

        self.assertEqual(first.goal_id, second.goal_id)
        self.assertEqual(first.spec_hash, second.spec_hash)
        self.assertEqual(first.spec_hash, canonical_goal_hash(first))
        restored = ensure_goal_spec(first.model_dump(mode="json"))
        self.assertEqual(restored, first)
        self.assertEqual(restored.spec_hash, first.spec_hash)

    def test_extra_fields_are_forbidden_at_every_contract_boundary(self) -> None:
        with self.assertRaises(ValidationError):
            compile_draft(
                task_id="task_extra",
                objective={
                    "statement": "回答问题",
                    "intent": "answer",
                    "chain_of_thought": "不允许保存这个字段",
                },
            )

        with self.assertRaises(ValidationError):
            ToolBinding.model_validate(
                {
                    "server_id": "report",
                    "tool_name": "generate_document",
                    "schema_hash": TOOL_SCHEMA_HASH,
                    "unexpected": True,
                }
            )

        payload = draft_with_all_fields().model_dump(mode="json")
        payload["unknown_top_level"] = "forbidden"
        with self.assertRaises(ValidationError):
            GoalSpec.model_validate(payload)

    def test_finalize_accepts_plain_dict_and_creates_a_versioned_lineage(self) -> None:
        draft = draft_with_all_fields()
        confirmed = finalize(
            draft.model_dump(mode="json"),
            confirmation=confirmed_by_user(),
        )

        self.assertEqual(draft.version, 1)
        self.assertEqual(draft.status, "draft")
        self.assertEqual(confirmed.version, 2)
        self.assertEqual(confirmed.status, "confirmed")
        self.assertEqual(confirmed.confirmation.status, "user_confirmed")
        self.assertIsNotNone(confirmed.supersedes)
        self.assertEqual(confirmed.supersedes.goal_id, draft.goal_id)
        self.assertEqual(confirmed.supersedes.version, draft.version)
        self.assertEqual(confirmed.supersedes.spec_hash, draft.spec_hash)
        self.assertNotEqual(confirmed.spec_hash, draft.spec_hash)

    def test_finalize_refuses_to_guess_a_required_missing_input(self) -> None:
        draft = compile_draft(
            task_id="task_missing",
            objective={"statement": "查询天气", "intent": "weather_query"},
            inputs=[
                {
                    "key": "city",
                    "label": "城市或地区",
                    "required": True,
                    "status": "missing",
                    "ask": "请告诉我需要查询的城市或地区。",
                }
            ],
        )
        self.assertEqual(draft.status, "needs_input")
        self.assertEqual([item.key for item in draft.missing_required_inputs], ["city"])

        with self.assertRaisesRegex(ValueError, "missing inputs: city"):
            finalize(draft)

    def test_steering_revision_invalidates_stale_bindings_and_acceptance(self) -> None:
        confirmed = finalize(draft_with_all_fields(), confirmation=confirmed_by_user())
        revised = revise_for_steering(
            confirmed.model_dump(mode="json"),
            {
                "objective": {
                    "statement": "把会议纪要改为 Markdown 文件",
                    "intent": "create_meeting_minutes_markdown",
                    "constraints": ["继续只使用原附件"],
                    "provenance": [
                        {
                            "source_type": "user_message",
                            "source_id": "event_steering_2",
                        }
                    ],
                },
                "deliverables": [
                    {
                        "id": "minutes_md",
                        "kind": "artifact",
                        "format": "md",
                        "filename": "会议纪要.md",
                        "download_required": True,
                        "source_input_keys": ["source_notes"],
                    }
                ],
            },
        )

        self.assertEqual(revised.version, confirmed.version + 1)
        self.assertEqual(revised.status, "draft")
        self.assertEqual(revised.confirmation.status, "unresolved")
        self.assertEqual(revised.capability_bindings.skills, ())
        self.assertEqual(revised.capability_bindings.tools, ())
        self.assertEqual(revised.deliverables[0].format, "md")
        self.assertEqual(
            {item.id for item in revised.acceptance},
            {"goal_semantics", "response_present", "format_minutes_md", "download_minutes_md"},
        )
        self.assertEqual(revised.supersedes.spec_hash, confirmed.spec_hash)
        self.assertEqual(confirmed.deliverables[0].format, "docx")

    def test_steering_can_supply_new_exact_bindings_explicitly(self) -> None:
        confirmed = finalize(draft_with_all_fields(), confirmation=confirmed_by_user())
        revised = revise_for_steering(
            confirmed,
            {
                "objective": {
                    "statement": "生成修订后的 Word 纪要",
                    "intent": "revise_meeting_minutes",
                },
                "capability_bindings": bindings(REVISED_TOOL_SCHEMA_HASH),
            },
        )

        self.assertEqual(
            revised.capability_bindings.tools[0].schema_hash,
            REVISED_TOOL_SCHEMA_HASH,
        )

    def test_tampered_serialization_fails_hash_verification(self) -> None:
        payload = draft_with_all_fields().model_dump(mode="json")
        payload["objective"]["statement"] = "被篡改的目标"

        with self.assertRaisesRegex(ValidationError, "hash does not match"):
            GoalSpec.model_validate(payload)

    def test_public_summary_never_exposes_input_values_or_provenance_excerpts(self) -> None:
        spec = draft_with_all_fields()
        summary = public_goal_summary(spec.model_dump(mode="json"))
        encoded = json.dumps(summary, ensure_ascii=False)

        self.assertEqual(summary, spec.public_summary())
        self.assertIn("根据上传的会议记录生成 Word 纪要", encoded)
        self.assertNotIn("内部解析记录", encoded)
        self.assertNotIn("敏感附件摘录", encoded)
        self.assertNotIn("attachment_id", encoded)
        self.assertNotIn(SKILL_HASH, encoded)
        self.assertNotIn(TOOL_SCHEMA_HASH, encoded)
        self.assertNotIn("chain_of_thought", encoded)

    def test_duplicate_exact_tool_bindings_are_rejected(self) -> None:
        duplicate = bindings()
        duplicate["tools"] = [*duplicate["tools"], dict(duplicate["tools"][0])]
        with self.assertRaisesRegex(ValidationError, "duplicate tool binding"):
            CapabilityBindings.model_validate(duplicate)


if __name__ == "__main__":
    unittest.main()
