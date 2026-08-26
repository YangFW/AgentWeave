from __future__ import annotations

import unittest

from app.services.model_semantic_judge import ModelSemanticJudge
from app.services.verification_service import VerificationService


class _Gateway:
    def __init__(self, response: str) -> None:
        self.response = response
        self.calls: list[dict] = []

    async def summarize(self, prompt: str, context: dict, model_config_id: str) -> str:
        self.calls.append(
            {"prompt": prompt, "context": context, "model_config_id": model_config_id}
        )
        return self.response


class ModelSemanticJudgeTests(unittest.IsolatedAsyncioTestCase):
    async def test_non_empty_but_off_topic_candidate_is_rejected(self) -> None:
        gateway = _Gateway(
            '{"status":"failed","public_reason":"回答讨论天气，未完成财务报告目标。",'
            '"repair_instructions":["围绕财务报告目标重新生成结果。"]}'
        )
        service = VerificationService(ModelSemanticJudge(gateway, "judge-model"))
        report = await service.verify(
            {"answer": "今天上海天气晴朗。", "artifacts": []},
            {"objective": "分析本季度财务数据并给出结论"},
            mode="semantic_required",
        )
        self.assertFalse(report.passed)
        self.assertEqual(report.semantic.status, "failed")
        self.assertEqual(gateway.calls[0]["model_config_id"], "judge-model")
        self.assertIn("非空但跑题", gateway.calls[0]["prompt"])

    async def test_passed_review_has_full_semantic_coverage(self) -> None:
        gateway = _Gateway(
            '{"status":"passed","public_reason":"候选结果与目标一致。",'
            '"repair_instructions":[]}'
        )
        service = VerificationService(ModelSemanticJudge(gateway, "judge-model"))
        report = await service.verify(
            {"answer": "本季度收入增长 12%，主要来自续费提升。", "artifacts": []},
            {"objective": "分析本季度财务数据并给出结论"},
            mode="semantic_required",
        )
        self.assertTrue(report.passed)
        self.assertTrue(report.semantic_verified)
        self.assertEqual(report.coverage, "rules_and_semantic")

    async def test_extra_reasoning_field_is_not_accepted_or_exposed(self) -> None:
        gateway = _Gateway(
            '{"status":"passed","public_reason":"一致。","repair_instructions":[],'
            '"analysis":"hidden chain"}'
        )
        service = VerificationService(ModelSemanticJudge(gateway, "judge-model"))
        report = await service.verify(
            {"answer": "候选结果", "artifacts": []},
            {"objective": "生成候选结果"},
            mode="semantic_required",
        )
        self.assertFalse(report.passed)
        self.assertEqual(report.semantic.status, "error")
        self.assertNotIn("hidden chain", report.model_dump_json())


if __name__ == "__main__":
    unittest.main()
