import io
import os
import unittest
from fastapi.testclient import TestClient
from app.main import app
from app import db
from app.services import auth_service
from app.services.workspace_path_manager import default_path_manager

class ModelDisplayAndWorkspaceFilesTests(unittest.TestCase):
    def setUp(self):
        db.execute("DELETE FROM login_limits")
        self.client = TestClient(app)
        self.client.post("/api/auth/login", json={"username": "admin", "password": "admin123456"})
        self.ws_id = "ws_test_files_lifecycle"
        self.client.post(
            "/api/workspaces",
            json={"id": self.ws_id, "name": "文件测试项目", "description": "用于测试项目文件展示与下载"},
        )

    def test_model_css_and_js_rules(self):
        """Verify CSS and JS rules for model item display to prevent truncation or zero width."""
        with open("web/styles.css", "r", encoding="utf-8") as f:
            css = f.read()
        self.assertIn('input[type="checkbox"], input[type="radio"]', css)
        self.assertIn('width: auto !important', css)
        self.assertIn('.source-model-item', css)
        self.assertIn('.source-model-name', css)
        self.assertIn('.default-radio-label', css)
        self.assertIn('.sidebar.collapsed .account-panel', css)
        self.assertIn('.sidebar.collapsed #logoutButton', css)
        self.assertIn('.sidebar.collapsed #logoutButton .logout-text', css)
        self.assertIn('.sidebar.collapsed #logoutButton .logout-icon', css)

        with open("web/index.html", "r", encoding="utf-8") as f:
            html = f.read()
        self.assertIn('<button id="usersNav"', html)
        self.assertIn('<div id="accountPanel"', html)
        self.assertIn('class="logout-icon"', html)
        self.assertIn('class="logout-text"', html)
        # usersNav should be inside <nav> and after brand
        nav_idx = html.find('<nav>')
        users_nav_idx = html.find('id="usersNav"')
        brand_idx = html.find('class="brand"')
        account_idx = html.find('id="accountPanel"')
        footer_idx = html.find('class="sidebar-footer"')
        self.assertGreater(users_nav_idx, brand_idx, "usersNav should be below brand, not above")
        self.assertGreater(users_nav_idx, nav_idx, "usersNav should be inside <nav>")
        self.assertGreater(account_idx, nav_idx, "accountPanel should be placed after nav")
        self.assertLess(account_idx, footer_idx, "accountPanel should be placed before sidebar-footer")

        with open("web/app.js", "r", encoding="utf-8") as f:
            js = f.read()
        self.assertIn('renderCurrentSourceModels', js)
        self.assertIn('displayName', js)
        self.assertIn('source-model-id-badge', js)
        self.assertIn('loadProjectFilesOnly', js)
        self.assertIn('renderProjectFilesList', js)
        self.assertIn('selectProjectFile', js)
        self.assertIn('uploadProjectFiles', js)

    def test_workspace_files_api_lifecycle(self):
        """Test workspace files listing, uploading, downloading, and deletion."""
        ws_id = self.ws_id

        # 2. List files in new workspace (should be empty initially)
        list_resp = self.client.get(f"/api/workspaces/{ws_id}/files")
        self.assertEqual(list_resp.status_code, 200)
        data = list_resp.json()
        self.assertEqual(data["workspace_id"], ws_id)
        self.assertIsInstance(data["items"], list)

        # 3. Upload a document into workspace
        doc_content = b"# Sales Report 2026\n\nTotal revenue: $1,000,000\n"
        file_payload = ("sales_report.md", io.BytesIO(doc_content), "text/markdown")
        upload_resp = self.client.post(
            f"/api/workspaces/{ws_id}/files/upload",
            files={"file": file_payload},
            data={"subpath": ""},
        )
        self.assertEqual(upload_resp.status_code, 200)
        upload_data = upload_resp.json()
        self.assertTrue(upload_data["ok"])
        self.assertEqual(upload_data["file"]["name"], "sales_report.md")
        self.assertEqual(upload_data["file"]["kind"], "md")

        # 4. List files again - should contain sales_report.md
        list_resp2 = self.client.get(f"/api/workspaces/{ws_id}/files")
        self.assertEqual(list_resp2.status_code, 200)
        items = list_resp2.json()["items"]
        filenames = [item["name"] for item in items]
        self.assertIn("sales_report.md", filenames)

        # 5. Download the file
        dl_resp = self.client.get(f"/api/workspaces/{ws_id}/files/sales_report.md")
        self.assertEqual(dl_resp.status_code, 200)
        self.assertEqual(dl_resp.content, doc_content)

        # 6. Inline preview of the file
        prev_resp = self.client.get(f"/api/workspaces/{ws_id}/files/sales_report.md?inline=true")
        self.assertEqual(prev_resp.status_code, 200)
        self.assertEqual(prev_resp.content, doc_content)

        # 7. Upload another file into a subdirectory
        sub_content = b"<html><body><h1>Chart</h1></body></html>"
        sub_file = ("chart.html", io.BytesIO(sub_content), "text/html")
        sub_upload_resp = self.client.post(
            f"/api/workspaces/{ws_id}/files/upload",
            files={"file": sub_file},
            data={"subpath": "reports/visuals"},
        )
        self.assertEqual(sub_upload_resp.status_code, 200)
        sub_data = sub_upload_resp.json()
        self.assertEqual(sub_data["file"]["path"], "reports/visuals/chart.html")

        # 8. List files recursively
        list_resp3 = self.client.get(f"/api/workspaces/{ws_id}/files")
        self.assertEqual(list_resp3.status_code, 200)
        all_paths = [item["path"] for item in list_resp3.json()["items"]]
        self.assertIn("sales_report.md", all_paths)
        self.assertIn("reports/visuals/chart.html", all_paths)

        # 9. Download the subdirectory file
        dl_sub_resp = self.client.get(f"/api/workspaces/{ws_id}/files/reports/visuals/chart.html")
        self.assertEqual(dl_sub_resp.status_code, 200)
        self.assertEqual(dl_sub_resp.content, sub_content)

        # 10. Delete the file
        del_resp = self.client.delete(f"/api/workspaces/{ws_id}/files/sales_report.md")
        self.assertEqual(del_resp.status_code, 200)
        self.assertTrue(del_resp.json()["ok"])

        # Verify it is deleted
        list_resp4 = self.client.get(f"/api/workspaces/{ws_id}/files")
        remaining_files = [item["name"] for item in list_resp4.json()["items"] if not item["is_dir"]]
        self.assertNotIn("sales_report.md", remaining_files)

    def test_chat_upload_with_workspace_id(self):
        """Verify /api/uploads with workspace_id copies file to workspace code_dir."""
        ws_id = self.ws_id
        attachment_content = b"Quarterly Financial Summary 2026"
        file_payload = ("finance_summary.txt", io.BytesIO(attachment_content), "text/plain")

        resp = self.client.post(
            f"/api/uploads?workspace_id={ws_id}",
            files={"file": file_payload},
        )
        self.assertIn(resp.status_code, (200, 201))
        upload_data = resp.json()
        self.assertEqual(upload_data["name"], "finance_summary.txt")

        # Verify the file is in the workspace files list
        list_resp = self.client.get(f"/api/workspaces/{ws_id}/files")
        self.assertEqual(list_resp.status_code, 200)
        filenames = [item["name"] for item in list_resp.json()["items"]]
        self.assertIn("finance_summary.txt", filenames)

        # Verify downloading it
        dl_resp = self.client.get(f"/api/workspaces/{ws_id}/files/finance_summary.txt")
        self.assertEqual(dl_resp.status_code, 200)
        self.assertEqual(dl_resp.content, attachment_content)

    def test_stdio_mcp_filtering_when_disabled(self):
        """Verify stdio MCP servers are filtered out when APP_ALLOW_STDIO_MCP is false."""
        from app.services.mcp_gateway import McpGateway
        gateway = McpGateway()
        tools = gateway.list_tools()
        stdio_server_ids = {"memory-local", "filesystem-local", "sequential-thinking-local"}
        for tool in tools:
            self.assertNotIn(tool.get("server_id"), stdio_server_ids)

    def test_agent_runtime_workspace_files_manifest(self):
        """Verify AgentRuntime generates workspace files manifest and extracts relevant content."""
        from app.services.agent_runtime import AgentRuntime
        from app.services.skill_registry import SkillRegistry
        from app.services.task_state import TaskStateService
        runtime = AgentRuntime(SkillRegistry(), None, None, task_state=TaskStateService(db.get_conn))
        manifest = runtime._workspace_files_manifest({
            "workspace": "project-3w1g8x",
            "organization_id": "local-org",
            "user_id": "user_fd180a33fc1a40db",
            "message": "当前项目下的试题，是什么文件",
        })
        if os.path.exists("data/workspaces/local-org/user_fd180a33fc1a40db/project-3w1g8x/code/实操样题.pdf"):
            self.assertIn("实操样题.pdf", manifest)
            self.assertIn("当前项目工作区文件与目录列表", manifest)

        empty_manifest = runtime._workspace_files_manifest({"workspace": "default"})
        self.assertEqual(empty_manifest, "")

    def test_disabled_mcp_not_in_plan_allowed_servers(self):
        """Verify disabled stdio MCP servers are excluded from execution plan allowed_servers."""
        from app.services.agent_runtime import AgentRuntime
        from app.services.skill_registry import SkillRegistry
        from app.services.mcp_gateway import McpGateway
        from app.services.task_state import TaskStateService
        runtime = AgentRuntime(SkillRegistry(), McpGateway(), None, task_state=TaskStateService(db.get_conn))
        plan = runtime._build_execution_plan(
            {"message": "当前项目下的试题，是什么文件", "workspace": "project-3w1g8x"},
            skills=[{"id": "knowledge_graph_memory", "name": "知识图谱记忆 Skill", "required_mcps": ["memory-local"]}],
            requested_format="",
            wants_report=False,
        )
        self.assertNotIn("memory-local", plan.get("allowed_servers", []))

    def test_pdf_preview_and_format_iso_date_in_assets(self):
        """Verify web/app.js contains formatIsoDate and iframe preview for PDF files."""
        with open("web/app.js", "r", encoding="utf-8") as f:
            js = f.read()
        self.assertIn("function formatIsoDate", js)
        self.assertIn("kind === 'pdf'", js)
        self.assertIn("iframe", js)

    def test_user_create_schema_flexibility_and_menu_roles(self):
        """Verify UserCreate supports flexible usernames and 6+ char passwords, and admin menu roles."""
        from app.schemas import UserCreate
        u1 = UserCreate(username="张工_01", password="password", role="user")
        self.assertEqual(u1.username, "张工_01")
        self.assertEqual(u1.password, "password")

        u2 = UserCreate(username="team@company.com", password="123456", role="admin")
        self.assertEqual(u2.username, "team@company.com")
        self.assertEqual(u2.role, "admin")

        with open("web/index.html", "r", encoding="utf-8") as f:
            html = f.read()
        self.assertIn('id="modelsNav"', html)
        self.assertIn('id="mcpNav"', html)
        self.assertIn('id="diagnosticsNav"', html)
        self.assertIn('id="skillsMemberNotice"', html)

        with open("web/app.js", "r", encoding="utf-8") as f:
            js = f.read()
        self.assertIn("adminOnlyTabs", js)
        self.assertIn("modelsNav", js)
        self.assertIn("mcpNav", js)
        self.assertIn("diagnosticsNav", js)

if __name__ == "__main__":
    unittest.main()
