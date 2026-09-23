import unittest
import json
import uuid
from fastapi.testclient import TestClient
from app.main import app, runtime
from app import db
from app.services import auth_service

class ConversationManagementAndCrossContextTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)
        self.client.post("/api/auth/login", json={"username": "admin", "password": "admin123456"})
        self.suffix = uuid.uuid4().hex[:8]
        self.workspace_a = f"ws_test_conv_a_{self.suffix}"
        self.workspace_b = f"ws_test_conv_b_{self.suffix}"
        db.execute("INSERT OR REPLACE INTO workspaces(id, organization_id, owner_user_id, name, description, default_agent_id, default_model_id, settings_json, enabled, created_at, updated_at) VALUES(?, 'local-org', 'test-user', 'Test Project A', '', 'general-agent', 'deterministic', '{}', 1, ?, ?)", (self.workspace_a, db.utc_now(), db.utc_now()))
        db.execute("INSERT OR REPLACE INTO workspaces(id, organization_id, owner_user_id, name, description, default_agent_id, default_model_id, settings_json, enabled, created_at, updated_at) VALUES(?, 'local-org', 'test-user', 'Test Project B', '', 'general-agent', 'deterministic', '{}', 1, ?, ?)", (self.workspace_b, db.utc_now(), db.utc_now()))

    def tearDown(self):
        db.execute("DELETE FROM workspaces WHERE id IN (?, ?)", (self.workspace_a, self.workspace_b))
        db.execute("DELETE FROM tasks WHERE workspace IN (?, ?)", (self.workspace_a, self.workspace_b))
        db.execute("DELETE FROM task_events WHERE task_id LIKE ?", (f"%{self.suffix}%",))
        db.execute("DELETE FROM conversation_metadata WHERE workspace_id IN (?, ?)", (self.workspace_a, self.workspace_b))

    def test_conversation_rename_and_list(self):
        conv_id = f"conv_rename_test_{self.suffix}"
        task_id = f"task_rename_test_{self.suffix}"
        now = db.utc_now()
        db.execute(
            "INSERT INTO tasks(id, title, message, agent_id, model_id, conversation_id, workspace, organization_id, user_id, status, created_at, updated_at) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (task_id, "初始测试消息", "初始测试消息", "general-agent", "deterministic", conv_id, self.workspace_a, "local-org", "test-user", "completed", now, now)
        )

        # 1. Update conversation title
        res = self.client.put(f"/api/conversations/{conv_id}/title", json={"title": "我的自定义会话标题"})
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data["title"], "我的自定义会话标题")

        # 2. Verify list_workspace_conversations returns the custom title
        list_res = self.client.get(f"/api/workspaces/{self.workspace_a}/conversations")
        self.assertEqual(list_res.status_code, 200)
        convs = list_res.json().get("conversations", [])
        target = next((c for c in convs if c["conversation_id"] == conv_id), None)
        self.assertIsNotNone(target)
        self.assertEqual(target["title"], "我的自定义会话标题")
        self.assertEqual(target["custom_title"], "我的自定义会话标题")

        # 3. Verify get_conversation_messages returns the custom title
        msg_res = self.client.get(f"/api/conversations/{conv_id}/messages")
        self.assertEqual(msg_res.status_code, 200)
        msg_data = msg_res.json()
        self.assertEqual(msg_data["title"], "我的自定义会话标题")
        self.assertEqual(msg_data["latest_task_id"], task_id)

    def test_cross_conversation_context_retrieval(self):
        conv_source = f"conv_source_secret_{self.suffix}"
        conv_target = f"conv_target_query_{self.suffix}"
        now = db.utc_now()

        # Set up source conversation in workspace_a with custom title and answer
        task_source = f"task_source_{self.suffix}"
        db.execute(
            "INSERT INTO tasks(id, title, message, agent_id, model_id, conversation_id, workspace, organization_id, user_id, status, created_at, updated_at) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (task_source, "告诉系统密钥是 9527", "告诉系统密钥是 9527", "general-agent", "deterministic", conv_source, self.workspace_a, "local-org", "test-user", "completed", now, now)
        )
        db.execute(
            "INSERT INTO task_events(task_id, ts, type, title, content, data_json) VALUES(?, ?, 'answer', '答复', '已记录密钥为9527', '{}')",
            (task_source, now)
        )
        db.execute(
            "INSERT INTO conversation_metadata(conversation_id, workspace_id, title, created_at, updated_at) VALUES(?, ?, '密钥备忘录' || ?, ?, ?)",
            (conv_source, self.workspace_a, self.suffix, now, now)
        )

        custom_title = f"密钥备忘录{self.suffix}"

        # Case 1: Target conversation in same workspace WITHOUT explicit reference -> Default isolation (empty history)
        task_isolated = {
            "id": f"task_isolated_{self.suffix}",
            "conversation_id": conv_target,
            "workspace": self.workspace_a,
            "message": "我的密钥是多少？",
            "created_at": db.utc_now(),
        }
        history_isolated = runtime._conversation_history(task_isolated)
        self.assertEqual(len(history_isolated), 0)

        # Case 2: Target conversation in same workspace WITH explicit reference -> Injects referenced context
        task_cross = {
            "id": f"task_cross_{self.suffix}",
            "conversation_id": conv_target,
            "workspace": self.workspace_a,
            "message": f"帮我查看会话‘{custom_title}’中的上下文，密钥是多少？",
            "created_at": db.utc_now(),
        }
        history_cross = runtime._conversation_history(task_cross)
        self.assertEqual(len(history_cross), 1)
        self.assertIn(custom_title, history_cross[0]["content"])
        self.assertIn("9527", history_cross[0]["content"])

        # Case 3: Target conversation in DIFFERENT workspace -> Strict cross-project isolation (cannot retrieve)
        task_cross_project = {
            "id": f"task_cross_p_{self.suffix}",
            "conversation_id": f"conv_in_b_{self.suffix}",
            "workspace": self.workspace_b,
            "message": f"帮我查看会话‘{custom_title}’中的上下文",
            "created_at": db.utc_now(),
        }
        history_cross_project = runtime._conversation_history(task_cross_project)
        self.assertEqual(len(history_cross_project), 0)

if __name__ == "__main__":
    unittest.main()
