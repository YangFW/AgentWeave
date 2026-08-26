from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from app import db
from app import main as main_module


class DiagnosticsApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_db_path = db.DB_PATH
        self.original_app_db_path = os.environ.get("APP_DB_PATH")
        self.db_path = Path(self.temp_dir.name) / "diagnostics-api.db"
        os.environ["APP_DB_PATH"] = str(self.db_path)
        db.DB_PATH = self.db_path
        db.init_db()

        self.patches = [
            patch.object(main_module.loop_scheduler, "start", return_value=None),
            patch.object(main_module.loop_scheduler, "stop", new_callable=AsyncMock),
            patch.object(main_module.skill_registry, "load_builtin_skills", return_value=None),
            patch.object(main_module.mcp_gateway, "seed_builtin_servers", return_value=None),
            patch.object(main_module, "seed_agents", return_value=None),
            patch.object(main_module, "_recover_interrupted_runs", return_value=[]),
            patch.object(main_module.runtime, "run_task", new_callable=AsyncMock),
        ]
        for item in self.patches:
            item.start()
        self.client_context = TestClient(main_module.app)
        self.client = self.client_context.__enter__()

    def tearDown(self) -> None:
        self.client_context.__exit__(None, None, None)
        for item in reversed(self.patches):
            item.stop()
        db.DB_PATH = self.original_db_path
        if self.original_app_db_path is None:
            os.environ.pop("APP_DB_PATH", None)
        else:
            os.environ["APP_DB_PATH"] = self.original_app_db_path
        self.temp_dir.cleanup()

    def test_diagnostics_endpoint_returns_expected_checks(self) -> None:
        response = self.client.get("/api/diagnostics")
        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertEqual(payload["schema_version"], "diagnostics.v1")
        self.assertRegex(payload["generated_at"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
        self.assertIn("overall", payload)
        self.assertIn("summary", payload)
        self.assertIn("actionable_summary", payload)
        self.assertIn("readiness", payload)
        self.assertIn("improvement_backlog", payload)
        self.assertIn("self_tests", payload)
        self.assertIn("self_test_summary", payload)
        self.assertIn("report_markdown", payload)
        self.assertIn("issues", payload)
        self.assertIn("checks", payload)
        report = payload["report_markdown"]
        self.assertIn("# AgentNexus 平台自检报告", report)
        self.assertIn(f"- 生成时间：{payload['generated_at']}", report)
        self.assertIn("- 报告版本：diagnostics.v1", report)
        self.assertIn("## 问题清单", report)
        self.assertIn("页面入口", report)
        self.assertIn("填入修复任务", report)
        self.assertIn("## 优先处理事项", report)
        self.assertIn("## 默认能力与使用入口", report)
        self.assertIn("模型配置", report)
        self.assertIn("工作台 → 添加附件", report)
        self.assertIn("技能中心 / 工具接入 / 市场", report)
        self.assertIn("工作台右侧运行控制", report)
        self.assertIn("## 能力矩阵", report)
        self.assertIn("## 平台优化建议", report)
        self.assertIn("## 建议自测", report)
        self.assertIn("可直接测", report)
        self.assertIn("需准备", report)
        self.assertIn("- 自测分类：", report)
        self.assertIn("- 自测产物：", report)
        self.assertNotIn("api_key", report.lower())
        self.assertNotIn("ciphertext", report.lower())
        self.assertNotIn(str(self.db_path), report)
        self_test_summary = payload["self_test_summary"]
        self.assertEqual(self_test_summary["total"], len(payload["self_tests"]))
        self.assertEqual(self_test_summary["ready"], len([item for item in payload["self_tests"] if item["readiness"] == "ready"]))
        self.assertEqual(self_test_summary["needs_setup"], len([item for item in payload["self_tests"] if item["readiness"] != "ready"]))
        self.assertIsInstance(self_test_summary["by_category"], dict)
        self.assertIsInstance(self_test_summary["by_artifact"], dict)
        self.assertIn("文档", self_test_summary["by_category"])
        self.assertIn("pptx", self_test_summary["by_artifact"])
        actionable = payload["actionable_summary"]
        for key in {
            "blocked_capabilities",
            "needs_config_capabilities",
            "ready_capabilities",
            "p0_improvements",
            "p1_improvements",
            "p2_improvements",
            "ready_self_tests",
            "total_self_tests",
            "next_actions",
            "issues",
        }:
            self.assertIn(key, actionable)
            self.assertIsInstance(actionable[key], int)
        self.assertEqual(actionable["issues"], len(payload["issues"]))
        for item in payload["issues"]:
            self.assertIn(item["severity"], {"blocking", "attention"})
            self.assertIn(item["status"], {"fail", "warn"})
            self.assertTrue(item["check_id"])
            self.assertTrue(item["title"])
            self.assertTrue(item["detail"])
            self.assertTrue(item["action"])
            self.assertIsInstance(item["action_target"], dict)
            self.assertIn(item["action_target"]["tab"], {"chat", "workspaces", "models", "skills", "mcp", "marketplace", "knowledge", "artifacts", "experts", "memory", "loops", "diagnostics"})
            self.assertTrue(item["action_target"]["label"])
            self.assertTrue(item["fix_prompt"])
            self.assertIn(item["check_id"], item["fix_prompt"])
            self.assertIn(item["detail"], item["fix_prompt"])
        self.assertIn("next_actions", payload)
        self.assertLessEqual(len(payload["next_actions"]), 5)
        self.assertEqual(actionable["next_actions"], len(payload["next_actions"]))
        for item in payload["next_actions"]:
            self.assertIn(item["kind"], {"readiness", "improvement"})
            self.assertIn(item["priority"], {"P0", "P1", "P2"})
            self.assertIn(item["action_type"], {"navigate", "prompt"})
            self.assertTrue(item["ref_id"])
            self.assertTrue(item["title"])
            self.assertTrue(item["action_label"])
            self.assertIn("target_tab", item)
            self.assertIn("prompt", item)
            if item["action_type"] == "navigate":
                self.assertIn(item["target_tab"], {"chat", "workspaces", "models", "skills", "mcp", "marketplace", "knowledge", "artifacts", "experts", "memory", "loops", "diagnostics"})
                self.assertEqual(item["prompt"], "")
            if item["action_type"] == "prompt":
                self.assertEqual(item["target_tab"], "chat")
                self.assertTrue(item["prompt"])
        check_ids = {item["id"] for item in payload["checks"]}
        self.assertTrue(
            {
                "database.integrity",
                "workspace.default",
                "models.capability",
                "models.configured",
                "file.upload_context",
                "security.permissions",
                "skill_mcp.capability",
                "expert.capability",
                "expert.teams_configured",
                "memory.capability",
                "memory.configured",
                "automation.capability",
                "automation.configured",
                "skills.enabled",
                "mcp.configured",
                "knowledge.capability",
                "knowledge.index",
                "document.output_formats",
                "artifacts.integrity",
                "runtime.contract_capability",
                "runtime.trace_records",
                "runtime.active_runs",
                "network.capability",
                "network.search",
            }.issubset(check_ids)
        )
        for item in payload["checks"]:
            self.assertIsInstance(item["action_target"], dict)
            self.assertIn(item["action_target"]["tab"], {"chat", "workspaces", "models", "skills", "mcp", "marketplace", "knowledge", "artifacts", "experts", "memory", "loops", "diagnostics"})
            self.assertTrue(item["action_target"]["label"])
        self.assertEqual(
            next(item for item in payload["checks"] if item["id"] == "network.search")["action_target"],
            {"tab": "mcp", "label": "配置联网搜索"},
        )
        self.assertEqual(
            next(item for item in payload["checks"] if item["id"] == "file.upload_context")["action_target"],
            {"tab": "chat", "label": "添加附件"},
        )
        readiness_ids = {item["id"] for item in payload["readiness"]}
        self.assertTrue(
            {
                "core_platform",
                "model_runtime",
                "skill_mcp",
                "expert_mode",
                "memory_context",
                "automation",
                "knowledge",
                "file_input",
                "documents",
                "network_tools",
                "local_tools",
                "security_governance",
                "install_flow",
                "runtime_recovery",
            }.issubset(readiness_ids)
        )
        for item in payload["readiness"]:
            self.assertIn(item["state"], {"ready", "needs_config", "blocked"})
            self.assertIn(item["status"], {"pass", "warn", "fail"})
            self.assertTrue(item["title"])
            self.assertIsInstance(item["action_target"], dict)
            self.assertIn(item["action_target"]["tab"], {"chat", "workspaces", "models", "skills", "mcp", "marketplace", "knowledge", "artifacts", "experts", "memory", "loops", "diagnostics"})
            self.assertTrue(item["action_target"]["label"])
        model_readiness = next(item for item in payload["readiness"] if item["id"] == "model_runtime")
        self.assertEqual(model_readiness["action_target"]["tab"], "models")
        self.assertIn("模型配置", model_readiness["detail"])
        model_capability = next(item for item in payload["checks"] if item["id"] == "models.capability")
        self.assertEqual(model_capability["status"], "pass")
        self.assertIn("deterministic", model_capability["evidence"]["providers"])
        self.assertTrue(model_capability["evidence"]["env_secret_supported"])
        self.assertTrue(model_capability["evidence"]["direct_secret_supported"])
        self.assertTrue(model_capability["evidence"]["direct_secret_encrypted"])
        install_readiness = next(item for item in payload["readiness"] if item["id"] == "install_flow")
        self.assertEqual(install_readiness["action_target"]["tab"], "marketplace")
        file_input_readiness = next(item for item in payload["readiness"] if item["id"] == "file_input")
        self.assertEqual(file_input_readiness["state"], "ready")
        self.assertEqual(file_input_readiness["action_target"]["tab"], "chat")
        upload_check = next(item for item in payload["checks"] if item["id"] == "file.upload_context")
        self.assertEqual(upload_check["status"], "pass")
        self.assertIn("pdf", upload_check["evidence"]["formats"])
        document_check = next(item for item in payload["checks"] if item["id"] == "document.output_formats")
        self.assertEqual(document_check["status"], "pass")
        self.assertIn("docx", document_check["evidence"]["formats"])
        self.assertIn("pptx", document_check["evidence"]["optional_formats"])
        self.assertIn("pptx_configured", document_check["evidence"])
        document_readiness = next(item for item in payload["readiness"] if item["id"] == "documents")
        self.assertIn("PPTX", document_readiness["detail"])
        knowledge_capability = next(item for item in payload["checks"] if item["id"] == "knowledge.capability")
        self.assertEqual(knowledge_capability["status"], "pass")
        self.assertTrue(knowledge_capability["evidence"]["runtime_injection"])
        self.assertIn("workspace", knowledge_capability["evidence"]["scopes"])
        self.assertIn("pdf", knowledge_capability["evidence"]["indexed_upload_formats"])
        knowledge_readiness = next(item for item in payload["readiness"] if item["id"] == "knowledge")
        self.assertIn("尚未建立可检索知识库", knowledge_readiness["detail"])
        security_check = next(item for item in payload["checks"] if item["id"] == "security.permissions")
        self.assertEqual(security_check["status"], "pass")
        self.assertTrue(security_check["evidence"]["direct_key_encrypted"])
        self.assertTrue(security_check["evidence"]["arbitrary_shell_blocked"])
        security_readiness = next(item for item in payload["readiness"] if item["id"] == "security_governance")
        self.assertEqual(security_readiness["state"], "ready")
        skill_mcp_capability = next(item for item in payload["checks"] if item["id"] == "skill_mcp.capability")
        self.assertEqual(skill_mcp_capability["status"], "pass")
        self.assertTrue(skill_mcp_capability["evidence"]["skill_create_supported"])
        self.assertTrue(skill_mcp_capability["evidence"]["skill_upload_install_supported"])
        self.assertTrue(skill_mcp_capability["evidence"]["marketplace_supported"])
        self.assertTrue(skill_mcp_capability["evidence"]["mcp_create_supported"])
        self.assertTrue(skill_mcp_capability["evidence"]["mcp_import_supported"])
        self.assertIn("remote_mcp_enabled", skill_mcp_capability["evidence"])
        skill_mcp_readiness = next(item for item in payload["readiness"] if item["id"] == "skill_mcp")
        self.assertIn("内置市场安装", skill_mcp_readiness["detail"])
        expert_capability = next(item for item in payload["checks"] if item["id"] == "expert.capability")
        self.assertEqual(expert_capability["status"], "pass")
        self.assertTrue(expert_capability["evidence"]["parallel_members"])
        self.assertTrue(expert_capability["evidence"]["supervisor_aggregation"])
        expert_configured = next(item for item in payload["checks"] if item["id"] == "expert.teams_configured")
        self.assertIn(expert_configured["status"], {"pass", "warn"})
        self.assertIn("enabled_teams", expert_configured["evidence"])
        expert_readiness = next(item for item in payload["readiness"] if item["id"] == "expert_mode")
        self.assertEqual(expert_readiness["action_target"]["tab"], "experts")
        self.assertIn("专家", expert_readiness["detail"])
        memory_capability = next(item for item in payload["checks"] if item["id"] == "memory.capability")
        self.assertEqual(memory_capability["status"], "pass")
        self.assertIn("workspace", memory_capability["evidence"]["scopes"])
        self.assertTrue(memory_capability["evidence"]["revision_history"])
        self.assertTrue(memory_capability["evidence"]["conversation_summary"]["automatic"])
        memory_configured = next(item for item in payload["checks"] if item["id"] == "memory.configured")
        self.assertIn(memory_configured["status"], {"pass", "warn"})
        self.assertIn("conversation_summaries", memory_configured["evidence"])
        memory_readiness = next(item for item in payload["readiness"] if item["id"] == "memory_context")
        self.assertEqual(memory_readiness["action_target"]["tab"], "memory")
        self.assertIn("对话摘要", memory_readiness["detail"])
        automation_capability = next(item for item in payload["checks"] if item["id"] == "automation.capability")
        self.assertEqual(automation_capability["status"], "pass")
        self.assertIn("cron", automation_capability["evidence"]["triggers"])
        self.assertTrue(automation_capability["evidence"]["signed_webhooks"])
        self.assertTrue(automation_capability["evidence"]["idempotency"])
        automation_configured = next(item for item in payload["checks"] if item["id"] == "automation.configured")
        self.assertIn(automation_configured["status"], {"pass", "warn"})
        self.assertIn("active_loops", automation_configured["evidence"])
        automation_readiness = next(item for item in payload["readiness"] if item["id"] == "automation")
        self.assertEqual(automation_readiness["action_target"]["tab"], "loops")
        self.assertIn("Webhook", automation_readiness["detail"])
        runtime_capability = next(item for item in payload["checks"] if item["id"] == "runtime.contract_capability")
        self.assertEqual(runtime_capability["status"], "pass")
        self.assertTrue(runtime_capability["evidence"]["goal_contract_supported"])
        self.assertTrue(runtime_capability["evidence"]["dynamic_plan_nodes_supported"])
        self.assertTrue(runtime_capability["evidence"]["sse_event_stream_supported"])
        self.assertTrue(runtime_capability["evidence"]["output_verification_supported"])
        runtime_trace = next(item for item in payload["checks"] if item["id"] == "runtime.trace_records")
        self.assertIn(runtime_trace["status"], {"pass", "warn"})
        self.assertIn("goal_specs", runtime_trace["evidence"])
        runtime_readiness = next(item for item in payload["readiness"] if item["id"] == "runtime_recovery")
        self.assertIn("目标合同", runtime_readiness["detail"])
        network_capability = next(item for item in payload["checks"] if item["id"] == "network.capability")
        self.assertEqual(network_capability["status"], "pass")
        self.assertTrue(network_capability["evidence"]["outbound_supported"])
        self.assertTrue(network_capability["evidence"]["web_search_supported"])
        self.assertIn("remote_mcp_enabled", network_capability["evidence"])
        network_readiness = next(item for item in payload["readiness"] if item["id"] == "network_tools")
        self.assertIn("联网搜索", network_readiness["detail"])
        backlog_ids = {item["id"] for item in payload["improvement_backlog"]}
        self.assertTrue(
            {
                "goal_confirmation",
                "dynamic_plan_trace",
                "model_config_readiness",
                "skill_mcp_lifecycle",
                "document_delivery_suite",
                "file_upload_context_flow",
                "pptx_design_templates",
                "template_gallery_quality",
                "knowledge_citation_quality",
                "expert_mode_orchestration",
                "permission_and_secret_governance",
                "network_search_governance",
                "task_pause_resume_replay",
            }.issubset(backlog_ids)
        )
        for item in payload["improvement_backlog"]:
            self.assertIn(item["priority"], {"P0", "P1", "P2"})
            self.assertIn(item["status"], {"planned", "in_progress", "ready"})
            self.assertTrue(item["title"])
            self.assertTrue(item["detail"])
            self.assertTrue(item["reason"])
            self.assertTrue(item["next_step"])
            self.assertTrue(item["prompt"])
            self.assertIn(item["title"], item["prompt"])
            self.assertIn(item["next_step"], item["prompt"])
        self_test_ids = {item["id"] for item in payload["self_tests"]}
        self.assertTrue(
            {
                "basic_chat_context",
                "model_online_smoke",
                "upload_context_delivery",
                "document_docx_delivery",
                "document_pptx_delivery",
                "document_xlsx_delivery",
                "document_md_html_delivery",
                "knowledge_citation",
                "marketplace_install_flow",
                "expert_team_review",
                "web_search_guard",
                "permission_status_review",
            }.issubset(self_test_ids)
        )
        for item in payload["self_tests"]:
            self.assertIn(item["readiness"], {"ready", "needs_setup"})
            self.assertIn(item["workbench_mode"], {"agent", "expert"})
            self.assertTrue(item["category"])
            self.assertTrue(item["prompt"])
            self.assertTrue(item["expected"])
            self.assertIsInstance(item["artifacts"], list)
        expert_test = next(item for item in payload["self_tests"] if item["id"] == "expert_team_review")
        self.assertEqual(expert_test["workbench_mode"], "expert")
        self.assertIn(expert_test["readiness"], {"ready", "needs_setup"})
        ppt_test = next(item for item in payload["self_tests"] if item["id"] == "document_pptx_delivery")
        self.assertIn("pptx", ppt_test["artifacts"])
        self.assertEqual(payload["actionable_summary"]["total_self_tests"], len(payload["self_tests"]))
        self.assertEqual(payload["actionable_summary"]["p0_improvements"], len([item for item in payload["improvement_backlog"] if item["priority"] == "P0"]))

    def test_default_workspace_passes_without_mutating_business_data(self) -> None:
        before_tables = {
            "skills": self.client.get("/api/skills").json(),
            "mcp": self.client.get("/api/mcp").json(),
            "models": self.client.get("/api/models").json(),
        }
        response = self.client.get("/api/diagnostics")
        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        workspace_check = next(item for item in payload["checks"] if item["id"] == "workspace.default")
        self.assertEqual(workspace_check["status"], "pass")
        self.assertTrue(workspace_check["evidence"]["default_exists"])
        after_tables = {
            "skills": self.client.get("/api/skills").json(),
            "mcp": self.client.get("/api/mcp").json(),
            "models": self.client.get("/api/models").json(),
        }
        self.assertEqual(before_tables, after_tables)


if __name__ == "__main__":
    unittest.main()
