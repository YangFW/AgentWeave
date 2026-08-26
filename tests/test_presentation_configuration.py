from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from zipfile import ZipFile

from fastapi import HTTPException

from app import main as main_app
from app.schemas import PresentationConfigureRequest
from app.services import mcp_gateway
from app.services.presentation_generator_python import generate_pptx


class PresentationConfigurationTests(unittest.TestCase):
    def test_native_python_generator_creates_valid_downloadable_pptx(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "travel.pptx"
            result = generate_pptx(
                "宁波旅行计划",
                "# 行程安排\n\n- 杭州出发\n- 宁波老外滩\n\n## 注意事项\n\n- 提前查看天气",
                output,
            )
            self.assertEqual(result["generator"], "python-pptx")
            self.assertGreater(output.stat().st_size, 0)
            with ZipFile(output) as archive:
                slides = [name for name in archive.namelist() if name.startswith("ppt/slides/slide") and name.endswith(".xml")]
            self.assertGreaterEqual(len(slides), 3)

    def test_native_python_generator_paginates_long_documents_without_truncating(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "long-travel-plan.pptx"
            content = "\n\n".join(
                f"## 第 {index} 节\n\n- 必须保留的旅行计划要点 {index}"
                for index in range(1, 26)
            )
            result = generate_pptx("长篇旅行计划", content, output)
            self.assertEqual(result["generator"], "python-pptx")
            self.assertGreaterEqual(int(result["slideCount"]), 27)
            self.assertLessEqual(int(result["slideCount"]), 42)
            with ZipFile(output) as archive:
                visible = "\n".join(
                    archive.read(name).decode("utf-8", errors="ignore")
                    for name in archive.namelist()
                    if name.startswith("ppt/slides/slide") and name.endswith(".xml")
                )
            self.assertIn("必须保留的旅行计划要点 1", visible)
            self.assertIn("必须保留的旅行计划要点 25", visible)

    def test_configuration_requires_explicit_confirmation_and_persists_local_env(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            env_file = Path(directory) / ".env.local"
            with patch.object(main_app, "_presentation_env_file", return_value=env_file), patch.dict(
                os.environ, {"APP_PPTX_GENERATOR": "artifact_tool"}, clear=False
            ):
                with self.assertRaises(HTTPException) as raised:
                    main_app.configure_presentation(
                        PresentationConfigureRequest(mode="python", confirmed=False)
                    )
                self.assertEqual(raised.exception.status_code, 400)
                result = main_app.configure_presentation(
                    PresentationConfigureRequest(mode="python", confirmed=True)
                )
                self.assertTrue(result["ok"])
                self.assertIn('APP_PPTX_GENERATOR="python"', env_file.read_text(encoding="utf-8"))

    def test_configuration_status_explains_private_artifact_tool_boundary(self) -> None:
        with patch.dict(os.environ, {"APP_PPTX_GENERATOR": "artifact_tool", "APP_ARTIFACT_TOOL_ENTRYPOINT": ""}, clear=False):
            status = mcp_gateway.presentation_configuration_status()
        self.assertIn("native_python_available", status)
        self.assertIn("native_python", status["instructions"])
        self.assertIn("artifact_tool", status["instructions"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
