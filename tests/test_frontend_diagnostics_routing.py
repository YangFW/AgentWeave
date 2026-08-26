import json
import re
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ROUTING_JS = ROOT / "web" / "diagnostics-routing.js"
INDEX_HTML = ROOT / "web" / "index.html"
APP_JS = ROOT / "web" / "app.js"


class FrontendDiagnosticsRoutingTests(unittest.TestCase):
    def run_node_cases(self, cases: list[dict]) -> dict:
        script = textwrap.dedent(
            f"""
            const assert = require('assert');
            const routing = require({json.dumps(str(ROUTING_JS))});
            const cases = {json.dumps(cases, ensure_ascii=False)};
            const result = {{}};
            for (const item of cases) {{
              result[item.name] = {{
                route: routing.diagnosticRouteTarget(item.input),
                skillMcpTarget: routing.isSkillMcpDiagnosticTarget(item.input)
                  ? routing.diagnosticSkillMcpTarget(item.input)
                  : '',
                mcpPreference: routing.diagnosticMcpPreference(item.input),
                isModel: routing.isModelDiagnosticTarget(item.input),
                isSkillMcp: routing.isSkillMcpDiagnosticTarget(item.input),
              }};
              assert.deepStrictEqual(result[item.name], item.expected, item.name);
            }}
            console.log(JSON.stringify(result, null, 2));
            """
        )
        with tempfile.NamedTemporaryFile("w", suffix=".cjs", delete=False) as handle:
            handle.write(script)
            script_path = Path(handle.name)
        try:
            completed = subprocess.run(
                ["node", str(script_path)],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=True,
            )
            return json.loads(completed.stdout)
        finally:
            script_path.unlink(missing_ok=True)

    def test_shared_route_mapping_covers_platform_diagnostics(self) -> None:
        cases = [
            {
                "name": "model runtime",
                "input": {"id": "model_runtime", "action_target": {"tab": "models"}},
                "expected": {"route": "models", "skillMcpTarget": "", "mcpPreference": {"id": "", "tool": "", "keywords": []}, "isModel": True, "isSkillMcp": False},
            },
            {
                "name": "skill list",
                "input": {"check_id": "skills.enabled", "action_target": {"tab": "skills"}},
                "expected": {"route": "skill_mcp", "skillMcpTarget": "skills", "mcpPreference": {"id": "", "tool": "", "keywords": []}, "isModel": False, "isSkillMcp": True},
            },
            {
                "name": "mcp list",
                "input": {"check_id": "mcp.configured", "action_target": {"tab": "mcp"}},
                "expected": {"route": "skill_mcp", "skillMcpTarget": "mcp", "mcpPreference": {"id": "", "tool": "", "keywords": []}, "isModel": False, "isSkillMcp": True},
            },
            {
                "name": "market install flow",
                "input": {"id": "install_flow", "target_tab": "marketplace"},
                "expected": {"route": "skill_mcp", "skillMcpTarget": "marketplace", "mcpPreference": {"id": "", "tool": "", "keywords": []}, "isModel": False, "isSkillMcp": True},
            },
            {
                "name": "knowledge",
                "input": {"check_id": "knowledge.index", "action_target": {"tab": "knowledge"}},
                "expected": {"route": "knowledge", "skillMcpTarget": "", "mcpPreference": {"id": "", "tool": "", "keywords": []}, "isModel": False, "isSkillMcp": False},
            },
            {
                "name": "artifact documents",
                "input": {"check_id": "document.output_formats", "target_tab": "artifacts"},
                "expected": {"route": "artifacts", "skillMcpTarget": "", "mcpPreference": {"id": "report", "tool": "generate_document", "keywords": ["report", "document", "文档"]}, "isModel": False, "isSkillMcp": False},
            },
            {
                "name": "experts",
                "input": {"id": "expert_mode", "target_tab": "experts"},
                "expected": {"route": "experts", "skillMcpTarget": "", "mcpPreference": {"id": "", "tool": "", "keywords": []}, "isModel": False, "isSkillMcp": False},
            },
            {
                "name": "memory",
                "input": {"check_id": "memory.configured", "action_target": {"tab": "memory"}},
                "expected": {"route": "memory", "skillMcpTarget": "", "mcpPreference": {"id": "", "tool": "", "keywords": []}, "isModel": False, "isSkillMcp": False},
            },
            {
                "name": "automation",
                "input": {"check_id": "automation.configured", "action_target": {"tab": "loops"}},
                "expected": {"route": "loops", "skillMcpTarget": "", "mcpPreference": {"id": "", "tool": "", "keywords": []}, "isModel": False, "isSkillMcp": False},
            },
            {
                "name": "file input",
                "input": {"id": "file_input", "target_tab": "chat"},
                "expected": {"route": "chat", "skillMcpTarget": "", "mcpPreference": {"id": "", "tool": "", "keywords": []}, "isModel": False, "isSkillMcp": False},
            },
            {
                "name": "workspace",
                "input": {"check_id": "workspace.default"},
                "expected": {"route": "workspaces", "skillMcpTarget": "", "mcpPreference": {"id": "", "tool": "", "keywords": []}, "isModel": False, "isSkillMcp": False},
            },
            {
                "name": "security",
                "input": {"check_id": "security.permissions"},
                "expected": {"route": "diagnostics", "skillMcpTarget": "", "mcpPreference": {"id": "", "tool": "", "keywords": []}, "isModel": False, "isSkillMcp": False},
            },
            {
                "name": "database",
                "input": {"check_id": "database.integrity"},
                "expected": {"route": "diagnostics", "skillMcpTarget": "", "mcpPreference": {"id": "", "tool": "", "keywords": []}, "isModel": False, "isSkillMcp": False},
            },
            {
                "name": "network search prefers search mcp",
                "input": {"check_id": "network.search", "action_target": {"tab": "mcp"}},
                "expected": {"route": "skill_mcp", "skillMcpTarget": "mcp", "mcpPreference": {"id": "web-search", "tool": "search", "keywords": ["search", "联网", "检索"]}, "isModel": False, "isSkillMcp": True},
            },
            {
                "name": "network capability prefers search mcp",
                "input": {"check_id": "network.capability", "target_tab": "mcp"},
                "expected": {"route": "skill_mcp", "skillMcpTarget": "mcp", "mcpPreference": {"id": "web-search", "tool": "search", "keywords": ["search", "联网", "检索"]}, "isModel": False, "isSkillMcp": True},
            },
        ]
        result = self.run_node_cases(cases)
        self.assertEqual(set(result), {case["name"] for case in cases})

    def test_page_loads_routing_before_app(self) -> None:
        html = INDEX_HTML.read_text(encoding="utf-8")
        routing_index = html.index("diagnostics-routing.js")
        app_index = html.index("app.js")
        self.assertLess(routing_index, app_index)
        versions = re.findall(r'<script src="\./(?:diagnostics-routing|app)\.js\?v=([^"]+)"></script>', html)
        self.assertEqual(len(versions), 2)
        self.assertEqual(versions[0], versions[1])

    def test_static_frontend_assets_are_served(self) -> None:
        from fastapi.testclient import TestClient

        from app import main as main_module

        with TestClient(main_module.app) as client:
            html_response = client.get("/")
            self.assertEqual(html_response.status_code, 200)
            html = html_response.text
            script_paths = re.findall(r'<script src="\./([^"]+)"></script>', html)
            self.assertGreaterEqual(len(script_paths), 2)
            self.assertTrue(script_paths[0].startswith("diagnostics-routing.js?v="))
            self.assertTrue(script_paths[1].startswith("app.js?v="))
            self.assertEqual(script_paths[0].split("?v=", 1)[1], script_paths[1].split("?v=", 1)[1])

            routing_response = client.get("/diagnostics-routing.js")
            self.assertEqual(routing_response.status_code, 200)
            self.assertIn("chooseDiagnosticMcpServer", routing_response.text)

            app_response = client.get("/app.js")
            self.assertEqual(app_response.status_code, 200)
            self.assertIn("AgentNexusDiagnosticsRouting.chooseDiagnosticMcpServer", app_response.text)

    def test_app_uses_shared_routing_module(self) -> None:
        source = APP_JS.read_text(encoding="utf-8")
        self.assertIn("AgentNexusDiagnosticsRouting.diagnosticRouteTarget", source)
        self.assertIn("AgentNexusDiagnosticsRouting.diagnosticSkillMcpTarget", source)
        self.assertIn("AgentNexusDiagnosticsRouting.chooseDiagnosticMcpServer", source)
        self.assertIn("function useDiagnosticCheckAction", source)
        self.assertIn("data-check-id", source)

    def test_mcp_candidate_selection_prefers_exact_search_server(self) -> None:
        script = textwrap.dedent(
            f"""
            const assert = require('assert');
            const routing = require({json.dumps(str(ROUTING_JS))});
            const servers = [
              {{
                id: 'weather',
                name: '天气预报 MCP',
                enabled: true,
                tools: [{{ name: 'forecast', description: '查询天气' }}],
              }},
              {{
                id: 'filesystem-local',
                name: '本地文件系统 MCP',
                enabled: true,
                tools: [{{ name: 'search_files', description: 'Search files locally' }}],
              }},
              {{
                id: 'web-search',
                name: '联网搜索 MCP',
                enabled: true,
                tools: [{{ name: 'search', description: '搜索互联网' }}],
              }},
            ];
            assert.strictEqual(
              routing.chooseDiagnosticMcpServer(servers, {{ check_id: 'network.capability', target_tab: 'mcp' }}).id,
              'web-search'
            );
            assert.strictEqual(
              routing.chooseDiagnosticMcpServer(servers, {{ check_id: 'network.search', action_target: {{ tab: 'mcp' }} }}).id,
              'web-search'
            );
            assert.strictEqual(
              routing.chooseDiagnosticMcpServer(servers, {{ check_id: 'mcp.configured', action_target: {{ tab: 'mcp' }} }}).id,
              'weather'
            );
            process.stdout.write('ok');
            """
        )
        with tempfile.NamedTemporaryFile("w", suffix=".cjs", delete=False) as handle:
            handle.write(script)
            script_path = Path(handle.name)
        try:
            completed = subprocess.run(
                ["node", str(script_path)],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=True,
            )
            self.assertEqual(completed.stdout, "ok")
        finally:
            script_path.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
