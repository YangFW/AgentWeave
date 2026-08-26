from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app import db
from app.seed import seed_agents


class SeedAgentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.original_db_path = db.DB_PATH
        self.temp_dir = tempfile.TemporaryDirectory()
        db.DB_PATH = Path(self.temp_dir.name) / "seed-agents.db"
        db.init_db()

    def tearDown(self) -> None:
        db.DB_PATH = self.original_db_path
        self.temp_dir.cleanup()

    def test_new_install_creates_only_the_general_assistant(self) -> None:
        seed_agents()

        agents = db.query_all("SELECT * FROM agents ORDER BY id")
        self.assertEqual([item["id"] for item in agents], ["general-agent"])
        general = agents[0]
        self.assertEqual(general["name"], "智枢助手")
        self.assertIn("清晰、可核验的结果", general["system_prompt"])
        self.assertEqual(
            db.json_loads(general["skills_json"], []),
            ["general_task", "report_generation"],
        )
        self.assertEqual(
            db.json_loads(general["mcp_servers_json"], []),
            ["spreadsheet", "report", "web-search", "weather"],
        )

    def test_existing_agent_customizations_are_not_overwritten(self) -> None:
        seed_agents()
        db.execute(
            """
            UPDATE agents
            SET name = ?, description = ?, system_prompt = ?, skills_json = ?
            WHERE id = ?
            """,
            (
                "我的工作助手",
                "只处理团队内部任务。",
                "只使用我明确批准的工具。",
                db.json_dumps(["my_skill"]),
                "general-agent",
            ),
        )

        seed_agents()

        general = db.query_one("SELECT * FROM agents WHERE id = ?", ("general-agent",))
        self.assertEqual(general["name"], "我的工作助手")
        self.assertEqual(general["description"], "只处理团队内部任务。")
        self.assertEqual(general["system_prompt"], "只使用我明确批准的工具。")
        self.assertEqual(db.json_loads(general["skills_json"], []), ["my_skill"])

    def test_migration_renames_only_the_original_recommended_skill_category(self) -> None:
        original = """---
id: mermaid_diagram
name: Mermaid 图表设计 Skill
description: 生成流程图、时序图、架构图和状态图，并提供可渲染的 Mermaid 代码。
category: marketplace
version: 1.0.0
---
# Mermaid 图表设计

当用户要求流程图、时序图、架构图或状态图时使用。

1. 先确定最适合的图类型和节点关系。
2. 节点文字保持简短，包含标点时使用引号。
3. 输出完整的 Mermaid fenced code block，并附一段阅读说明。
4. 复杂图拆成两张，避免单图节点过多。
"""
        raw = original.encode("utf-8")
        now = db.utc_now()
        db.execute(
            """
            INSERT INTO skills(
                id, name, description, category, version, content, enabled,
                required_mcps, created_at, updated_at
            ) VALUES ('mermaid_diagram', 'Mermaid 图表设计 Skill', '',
                      'marketplace', '1.0.0', ?, 1, '[]', ?, ?)
            """,
            (original, now, now),
        )
        db.execute(
            """
            INSERT INTO skill_files(
                skill_id, path, content, content_type, is_binary, size, updated_at
            ) VALUES ('mermaid_diagram', 'SKILL.md', ?, 'text/markdown', 0, ?, ?)
            """,
            (raw, len(raw), now),
        )
        db.execute("DELETE FROM schema_migrations WHERE version = 6")

        db.init_db()

        skill = db.query_one(
            "SELECT category, content FROM skills WHERE id = 'mermaid_diagram'"
        )
        self.assertEqual(skill["category"], "recommended")
        self.assertIn("category: recommended", skill["content"])

    def test_migration_preserves_an_edited_same_id_skill(self) -> None:
        now = db.utc_now()
        custom = "---\nid: mermaid_diagram\ncategory: marketplace\n---\n用户维护内容"
        db.execute(
            """
            INSERT INTO skills(
                id, name, description, category, version, content, enabled,
                required_mcps, created_at, updated_at
            ) VALUES ('mermaid_diagram', '团队图表规范', '',
                      'marketplace', '2.0.0', ?, 1, '[]', ?, ?)
            """,
            (custom, now, now),
        )
        db.execute("DELETE FROM schema_migrations WHERE version = 6")

        db.init_db()

        skill = db.query_one(
            "SELECT category, content FROM skills WHERE id = 'mermaid_diagram'"
        )
        self.assertEqual(skill["category"], "marketplace")
        self.assertEqual(skill["content"], custom)

if __name__ == "__main__":
    unittest.main()
