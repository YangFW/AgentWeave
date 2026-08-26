from __future__ import annotations

import unittest
from typing import Any

from pydantic import ValidationError

from app.services.verification_service import (
    CandidateOutput,
    EvidenceBundle,
    RuleResult,
    SemanticResult,
    VerificationReport,
    VerificationService,
)


class RecordingJudge:
    def __init__(self, result: dict[str, Any] | None = None) -> None:
        self.result = result or {
            "status": "passed",
            "public_reason": "最终结果与目标及证据一致。",
            "repair_instructions": [],
        }
        self.calls = 0
        self.last_candidate: CandidateOutput | None = None
        self.last_evidence: EvidenceBundle | None = None
        self.last_rules: tuple[RuleResult, ...] = ()

    async def evaluate(
        self,
        *,
        candidate: CandidateOutput,
        evidence: EvidenceBundle,
        rule_results: tuple[RuleResult, ...],
    ) -> dict[str, Any]:
        self.calls += 1
        self.last_candidate = candidate
        self.last_evidence = evidence
        self.last_rules = rule_results
        return self.result


class ExplodingJudge:
    def __init__(self) -> None:
        self.calls = 0

    async def evaluate(self, **_: Any) -> SemanticResult:
        self.calls += 1
        raise RuntimeError(
            "SECRET_TOKEN=do-not-leak; prompt=hidden; chain_of_thought=private"
        )


def good_candidate() -> CandidateOutput:
    return CandidateOutput(
        answer="结论：宁波明天天气为多云，报告已经生成。",
        artifacts=[
            {
                "id": "art_weather",
                "name": "宁波天气.docx",
                "kind": "word",
                "download_url": "/api/artifacts/art_weather/download",
                "content_text": "宁波天气报告\n日期：明天\n天气：多云\n建议：携带外套。",
            }
        ],
    )


def good_evidence() -> EvidenceBundle:
    return EvidenceBundle(
        objective="查询宁波明天天气，并生成可以下载的 Word 报告。",
        expected_format="docx",
        requires_download=True,
        must_include=["宁波", "多云", "建议"],
        must_not_include=["上海天气", "内部思考"],
        required_tool_facts={
            "weather.forecast.city": "宁波",
            "weather.forecast.day": "tomorrow",
            "weather.forecast.temperature": {"low": 18, "high": 25},
        },
        tool_facts={
            "weather.forecast.city": "宁波",
            "weather.forecast.day": "tomorrow",
            "weather.forecast.temperature": {"high": 25, "low": 18},
            "weather.forecast.provider": "fixture",
        },
        goal_parameters={"city": "宁波", "day": "tomorrow", "format": "docx"},
        goal_parameter_evidence={
            "city": "宁波",
            "day": "tomorrow",
            "format": "docx",
        },
    )


class StrictSchemaTests(unittest.TestCase):
    def test_named_public_models_forbid_extra_fields(self) -> None:
        valid_rule = RuleResult(
            id="candidate.non_empty",
            title="最终交付不为空",
            status="passed",
            public_reason="已生成非空交付。",
        )
        valid_semantic = SemanticResult(
            status="skipped", public_reason="当前未执行语义复核。"
        )
        cases = [
            (
                CandidateOutput,
                {"answer": "结果", "artifacts": [], "reasoning": "private"},
            ),
            (
                EvidenceBundle,
                {"objective": "目标", "prompt": "private"},
            ),
            (
                RuleResult,
                {
                    **valid_rule.model_dump(),
                    "chain_of_thought": "private",
                },
            ),
            (
                SemanticResult,
                {**valid_semantic.model_dump(), "analysis": "private"},
            ),
            (
                VerificationReport,
                {
                    "mode": "rules_only",
                    "verdict": "rules_passed",
                    "passed": True,
                    "coverage": "rules_only",
                    "semantic_verified": False,
                    "public_reason": "规则验收已通过。",
                    "rules": [valid_rule.model_dump()],
                    "semantic": valid_semantic.model_dump(),
                    "repair_instructions": [],
                    "raw_prompt": "private",
                },
            ),
        ]

        for model, payload in cases:
            with self.subTest(model=model.__name__), self.assertRaises(
                ValidationError
            ):
                model.model_validate(payload)

    def test_report_cannot_label_skipped_semantics_as_fully_passed(self) -> None:
        with self.assertRaises(ValidationError):
            VerificationReport(
                mode="semantic_optional",
                verdict="passed",
                passed=True,
                coverage="rules_and_semantic",
                semantic_verified=True,
                public_reason="错误地声称语义复核通过。",
                rules=[
                    RuleResult(
                        id="candidate.non_empty",
                        title="最终交付不为空",
                        status="passed",
                        public_reason="已有结果。",
                    )
                ],
                semantic=SemanticResult(
                    status="skipped", public_reason="未执行语义复核。"
                ),
            )

    def test_report_rejects_verdicts_outside_service_state_machine(self) -> None:
        rule = {
            "id": "candidate.non_empty",
            "title": "最终交付不为空",
            "status": "passed",
            "public_reason": "已有结果。",
            "repair_instruction": None,
        }
        invalid_states = [
            {
                "name": "optional semantic failure cannot pass on rules",
                "mode": "semantic_optional",
                "verdict": "rules_passed",
                "passed": True,
                "coverage": "rules_and_semantic",
                "semantic_attempted": True,
                "semantic_verified": True,
                "semantic_status": "failed",
            },
            {
                "name": "rules-only cannot report a semantic pass",
                "mode": "rules_only",
                "verdict": "passed",
                "passed": True,
                "coverage": "rules_and_semantic",
                "semantic_attempted": True,
                "semantic_verified": True,
                "semantic_status": "passed",
            },
            {
                "name": "rules-only cannot report a semantic failure",
                "mode": "rules_only",
                "verdict": "failed",
                "passed": False,
                "coverage": "rules_and_semantic",
                "semantic_attempted": True,
                "semantic_verified": True,
                "semantic_status": "failed",
            },
            {
                "name": "rules-only cannot report a semantic error",
                "mode": "rules_only",
                "verdict": "rules_passed",
                "passed": True,
                "coverage": "rules_only",
                "semantic_attempted": True,
                "semantic_verified": False,
                "semantic_status": "error",
            },
            {
                "name": "required skipped semantics cannot pass on rules",
                "mode": "semantic_required",
                "verdict": "rules_passed",
                "passed": True,
                "coverage": "rules_only",
                "semantic_attempted": False,
                "semantic_verified": False,
                "semantic_status": "skipped",
            },
            {
                "name": "required semantic error cannot pass on rules",
                "mode": "semantic_required",
                "verdict": "rules_passed",
                "passed": True,
                "coverage": "rules_only",
                "semantic_attempted": True,
                "semantic_verified": False,
                "semantic_status": "error",
            },
        ]

        for state in invalid_states:
            with self.subTest(state=state["name"]), self.assertRaises(
                ValidationError
            ):
                VerificationReport.model_validate(
                    {
                        "mode": state["mode"],
                        "verdict": state["verdict"],
                        "passed": state["passed"],
                        "coverage": state["coverage"],
                        "semantic_attempted": state["semantic_attempted"],
                        "semantic_verified": state["semantic_verified"],
                        "public_reason": "伪造的验收状态。",
                        "rules": [rule],
                        "semantic": {
                            "status": state["semantic_status"],
                            "public_reason": "伪造的语义状态。",
                            "repair_instructions": [],
                        },
                        "repair_instructions": [],
                    }
                )

    def test_semantic_failure_is_valid_only_as_a_failed_report(self) -> None:
        report = VerificationReport.model_validate(
            {
                "mode": "semantic_optional",
                "verdict": "failed",
                "passed": False,
                "coverage": "rules_and_semantic",
                "semantic_attempted": True,
                "semantic_verified": True,
                "public_reason": "规则通过，但语义验收失败。",
                "rules": [
                    {
                        "id": "candidate.non_empty",
                        "title": "最终交付不为空",
                        "status": "passed",
                        "public_reason": "已有结果。",
                        "repair_instruction": None,
                    }
                ],
                "semantic": {
                    "status": "failed",
                    "public_reason": "结果未满足目标。",
                    "repair_instructions": ["重新生成与目标一致的结果。"],
                },
                "repair_instructions": ["重新生成与目标一致的结果。"],
            }
        )

        self.assertFalse(report.passed)
        self.assertEqual(report.verdict, "failed")


class VerificationServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_rules_only_checks_every_required_evidence_offline(self) -> None:
        judge = ExplodingJudge()
        report = await VerificationService(judge).verify(
            good_candidate(), good_evidence(), mode="rules_only"
        )

        self.assertTrue(report.passed)
        self.assertEqual(report.verdict, "rules_passed")
        self.assertEqual(report.coverage, "rules_only")
        self.assertFalse(report.semantic_verified)
        self.assertEqual(report.semantic.status, "skipped")
        self.assertEqual(judge.calls, 0)
        self.assertEqual(
            {item.id for item in report.rules},
            {
                "candidate.non_empty",
                "delivery.format",
                "delivery.download",
                "content.must_include",
                "content.must_not_include",
                "evidence.tool_facts",
                "evidence.goal_parameters",
            },
        )
        self.assertTrue(all(item.status == "passed" for item in report.rules))

    async def test_deterministic_failures_are_complete_and_repairable(self) -> None:
        candidate = CandidateOutput(
            answer="上海天气；只写了宁波。",
            artifacts=[
                {
                    "name": "错误报告.pdf",
                    "kind": "pdf",
                    "download_url": "",
                    "content_text": "天气：小雨",
                }
            ],
        )
        evidence = good_evidence().model_copy(
            update={
                "tool_facts": {
                    "weather.forecast.city": "宁波",
                    "weather.forecast.day": "today",
                    "weather.forecast.temperature": {"low": 18, "high": 25},
                },
                # JSON exactness must not treat True as integer 1.
                "goal_parameters": {"city": "宁波", "confirmed": True},
                "goal_parameter_evidence": {"city": "宁波", "confirmed": 1},
            }
        )
        judge = RecordingJudge()

        report = await VerificationService(judge).verify(
            candidate, evidence, mode="semantic_required"
        )

        self.assertFalse(report.passed)
        self.assertEqual(report.verdict, "failed")
        self.assertEqual(report.semantic.status, "skipped")
        self.assertEqual(judge.calls, 0, "rules must fail before any semantic call")
        failed = {item.id: item for item in report.rules if item.status == "failed"}
        self.assertEqual(
            set(failed),
            {
                "delivery.format",
                "delivery.download",
                "content.must_include",
                "content.must_not_include",
                "evidence.tool_facts",
                "evidence.goal_parameters",
            },
        )
        self.assertIn("多云", failed["content.must_include"].public_reason)
        self.assertIn("建议", failed["content.must_include"].public_reason)
        self.assertIn("上海天气", failed["content.must_not_include"].public_reason)
        self.assertIn("weather.forecast.day", failed["evidence.tool_facts"].public_reason)
        self.assertIn("confirmed", failed["evidence.goal_parameters"].public_reason)
        self.assertEqual(len(report.repair_instructions), len(failed))

    async def test_empty_final_answer_fails_the_non_empty_rule(self) -> None:
        report = await VerificationService().verify(
            CandidateOutput(answer="   ", artifacts=[]),
            EvidenceBundle(objective="回答问题"),
        )

        self.assertFalse(report.passed)
        non_empty = next(item for item in report.rules if item.id == "candidate.non_empty")
        self.assertEqual(non_empty.status, "failed")
        self.assertTrue(non_empty.repair_instruction)

    async def test_semantic_optional_without_judge_is_honest_rules_coverage(self) -> None:
        report = await VerificationService().verify(
            good_candidate(), good_evidence(), mode="semantic_optional"
        )

        self.assertTrue(report.passed)
        self.assertEqual(report.verdict, "rules_passed")
        self.assertFalse(report.semantic_verified)
        self.assertEqual(report.semantic.status, "skipped")
        self.assertEqual(report.coverage, "rules_only")
        self.assertIn("仅覆盖规则验收", report.public_reason)

    async def test_semantic_required_without_judge_fails_closed(self) -> None:
        report = await VerificationService().verify(
            good_candidate(), good_evidence(), mode="semantic_required"
        )

        self.assertFalse(report.passed)
        self.assertEqual(report.verdict, "failed")
        self.assertEqual(report.semantic.status, "skipped")
        self.assertFalse(report.semantic_verified)
        self.assertTrue(report.repair_instructions)

    async def test_injected_async_judge_can_complete_semantic_verification(self) -> None:
        judge = RecordingJudge()
        candidate = good_candidate()
        evidence = good_evidence()

        report = await VerificationService(judge).verify(
            candidate, evidence, mode="semantic_required"
        )

        self.assertTrue(report.passed)
        self.assertEqual(report.verdict, "passed")
        self.assertEqual(report.coverage, "rules_and_semantic")
        self.assertTrue(report.semantic_verified)
        self.assertEqual(report.semantic.status, "passed")
        self.assertEqual(judge.calls, 1)
        self.assertEqual(judge.last_candidate, candidate)
        self.assertEqual(judge.last_evidence, evidence)
        self.assertTrue(judge.last_rules)

    async def test_semantic_failure_returns_only_public_repair_instructions(self) -> None:
        judge = RecordingJudge(
            {
                "status": "failed",
                "public_reason": "结论没有直接回应用户要求的风险建议。",
                "repair_instructions": ["补充明确的风险结论和可执行建议。"],
            }
        )

        report = await VerificationService(judge).verify(
            good_candidate(), good_evidence(), mode="semantic_optional"
        )

        self.assertFalse(report.passed)
        self.assertEqual(report.semantic.status, "failed")
        self.assertTrue(report.semantic_attempted)
        self.assertTrue(report.semantic_verified)
        self.assertEqual(report.coverage, "rules_and_semantic")
        self.assertEqual(
            report.repair_instructions,
            ["补充明确的风险结论和可执行建议。"],
        )
        serialised = report.model_dump_json()
        self.assertNotIn("prompt", serialised.lower())
        self.assertNotIn("chain_of_thought", serialised.lower())

    async def test_judge_exception_never_leaks_provider_diagnostics(self) -> None:
        judge = ExplodingJudge()

        optional = await VerificationService(judge).verify(
            good_candidate(), good_evidence(), mode="semantic_optional"
        )
        required = await VerificationService(judge).verify(
            good_candidate(), good_evidence(), mode="semantic_required"
        )

        self.assertTrue(optional.passed)
        self.assertEqual(optional.verdict, "rules_passed")
        self.assertEqual(optional.semantic.status, "error")
        self.assertFalse(optional.semantic_verified)
        self.assertFalse(required.passed)
        self.assertEqual(required.semantic.status, "error")
        combined = optional.model_dump_json() + required.model_dump_json()
        for secret in ("SECRET_TOKEN", "do-not-leak", "hidden", "private"):
            self.assertNotIn(secret, combined)

    async def test_unexpected_semantic_fields_are_rejected_without_leaking_raw_output(self) -> None:
        judge = RecordingJudge(
            {
                "status": "passed",
                "public_reason": "看起来完成了目标。",
                "repair_instructions": [],
                "analysis": "不应公开的模型推理过程",
            }
        )

        report = await VerificationService(judge).verify(
            good_candidate(), good_evidence(), mode="semantic_required"
        )

        self.assertFalse(report.passed)
        self.assertEqual(report.semantic.status, "error")
        self.assertNotIn("不应公开", report.model_dump_json())

    async def test_artifact_sections_cannot_be_satisfied_by_chat_answer(self) -> None:
        candidate = {
            "answer": "聊天回答包含唯一章节标记：风险清单。",
            "artifacts": [
                {
                    "id": "art_word",
                    "deliverable_id": "word_report",
                    "task_id": "task-1",
                    "run_id": "run-1",
                    "name": "报告.docx",
                    "kind": "docx",
                    "download_url": "/api/artifacts/art_word/download",
                    "content_text": "这里是文件正文，但没有要求的章节。",
                    "size_bytes": 1200,
                    "sha256": "a" * 64,
                    "exists": True,
                    "readable": True,
                    "download_ready": True,
                }
            ],
        }
        evidence = {
            "objective": "生成包含风险清单章节的 Word 报告",
            "task_id": "task-1",
            "run_id": "run-1",
            "deliverables": [
                {
                    "deliverable_id": "answer",
                    "kind": "answer",
                    "format": "text",
                },
                {
                    "deliverable_id": "word_report",
                    "kind": "artifact",
                    "format": "docx",
                    "filename": "报告.docx",
                    "download_required": True,
                    "required_sections": ["风险清单"],
                },
            ],
        }

        report = await VerificationService().verify(candidate, evidence)

        self.assertFalse(report.passed)
        failed = {item.id for item in report.rules if item.status == "failed"}
        self.assertEqual(failed, {"deliverable.word_report.sections"})

    async def test_each_artifact_deliverable_requires_its_own_verified_file(self) -> None:
        candidate = {
            "answer": "两个文件已准备。",
            "artifacts": [
                {
                    "id": "art_word",
                    "deliverable_id": "word_report",
                    "task_id": "task-2",
                    "run_id": "run-2",
                    "name": "报告.docx",
                    "kind": "docx",
                    "download_url": "/api/artifacts/art_word/download",
                    "content_text": "结论",
                    "size_bytes": 100,
                    "sha256": "b" * 64,
                    "exists": True,
                    "readable": True,
                    "download_ready": True,
                }
            ],
        }
        evidence = {
            "objective": "同时生成 Word 和 Excel",
            "task_id": "task-2",
            "run_id": "run-2",
            "deliverables": [
                {
                    "deliverable_id": "word_report",
                    "kind": "artifact",
                    "format": "docx",
                    "download_required": True,
                },
                {
                    "deliverable_id": "excel_data",
                    "kind": "artifact",
                    "format": "xlsx",
                    "download_required": True,
                },
            ],
        }

        report = await VerificationService().verify(candidate, evidence)

        self.assertFalse(report.passed)
        failed = {item.id for item in report.rules if item.status == "failed"}
        self.assertIn("deliverable.excel_data.format", failed)
        self.assertIn("deliverable.excel_data.integrity", failed)
        self.assertIn("deliverable.excel_data.download", failed)

    async def test_url_without_file_integrity_does_not_pass_download_delivery(self) -> None:
        report = await VerificationService().verify(
            {
                "answer": "文件已生成。",
                "artifacts": [
                    {
                        "deliverable_id": "report",
                        "task_id": "task-3",
                        "run_id": "run-3",
                        "name": "report.pdf",
                        "kind": "pdf",
                        "download_url": "/api/artifacts/missing/download",
                        "download_ready": True,
                    }
                ],
            },
            {
                "objective": "生成 PDF",
                "task_id": "task-3",
                "run_id": "run-3",
                "deliverables": [
                    {
                        "deliverable_id": "report",
                        "kind": "artifact",
                        "format": "pdf",
                        "download_required": True,
                    }
                ],
            },
        )

        self.assertFalse(report.passed)
        failed = {item.id for item in report.rules if item.status == "failed"}
        self.assertIn("deliverable.report.integrity", failed)
        self.assertIn("deliverable.report.download", failed)


if __name__ == "__main__":
    unittest.main()
