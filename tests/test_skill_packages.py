from __future__ import annotations

import tempfile
import unittest
import asyncio
import zipfile
from io import BytesIO
from pathlib import Path
from starlette.datastructures import UploadFile

from app import db
from app.main import export_skill_package, upload_skill_file
from app.services.skill_registry import SkillRegistry


class SkillPackageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.original_db_path = db.DB_PATH
        self.temp_dir = tempfile.TemporaryDirectory()
        db.DB_PATH = Path(self.temp_dir.name) / "test.db"
        db.init_db()
        self.registry = SkillRegistry()

    def tearDown(self) -> None:
        db.DB_PATH = self.original_db_path
        self.temp_dir.cleanup()

    def test_install_preserves_scripts_references_and_rules(self) -> None:
        skill = self.registry.install_package({
            "demo/SKILL.md": b"---\nid: package_demo\nname: Package Demo\ndescription: demo\n---\n# Demo\n",
            "demo/scripts/run.py": b"print('ok')\n",
            "demo/references/rules.md": b"# Rules\nKeep parameters.\n",
        })
        self.assertEqual(skill["id"], "package_demo")
        self.assertEqual([item["path"] for item in self.registry.list_files("package_demo")], [
            "SKILL.md", "references/rules.md", "scripts/run.py",
        ])
        self.assertIn("Keep parameters", self.registry.get_file("package_demo", "references/rules.md")["content"])

    def test_skill_md_update_syncs_main_content(self) -> None:
        self.registry.install_content("---\nid: sync_demo\nname: Sync Demo\n---\n# Old\n")
        self.registry.put_file("sync_demo", "SKILL.md", b"---\nid: sync_demo\nname: Sync Demo\n---\n# New\n")
        self.assertIn("# New", self.registry.get_skill("sync_demo")["content"])

    def test_package_hash_covers_all_text_and_binary_files(self) -> None:
        self.registry.install_package({
            "demo/SKILL.md": b"---\nid: hash_demo\nname: Hash Demo\n---\n# Demo\n",
            "demo/references/rules.md": b"stable rule\n",
            "demo/assets/icon.bin": b"\x00\x01\x02",
        })
        first = self.registry.package_hash("hash_demo")
        self.assertEqual(first, self.registry.package_hash("hash_demo"))
        self.registry.put_file(
            "hash_demo", "assets/icon.bin", b"\x00\x01\x03", sync_skill=False
        )
        self.assertNotEqual(first, self.registry.package_hash("hash_demo"))

    def test_rejects_unsafe_package_path(self) -> None:
        with self.assertRaises(ValueError):
            self.registry.install_package({"../SKILL.md": b"bad"})

    def test_memory_query_scores_memory_skill_above_generic(self) -> None:
        self.registry.install_content(
            "---\nid: knowledge_graph_memory\nname: 知识图谱记忆 Skill\ndescription: 查询和维护实体偏好。\n---\n# 记忆\n"
        )
        scored = self.registry.score_skills("查询实体 regression_user 的平台偏好")
        self.assertEqual(scored[0]["skill"]["id"], "knowledge_graph_memory")

    def test_weak_bigram_overlap_cannot_grant_skill_tools(self) -> None:
        self.registry.install_content(
            "---\nid: accidental_tool_skill\nname: 外部工具 Skill\n"
            "description: 调用工具处理复杂工作。\n"
            "required_mcps: external-system\n---\n# 工具流程\n"
        )
        scored = self.registry.score_skills(
            "用两句话说明模型连接已经通过测试，不要调用天气工具，也不要生成文件。"
        )
        self.assertNotIn(
            "accidental_tool_skill", [item["skill"]["id"] for item in scored]
        )

    def test_specialty_skills_require_explicit_semantic_evidence(self) -> None:
        self.registry.install_content(
            "---\nid: mermaid_diagram\nname: Mermaid 图表设计 Skill\n"
            "description: 生成平台流程图、架构图和状态图。\n---\n# Mermaid\n"
        )
        self.registry.install_content(
            "---\nid: product_requirement_document\nname: 产品需求文档 PRD Skill\n"
            "description: 生成用户故事和验收标准。\n---\n# PRD\n"
        )

        prd_scores = self.registry.score_skills("请概括这套智能体平台的产品定位和目标")
        self.assertNotIn("mermaid_diagram", [item["skill"]["id"] for item in prd_scores])
        self.assertNotIn("product_requirement_document", [item["skill"]["id"] for item in prd_scores])

        diagram_scores = self.registry.score_skills("请用 Mermaid 流程图说明任务执行过程")
        self.assertIn("mermaid_diagram", [item["skill"]["id"] for item in diagram_scores])
        explicit_prd_scores = self.registry.score_skills("请写一份包含用户故事和验收标准的 PRD")
        self.assertIn("product_requirement_document", [item["skill"]["id"] for item in explicit_prd_scores])

    def test_research_and_context_skills_do_not_hijack_plain_answers(self) -> None:
        self.registry.install_content(
            "---\nid: research_report\nname: 联网研究报告 Skill\n"
            "description: 联网搜索并生成带来源的研究报告。\n"
            "required_mcps: web-search,report\n---\n# 联网研究报告\n"
        )
        self.registry.install_content(
            "---\nid: context_parameter_guard\nname: 多轮任务参数校验 Skill\n"
            "description: 补全多轮对话中的范围、阈值与输出格式。\n"
            "required_mcps: report,spreadsheet\n---\n# 多轮参数校验\n"
        )
        self.registry.install_content(
            "---\nid: data_analysis\nname: 表格数据分析 Skill\n"
            "description: 分析表格数据并输出统计结果。\n"
            "required_mcps: spreadsheet,report\n---\n# 数据分析\n"
        )
        self.registry.install_content(
            "---\nid: meeting_minutes\nname: 会议纪要与行动项 Skill\n"
            "description: 将会议内容整理为纪要和行动项。\n"
            "required_mcps: report\n---\n# 会议纪要\n"
        )

        plain = self.registry.score_skills(
            "请用两句话说明模型连接已经通过测试，不要调用天气工具，也不要生成文件。"
        )
        plain_ids = [item["skill"]["id"] for item in plain]
        self.assertNotIn("research_report", plain_ids)
        self.assertNotIn("context_parameter_guard", plain_ids)
        self.assertNotIn("data_analysis", plain_ids)
        self.assertNotIn("meeting_minutes", plain_ids)

        research = self.registry.score_skills("请联网搜索最新行业信息并生成带来源的研究报告")
        self.assertIn("research_report", [item["skill"]["id"] for item in research])
        follow_up = self.registry.score_skills("按刚才的范围把它导出为 Word")
        self.assertIn("context_parameter_guard", [item["skill"]["id"] for item in follow_up])
        data = self.registry.score_skills("分析这份 CSV 数据的趋势和关键指标")
        self.assertIn("data_analysis", [item["skill"]["id"] for item in data])
        minutes = self.registry.score_skills("把这段会议记录整理成会议纪要和行动项")
        self.assertIn("meeting_minutes", [item["skill"]["id"] for item in minutes])

    def test_legacy_skill_gets_inspectable_entry_file(self) -> None:
        now = db.utc_now()
        db.execute(
            "INSERT INTO skills(id, name, description, category, version, content, enabled, required_mcps, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("legacy_skill", "Legacy", "", "custom", "0.1.0", "# Legacy\n", 1, "[]", now, now),
        )
        self.assertEqual(self.registry.list_files("legacy_skill"), [])
        self.registry.load_builtin_skills()
        self.assertEqual([item["path"] for item in self.registry.list_files("legacy_skill")], ["SKILL.md"])

    def test_export_zip_contains_complete_skill_package(self) -> None:
        self.registry.install_package({
            "demo/SKILL.md": b"---\nid: export_demo\nname: Export Demo\n---\n# Demo\n",
            "demo/scripts/run.py": b"print('ok')\n",
            "demo/assets/icon.bin": b"\x00\x01\x02",
        })
        response = export_skill_package("export_demo")

        async def collect() -> bytes:
            chunks = []
            async for chunk in response.body_iterator:
                chunks.append(chunk if isinstance(chunk, bytes) else chunk.encode("utf-8"))
            return b"".join(chunks)

        archive_bytes = asyncio.run(collect())
        with zipfile.ZipFile(BytesIO(archive_bytes)) as archive:
            self.assertEqual(sorted(archive.namelist()), ["SKILL.md", "assets/icon.bin", "scripts/run.py"])
            self.assertEqual(archive.read("assets/icon.bin"), b"\x00\x01\x02")

    def test_single_file_upload_supports_binary_assets(self) -> None:
        self.registry.install_content("---\nid: upload_demo\nname: Upload Demo\n---\n# Demo\n")
        upload = UploadFile(filename="logo.bin", file=BytesIO(b"\x00\xff\x01"))
        saved = asyncio.run(upload_skill_file("upload_demo", "assets/logo.bin", upload))
        self.assertEqual(saved["path"], "assets/logo.bin")
        self.assertTrue(saved["is_binary"])
        self.assertEqual(saved["size"], 3)


if __name__ == "__main__":
    unittest.main()
