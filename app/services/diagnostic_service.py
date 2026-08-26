from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable

from app import db
from app.services.knowledge_base_service import KnowledgeBaseService
from app.services.workspace_service import WorkspaceService


DiagnosticStatus = str


def _status_rank(status: DiagnosticStatus) -> int:
    return {"pass": 0, "warn": 1, "fail": 2}.get(status, 1)


class DiagnosticService:
    """Read-only platform health checks for the local AgentNexus instance."""

    ALLOWED_ACTION_TABS = {"chat", "workspaces", "models", "skills", "mcp", "marketplace", "knowledge", "artifacts", "experts", "memory", "loops", "diagnostics"}
    CHECK_ACTION_TARGETS = {
        "database.integrity": {"tab": "diagnostics", "label": "查看明细"},
        "workspace.default": {"tab": "workspaces", "label": "查看项目"},
        "models.capability": {"tab": "models", "label": "配置模型"},
        "models.configured": {"tab": "models", "label": "配置模型"},
        "file.upload_context": {"tab": "chat", "label": "添加附件"},
        "security.permissions": {"tab": "diagnostics", "label": "查看安全自检"},
        "skill_mcp.capability": {"tab": "marketplace", "label": "查看 Skill/MCP 市场"},
        "skills.enabled": {"tab": "skills", "label": "管理 Skill"},
        "mcp.configured": {"tab": "mcp", "label": "配置 MCP"},
        "expert.capability": {"tab": "experts", "label": "配置专家团"},
        "expert.teams_configured": {"tab": "experts", "label": "配置专家团"},
        "memory.capability": {"tab": "memory", "label": "管理记忆"},
        "memory.configured": {"tab": "memory", "label": "管理记忆"},
        "automation.capability": {"tab": "loops", "label": "配置自动化"},
        "automation.configured": {"tab": "loops", "label": "配置自动化"},
        "knowledge.capability": {"tab": "knowledge", "label": "配置知识库"},
        "knowledge.index": {"tab": "knowledge", "label": "配置知识库"},
        "document.output_formats": {"tab": "artifacts", "label": "查看产物"},
        "artifacts.integrity": {"tab": "artifacts", "label": "查看产物"},
        "runtime.contract_capability": {"tab": "chat", "label": "发起任务"},
        "runtime.trace_records": {"tab": "chat", "label": "发起任务"},
        "runtime.active_runs": {"tab": "chat", "label": "回到工作台"},
        "network.capability": {"tab": "mcp", "label": "配置联网工具"},
        "network.search": {"tab": "mcp", "label": "配置联网搜索"},
    }

    def __init__(
        self,
        *,
        workspace_service: WorkspaceService,
        knowledge_service: KnowledgeBaseService,
        capabilities_provider: Callable[[], dict[str, Any]],
    ) -> None:
        self.workspace_service = workspace_service
        self.knowledge_service = knowledge_service
        self.capabilities_provider = capabilities_provider

    @classmethod
    def _action_target_for_check(cls, check_id: str) -> dict[str, str]:
        target = cls.CHECK_ACTION_TARGETS.get(str(check_id), {"tab": "diagnostics", "label": "查看明细"})
        tab = str(target.get("tab") or "")
        return {
            "tab": tab if tab in cls.ALLOWED_ACTION_TABS else "diagnostics",
            "label": str(target.get("label") or "去处理"),
        }

    @classmethod
    def _check(cls, check_id: str, title: str, status: DiagnosticStatus, detail: str, *, evidence: dict[str, Any] | None = None, action: str = "") -> dict[str, Any]:
        return {
            "id": check_id,
            "title": title,
            "status": status,
            "detail": detail,
            "evidence": evidence or {},
            "action": action,
            "action_target": cls._action_target_for_check(check_id),
        }

    @staticmethod
    def _count(table: str, where: str = "", params: tuple[Any, ...] = ()) -> int:
        suffix = f" WHERE {where}" if where else ""
        row = db.query_one(f"SELECT COUNT(*) AS total FROM {table}{suffix}", params)  # noqa: S608 - table names are fixed by callers
        return int((row or {}).get("total") or 0)

    @staticmethod
    def _readiness_item(
        item_id: str,
        title: str,
        status: DiagnosticStatus,
        detail: str,
        *,
        action: str = "",
        capability: str = "",
        action_tab: str = "",
        action_label: str = "",
    ) -> dict[str, Any]:
        state = "ready" if status == "pass" else "blocked" if status == "fail" else "needs_config"
        action_target = {
            "tab": action_tab if action_tab in DiagnosticService.ALLOWED_ACTION_TABS else "",
            "label": action_label or "去处理",
        }
        return {
            "id": item_id,
            "title": title,
            "state": state,
            "status": status,
            "detail": detail,
            "action": action,
            "capability": capability or item_id,
            "action_target": action_target if action_target["tab"] else None,
        }

    def _readiness(self, checks: list[dict[str, Any]], capabilities: dict[str, Any]) -> list[dict[str, Any]]:
        by_id = {str(item.get("id") or ""): item for item in checks}

        def check_status(*ids: str) -> DiagnosticStatus:
            found = [by_id[item]["status"] for item in ids if item in by_id]
            if not found:
                return "warn"
            if any(item == "fail" for item in found):
                return "fail"
            if any(item == "warn" for item in found):
                return "warn"
            return "pass"

        document_output = capabilities.get("document_output") if isinstance(capabilities, dict) else {}
        pptx_configured = bool(document_output.get("pptx_configured")) if isinstance(document_output, dict) else False
        web_search = capabilities.get("web_search") if isinstance(capabilities, dict) else {}
        remote_mcp = capabilities.get("remote_mcp") if isinstance(capabilities, dict) else {}
        stdio_mcp = capabilities.get("stdio_mcp") if isinstance(capabilities, dict) else {}
        remote_install = capabilities.get("remote_install") if isinstance(capabilities, dict) else {}

        return [
            self._readiness_item(
                "core_platform",
                "平台基础",
                check_status("database.integrity", "workspace.default"),
                "数据库和默认项目可用" if check_status("database.integrity", "workspace.default") == "pass" else "数据库或默认项目需要处理",
                action="先处理数据库完整性和默认项目问题，再继续配置业务能力。",
                capability="database/workspace",
                action_tab="workspaces",
                action_label="查看项目",
            ),
            self._readiness_item(
                "model_runtime",
                "模型运行",
                check_status("models.capability", "models.configured"),
                (
                    f"{by_id.get('models.capability', {}).get('detail', '模型配置能力未确认')}；"
                    f"{by_id.get('models.configured', {}).get('detail', '尚未检查模型配置')}"
                ),
                action=by_id.get("models.configured", {}).get("action", "") or by_id.get("models.capability", {}).get("action", ""),
                capability="models",
                action_tab="models",
                action_label="配置模型",
            ),
            self._readiness_item(
                "skill_mcp",
                "Skill 与 MCP",
                check_status("skill_mcp.capability", "skills.enabled", "mcp.configured"),
                (
                    f"{by_id.get('skill_mcp.capability', {}).get('detail', 'Skill/MCP 基础能力未确认')}；"
                    f"{'Skill 可用，工具服务按需启用' if check_status('skills.enabled', 'mcp.configured') == 'pass' else 'Skill 或 MCP 尚未达到完整可用状态'}"
                ),
                action=by_id.get("skill_mcp.capability", {}).get("action", "") or "先保证常用 Skill 启用，再按 Agent 权限绑定需要的 MCP 工具。",
                capability="skills/mcp",
                action_tab="skills",
                action_label="管理 Skill",
            ),
            self._readiness_item(
                "expert_mode",
                "专家模式",
                check_status("expert.capability", "expert.teams_configured"),
                (
                    f"{by_id.get('expert.capability', {}).get('detail', '专家模式能力未确认')}；"
                    f"{by_id.get('expert.teams_configured', {}).get('detail', '尚未检查专家团配置')}"
                ),
                action=by_id.get("expert.teams_configured", {}).get("action", "") or by_id.get("expert.capability", {}).get("action", ""),
                capability="expert_team",
                action_tab="experts",
                action_label="配置专家团",
            ),
            self._readiness_item(
                "memory_context",
                "上下文记忆",
                check_status("memory.capability", "memory.configured"),
                (
                    f"{by_id.get('memory.capability', {}).get('detail', '记忆能力未确认')}；"
                    f"{by_id.get('memory.configured', {}).get('detail', '尚未检查记忆配置')}"
                ),
                action=by_id.get("memory.configured", {}).get("action", "") or by_id.get("memory.capability", {}).get("action", ""),
                capability="memory",
                action_tab="memory",
                action_label="管理记忆",
            ),
            self._readiness_item(
                "automation",
                "自动化",
                check_status("automation.capability", "automation.configured"),
                (
                    f"{by_id.get('automation.capability', {}).get('detail', '自动化能力未确认')}；"
                    f"{by_id.get('automation.configured', {}).get('detail', '尚未检查自动化配置')}"
                ),
                action=by_id.get("automation.configured", {}).get("action", "") or by_id.get("automation.capability", {}).get("action", ""),
                capability="automation",
                action_tab="loops",
                action_label="配置自动化",
            ),
            self._readiness_item(
                "knowledge",
                "知识库",
                check_status("knowledge.capability", "knowledge.index"),
                (
                    f"{by_id.get('knowledge.capability', {}).get('detail', '知识库能力未确认')}；"
                    f"{by_id.get('knowledge.index', {}).get('detail', '尚未建立索引')}"
                ),
                action=by_id.get("knowledge.index", {}).get("action", "") or by_id.get("knowledge.capability", {}).get("action", ""),
                capability="knowledge_base",
                action_tab="knowledge",
                action_label="配置知识库",
            ),
            self._readiness_item(
                "file_input",
                "文件输入",
                check_status("file.upload_context"),
                by_id.get("file.upload_context", {}).get("detail", "尚未检查文件上传能力"),
                action=by_id.get("file.upload_context", {}).get("action", ""),
                capability="file_upload",
                action_tab="chat",
                action_label="上传附件",
            ),
            self._readiness_item(
                "documents",
                "文档交付",
                "pass" if check_status("artifacts.integrity", "document.output_formats") == "pass" and pptx_configured else "warn" if check_status("artifacts.integrity", "document.output_formats") == "pass" else "fail",
                by_id.get("document.output_formats", {}).get("detail", "尚未检查文档输出格式"),
                action=by_id.get("document.output_formats", {}).get("action", "PPTX 默认使用平台内置 Python 生成器；如需外部 Artifact Tool，再配置 APP_NODE_BINARY 和 APP_ARTIFACT_TOOL_ENTRYPOINT，并运行一次生成测试。"),
                capability="artifacts",
                action_tab="artifacts",
                action_label="查看产物",
            ),
            self._readiness_item(
                "network_tools",
                "联网与远程能力",
                check_status("network.capability", "network.search"),
                (
                    f"{by_id.get('network.capability', {}).get('detail', '联网基础能力未确认')}；"
                    f"{by_id.get('network.search', {}).get('detail', '尚未检查搜索配置')}"
                ),
                action=by_id.get("network.search", {}).get("action", "") or by_id.get("network.capability", {}).get("action", ""),
                capability="network",
                action_tab="mcp",
                action_label="配置工具",
            ),
            self._readiness_item(
                "local_tools",
                "本地工具执行",
                "pass" if bool(stdio_mcp.get("enabled")) else "warn",
                "允许启动白名单内 stdio MCP" if bool(stdio_mcp.get("enabled")) else "本地 stdio MCP 默认关闭，普通问答和内置工具不受影响",
                action="仅对可信命令开启 APP_ALLOW_STDIO_MCP 和 APP_STDIO_COMMAND_ALLOWLIST。",
                capability="stdio_mcp",
                action_tab="mcp",
                action_label="配置 MCP",
            ),
            self._readiness_item(
                "security_governance",
                "权限与安全",
                check_status("security.permissions"),
                by_id.get("security.permissions", {}).get("detail", "尚未检查权限与安全边界"),
                action=by_id.get("security.permissions", {}).get("action", ""),
                capability="security",
                action_tab="diagnostics",
                action_label="查看自检",
            ),
            self._readiness_item(
                "install_flow",
                "市场安装",
                "pass" if bool(remote_install.get("enabled")) else "warn",
                "支持从远程链接安装 Skill/MCP" if bool(remote_install.get("enabled")) else "本地创建、上传安装可用；远程链接安装默认关闭",
                action="需要从 GitHub/raw URL 安装时，开启 APP_ALLOW_REMOTE_INSTALL 并配置主机白名单。",
                capability="marketplace",
                action_tab="marketplace",
                action_label="打开市场",
            ),
            self._readiness_item(
                "runtime_recovery",
                "任务运行",
                check_status("runtime.contract_capability", "runtime.trace_records", "runtime.active_runs"),
                (
                    f"{by_id.get('runtime.contract_capability', {}).get('detail', '运行闭环能力未确认')}；"
                    f"{by_id.get('runtime.trace_records', {}).get('detail', '尚未检查运行证据记录')}；"
                    f"{by_id.get('runtime.active_runs', {}).get('detail', '尚未检查运行状态')}"
                ),
                action=by_id.get("runtime.active_runs", {}).get("action", "") or by_id.get("runtime.contract_capability", {}).get("action", ""),
                capability="runtime",
                action_tab="chat",
                action_label="回到工作台",
            ),
        ]

    @staticmethod
    def _self_test(
        test_id: str,
        title: str,
        prompt: str,
        expected: list[str],
        *,
        category: str = "core",
        workbench_mode: str = "agent",
        readiness: str = "ready",
        requires: list[str] | None = None,
        setup: str = "",
        artifacts: list[str] | None = None,
    ) -> dict[str, Any]:
        return {
            "id": test_id,
            "title": title,
            "category": category,
            "workbench_mode": "expert" if workbench_mode == "expert" else "agent",
            "readiness": readiness,
            "requires": requires or [],
            "prompt": prompt,
            "expected": expected,
            "setup": setup,
            "artifacts": artifacts or [],
        }

    def _self_tests(self, readiness: list[dict[str, Any]]) -> list[dict[str, Any]]:
        by_id = {str(item.get("id") or ""): item for item in readiness}

        def ready(*ids: str) -> bool:
            return all(by_id.get(item, {}).get("state") == "ready" for item in ids)

        return [
            self._self_test(
                "basic_chat_context",
                "普通对话与上下文",
                "你好。请用一句话说明你现在能帮我做什么。",
                ["工作台出现流式草稿", "最终答复通过验收后发布", "右侧执行证据显示当前节点和模型"],
                category="对话",
                readiness="ready" if ready("core_platform", "runtime_recovery") else "needs_setup",
                requires=["平台基础", "任务运行"],
            ),
            self._self_test(
                "model_online_smoke",
                "在线模型可用性",
                "请用三条要点解释 AgentNexus 的用途。",
                ["发送前模型不被拦截", "模型卡片最近测试通过", "回答不是离线确定性提示"],
                category="模型",
                readiness="ready" if ready("model_runtime") else "needs_setup",
                requires=["模型运行"],
                setup="先在模型设置中配置真实模型并测试连接。",
            ),
            self._self_test(
                "upload_context_delivery",
                "附件上传、解析与上下文",
                "我会上传一份资料。请先总结附件核心内容，再把总结整理成 Markdown 文档并提供下载。",
                ["附件上传后显示解析状态", "执行证据显示读取了附件上下文", "最终回答基于附件内容并出现 MD 下载入口"],
                category="文件",
                readiness="ready" if ready("file_input", "runtime_recovery", "documents") else "needs_setup",
                requires=["文件输入", "任务运行", "文档交付"],
                setup="先在工作台添加一个 txt/md/pdf/docx/xlsx/pptx 附件，再发送该任务。",
                artifacts=["md"],
            ),
            self._self_test(
                "document_docx_delivery",
                "Word 文档生成与下载",
                "请把 AgentNexus 的核心能力整理成一份 Word 报告，要求包含标题、摘要和行动建议，并提供下载。",
                ["执行计划进入文件生成节点", "最终回答和执行证据中出现 DOCX 下载入口", "产物页能预览或下载文件"],
                category="文档",
                readiness="ready" if ready("documents", "runtime_recovery") else "needs_setup",
                requires=["文档交付", "任务运行"],
                artifacts=["docx"],
            ),
            self._self_test(
                "document_pptx_delivery",
                "PPT 生成与下载",
                "请生成一份 5 页 PPT，主题是 AgentNexus 平台能力介绍，要求包含封面、能力架构、典型流程、落地建议和总结页，并提供下载。",
                ["执行计划识别 PPTX 交付物", "如 PPTX 工具未配置，会明确提示缺失能力或推荐安装", "配置完成后产物页出现 PPTX 下载入口"],
                category="文档",
                readiness="ready" if ready("documents", "runtime_recovery") else "needs_setup",
                requires=["文档交付", "任务运行"],
                setup="PPTX 默认使用平台内置 Python 生成器；如切换外部 Artifact Tool，才需要 Node.js。未配置时应触发友好缺能力提示。",
                artifacts=["pptx"],
            ),
            self._self_test(
                "document_xlsx_delivery",
                "Excel 表格生成与下载",
                "请生成一份 Excel 表格，对比 AgentNexus 的普通模式和专家模式，包含能力、适用场景、优点、风险和建议使用方式，并提供下载。",
                ["执行计划识别 XLSX 交付物", "最终回答和执行证据中出现 XLSX 下载入口", "表格至少包含表头和 3 行以上内容"],
                category="文档",
                readiness="ready" if ready("documents", "runtime_recovery") else "needs_setup",
                requires=["文档交付", "任务运行"],
                artifacts=["xlsx"],
            ),
            self._self_test(
                "document_md_html_delivery",
                "Markdown / HTML 输出",
                "请把 AgentNexus 的使用说明整理成 Markdown 和 HTML 两种格式，要求包含目录、快速开始、模型配置、Skill/MCP 安装和常见问题，并提供可下载文件。",
                ["执行计划识别 MD 与 HTML 交付物", "回答中的 Markdown 被渲染而不是源码堆叠", "产物页出现 MD/HTML 下载入口"],
                category="文档",
                readiness="ready" if ready("documents", "runtime_recovery") else "needs_setup",
                requires=["文档交付", "任务运行"],
                artifacts=["md", "html"],
            ),
            self._self_test(
                "knowledge_citation",
                "知识库检索与引用",
                "根据当前知识库资料，回答 AgentNexus 的定位，并列出引用到的文档名。",
                ["时间线出现知识引用卡片", "执行证据显示知识引用数量", "回答包含文档名或片段编号"],
                category="知识库",
                readiness="ready" if ready("knowledge", "runtime_recovery") else "needs_setup",
                requires=["知识库", "任务运行"],
                setup="先在知识库中上传并索引至少一份包含 AgentNexus 信息的文档。",
            ),
            self._self_test(
                "marketplace_install_flow",
                "Skill/MCP 市场安装",
                "请帮我生成一个 Mermaid 流程图。",
                ["如缺少 Skill，会出现安装确认或可去市场安装", "市场卡片显示安装影响和权限", "安装后技能中心能看到该 Skill"],
                category="Skill/MCP",
                readiness="ready" if ready("skill_mcp", "install_flow") else "needs_setup",
                requires=["Skill 与 MCP", "市场安装"],
            ),
            self._self_test(
                "expert_team_review",
                "专家模式评审",
                "评审这个上线方案：本周发布新工作台。请从产品、技术和安全三个角度给出 P0/P1/P2 建议。",
                ["切换专家模式后选择或自动匹配专家团", "时间线显示专家选择和并行成员", "主管汇总后出现验收结果"],
                category="专家团",
                workbench_mode="expert",
                readiness="ready" if ready("expert_mode") else "needs_setup",
                requires=["专家模式"],
                setup="先在专家团中创建并启用至少一个包含多名成员的团队。",
            ),
            self._self_test(
                "web_search_guard",
                "联网搜索与偏航防护",
                "查一下 AgentNexus 相关开源智能体平台的最新趋势，并给出来源链接。",
                ["联网搜索能力可用时调用 web-search", "回答保留来源 URL", "未配置联网时任务会明确提示能力不可用"],
                category="联网",
                readiness="ready" if ready("network_tools") else "needs_setup",
                requires=["联网与远程能力"],
                setup="配置搜索 Provider、APP_ALLOW_WEB_SEARCH 和出站网络白名单。",
            ),
            self._self_test(
                "permission_status_review",
                "权限开关与密钥脱敏",
                "请检查当前平台的联网搜索、远程安装、本地 MCP、HTTP 工具和模型密钥配置状态，只输出是否启用、配置入口和风险提示，不要输出任何密钥内容。",
                ["回答只展示开关状态和配置入口", "不出现明文密钥、密文或本地文件路径", "未开启能力给出友好配置建议"],
                category="安全",
                readiness="ready" if ready("security_governance") else "needs_setup",
                requires=["权限与安全"],
            ),
        ]

    @staticmethod
    def _improvement_item(
        item_id: str,
        priority: str,
        title: str,
        detail: str,
        *,
        reason: str,
        next_step: str,
        capability: str = "",
        status: str = "planned",
    ) -> dict[str, Any]:
        normalized_priority = priority if priority in {"P0", "P1", "P2"} else "P2"
        normalized_status = status if status in {"planned", "in_progress", "ready"} else "planned"
        prompt = (
            f"请优化 AgentNexus 的“{title}”能力。\n\n"
            f"当前问题：{detail}\n"
            f"为什么要做：{reason}\n"
            f"建议下一步：{next_step}\n\n"
            "要求：先检查当前实现，再设计最小可落地方案；完成后必须自己验证，包括相关单测、前端语法检查或接口检查；不要只说明思路。"
        )
        return {
            "id": item_id,
            "priority": normalized_priority,
            "title": title,
            "detail": detail,
            "reason": reason,
            "next_step": next_step,
            "capability": capability or item_id,
            "status": normalized_status,
            "prompt": prompt,
        }

    def _improvement_backlog(self, readiness: list[dict[str, Any]], capabilities: dict[str, Any]) -> list[dict[str, Any]]:
        by_id = {str(item.get("id") or ""): item for item in readiness}

        def state(item_id: str) -> str:
            return str(by_id.get(item_id, {}).get("state") or "needs_config")

        document_output = capabilities.get("document_output") if isinstance(capabilities, dict) else {}
        pptx_configured = bool(document_output.get("pptx_configured")) if isinstance(document_output, dict) else False
        suggestions: list[dict[str, Any]] = [
            self._improvement_item(
                "goal_confirmation",
                "P0",
                "目标确认与偏航防护",
                "复杂任务需要在执行前形成目标合同，输出前按目标合同校验，避免答非所问或误调用无关工具。",
                reason="这是解决“整理文档却去查天气”等偏航问题的核心保护层。",
                next_step="将目标合同、动态计划和最终验收结果固定展示在运行证据区，并允许用户在执行前确认或修正。",
                capability="runtime_contract",
                status="in_progress",
            ),
            self._improvement_item(
                "dynamic_plan_trace",
                "P0",
                "动态执行计划与子级下钻",
                "大节点保持稳定，小节点按任务动态生成，并展示实际调用的 Skill、MCP、模型和产物。",
                reason="用户需要知道执行到哪一步、调用了什么能力、失败在哪个节点。",
                next_step="继续完善运行时 trace，将每个计划节点绑定调用证据、输入摘要、输出摘要和失败恢复建议。",
                capability="runtime_trace",
                status="in_progress",
            ),
            self._improvement_item(
                "model_config_readiness",
                "P0",
                "模型配置可见性与连通性",
                "模型保存后必须看到已配置模型、密钥来源、能力标签、最近测试和不可用原因。",
                reason="模型不可选、保存没反馈会直接阻断平台使用。",
                next_step="补齐模型 Provider 的能力探测和流式/工具调用 smoke test，把失败原因展示在模型卡片和发送拦截提示中。",
                capability="models",
                status="ready" if state("model_runtime") == "ready" else "in_progress",
            ),
            self._improvement_item(
                "skill_mcp_lifecycle",
                "P1",
                "Skill/MCP 生命周期管理",
                "安装前展示权限、依赖和影响；安装后展示已安装、可用、缺配置、测试失败、已禁用等状态。",
                reason="平台级产品必须让用户知道能力从哪里来、是否装好、是否安全可用。",
                next_step="为每个 Skill/MCP 增加测试动作、状态证据、权限说明和对话中推荐安装确认流。",
                capability="skills/mcp",
                status="in_progress" if state("skill_mcp") != "blocked" else "planned",
            ),
            self._improvement_item(
                "document_delivery_suite",
                "P1",
                "多格式文档交付闭环",
                "支持 Word、Excel、PPT、Markdown、HTML、PDF 的生成、预览、下载、失败提示和重新生成。",
                reason="用户经常要求输出文档，只有文本回答不符合平台定位。",
                next_step="把文档生成能力拆成格式级自测；PPTX 重点补模板、美化和下载校验。",
                capability="artifacts",
                status="in_progress" if state("documents") == "ready" else "planned",
            ),
            self._improvement_item(
                "file_upload_context_flow",
                "P1",
                "文件上传解析与上下文注入",
                "附件上传后需要展示解析状态、可读内容摘要、失败原因，并确认后续任务确实引用了附件上下文。",
                reason="用户默认会把资料、表格、PDF、PPT 发给平台处理；如果附件没有进上下文，最终回答会失真。",
                next_step="为上传文件增加格式级解析状态、上下文引用证据和附件回归用例；失败时提示可转换格式或转入知识库。",
                capability="file_upload",
                status="in_progress",
            ),
            self._improvement_item(
                "pptx_design_templates",
                "P1",
                "PPT 模板与视觉质量",
                "PPT 不能只做到能生成，需要内置封面、目录、章节页、图表页和总结页模板。",
                reason="PPT 太丑通常不是模型问题，而是缺少结构化模板和渲染规范。",
                next_step="基于 python-pptx 或 Node artifact 工具内置 2-3 套主题模板，并增加 PPTX 视觉回归样例。",
                capability="pptx",
                status="ready" if pptx_configured else "planned",
            ),
            self._improvement_item(
                "template_gallery_quality",
                "P1",
                "文档模板库与质量校验",
                "常见 Word、PPT、HTML 报告需要模板、主题、排版规则和最小视觉验收，而不是每次从空白生成。",
                reason="文档能下载只是底线，真正可用还取决于版式、层级、图表和品牌一致性。",
                next_step="内置报告、方案、复盘、路演四类模板，并为 PPT/HTML 增加截图或结构化质量检查。",
                capability="templates",
                status="planned",
            ),
            self._improvement_item(
                "knowledge_citation_quality",
                "P1",
                "知识库引用质量",
                "知识库回答需要展示命中文档、片段编号、引用数量，并能测试检索是否命中。",
                reason="没有引用证据，用户无法判断回答是否基于上传资料。",
                next_step="完善知识库检索测试、引用卡片和回答中的来源格式；缺知识库时给出创建入口。",
                capability="knowledge_base",
                status="in_progress" if state("knowledge") == "ready" else "planned",
            ),
            self._improvement_item(
                "expert_mode_orchestration",
                "P2",
                "专家模式自动编排",
                "普通模式自动选 Skill/MCP；专家模式根据复杂度自动选专家，并展示专家意见摘要和分歧。",
                reason="用户不应该每次手动决定专家团，平台应根据任务复杂度自动组织协作。",
                next_step="补专家选择解释、专家贡献卡片、冲突检查和最终采纳理由。",
                capability="expert_team",
                status="in_progress",
            ),
            self._improvement_item(
                "permission_and_secret_governance",
                "P2",
                "权限确认与密钥脱敏",
                "即使暂时不加沙箱，也需要对联网、命令执行、文件写入、远程安装、密钥展示和日志导出做权限分级。",
                reason="平台会接入本地工具和远程服务，不做权限边界会让用户难以判断风险。",
                next_step="建立工具权限标签、敏感字段脱敏、导出报告脱敏和高风险动作二次确认。",
                capability="security_governance",
                status="planned",
            ),
            self._improvement_item(
                "network_search_governance",
                "P2",
                "联网搜索治理",
                "按任务判断是否需要联网，展示搜索词、来源链接和未配置时的友好降级。",
                reason="联网能力涉及时效性、来源可信度和权限控制，不能黑盒调用。",
                next_step="补搜索开关、来源引用、搜索失败原因和推荐配置说明。",
                capability="network",
                status="planned" if state("network_tools") != "ready" else "in_progress",
            ),
            self._improvement_item(
                "task_pause_resume_replay",
                "P2",
                "任务暂停、继续与回放",
                "长任务需要支持用户暂停、修正目标、继续执行，并能回放关键节点和最终交付物。",
                reason="平台级智能体常会跑多步任务；没有恢复和回放，用户无法审计过程，也难以接着改。",
                next_step="为任务运行状态增加暂停/继续动作、节点快照、失败恢复入口和历史回放视图。",
                capability="runtime_recovery",
                status="planned",
            ),
        ]
        order = {"P0": 0, "P1": 1, "P2": 2}
        status_order = {"in_progress": 0, "planned": 1, "ready": 2}
        return sorted(suggestions, key=lambda item: (order[item["priority"]], status_order[item["status"]], item["id"]))

    @staticmethod
    def _next_actions(readiness: list[dict[str, Any]], improvement_backlog: list[dict[str, Any]]) -> list[dict[str, Any]]:
        actions: list[dict[str, Any]] = []
        readiness_order = {"blocked": 0, "needs_config": 1, "ready": 2}
        for item in sorted(readiness, key=lambda entry: (readiness_order.get(str(entry.get("state") or ""), 3), str(entry.get("id") or ""))):
            state = str(item.get("state") or "")
            target = item.get("action_target") if isinstance(item.get("action_target"), dict) else {}
            if state == "ready" or not target.get("tab"):
                continue
            actions.append({
                "id": f"readiness:{item.get('id')}",
                "kind": "readiness",
                "ref_id": item.get("id"),
                "priority": "P0" if state == "blocked" else "P1",
                "title": f"处理能力：{item.get('title')}",
                "detail": item.get("action") or item.get("detail") or "",
                "action_label": target.get("label") or "去处理",
                "action_type": "navigate",
                "target_tab": target.get("tab") or "",
                "prompt": "",
                "status": state,
            })
        for item in improvement_backlog:
            if item.get("priority") != "P0":
                continue
            actions.append({
                "id": f"improvement:{item.get('id')}",
                "kind": "improvement",
                "ref_id": item.get("id"),
                "priority": item.get("priority"),
                "title": f"推进优化：{item.get('title')}",
                "detail": item.get("next_step") or item.get("detail") or "",
                "action_label": "填入工作台",
                "action_type": "prompt",
                "target_tab": "chat",
                "prompt": item.get("prompt") or "",
                "status": item.get("status") or "planned",
            })
        priority_order = {"P0": 0, "P1": 1, "P2": 2}
        status_order = {"blocked": 0, "needs_config": 1, "in_progress": 2, "planned": 3, "ready": 4}
        return sorted(actions, key=lambda item: (priority_order.get(str(item.get("priority") or ""), 9), status_order.get(str(item.get("status") or ""), 9), str(item.get("id") or "")))[:5]

    @staticmethod
    def _issues(checks: list[dict[str, Any]]) -> list[dict[str, Any]]:
        severity_labels = {"fail": "blocking", "warn": "attention"}
        issues = [
            {
                "id": f"issue:{item.get('id')}",
                "check_id": item.get("id"),
                "severity": severity_labels.get(str(item.get("status") or ""), "attention"),
                "status": item.get("status"),
                "title": item.get("title"),
                "detail": item.get("detail") or "",
                "action": item.get("action") or "查看检查明细并按提示处理。",
                "action_target": item.get("action_target") if isinstance(item.get("action_target"), dict) else {"tab": "diagnostics", "label": "查看明细"},
                "fix_prompt": (
                    f"请处理 AgentNexus 自检问题“{item.get('title') or item.get('id')}”。\n\n"
                    f"检查项：{item.get('id')}\n"
                    f"状态：{item.get('status')}\n"
                    f"问题描述：{item.get('detail') or ''}\n"
                    f"处理建议：{item.get('action') or '查看检查明细并按提示处理。'}\n\n"
                    "要求：先定位当前实现和配置状态，再给出最小可落地修复；如果能在代码内修复就实现并验证，如果需要用户配置则给出明确配置位置和验证方式。"
                ),
            }
            for item in checks
            if item.get("status") in {"fail", "warn"}
        ]
        severity_order = {"blocking": 0, "attention": 1}
        return sorted(issues, key=lambda item: (severity_order.get(str(item.get("severity") or ""), 9), str(item.get("check_id") or "")))

    @staticmethod
    def _report_markdown(
        *,
        overall: str,
        summary: dict[str, Any],
        actionable_summary: dict[str, Any],
        self_test_summary: dict[str, Any],
        issues: list[dict[str, Any]],
        next_actions: list[dict[str, Any]],
        readiness: list[dict[str, Any]],
        checks: list[dict[str, Any]],
        improvement_backlog: list[dict[str, Any]],
        self_tests: list[dict[str, Any]],
        generated_at: str,
        schema_version: str,
    ) -> str:
        overall_labels = {"pass": "通过", "warn": "提醒", "fail": "失败"}
        readiness_labels = {"ready": "可使用", "needs_config": "待配置", "blocked": "需处理"}
        action_lines = [
            f"- [{item.get('priority') or 'P1'}] {item.get('title') or item.get('id')}：{item.get('detail') or item.get('action_label') or ''}".rstrip("：")
            for item in next_actions
        ] or ["- 暂无必须优先处理的事项"]
        issue_lines = [
            (
                f"- [{item.get('severity')}] {item.get('title') or item.get('check_id')}：{item.get('detail') or ''}\n"
                f"  - 处理建议：{item.get('action') or ''}\n"
                f"  - 页面入口：{(item.get('action_target') or {}).get('label') or '查看明细'}\n"
                "  - 可在问题清单点击“填入修复任务”转成工作台任务"
            ).rstrip()
            for item in issues
        ] or ["- 暂无失败或提醒项"]
        readiness_lines = [
            f"- {item.get('title') or item.get('id')}：{readiness_labels.get(str(item.get('state') or ''), item.get('state') or '未知')}。{item.get('detail') or ''}".rstrip()
            for item in readiness
        ]
        checks_by_id = {str(item.get("id") or ""): item for item in checks}

        def usage_line(label: str, check_id: str, entry: str) -> str:
            item = checks_by_id.get(check_id, {})
            status = overall_labels.get(str(item.get("status") or ""), item.get("status") or "未知")
            detail = item.get("detail") or "尚未检查"
            return f"- {label}：{status}。{detail}。入口：{entry}"

        default_capability_lines = [
            usage_line("模型配置", "models.capability", "模型设置"),
            usage_line("文件上传", "file.upload_context", "工作台 → 添加附件"),
            usage_line("文档输出", "document.output_formats", "工作台或产物页"),
            usage_line("Skill/MCP", "skill_mcp.capability", "技能中心 / 工具接入 / 市场"),
            usage_line("知识库", "knowledge.capability", "知识库"),
            usage_line("联网与远程", "network.capability", "工具接入 / 环境变量开关"),
            usage_line("上下文记忆", "memory.capability", "记忆"),
            usage_line("专家模式", "expert.capability", "专家团 / 工作台模式切换"),
            usage_line("自动化", "automation.capability", "自动化"),
            usage_line("执行闭环", "runtime.contract_capability", "工作台右侧运行控制"),
            usage_line("权限安全", "security.permissions", "自检 / 模型设置 / 工具接入"),
        ]
        improvement_lines = [
            f"- [{item.get('priority') or 'P2'}] {item.get('title') or item.get('id')}：{item.get('next_step') or item.get('detail') or ''}".rstrip()
            for item in improvement_backlog[:8]
        ]
        ready_tests = [item for item in self_tests if item.get("readiness") == "ready"]
        needs_setup_tests = [item for item in self_tests if item.get("readiness") != "ready"]
        self_test_lines = [
            (
                f"- [{item.get('category') or '未分类'} / {'可直接测' if item.get('readiness') == 'ready' else '需准备'}] "
                f"{item.get('title') or item.get('id')}"
                f"{'；产物：' + '、'.join(item.get('artifacts') or []) if item.get('artifacts') else ''}"
                f"{'；准备：' + str(item.get('setup')) if item.get('setup') else ''}"
            )
            for item in self_tests
        ] or ["- 暂无建议自测"]
        return "\n".join([
            "# AgentNexus 平台自检报告",
            "",
            f"- 生成时间：{generated_at}",
            f"- 报告版本：{schema_version}",
            f"- 整体状态：{overall_labels.get(overall, overall)}",
            f"- 检查项：通过 {int(summary.get('passed') or 0)} / 提醒 {int(summary.get('warnings') or 0)} / 失败 {int(summary.get('failed') or 0)}",
            f"- 能力状态：可用 {int(actionable_summary.get('ready_capabilities') or 0)} / 待配置 {int(actionable_summary.get('needs_config_capabilities') or 0)} / 阻塞 {int(actionable_summary.get('blocked_capabilities') or 0)}",
            f"- 优化建议：P0 {int(actionable_summary.get('p0_improvements') or 0)} / P1 {int(actionable_summary.get('p1_improvements') or 0)} / P2 {int(actionable_summary.get('p2_improvements') or 0)}",
            f"- 建议自测：可直接测 {len(ready_tests)} / 需准备 {len(needs_setup_tests)}",
            f"- 自测分类：{', '.join(f'{key} {value}' for key, value in (self_test_summary.get('by_category') or {}).items()) or '暂无'}",
            f"- 自测产物：{', '.join(f'{key} {value}' for key, value in (self_test_summary.get('by_artifact') or {}).items()) or '暂无'}",
            "",
            "## 问题清单",
            *issue_lines,
            "",
            "## 优先处理事项",
            *action_lines,
            "",
            "## 默认能力与使用入口",
            *default_capability_lines,
            "",
            "## 能力矩阵",
            *readiness_lines,
            "",
            "## 平台优化建议",
            *improvement_lines,
            "",
            "## 建议自测",
            *self_test_lines,
            "",
            "## 说明",
            "- 本报告来自只读自检，不执行外部网络请求，也不改写业务数据。",
            "- 报告不包含 API Key、文件路径、知识库原文或模型密钥。",
        ]).strip()

    def run(self, *, organization_id: str = "local-org", workspace_id: str = "default", user_id: str = "local-user") -> dict[str, Any]:
        checks: list[dict[str, Any]] = []
        capabilities = self.capabilities_provider()

        try:
            integrity = db.query_one("PRAGMA integrity_check")
            value = str(next(iter(dict(integrity or {}).values()), "unknown"))
            checks.append(self._check("database.integrity", "数据库完整性", "pass" if value == "ok" else "fail", value, evidence={"result": value}))
        except Exception as exc:
            checks.append(self._check("database.integrity", "数据库完整性", "fail", "无法执行数据库完整性检查", evidence={"error": type(exc).__name__}))

        workspaces = self.workspace_service.list_workspaces({"organization_id": organization_id, "workspace_id": workspace_id, "user_id": user_id}, include_disabled=True)
        active_workspaces = [item for item in workspaces if item.get("enabled")]
        default_workspace = next((item for item in workspaces if item.get("id") == "default"), None)
        checks.append(self._check(
            "workspace.default",
            "默认项目",
            "pass" if default_workspace and default_workspace.get("enabled") else "fail",
            "默认项目可用" if default_workspace and default_workspace.get("enabled") else "默认项目缺失或已停用",
            evidence={"workspace_count": len(active_workspaces), "default_exists": bool(default_workspace)},
            action="重启平台会自动补建默认项目；若仍失败，需要检查数据库写权限。",
        ))

        model_providers = capabilities.get("models") if isinstance(capabilities, dict) else []
        model_providers = model_providers if isinstance(model_providers, list) else []
        direct_key = capabilities.get("direct_api_key") if isinstance(capabilities, dict) else {}
        model_capability_ready = "deterministic" in model_providers and bool(direct_key.get("supported"))
        checks.append(self._check(
            "models.capability",
            "模型配置基础能力",
            "pass" if model_capability_ready else "fail",
            (
                f"模型配置支持 {', '.join(str(item) for item in model_providers)}；密钥支持环境变量和本机加密保存"
                if model_capability_ready
                else "模型配置基础能力不可用"
            ),
            evidence={
                "providers": model_providers,
                "offline_fallback": "deterministic" in model_providers,
                "env_secret_supported": True,
                "direct_secret_supported": bool(direct_key.get("supported")) if isinstance(direct_key, dict) else False,
                "direct_secret_encrypted": bool(direct_key.get("encrypted")) if isinstance(direct_key, dict) else False,
                "direct_secret_storage": str(direct_key.get("storage") or "") if isinstance(direct_key, dict) else "",
            },
            action="在“模型设置”中可选择环境变量或直接密钥；保存后模型列表应显示密钥来源、能力标签和测试结果。",
        ))

        model_total = self._count("model_configs")
        model_enabled = self._count("model_configs", "enabled = 1")
        configured_online = self._count(
            "model_configs",
            "enabled = 1 AND id != 'deterministic' AND (api_key_env != '' OR api_key_ciphertext != '')",
        )
        checks.append(self._check(
            "models.configured",
            "模型配置",
            "pass" if configured_online else "warn",
            "已有可调用在线模型" if configured_online else "仅检测到离线/未配置密钥模型，真实问答可能只能返回确定性提示",
            evidence={"total": model_total, "enabled": model_enabled, "configured_online": configured_online},
            action="到“模型设置”添加在线模型，并配置环境变量或直接 API Key。",
        ))

        file_upload = capabilities.get("file_upload") if isinstance(capabilities, dict) else {}
        upload_supported = bool(file_upload.get("supported")) if isinstance(file_upload, dict) else False
        extraction_formats = file_upload.get("text_extraction") if isinstance(file_upload, dict) else []
        extraction_formats = extraction_formats if isinstance(extraction_formats, list) else []
        checks.append(self._check(
            "file.upload_context",
            "文件上传与上下文解析",
            "pass" if upload_supported and extraction_formats else "fail",
            (
                f"默认支持附件上传；可解析 {len(extraction_formats)} 类文本/文档格式"
                if upload_supported and extraction_formats
                else "附件上传或正文解析能力不可用"
            ),
            evidence={
                "supported": upload_supported,
                "formats": extraction_formats,
                "max_mb": file_upload.get("max_mb") if isinstance(file_upload, dict) else None,
                "max_files_per_task": file_upload.get("max_files_per_task") if isinstance(file_upload, dict) else None,
                "max_chars_per_file": file_upload.get("max_chars_per_file") if isinstance(file_upload, dict) else None,
                "max_context_chars": file_upload.get("max_context_chars") if isinstance(file_upload, dict) else None,
            },
            action="在工作台点击“添加附件”上传文件；发送前会提示该附件是否可进入模型上下文。",
        ))

        direct_key = capabilities.get("direct_api_key") if isinstance(capabilities, dict) else {}
        policy_hooks = capabilities.get("policy_hooks") if isinstance(capabilities, dict) else {}
        outbound = capabilities.get("outbound_network") if isinstance(capabilities, dict) else {}
        web_search = capabilities.get("web_search") if isinstance(capabilities, dict) else {}
        remote_install = capabilities.get("remote_install") if isinstance(capabilities, dict) else {}
        remote_mcp = capabilities.get("remote_mcp") if isinstance(capabilities, dict) else {}
        http_tools = capabilities.get("http_tools") if isinstance(capabilities, dict) else {}
        stdio_mcp = capabilities.get("stdio_mcp") if isinstance(capabilities, dict) else {}
        secret_storage_ok = bool(direct_key.get("supported")) and bool(direct_key.get("encrypted"))
        arbitrary_shell_blocked = not bool(policy_hooks.get("arbitrary_shell"))
        checks.append(self._check(
            "security.permissions",
            "权限开关与密钥保护",
            "pass" if secret_storage_ok and arbitrary_shell_blocked else "warn",
            (
                "高风险能力由显式开关控制，直接模型密钥本机加密保存"
                if secret_storage_ok and arbitrary_shell_blocked
                else "权限开关或密钥保护策略需要复核"
            ),
            evidence={
                "outbound_network_enabled": bool(outbound.get("enabled")) if isinstance(outbound, dict) else False,
                "web_search_enabled": bool(web_search.get("enabled")) if isinstance(web_search, dict) else False,
                "remote_install_enabled": bool(remote_install.get("enabled")) if isinstance(remote_install, dict) else False,
                "remote_mcp_enabled": bool(remote_mcp.get("enabled")) if isinstance(remote_mcp, dict) else False,
                "http_tools_enabled": bool(http_tools.get("enabled")) if isinstance(http_tools, dict) else False,
                "stdio_mcp_enabled": bool(stdio_mcp.get("enabled")) if isinstance(stdio_mcp, dict) else False,
                "direct_key_encrypted": secret_storage_ok,
                "arbitrary_shell_blocked": arbitrary_shell_blocked,
            },
            action="在模型、MCP、市场和联网配置中按需开启能力；导出报告和任务响应应继续保持脱敏。",
        ))

        checks.append(self._check(
            "skill_mcp.capability",
            "Skill/MCP 基础能力",
            "pass",
            "支持本地创建、上传安装、内置市场安装；远程安装、本地 stdio MCP、远程 MCP 和 HTTP 工具由显式开关控制",
            evidence={
                "skill_create_supported": True,
                "skill_upload_install_supported": True,
                "skill_remote_install_supported": bool(remote_install.get("supported")) if isinstance(remote_install, dict) else False,
                "skill_remote_install_enabled": bool(remote_install.get("enabled")) if isinstance(remote_install, dict) else False,
                "marketplace_supported": True,
                "mcp_create_supported": True,
                "mcp_import_supported": True,
                "stdio_mcp_supported": bool(stdio_mcp.get("supported")) if isinstance(stdio_mcp, dict) else False,
                "stdio_mcp_enabled": bool(stdio_mcp.get("enabled")) if isinstance(stdio_mcp, dict) else False,
                "remote_mcp_supported": bool(remote_mcp.get("supported")) if isinstance(remote_mcp, dict) else False,
                "remote_mcp_enabled": bool(remote_mcp.get("enabled")) if isinstance(remote_mcp, dict) else False,
                "http_tools_supported": bool(http_tools.get("supported")) if isinstance(http_tools, dict) else False,
                "http_tools_enabled": bool(http_tools.get("enabled")) if isinstance(http_tools, dict) else False,
            },
            action="本地 Skill/MCP 可直接在技能中心和工具接入页创建或导入；远程链接安装和外部工具需管理员开启对应环境变量。",
        ))

        expert_capability = capabilities.get("expert_teams") if isinstance(capabilities, dict) else {}
        expert_supported = bool(expert_capability.get("supported")) if isinstance(expert_capability, dict) else False
        checks.append(self._check(
            "expert.capability",
            "专家模式基础能力",
            "pass" if expert_supported else "fail",
            (
                "支持专家模板安装、多成员并行、隔离成员上下文、主管汇总和单成员重试"
                if expert_supported
                else "专家模式基础能力不可用"
            ),
            evidence={
                "supported": expert_supported,
                "template_installation": bool(expert_capability.get("template_installation")) if isinstance(expert_capability, dict) else False,
                "parallel_members": bool(expert_capability.get("parallel_members")) if isinstance(expert_capability, dict) else False,
                "isolated_member_context": bool(expert_capability.get("isolated_member_context")) if isinstance(expert_capability, dict) else False,
                "supervisor_aggregation": bool(expert_capability.get("supervisor_aggregation")) if isinstance(expert_capability, dict) else False,
                "single_member_retry": bool(expert_capability.get("single_member_retry")) if isinstance(expert_capability, dict) else False,
            },
            action="专家模式需要至少一个启用专家团；可在“专家团”页安装专家模板并创建团队。",
        ))

        expert_templates = self._count("expert_templates")
        expert_installations = self._count("expert_installations")
        expert_teams = self._count("agent_teams")
        enabled_expert_teams = self._count("agent_teams", "enabled = 1")
        enabled_team_members = int((db.query_one(
            """
            SELECT COUNT(*) AS total
            FROM agent_team_members AS m
            JOIN agent_teams AS t ON t.id = m.team_id
            WHERE t.enabled = 1
            """
        ) or {}).get("total") or 0)
        checks.append(self._check(
            "expert.teams_configured",
            "专家团配置",
            "pass" if enabled_expert_teams and enabled_team_members >= 2 else "warn",
            (
                f"已启用 {enabled_expert_teams} 个专家团，启用团队包含 {enabled_team_members} 位成员"
                if enabled_expert_teams
                else "尚未配置启用的专家团；专家模式会阻止发送并提示先创建团队"
            ),
            evidence={
                "templates": expert_templates,
                "installations": expert_installations,
                "teams": expert_teams,
                "enabled_teams": enabled_expert_teams,
                "enabled_team_members": enabled_team_members,
            },
            action="到“专家团”安装专家模板，创建至少包含主管和两位成员的启用团队。",
        ))

        memory_capability = capabilities.get("memory") if isinstance(capabilities, dict) else {}
        memory_supported = bool(memory_capability.get("supported")) if isinstance(memory_capability, dict) else False
        memory_scopes = memory_capability.get("scopes") if isinstance(memory_capability, dict) else []
        memory_scopes = memory_scopes if isinstance(memory_scopes, list) else []
        conversation_summary = memory_capability.get("conversation_summary") if isinstance(memory_capability, dict) else {}
        conversation_summary = conversation_summary if isinstance(conversation_summary, dict) else {}
        checks.append(self._check(
            "memory.capability",
            "上下文记忆基础能力",
            "pass" if memory_supported and memory_scopes else "fail",
            (
                f"支持 {', '.join(memory_scopes)} 作用域记忆，支持自动对话摘要和修订记录"
                if memory_supported and memory_scopes
                else "上下文记忆基础能力不可用"
            ),
            evidence={
                "supported": memory_supported,
                "scopes": memory_scopes,
                "revision_history": bool(memory_capability.get("revision_history")) if isinstance(memory_capability, dict) else False,
                "conversation_summary": {
                    "automatic": bool(conversation_summary.get("automatic")),
                    "viewable": bool(conversation_summary.get("viewable")),
                    "editable": bool(conversation_summary.get("editable")),
                    "deletable": bool(conversation_summary.get("deletable")),
                },
            },
            action="到“记忆”页查看当前有效上下文、手动维护长期记忆或编辑对话摘要。",
        ))

        memory_total = self._count("memory_entries")
        memory_enabled = self._count("memory_entries", "enabled = 1")
        memory_revisions = self._count("memory_revisions")
        conversation_summaries = self._count("conversation_summaries")
        checks.append(self._check(
            "memory.configured",
            "记忆与对话摘要配置",
            "pass" if memory_enabled or conversation_summaries else "warn",
            (
                f"已启用 {memory_enabled} 条长期记忆，已有 {conversation_summaries} 条对话摘要"
                if memory_enabled or conversation_summaries
                else "尚未建立长期记忆或对话摘要；当前对话仍按任务历史提供上下文"
            ),
            evidence={
                "memories": memory_total,
                "enabled_memories": memory_enabled,
                "memory_revisions": memory_revisions,
                "conversation_summaries": conversation_summaries,
            },
            action="到“记忆”页新增稳定偏好/规则；长对话会生成可查看、可编辑、可删除的摘要。",
        ))

        automation_capability = capabilities.get("automation") if isinstance(capabilities, dict) else {}
        automation_supported = bool(automation_capability.get("supported")) if isinstance(automation_capability, dict) else False
        automation_triggers = automation_capability.get("triggers") if isinstance(automation_capability, dict) else []
        automation_triggers = automation_triggers if isinstance(automation_triggers, list) else []
        checks.append(self._check(
            "automation.capability",
            "自动化基础能力",
            "pass" if automation_supported and automation_triggers else "fail",
            (
                f"支持 {', '.join(automation_triggers)} 触发，包含持久化历史、签名 Webhook、幂等和通知"
                if automation_supported and automation_triggers
                else "自动化基础能力不可用"
            ),
            evidence={
                "supported": automation_supported,
                "triggers": automation_triggers,
                "persistent_history": bool(automation_capability.get("persistent_history")) if isinstance(automation_capability, dict) else False,
                "signed_webhooks": bool(automation_capability.get("signed_webhooks")) if isinstance(automation_capability, dict) else False,
                "idempotency": bool(automation_capability.get("idempotency")) if isinstance(automation_capability, dict) else False,
                "notifications": bool(automation_capability.get("notifications")) if isinstance(automation_capability, dict) else False,
                "structured_state_diff": bool(automation_capability.get("structured_state_diff")) if isinstance(automation_capability, dict) else False,
                "api": str(automation_capability.get("legacy_api") or "") if isinstance(automation_capability, dict) else "",
            },
            action="到“自动化”页创建循环任务，可选择间隔、Cron、一次性或 Webhook 触发。",
        ))

        loop_total = self._count("loops", "organization_id = ? AND workspace_id = ? AND user_id = ?", (organization_id, workspace_id, user_id))
        loop_active = self._count("loops", "organization_id = ? AND workspace_id = ? AND user_id = ? AND status = 'active'", (organization_id, workspace_id, user_id))
        loop_runs = self._count("loop_runs")
        trigger_events = self._count("automation_trigger_events", "organization_id = ? AND workspace_id = ? AND user_id = ?", (organization_id, workspace_id, user_id))
        notifications = self._count("notifications", "organization_id = ? AND workspace_id = ? AND user_id = ? AND kind = 'automation'", (organization_id, workspace_id, user_id))
        checks.append(self._check(
            "automation.configured",
            "自动化配置",
            "pass" if loop_total else "warn",
            (
                f"已配置 {loop_total} 个自动化，其中 {loop_active} 个正在调度"
                if loop_total
                else "尚未配置自动化；普通对话和手动任务不受影响"
            ),
            evidence={
                "loops": loop_total,
                "active_loops": loop_active,
                "runs": loop_runs,
                "trigger_events": trigger_events,
                "notifications": notifications,
            },
            action="到“自动化”页新建任务，配置目标、触发器、重试和状态 JSON；保存后可先试运行。",
        ))

        outbound_capability = capabilities.get("outbound_network") if isinstance(capabilities, dict) else {}
        web_search_capability = capabilities.get("web_search") if isinstance(capabilities, dict) else {}
        remote_mcp_capability = capabilities.get("remote_mcp") if isinstance(capabilities, dict) else {}
        http_tools_capability = capabilities.get("http_tools") if isinstance(capabilities, dict) else {}
        remote_install_capability = capabilities.get("remote_install") if isinstance(capabilities, dict) else {}
        checks.append(self._check(
            "network.capability",
            "联网与远程基础能力",
            "pass" if bool(outbound_capability.get("supported")) else "fail",
            (
                "支持出站网络、联网搜索、远程 MCP、HTTP 工具和远程安装；默认按开关和白名单启用"
                if bool(outbound_capability.get("supported"))
                else "联网与远程基础能力不可用"
            ),
            evidence={
                "outbound_supported": bool(outbound_capability.get("supported")) if isinstance(outbound_capability, dict) else False,
                "outbound_enabled": bool(outbound_capability.get("enabled")) if isinstance(outbound_capability, dict) else False,
                "web_search_supported": bool(web_search_capability.get("supported")) if isinstance(web_search_capability, dict) else False,
                "web_search_enabled": bool(web_search_capability.get("enabled")) if isinstance(web_search_capability, dict) else False,
                "web_search_configured": bool(web_search_capability.get("configured")) if isinstance(web_search_capability, dict) else False,
                "search_provider": str(web_search_capability.get("provider") or "") if isinstance(web_search_capability, dict) else "",
                "remote_mcp_supported": bool(remote_mcp_capability.get("supported")) if isinstance(remote_mcp_capability, dict) else False,
                "remote_mcp_enabled": bool(remote_mcp_capability.get("enabled")) if isinstance(remote_mcp_capability, dict) else False,
                "http_tools_supported": bool(http_tools_capability.get("supported")) if isinstance(http_tools_capability, dict) else False,
                "http_tools_enabled": bool(http_tools_capability.get("enabled")) if isinstance(http_tools_capability, dict) else False,
                "remote_install_supported": bool(remote_install_capability.get("supported")) if isinstance(remote_install_capability, dict) else False,
                "remote_install_enabled": bool(remote_install_capability.get("enabled")) if isinstance(remote_install_capability, dict) else False,
            },
            action="需要联网搜索或远程工具时，开启 APP_ALLOW_OUTBOUND_NETWORK，并分别配置 APP_ALLOW_WEB_SEARCH、APP_ALLOW_REMOTE_MCP、APP_ALLOW_HTTP_TOOLS 或 APP_ALLOW_REMOTE_INSTALL。",
        ))

        skill_enabled = self._count("skills", "enabled = 1")
        checks.append(self._check(
            "skills.enabled",
            "Skill 可用性",
            "pass" if skill_enabled else "fail",
            f"已启用 {skill_enabled} 个 Skill" if skill_enabled else "没有启用的 Skill",
            evidence={"enabled": skill_enabled},
            action="到“技能中心”启用或安装 Skill。",
        ))

        mcp_total = self._count("mcp_servers")
        mcp_enabled = self._count("mcp_servers", "enabled = 1")
        checks.append(self._check(
            "mcp.configured",
            "MCP / 工具接入",
            "pass" if mcp_enabled else "warn",
            f"已启用 {mcp_enabled} 个工具服务" if mcp_enabled else "尚未启用工具服务；普通问答不受影响，但工具调用能力有限",
            evidence={"total": mcp_total, "enabled": mcp_enabled},
            action="到“工具接入”启用本地或远程 MCP。",
        ))

        knowledge_capability = capabilities.get("knowledge_base") if isinstance(capabilities, dict) else {}
        knowledge_supported = bool(knowledge_capability.get("supported")) if isinstance(knowledge_capability, dict) else False
        knowledge_scopes = knowledge_capability.get("scopes") if isinstance(knowledge_capability, dict) else []
        knowledge_scopes = knowledge_scopes if isinstance(knowledge_scopes, list) else []
        indexed_formats = knowledge_capability.get("indexed_upload_formats") if isinstance(knowledge_capability, dict) else []
        indexed_formats = indexed_formats if isinstance(indexed_formats, list) else []
        runtime_injection = bool(knowledge_capability.get("runtime_injection")) if isinstance(knowledge_capability, dict) else False
        checks.append(self._check(
            "knowledge.capability",
            "知识库基础能力",
            "pass" if knowledge_supported and runtime_injection and indexed_formats else "fail",
            (
                f"支持 {', '.join(knowledge_scopes)} 范围知识库，索引格式 {len(indexed_formats)} 类，运行时可注入"
                if knowledge_supported and runtime_injection and indexed_formats
                else "知识库基础能力不可用或运行时注入未开启"
            ),
            evidence={
                "supported": knowledge_supported,
                "scopes": knowledge_scopes,
                "retrieval": knowledge_capability.get("retrieval") if isinstance(knowledge_capability, dict) else "",
                "runtime_injection": runtime_injection,
                "indexed_upload_formats": indexed_formats,
            },
            action="到“知识库”创建知识库并上传资料；后续对话会检索启用且可见的知识库。",
        ))

        kb_total = self._count("knowledge_bases", "organization_id = ? AND enabled = 1", (organization_id,))
        kb_docs = self._count("knowledge_documents")
        kb_chunks = self._count("knowledge_chunks")
        checks.append(self._check(
            "knowledge.index",
            "知识库索引",
            "pass" if kb_total and kb_chunks else "warn",
            f"已启用 {kb_total} 个知识库，{kb_docs} 个文档，{kb_chunks} 个片段" if kb_total else "尚未建立可检索知识库",
            evidence={"enabled_bases": kb_total, "documents": kb_docs, "chunks": kb_chunks},
            action="到“知识库”上传资料并建立索引。",
        ))

        document_output = capabilities.get("document_output") if isinstance(capabilities, dict) else {}
        output_formats = document_output.get("formats") if isinstance(document_output, dict) else []
        output_formats = output_formats if isinstance(output_formats, list) else []
        optional_formats = document_output.get("optional_formats") if isinstance(document_output, dict) else []
        optional_formats = optional_formats if isinstance(optional_formats, list) else []
        pptx_configured = bool(document_output.get("pptx_configured")) if isinstance(document_output, dict) else False
        required_formats = {"markdown", "docx", "pdf", "xlsx", "csv", "html"}
        available_formats = {str(item).lower() for item in output_formats}
        missing_formats = sorted(required_formats - available_formats)
        checks.append(self._check(
            "document.output_formats",
            "文档输出格式",
            "pass" if not missing_formats else "fail",
            (
                f"默认支持 {', '.join(output_formats)}；PPTX {'已配置' if pptx_configured else '为可选能力，尚未配置'}"
                if not missing_formats
                else f"缺少必要输出格式：{', '.join(missing_formats)}"
            ),
            evidence={
                "formats": output_formats,
                "optional_formats": optional_formats,
                "pptx_configured": pptx_configured,
                "pptx_reason": str(document_output.get("pptx_reason") or "") if isinstance(document_output, dict) else "",
                "missing_required_formats": missing_formats,
            },
            action="常见格式可直接在任务中要求输出；PPTX 默认使用内置 Python 生成器，只有切换外部 Artifact Tool 时才需要配置 APP_NODE_BINARY 和 APP_ARTIFACT_TOOL_ENTRYPOINT。",
        ))

        artifact_rows = db.query_all("SELECT id, relative_path, path, sha256, delivery_status FROM artifacts WHERE delivery_status = 'published' LIMIT 300")
        missing = 0
        hash_mismatch = 0
        for row in artifact_rows:
            path = Path(str(row.get("path") or ""))
            if not path.exists() and row.get("relative_path"):
                try:
                    from app.services.mcp_gateway import resolve_artifact_path

                    path = resolve_artifact_path(str(row.get("relative_path") or ""))
                except Exception:
                    path = Path("")
            if not path.exists() or not path.is_file():
                missing += 1
                continue
            expected = str(row.get("sha256") or "")
            if expected:
                actual = hashlib.sha256(path.read_bytes()).hexdigest()
                if actual != expected:
                    hash_mismatch += 1
        artifact_status = "pass" if not missing and not hash_mismatch else "fail"
        checks.append(self._check(
            "artifacts.integrity",
            "产物文件完整性",
            artifact_status,
            "已发布产物文件存在且校验一致" if artifact_status == "pass" else "部分已发布产物文件缺失或校验不一致",
            evidence={"sampled": len(artifact_rows), "missing": missing, "hash_mismatch": hash_mismatch},
            action="重新生成受影响产物，或检查 data/artifacts 目录是否被移动。",
        ))

        checks.append(self._check(
            "runtime.contract_capability",
            "任务执行闭环能力",
            "pass",
            "支持目标合同、动态计划节点、实时事件流、子级能力证据和输出前验收",
            evidence={
                "goal_contract_supported": True,
                "dynamic_plan_nodes_supported": True,
                "two_level_node_tree_supported": True,
                "sse_event_stream_supported": True,
                "public_event_projection": True,
                "tool_skill_trace_supported": True,
                "output_verification_supported": True,
                "checkpoint_resume_supported": True,
                "approval_pause_supported": True,
            },
            action="在工作台发送任务后，右侧运行控制应展示目标合同、当前节点、执行证据、产物和验收状态。",
        ))

        run_records = self._count("task_runs")
        node_records = self._count("task_nodes")
        goal_specs = self._count("task_goal_specs")
        verifications = self._count("task_verifications")
        checkpoints = self._count("task_checkpoints")
        command_records = self._count("task_commands")
        checks.append(self._check(
            "runtime.trace_records",
            "任务执行证据记录",
            "pass" if run_records or node_records or goal_specs or verifications else "warn",
            (
                f"已有 {run_records} 条运行、{node_records} 个节点、{goal_specs} 份目标合同、{verifications} 份验收报告"
                if run_records or node_records or goal_specs or verifications
                else "当前还没有任务运行证据记录；发送一次任务后应产生运行、节点、目标合同和验收记录"
            ),
            evidence={
                "runs": run_records,
                "nodes": node_records,
                "goal_specs": goal_specs,
                "verifications": verifications,
                "checkpoints": checkpoints,
                "commands": command_records,
            },
            action="发送一个普通任务并观察右侧运行控制；若长期没有节点、目标合同或验收记录，需要检查运行时链路。",
        ))

        active_runs = self._count("task_runs", "status IN ('queued','running','processing','waiting','waiting_approval','cancel_requested')")
        checks.append(self._check(
            "runtime.active_runs",
            "运行时残留",
            "pass" if active_runs == 0 else "warn",
            "当前没有活动运行残留" if active_runs == 0 else f"当前有 {active_runs} 个非终态运行；可能是正在运行，也可能需要恢复",
            evidence={"active_runs": active_runs},
            action="如页面长期无输出，可重启服务触发恢复逻辑，或到运行记录查看具体任务。",
        ))

        search_capability = capabilities.get("web_search") if isinstance(capabilities, dict) else {}
        checks.append(self._check(
            "network.search",
            "联网搜索配置",
            "pass" if search_capability.get("enabled") else "warn",
            "联网搜索已启用" if search_capability.get("enabled") else "联网搜索未启用或未配置 Provider",
            evidence={
                "supported": bool(search_capability.get("supported")),
                "enabled": bool(search_capability.get("enabled")),
                "configured": bool(search_capability.get("configured")),
            },
            action="配置搜索 Provider Key，并启用 APP_ALLOW_WEB_SEARCH。",
        ))

        failed = sum(1 for item in checks if item["status"] == "fail")
        warnings = sum(1 for item in checks if item["status"] == "warn")
        overall = "fail" if failed else "warn" if warnings else "pass"
        issues = self._issues(checks)
        readiness = self._readiness(checks, capabilities if isinstance(capabilities, dict) else {})
        improvement_backlog = self._improvement_backlog(readiness, capabilities if isinstance(capabilities, dict) else {})
        next_actions = self._next_actions(readiness, improvement_backlog)
        self_tests = self._self_tests(readiness)
        self_test_summary = {
            "total": len(self_tests),
            "ready": sum(1 for item in self_tests if item.get("readiness") == "ready"),
            "needs_setup": sum(1 for item in self_tests if item.get("readiness") != "ready"),
            "by_category": {},
            "by_artifact": {},
        }
        for item in self_tests:
            category = str(item.get("category") or "未分类")
            self_test_summary["by_category"][category] = int(self_test_summary["by_category"].get(category, 0)) + 1
            for artifact in item.get("artifacts") or []:
                key = str(artifact)
                self_test_summary["by_artifact"][key] = int(self_test_summary["by_artifact"].get(key, 0)) + 1
        readiness_blocked = sum(1 for item in readiness if item.get("state") == "blocked")
        readiness_needs_config = sum(1 for item in readiness if item.get("state") == "needs_config")
        actionable_summary = {
            "blocked_capabilities": readiness_blocked,
            "needs_config_capabilities": readiness_needs_config,
            "ready_capabilities": sum(1 for item in readiness if item.get("state") == "ready"),
            "p0_improvements": sum(1 for item in improvement_backlog if item.get("priority") == "P0"),
            "p1_improvements": sum(1 for item in improvement_backlog if item.get("priority") == "P1"),
            "p2_improvements": sum(1 for item in improvement_backlog if item.get("priority") == "P2"),
            "ready_self_tests": self_test_summary["ready"],
            "total_self_tests": self_test_summary["total"],
            "next_actions": len(next_actions),
            "issues": len(issues),
        }
        summary = {"total": len(checks), "passed": sum(1 for item in checks if item["status"] == "pass"), "warnings": warnings, "failed": failed}
        generated_at = datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
        schema_version = "diagnostics.v1"
        report_markdown = self._report_markdown(
            overall=overall,
            summary=summary,
            actionable_summary=actionable_summary,
            self_test_summary=self_test_summary,
            issues=issues,
            next_actions=next_actions,
            readiness=readiness,
            checks=checks,
            improvement_backlog=improvement_backlog,
            self_tests=self_tests,
            generated_at=generated_at,
            schema_version=schema_version,
        )
        return {
            "schema_version": schema_version,
            "generated_at": generated_at,
            "overall": overall,
            "summary": summary,
            "actionable_summary": actionable_summary,
            "self_test_summary": self_test_summary,
            "issues": issues,
            "next_actions": next_actions,
            "readiness": readiness,
            "improvement_backlog": improvement_backlog,
            "self_tests": self_tests,
            "report_markdown": report_markdown,
            "checks": sorted(checks, key=lambda item: (_status_rank(item["status"]), item["id"])),
        }
