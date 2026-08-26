from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app import db
from app.services.agent_runtime import AgentRuntime, create_task_record
from app.services.context_service import ExecutionScope
from app.services.conversation_summary_service import (
    ConversationSummaryConflictError,
    ConversationSummaryService,
)
from app.services.mcp_gateway import McpGateway
from app.services.model_gateway import ModelGateway
from app.services.skill_registry import SkillRegistry


class ConversationSummaryServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.original_db_path = db.DB_PATH
        self.temp_dir = tempfile.TemporaryDirectory()
        db.DB_PATH = Path(self.temp_dir.name) / "summaries.db"
        db.init_db()
        self.service = ConversationSummaryService()
        self.alice = ExecutionScope(
            organization_id="org-a",
            workspace_id="workspace-a",
            user_id="alice",
            conversation_id="conv-alice",
        )

    def tearDown(self) -> None:
        db.DB_PATH = self.original_db_path
        self.temp_dir.cleanup()

    def test_crud_is_versioned_and_scope_bound(self) -> None:
        created = self.service.upsert(
            self.alice,
            summary="第一版摘要",
            preserved_constraints=["必须用中文", "必须用中文"],
            through_task_id="task-1",
        )
        self.assertEqual(created["version"], 1)
        self.assertEqual(created["preserved_constraints"], ["必须用中文"])
        self.assertEqual(self.service.get(self.alice)["summary"], "第一版摘要")

        updated = self.service.upsert(
            self.alice,
            summary="第二版摘要",
            preserved_constraints=["不要调用天气工具"],
            through_task_id="task-2",
        )
        self.assertEqual(updated["version"], 2)
        self.assertEqual(updated["created_at"], created["created_at"])
        self.assertEqual(self.service.list(self.alice)[0]["through_task_id"], "task-2")

        bob = ExecutionScope(
            organization_id="org-a",
            workspace_id="workspace-a",
            user_id="bob",
            conversation_id="conv-alice",
        )
        self.assertIsNone(self.service.get(bob))
        self.assertFalse(self.service.delete(bob))
        with self.assertRaises(ConversationSummaryConflictError):
            self.service.upsert(bob, summary="不能覆盖 Alice")

        self.assertTrue(self.service.delete(self.alice))
        self.assertIsNone(self.service.get(self.alice))

    def test_compaction_preserves_explicit_constraints_and_builds_safe_prefix(self) -> None:
        compacted = self.service.compact(
            self.alice,
            [
                {"role": "user", "content": "分析一份项目资料。"},
                {"role": "assistant", "content": "请上传资料。"},
                {"role": "user", "content": "必须使用中文，最终输出 Word 文档。"},
                {"role": "assistant", "content": "收到，我会按要求处理。"},
                {"role": "user", "content": "不要调用天气工具，重点关注风险。"},
            ],
            through_task_id="task-5",
            max_summary_chars=700,
        )
        self.assertEqual(compacted["through_task_id"], "task-5")
        self.assertEqual(
            compacted["preserved_constraints"],
            ["必须使用中文，最终输出 Word 文档。", "不要调用天气工具，重点关注风险。"],
        )
        prefix = self.service.history_prefix(self.alice)
        self.assertEqual(prefix[0]["role"], "assistant")
        self.assertIn("不是本次新目标", prefix[0]["content"])
        self.assertIn("不要调用天气工具", prefix[0]["content"])
        self.assertGreater(compacted["token_count"], 0)

    def test_empty_compaction_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            self.service.compact(self.alice, [])

    def test_runtime_compacts_old_turns_and_injects_summary_before_recent_history(self) -> None:
        runtime = AgentRuntime(
            SkillRegistry(),
            McpGateway(),
            ModelGateway(),
            conversation_summary_service=self.service,
        )
        tasks = []
        for index in range(9):
            message = "必须始终使用中文。" if index == 0 else f"第 {index + 1} 个连续问题"
            task = create_task_record(
                message,
                "general-agent",
                workspace="workspace-a",
                conversation_id="conv-alice",
                organization_id="org-a",
                user_id="alice",
            )
            created_at = f"2026-08-11T00:00:{index:02d}+00:00"
            db.execute(
                "UPDATE tasks SET status = 'completed', created_at = ?, result_json = ? WHERE id = ?",
                (created_at, db.json_dumps({"summary": f"回答 {index + 1}"}), task["id"]),
            )
            db.execute(
                "INSERT INTO task_events(task_id, type, title, content, data_json, ts) VALUES (?, 'answer', '回答', ?, '{}', ?)",
                (task["id"], f"回答 {index + 1}", created_at),
            )
            tasks.append(db.query_one("SELECT * FROM tasks WHERE id = ?", (task["id"],)))

        compacted = runtime._maybe_compact_conversation(tasks[-1])
        self.assertIsNotNone(compacted)
        self.assertEqual(compacted["through_task_id"], tasks[2]["id"])
        self.assertIn("必须始终使用中文。", compacted["preserved_constraints"])

        pending = create_task_record(
            "整理前面的结论",
            "general-agent",
            workspace="workspace-a",
            conversation_id="conv-alice",
            organization_id="org-a",
            user_id="alice",
        )
        db.execute(
            "UPDATE tasks SET created_at = ? WHERE id = ?",
            ("2026-08-11T00:00:10+00:00", pending["id"]),
        )
        pending = db.query_one("SELECT * FROM tasks WHERE id = ?", (pending["id"],))
        history = runtime._conversation_history(pending)

        self.assertEqual(history[0]["role"], "assistant")
        self.assertIn("较早对话摘要", history[0]["content"])
        self.assertIn("不是本次新目标", history[0]["content"])
        self.assertEqual([item["content"] for item in history if item["role"] == "user"], [
            "第 4 个连续问题",
            "第 5 个连续问题",
            "第 6 个连续问题",
            "第 7 个连续问题",
            "第 8 个连续问题",
            "第 9 个连续问题",
        ])


if __name__ == "__main__":
    unittest.main()
