from __future__ import annotations

import unittest

from app.services.agent_runtime import AgentRuntime


class DocumentRoutingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.runtime = object.__new__(AgentRuntime)

    def test_format_preference_does_not_create_document(self) -> None:
        self.assertEqual(self.runtime._requested_document_format("默认输出格式改成 PDF"), "")
        self.assertFalse(self.runtime._wants_report_artifact("默认输出格式改成 PDF"))

    def test_explicit_pdf_request_creates_document(self) -> None:
        self.assertEqual(self.runtime._requested_document_format("请生成一份 PDF 文档"), "pdf")

    def test_explicit_word_request_creates_document(self) -> None:
        self.assertEqual(self.runtime._requested_document_format("把结果整理成 Word 文档给我下载"), "docx")

    def test_explicit_csv_request_creates_csv_artifact(self) -> None:
        self.assertEqual(self.runtime._requested_document_format("将结果导出为 CSV 文件"), "csv")
        self.assertEqual(self.runtime._requested_document_format("生成逗号分隔文件供我下载"), "csv")
        self.assertEqual(self.runtime._requested_document_format("把 Excel 转成 CSV 文件"), "csv")
        self.assertEqual(self.runtime._requested_document_format("把 CSV 转成 Excel 文件"), "xlsx")

    def test_markdown_text_output_does_not_create_download_artifact(self) -> None:
        self.assertFalse(self.runtime._wants_report_artifact("请使用 Markdown 输出这份行程"))


if __name__ == "__main__":
    unittest.main()
