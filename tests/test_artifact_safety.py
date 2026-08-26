from __future__ import annotations

import asyncio
import csv
import hashlib
import os
import re
import tempfile
import threading
import time
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch
from xml.etree import ElementTree

from fastapi import HTTPException

from app import db
from app import main as main_app
from app.services.agent_runtime import create_task_record
from app.services import mcp_gateway as mcp_module
from app.services.mcp_gateway import McpGateway, ToolError
from app.services.task_state import TaskStateService


class ArtifactSafetyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.original_db_path = db.DB_PATH
        self.original_artifact_dir = mcp_module.ARTIFACT_DIR
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        db.DB_PATH = root / "artifacts.db"
        mcp_module.ARTIFACT_DIR = root / "artifacts"
        db.init_db()
        self.state = TaskStateService(db.get_conn)
        self.gateway = McpGateway()

    def tearDown(self) -> None:
        mcp_module.ARTIFACT_DIR = self.original_artifact_dir
        db.DB_PATH = self.original_db_path
        self.temp_dir.cleanup()

    def _task_and_run(self) -> tuple[dict, dict]:
        task = create_task_record(
            "生成安全产物",
            "general-agent",
            workspace="workspace-artifact-test",
            conversation_id="conv_artifact_safety",
        )
        return task, self.state.begin_run(task["id"])

    def test_same_filename_across_runs_is_immutable_and_versioned(self) -> None:
        task, first_run = self._task_and_run()
        first = self.gateway._generate_document(
            {
                "title": "第一版",
                "content": "这是第一轮生成的内容。",
                "format": "md",
                "filename": "same-name.md",
            },
            task_id=task["id"],
        )["artifact"]
        self.state.finish_run(first_run["id"], result={"ok": True})

        second_run = self.state.begin_run(task["id"])
        second = self.gateway._generate_document(
            {
                "title": "第二版",
                "content": "这是第二轮生成的不同内容。",
                "format": "md",
                "filename": "same-name.md",
            },
            task_id=task["id"],
        )["artifact"]

        first_row = db.query_one("SELECT * FROM artifacts WHERE id = ?", (first["id"],))
        second_row = db.query_one("SELECT * FROM artifacts WHERE id = ?", (second["id"],))
        self.assertIsNotNone(first_row)
        self.assertIsNotNone(second_row)
        first_path = Path(first_row["path"])
        second_path = Path(second_row["path"])

        self.assertNotEqual(first_path, second_path)
        self.assertTrue(first_path.exists())
        self.assertTrue(second_path.exists())
        self.assertIn("第一轮", first_path.read_text(encoding="utf-8"))
        self.assertIn("第二轮", second_path.read_text(encoding="utf-8"))
        self.assertEqual(first_row["run_id"], first_run["id"])
        self.assertEqual(second_row["run_id"], second_run["id"])
        self.assertIn(first_run["id"], Path(first_row["relative_path"]).parts)
        self.assertIn(second_run["id"], Path(second_row["relative_path"]).parts)
        self.assertEqual((first["version"], second["version"]), (1, 2))
        self.assertNotIn("path", first)
        self.assertNotIn("path", second)

    def test_registered_metadata_hash_size_and_public_shape(self) -> None:
        task, run = self._task_and_run()
        artifact = self.gateway._generate_markdown_report(
            {
                "summary": "哈希与大小验证",
                "rows": [{"item": "A", "value": 1}],
                "filename": "metadata.md",
            },
            task_id=task["id"],
        )["artifact"]
        row = db.query_one("SELECT * FROM artifacts WHERE id = ?", (artifact["id"],))
        self.assertIsNotNone(row)
        stored_path = Path(row["path"])
        content = stored_path.read_bytes()
        expected_hash = hashlib.sha256(content).hexdigest()

        self.assertEqual(row["run_id"], run["id"])
        self.assertEqual(row["workspace_id"], "workspace-artifact-test")
        self.assertEqual(row["mime_type"], "text/markdown")
        self.assertEqual(row["size"], len(content))
        self.assertEqual(row["sha256"], expected_hash)
        self.assertEqual(artifact["size"], len(content))
        self.assertEqual(artifact["sha256"], expected_hash)
        self.assertEqual(artifact["relative_path"], row["relative_path"])
        self.assertNotIn("path", artifact)
        self.assertFalse(Path(artifact["relative_path"]).is_absolute())

    def test_core_document_formats_remain_generatable_and_resolvable(self) -> None:
        task, _ = self._task_and_run()
        artifacts: list[dict] = []
        for fmt in ("docx", "pdf", "md", "html"):
            result = self.gateway._generate_document(
                {
                    "title": f"{fmt.upper()} 安全产物",
                    "content": "# 验证\n\n- 产物能够生成\n- 元数据能够登记",
                    "format": fmt,
                    "filename": f"format-test.{fmt}",
                },
                task_id=task["id"],
            )
            artifacts.append(result["artifact"])
        artifacts.append(
            self.gateway._create_excel(
                {"rows": [{"name": "测试", "value": 1}], "filename": "format-test.xlsx"},
                task_id=task["id"],
            )["artifact"]
        )
        artifacts.append(
            self.gateway._create_excel(
                {"rows": [{"name": "测试", "value": "含,逗号"}], "filename": "format-test.csv"},
                task_id=task["id"],
            )["artifact"]
        )

        self.assertEqual({item["kind"] for item in artifacts}, {"docx", "pdf", "markdown", "html", "xlsx", "csv"})
        expected_previews = {
            "docx": "document",
            "pdf": "pdf",
            "markdown": "markdown",
            "html": "html",
            "xlsx": "spreadsheet",
            "csv": "spreadsheet",
        }
        for artifact in artifacts:
            with self.subTest(kind=artifact["kind"]):
                self.assertNotIn("path", artifact)
                resolved = mcp_module.resolve_artifact_path(artifact["relative_path"])
                self.assertTrue(resolved.is_file())
                self.assertGreater(artifact["size"], 0)
                self.assertEqual(hashlib.sha256(resolved.read_bytes()).hexdigest(), artifact["sha256"])
                with self.assertRaises(HTTPException) as pending:
                    main_app.preview_artifact(artifact["id"])
                self.assertEqual(pending.exception.status_code, 409)
                db.execute(
                    """
                    UPDATE artifacts
                    SET delivery_status = 'published',
                        verification_id = 'test-verification',
                        published_at = ?
                    WHERE id = ?
                    """,
                    (db.utc_now(), artifact["id"]),
                )
                preview = main_app.preview_artifact(artifact["id"])
                self.assertEqual(preview["preview_kind"], expected_previews[artifact["kind"]])

        csv_artifact = next(item for item in artifacts if item["kind"] == "csv")
        csv_path = mcp_module.resolve_artifact_path(csv_artifact["relative_path"])
        with csv_path.open("r", encoding="utf-8-sig", newline="") as stream:
            rows = list(csv.DictReader(stream))
        self.assertEqual(rows, [{"name": "测试", "value": "含,逗号"}])
        self.assertEqual(csv_artifact["name"], "format-test.csv")
        self.assertEqual(csv_artifact["mime_type"], "text/csv")
        self.assertEqual(main_app.preview_artifact(csv_artifact["id"])["sheets"][0]["rows"][1], ["测试", "含,逗号"])

    def test_pptx_capability_reports_missing_optional_component(self) -> None:
        missing_message = "PowerPoint 生成组件尚未安装，请配置 APP_ARTIFACT_TOOL_ENTRYPOINT 后重试"
        # The local platform now defaults to the bundled Python generator.  To
        # exercise the optional external-generator failure boundary, pin this
        # test to the Artifact Tool mode explicitly.
        with patch.dict(os.environ, {"APP_PPTX_GENERATOR": "artifact_tool"}), patch.object(
            mcp_module.shutil, "which", return_value="/usr/bin/node"
        ), patch.object(
            mcp_module,
            "_artifact_tool_entrypoint",
            side_effect=ToolError(missing_message),
        ):
            status = mcp_module.presentation_generation_status()
            self.assertEqual(status, {"configured": False, "reason": missing_message})

            task, _ = self._task_and_run()
            with self.assertRaisesRegex(ToolError, "PowerPoint 生成组件尚未安装"):
                self.gateway._generate_document(
                    {
                        "title": "未配置组件",
                        "content": "明确报告可选组件未配置，而不是产生损坏文件。",
                        "format": "pptx",
                        "filename": "missing-component.pptx",
                    },
                    task_id=task["id"],
                )

        self.assertFalse(list(mcp_module.ARTIFACT_DIR.rglob("missing-component.pptx")))

    def test_pptx_uses_bounded_audience_facing_slides(self) -> None:
        status = mcp_module.presentation_generation_status()
        if not status.get("configured"):
            self.skipTest(f"可选 PPTX 组件未配置：{status.get('reason') or '未知原因'}")
        task, _ = self._task_and_run()
        legitimate_sections = "\n\n".join(
            f"## 第{index}部分\n\n- 这是面向受众的第{index}项结论"
            for index in range(1, 12)
        )
        result = self.gateway._generate_document(
            {
                "title": "平台能力评审",
                "content": (
                    "# 市场判断\n\n- 用户需要稳定、清晰、可下载的交付结果\n\n"
                    "- 推荐 Skill 后仍应原样保留受众文案\n\n"
                    "## 视觉元素建议\n\n- 这条制作说明不得出现在演示文稿中\n\n"
                    "## 执行策略\n\n- 先验证关键链路，再逐步扩大使用范围\n\n"
                    "- 制作建议：使用重复卡片铺满页面\n\n"
                    f"{legitimate_sections}\n\n"
                    "## 设计说明\n\n- 这条设计说明也不得进入可见页面"
                ),
                "format": "pptx",
                "filename": "audience-facing.pptx",
            },
            task_id=task["id"],
        )
        artifact = result["artifact"]
        generated = mcp_module.resolve_artifact_path(artifact["relative_path"])

        with zipfile.ZipFile(generated) as archive:
            slide_names = sorted(
                name
                for name in archive.namelist()
                if re.fullmatch(r"ppt/slides/slide\d+\.xml", name)
            )
            visible_text: list[str] = []
            for slide_name in slide_names:
                root = ElementTree.fromstring(archive.read(slide_name))
                visible_text.extend(
                    node.text or ""
                    for node in root.iter("{http://schemas.openxmlformats.org/drawingml/2006/main}t")
                )
            notes_xml = "\n".join(
                archive.read(name).decode("utf-8", errors="ignore")
                for name in archive.namelist()
                if name.startswith("ppt/notesSlides/") and name.endswith(".xml")
            )

        combined = "\n".join(visible_text)
        self.assertGreaterEqual(len(slide_names), 3)
        # The bundled generator paginates long input instead of applying the
        # old eight-content-page demo limit.  The safety ceiling is 40 content
        # pages plus a cover and (for multi-section input) a summary page.
        self.assertLessEqual(len(slide_names), 42)
        self.assertIn("平台能力评审", combined)
        self.assertIn("市场判断", combined)
        self.assertIn("执行策略", combined)
        self.assertIn("第11部分", combined)
        self.assertIn("推荐 Skill 后仍应原样保留受众文案", combined)
        self.assertNotIn("推荐\u00a0Skill\u00a0后", combined)
        self.assertNotIn("视觉元素建议", combined)
        self.assertNotIn("制作说明不得", combined)
        self.assertNotIn("制作建议", combined)
        self.assertNotIn("设计说明", combined)
        self.assertNotIn("[Sources]", notes_xml)
        self.assertNotIn("用户提供的内容", notes_xml)

    def test_pptx_rejects_content_that_cannot_fit_without_loss(self) -> None:
        status = mcp_module.presentation_generation_status()
        if not status.get("configured"):
            self.skipTest(f"可选 PPTX 组件未配置：{status.get('reason') or '未知原因'}")
        task, _ = self._task_and_run()
        content = "# 容量验证\n\n" + "\n".join(
            f"- 必须保留的完整要点 {index}"
            for index in range(1, 260)
        )

        with self.assertRaises(ToolError) as raised:
            self.gateway._generate_document(
                {
                    "title": "不可静默裁剪的演示文稿",
                    "content": content,
                    "format": "pptx",
                    "filename": "too-much-content.pptx",
                },
                task_id=task["id"],
            )

        self.assertIsInstance(raised.exception.__cause__, ValueError)
        self.assertIn("超过本地生成器的 40 页安全上限", str(raised.exception))
        self.assertFalse(list(mcp_module.ARTIFACT_DIR.rglob("too-much-content.pptx")))

    def test_safe_path_resolution_rejects_traversal_escape_and_missing_files(self) -> None:
        root = mcp_module.ARTIFACT_DIR
        safe_file = root / "task" / "run" / "artifact" / "safe.txt"
        safe_file.parent.mkdir(parents=True)
        safe_file.write_text("safe", encoding="utf-8")
        outside_file = Path(self.temp_dir.name) / "outside.txt"
        outside_file.write_text("outside", encoding="utf-8")

        resolved = mcp_module.resolve_artifact_path("task/run/artifact/safe.txt")
        self.assertEqual(resolved, safe_file.resolve())

        invalid_paths = [
            "../outside.txt",
            "task/run/artifact/../../../../outside.txt",
            str(outside_file.resolve()),
            "missing.txt",
            "C:/outside.txt",
        ]
        for value in invalid_paths:
            with self.subTest(path=value), self.assertRaises(ToolError):
                mcp_module.resolve_artifact_path(value)

        escape_link = root / "escape-link.txt"
        try:
            escape_link.symlink_to(outside_file)
        except OSError as exc:  # pragma: no cover - platforms without symlink support
            self.skipTest(f"symlinks unavailable: {exc}")
        with self.assertRaises(ToolError):
            mcp_module.resolve_artifact_path("escape-link.txt")

    def test_legacy_artifact_metadata_is_backfilled_without_trusting_outside_paths(self) -> None:
        task, run = self._task_and_run()
        legacy = mcp_module.ARTIFACT_DIR / task["id"] / "legacy.md"
        legacy.parent.mkdir(parents=True, exist_ok=True)
        legacy.write_text("# 旧产物\n\n可安全回填。", encoding="utf-8")
        outside = Path(self.temp_dir.name) / "outside.md"
        outside.write_text("outside", encoding="utf-8")
        now = db.utc_now()
        db.execute(
            "INSERT INTO artifacts(id, task_id, name, kind, path, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            ("art_legacy_safe", task["id"], "legacy.md", "markdown", str(legacy), now),
        )
        db.execute(
            "INSERT INTO artifacts(id, task_id, name, kind, path, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            ("art_legacy_outside", task["id"], "outside.md", "markdown", str(outside), now),
        )

        previous = os.environ.get("APP_ARTIFACT_DIR")
        os.environ["APP_ARTIFACT_DIR"] = str(mcp_module.ARTIFACT_DIR)
        conn = db.get_conn()
        try:
            db._backfill_legacy_artifact_metadata(conn)
            conn.commit()
        finally:
            conn.close()
            if previous is None:
                os.environ.pop("APP_ARTIFACT_DIR", None)
            else:
                os.environ["APP_ARTIFACT_DIR"] = previous

        safe = db.query_one("SELECT * FROM artifacts WHERE id = 'art_legacy_safe'")
        unsafe = db.query_one("SELECT * FROM artifacts WHERE id = 'art_legacy_outside'")
        self.assertEqual(safe["relative_path"], f"{task['id']}/legacy.md")
        self.assertEqual(safe["size"], legacy.stat().st_size)
        self.assertEqual(safe["sha256"], hashlib.sha256(legacy.read_bytes()).hexdigest())
        self.assertEqual(safe["mime_type"], "text/markdown")
        self.assertEqual(safe["workspace_id"], "workspace-artifact-test")
        self.assertEqual(safe["run_id"], run["id"])
        self.assertEqual(unsafe["relative_path"], "")
        self.assertEqual(unsafe["sha256"], "")


class ArtifactAsyncInvocationTests(unittest.IsolatedAsyncioTestCase):
    async def test_file_generators_run_off_the_event_loop_thread(self) -> None:
        gateway = McpGateway()
        main_thread = threading.get_ident()
        cases = [
            ("spreadsheet", "create_excel", "_create_excel"),
            ("report", "generate_markdown_report", "_generate_markdown_report"),
            ("report", "generate_document", "_generate_document"),
        ]
        for server_id, tool_name, method_name in cases:
            with self.subTest(tool=f"{server_id}.{tool_name}"):
                worker_threads: list[int] = []

                def blocking_generator(
                    arguments: dict,
                    task_id: str | None = None,
                    *,
                    tool_effect_id: str = "",
                ) -> dict:
                    worker_threads.append(threading.get_ident())
                    time.sleep(0.03)
                    return {
                        "arguments": arguments,
                        "task_id": task_id,
                        "tool_effect_id": tool_effect_id,
                    }

                with patch.object(gateway, method_name, side_effect=blocking_generator):
                    invocation = asyncio.create_task(
                        gateway._invoke_builtin(server_id, tool_name, {"value": 1}, task_id="task-thread-test")
                    )
                    await asyncio.sleep(0.005)
                    self.assertFalse(invocation.done())
                    result = await invocation

                self.assertEqual(result["task_id"], "task-thread-test")
                self.assertEqual(result["arguments"], {"value": 1})
                self.assertEqual(len(worker_threads), 1)
                self.assertNotEqual(worker_threads[0], main_thread)


if __name__ == "__main__":
    unittest.main()
