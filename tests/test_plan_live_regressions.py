"""Regression checks for failures found while executing ``docs/TEST_PLAN.md``.

These are intentionally additive checks.  The plan and immutable sample
documents remain the source of the scenarios; this file only protects the two
public-contract regressions found by the live acceptance run.
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

from app import db, main as main_app
from app.services.agent_runtime import AgentRuntime


class PublicFailureContractTests(unittest.TestCase):
    def test_missing_pptx_component_is_actionable_without_raw_details(self) -> None:
        event = main_app._public_event(
            {
                "id": 1,
                "task_id": "task_regression",
                "type": "error",
                "title": "任务失败",
                "content": "工具调用 report.generate_document 失败：PowerPoint 生成组件尚未安装，请配置 APP_ARTIFACT_TOOL_ENTRYPOINT 后重试",
                "data_json": db.json_dumps({"error_type": "ToolError"}),
            }
        )

        self.assertEqual(event["data"]["error_code"], "artifact_pptx_unavailable")
        self.assertEqual(event["title"], "PowerPoint 生成功能不可用")
        self.assertIn("PowerPoint", event["content"])
        self.assertNotIn("APP_ARTIFACT_TOOL_ENTRYPOINT", event["content"])
        self.assertNotIn("Traceback", event["content"])


class AttachmentContinuityContractTests(unittest.TestCase):
    def test_attachment_title_is_preferred_over_sampled_body_lines(self) -> None:
        runtime = object.__new__(AgentRuntime)
        context = "--- codex-alternative-prd.md ---\n# AgentNexus 软件平替项目需求文档\n## 1. 学习目标"
        self.assertEqual(
            runtime._source_marker_from_attachment_context(context), "AgentNexus"
        )

    def test_final_summary_can_show_verified_attachment_topic(self) -> None:
        runtime = object.__new__(AgentRuntime)
        plan = {"attachment_requirements": ["# AgentNexus 软件平替项目需求文档"]}
        self.assertEqual(runtime._attachment_source_marker(plan), "AgentNexus")
        with patch.object(
            runtime,
            "_artifact_content_check",
            return_value=(True, "已检查", "AgentNexus 软件平替项目需求"),
        ):
            self.assertTrue(
                runtime._artifact_contains_source_marker(
                    [{"kind": "md", "id": "artifact-regression"}],
                    "AgentNexus",
                )
            )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
