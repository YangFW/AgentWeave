from __future__ import annotations

import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from app.services.context_service import (
    ContextService,
    ExecutionScope,
    MEMORY_SCOPE_PRECEDENCE,
    MemoryNotFoundError,
)


class ContextServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "context.db"
        self.now = "2026-08-11T02:00:00+00:00"

        def connect() -> sqlite3.Connection:
            return sqlite3.connect(self.db_path, timeout=5, check_same_thread=False)

        self.connect = connect
        self.service = ContextService(connect, clock=lambda: self.now)
        self.alice = ExecutionScope(
            organization_id="org-a",
            workspace_id="workspace-a",
            user_id="alice",
            agent_id="researcher",
            conversation_id="conversation-a",
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_scope_normalisation_crud_and_public_serialisation(self) -> None:
        scope = ExecutionScope.normalise(
            {
                "organization": " org-a ",
                "workspace": " workspace-a ",
                "user": " alice ",
                "agent": " researcher ",
                "conversation": " conversation-a ",
            }
        )
        self.assertEqual(scope, self.alice)
        memory = self.service.create_memory(
            scope,
            scope_type="USER",
            kind="Preference",
            title=" 回答风格 ",
            content=" 使用简体中文 ",
            tags=["语言", "语言", "简洁"],
            trust_level=90,
        )
        self.assertEqual(memory["scope_type"], "user")
        self.assertEqual(memory["scope_id"], "alice")
        self.assertEqual(memory["kind"], "preference")
        self.assertEqual(memory["tags"], ["语言", "简洁"])
        self.assertIs(memory["enabled"], True)
        self.assertEqual(self.service.get_memory(memory["id"], scope), memory)
        self.assertEqual([item["id"] for item in self.service.list_memories(scope)], [memory["id"]])

        updated = self.service.update_memory(
            memory["id"],
            scope,
            content="中文回答，并给出结论",
            tags=["语言", "结论"],
            reason="user correction",
        )
        self.assertEqual(updated["content"], "中文回答，并给出结论")
        self.assertNotIn("tags_json", updated)
        self.assertNotIn("organization_id", updated)
        with closing(self.connect()) as conn, conn:
            raw = conn.execute(
                "SELECT tags_json, enabled FROM memory_entries WHERE id = ?", (memory["id"],)
            ).fetchone()
        self.assertEqual(raw[0], '["语言","结论"]')
        self.assertEqual(raw[1], 1)

    def test_every_mutation_has_a_revision_including_delete(self) -> None:
        memory = self.service.create_memory(
            self.alice,
            content="生成文档时先给提纲",
            title="文档流程",
        )
        self.service.update_memory(memory["id"], self.alice, content="先确认目标，再给提纲")
        self.service.disable_memory(memory["id"], self.alice)
        self.service.enable_memory(memory["id"], self.alice)
        deleted = self.service.delete_memory(memory["id"], self.alice, reason="privacy request")
        self.assertEqual(deleted, {"id": memory["id"], "deleted": True})
        self.assertIsNone(self.service.get_memory(memory["id"], self.alice))

        revisions = self.service.list_revisions(memory["id"], self.alice)
        self.assertEqual([item["revision"] for item in revisions], [1, 2, 3, 4, 5])
        self.assertEqual(
            [item["reason"] for item in revisions],
            ["created", "updated", "disabled", "enabled", "privacy request"],
        )
        self.assertEqual(revisions[0]["before"], {})
        self.assertEqual(revisions[-1]["after"], {})
        self.assertNotIn("tags_json", revisions[-1]["before"])
        with closing(self.connect()) as conn, conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM memory_entries WHERE id = ?", (memory["id"],)
            ).fetchone()[0]
            revision_count = conn.execute(
                "SELECT COUNT(*) FROM memory_revisions WHERE memory_id = ?", (memory["id"],)
            ).fetchone()[0]
        self.assertEqual(count, 0)
        self.assertEqual(revision_count, 5)

    def test_user_workspace_and_organization_isolation(self) -> None:
        bob_same_workspace = ExecutionScope(
            organization_id="org-a",
            workspace_id="workspace-a",
            user_id="bob",
            agent_id="researcher",
            conversation_id="conversation-b",
        )
        alice_other_workspace = ExecutionScope(
            organization_id="org-a",
            workspace_id="workspace-b",
            user_id="alice",
            agent_id="researcher",
            conversation_id="conversation-c",
        )
        outsider = ExecutionScope(
            organization_id="org-b",
            workspace_id="workspace-a",
            user_id="alice",
            agent_id="researcher",
            conversation_id="conversation-a",
        )
        organization = self.service.create_memory(
            self.alice, scope_type="organization", title="安全", content="禁止泄露密钥"
        )
        workspace = self.service.create_memory(
            self.alice, scope_type="workspace", title="术语", content="统一使用平台一词"
        )
        user = self.service.create_memory(
            self.alice, scope_type="user", title="语言", content="使用中文"
        )
        agent = self.service.create_memory(
            self.alice, scope_type="agent", title="研究", content="引用来源"
        )
        conversation = self.service.create_memory(
            self.alice, scope_type="conversation", title="交付", content="本轮输出 Word"
        )
        self.assertEqual(agent["execution_scope"]["agent_id"], "researcher")
        self.assertEqual(conversation["execution_scope"]["conversation_id"], "conversation-a")

        self.assertEqual(
            {item["id"] for item in self.service.list_memories(self.alice)},
            {organization["id"], workspace["id"], user["id"], agent["id"], conversation["id"]},
        )
        self.assertEqual(
            {item["id"] for item in self.service.list_memories(bob_same_workspace)},
            {organization["id"], workspace["id"]},
        )
        self.assertEqual(
            {item["id"] for item in self.service.list_memories(alice_other_workspace)},
            {organization["id"]},
        )
        self.assertEqual(self.service.list_memories(outsider), [])
        self.assertIsNone(self.service.get_memory(user["id"], bob_same_workspace))
        with self.assertRaises(MemoryNotFoundError):
            self.service.update_memory(user["id"], bob_same_workspace, content="越权修改")
        self.assertEqual(self.service.list_revisions(user["id"], bob_same_workspace), [])

    def test_organization_rule_wins_conflicts_and_order_is_explicit(self) -> None:
        self.assertEqual(
            MEMORY_SCOPE_PRECEDENCE,
            ("organization", "workspace", "user", "agent", "conversation"),
        )
        organization = self.service.create_memory(
            self.alice,
            scope_type="organization",
            kind="rule",
            title="敏感信息",
            content="一律隐藏 API Key",
            trust_level=60,
        )
        workspace = self.service.create_memory(
            self.alice,
            scope_type="workspace",
            kind="rule",
            title="输出格式",
            content="默认 Markdown",
        )
        self.service.create_memory(
            self.alice,
            scope_type="user",
            kind="rule",
            title="敏感信息",
            content="可以打印 API Key",
            trust_level=100,
        )
        conversation = self.service.create_memory(
            self.alice,
            scope_type="conversation",
            title="本轮目标",
            content="生成调研文档",
        )

        effective = self.service.get_effective_context(self.alice)
        self.assertEqual(
            effective["used_memory_ids"],
            [organization["id"], workspace["id"], conversation["id"]],
        )
        self.assertIn("一律隐藏 API Key", effective["effective_context"])
        self.assertNotIn("可以打印 API Key", effective["effective_context"])
        self.assertLess(
            effective["effective_context"].index("组织规则"),
            effective["effective_context"].index("工作区规则"),
        )

    def test_disabled_expired_and_deleted_memories_are_not_injected(self) -> None:
        active = self.service.create_memory(
            self.alice, title="活跃", content="保留这条", expires_at="2026-08-12T00:00:00Z"
        )
        disabled = self.service.create_memory(self.alice, title="停用", content="不要注入")
        expired = self.service.create_memory(
            self.alice, title="过期", content="也不要注入", expires_at="2026-08-10T00:00:00Z"
        )
        deleted = self.service.create_memory(self.alice, title="删除", content="不能注入")
        self.service.disable_memory(disabled["id"], self.alice)
        self.service.delete_memory(deleted["id"], self.alice)

        managed = {item["id"] for item in self.service.list_memories(self.alice)}
        self.assertEqual(managed, {active["id"], disabled["id"], expired["id"]})
        effective = self.service.get_effective_context(self.alice)
        self.assertEqual(effective["used_memory_ids"], [active["id"]])
        self.assertEqual(effective["memories"][0]["title"], "活跃")
        self.assertNotIn("不要注入", effective["effective_context"])

    def test_explicit_remember_and_forget_are_scoped_and_audited(self) -> None:
        remembered = self.service.remember(
            self.alice,
            "以后交付 Word 和 PDF 两种格式",
            title="交付格式",
            tags=["文档"],
        )
        self.assertEqual(remembered["source_type"], "user_explicit")
        self.assertEqual(
            self.service.list_revisions(remembered["id"], self.alice)[0]["reason"],
            "explicit_remember",
        )
        self.assertEqual(self.service.forget(self.alice, query="不存在"), [])
        self.assertEqual(
            self.service.forget(self.alice, query="交付格式", scope_type="user"),
            [remembered["id"]],
        )
        revisions = self.service.list_revisions(remembered["id"], self.alice)
        self.assertEqual(revisions[-1]["reason"], "explicit_forget")

        another = self.service.remember(self.alice, "使用中文", title="语言")
        self.assertEqual(self.service.forget(self.alice, another["id"]), [another["id"]])
        self.assertIsNone(self.service.get_memory(another["id"], self.alice))

    def test_invalid_data_is_rejected_without_partial_write(self) -> None:
        with self.assertRaises(ValueError):
            self.service.create_memory(self.alice, scope_type="unknown", content="x")
        with self.assertRaises(ValueError):
            self.service.create_memory(self.alice, scope_type="user", content="  ")
        with self.assertRaises(ValueError):
            self.service.create_memory(self.alice, content="x", trust_level=101)
        without_agent = ExecutionScope("org-a", "workspace-a", "alice")
        with self.assertRaises(ValueError):
            self.service.create_memory(without_agent, scope_type="agent", content="x")
        with closing(self.connect()) as conn, conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM memory_entries").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM memory_revisions").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
