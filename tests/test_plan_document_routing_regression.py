"""Regression checks for document-format routing discovered by live testing."""

from __future__ import annotations

import unittest

from app.services.agent_runtime import AgentRuntime


class DocumentRoutingRegressionTests(unittest.TestCase):
    def setUp(self) -> None:
        # The format detector is deliberately pure; bypass runtime wiring so
        # this regression test does not require a database or a live service.
        self.runtime = AgentRuntime.__new__(AgentRuntime)

    def test_markdown_after_write_verb_is_a_downloadable_deliverable(self) -> None:
        self.assertEqual(
            self.runtime._requested_document_formats(
                "帮我写个 Transformer 的入门学习文档，Markdown 格式给我。"
            ),
            ["md"],
        )

    def test_markdown_after_output_verb_survives_model_intent_rewrite(self) -> None:
        self.assertEqual(
            self.runtime._requested_document_formats(
                "撰写一份 Transformer 的入门学习文档，并以 Markdown 格式输出。"
            ),
            ["md"],
        )

    def test_multi_format_request_keeps_user_order(self) -> None:
        self.assertEqual(
            self.runtime._requested_document_formats(
                "帮我写一份旅行计划，并生成一个 PPT 和 Word 文档。"
            ),
            ["pptx", "docx"],
        )

    def test_report_tool_format_aliases_are_canonicalised(self) -> None:
        self.assertEqual(
            self.runtime._canonical_document_format("Markdown"),
            "md",
        )
        self.assertEqual(
            self.runtime._normalise_tool_arguments(
                "report", "generate_document", {"format": "markdown", "title": "T"}
            ),
            {"format": "md", "title": "T"},
        )
        self.assertEqual(
            self.runtime._normalise_tool_arguments(
                "report", "generate_document", {"format": "powerpoint"}
            )["format"],
            "pptx",
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
