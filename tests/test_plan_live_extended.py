"""Extended live acceptance checks mapped to ``docs/TEST_PLAN.md``.

The checklist and files under ``tests/fixtures/agentnexus_samples`` are
immutable inputs.  This module only adds executable checks for the live local
service; all IDs created by the checks are disposable and are cleaned up when
the corresponding API supports deletion.
"""

from __future__ import annotations

import hashlib
import hmac
import io
import json
import mimetypes
import time
import unittest
import uuid
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx


ROOT = Path(__file__).resolve().parents[1]
SAMPLES = ROOT / "tests" / "fixtures" / "agentnexus_samples"
SCENARIOS = SAMPLES / "scenarios"


class ExtendedLivePlanTests(unittest.TestCase):
    base_url = "http://127.0.0.1:8000"
    timeout = 180.0

    @classmethod
    def setUpClass(cls) -> None:
        cls.client = httpx.Client(timeout=45.0)
        try:
            response = cls.client.get(f"{cls.base_url}/api/health")
            response.raise_for_status()
            if not response.json().get("ok"):
                raise RuntimeError("health did not return ok=true")
        except Exception as exc:  # pragma: no cover - local prerequisite
            cls.client.close()
            raise unittest.SkipTest(f"本地 AgentNexus 服务未启动：{exc}") from exc

    @classmethod
    def tearDownClass(cls) -> None:
        cls.client.close()

    def _request(
        self,
        method: str,
        path: str,
        *,
        expected: tuple[int, ...] = (200,),
        **kwargs: Any,
    ) -> Any:
        response = self.client.request(method, f"{self.base_url}{path}", **kwargs)
        self.assertIn(
            response.status_code,
            expected,
            f"{method} {path}: {response.text[:1200]}",
        )
        if not response.content:
            return None
        content_type = response.headers.get("content-type", "")
        if "json" in content_type:
            return response.json()
        return response.content

    def _upload(self, path: Path) -> dict[str, Any]:
        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        with path.open("rb") as stream:
            return self._request(
                "POST",
                "/api/uploads",
                files={"file": (path.name, stream, content_type)},
            )

    def _task(
        self,
        message: str,
        *,
        conversation_id: str | None = None,
        attachments: list[str] | None = None,
        model_id: str = "deterministic",
        executor_type: str = "agent",
        agent_id: str = "general-agent",
        workspace: str = "default",
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        payload: dict[str, Any] = {
            "message": message,
            "agent_id": agent_id,
            "model_id": model_id,
            "conversation_id": conversation_id or f"live-{uuid.uuid4().hex}",
            "attachment_ids": attachments or [],
            "executor_type": executor_type,
            "workspace": workspace,
        }
        created = self._request("POST", "/api/tasks", json=payload)
        task_id = created["id"]
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            task = self._request("GET", f"/api/tasks/{task_id}")
            if task.get("status") in {"completed", "failed", "cancelled"}:
                events = self._request("GET", f"/api/tasks/{task_id}/events")
                return task, events
            time.sleep(0.25)
        self.fail(f"任务 {task_id} 超过 {self.timeout:g} 秒未结束")

    @staticmethod
    def _events(events: list[dict[str, Any]], kind: str) -> list[dict[str, Any]]:
        return [item for item in events if item.get("type") == kind]

    def test_01_preflight_public_catalog_and_redaction(self) -> None:
        root = self.client.get(f"{self.base_url}/")
        self.assertEqual(root.status_code, 200)
        self.assertIn("AgentNexus", root.text)
        self.assertNotIn("file://", root.url.__str__())
        health = self._request("GET", "/api/health")
        self.assertTrue(health["ok"])
        capabilities = self._request("GET", "/api/capabilities")
        self.assertTrue(capabilities["file_upload"]["supported"])
        self.assertIn("markdown", capabilities["document_output"]["formats"])
        self.assertIn("xlsx", capabilities["document_output"]["formats"])
        for path in ("/api/models", "/api/skills", "/api/mcp", "/api/workspaces", "/api/tasks", "/api/diagnostics"):
            self.assertIsNotNone(self._request("GET", path))
        for model in self._request("GET", "/api/models"):
            self.assertNotIn("api_key", model)
            self.assertNotIn("api_key_ciphertext", model)
        for server in self._request("GET", "/api/mcp"):
            self.assertNotIn("api_key", json.dumps(server, ensure_ascii=False))
        self.assertIsInstance(self._request("GET", "/api/marketplace"), dict)

    def test_02_all_sample_upload_formats_and_limits(self) -> None:
        names = (
            "sample.txt", "sample.md", "sample.csv", "sample.json", "sample.docx",
            "sample.xlsx", "sample.pptx", "sample.pdf", "sample.html",
        )
        uploaded = [self._upload(SAMPLES / name) for name in names]
        self.assertEqual(len(uploaded), len(names))
        for item in uploaded:
            status = item.get("context_status") or {}
            self.assertEqual(status.get("state"), "ready", item)
            self.assertTrue(status.get("extractable"), item)
            self.assertNotIn("path", item)
        task, events = self._task(
            "请总结我上传的资料，列出关键结论、风险和待办。",
            attachments=[item["id"] for item in uploaded[:10]],
        )
        self.assertEqual(task.get("status"), "completed", task)
        self.assertTrue(self._events(events, "answer"))
        answer = "\n".join(str(item.get("content") or "") for item in self._events(events, "answer"))
        self.assertIn("AgentNexus", answer)

        binary = self._request(
            "POST",
            "/api/uploads",
            files={"file": ("unsupported.bin", b"\x00\x01\x02", "application/octet-stream")},
        )
        self.assertEqual((binary.get("context_status") or {}).get("state"), "unsupported")
        oversized = self._request(
            "POST",
            "/api/uploads",
            expected=(413,),
            files={
                "file": (
                    "oversized.bin",
                    b"x" * (20 * 1024 * 1024 + 1),
                    "application/octet-stream",
                )
            },
        )
        self.assertIsNotNone(oversized)

    def test_03_document_artifacts_preview_download_and_pptx_boundary(self) -> None:
        source = self._upload(SCENARIOS / "artifact-request.md")
        task, events = self._task(
            "请严格按照附件中的同一份事实，分别生成 Markdown、HTML、Word、Excel、PDF；如果 PPTX 能力已配置再生成 PPTX，并提供预览和下载。",
            attachments=[source["id"]],
        )
        capabilities = self._request("GET", "/api/capabilities")
        pptx_ready = bool((capabilities.get("document_output") or {}).get("pptx_configured"))
        expected = {"docx", "pdf", "md", "html", "xlsx"}
        if pptx_ready:
            expected.add("pptx")
        self.assertEqual(task.get("status"), "completed", task)
        artifacts = task.get("artifacts") or []
        kinds = {str(item.get("kind")) for item in artifacts}
        self.assertTrue(expected.issubset(kinds), (expected, kinds, task))
        for item in artifacts:
            artifact_id = str(item["id"])
            detail = self._request("GET", f"/api/artifacts/{artifact_id}")
            self.assertEqual(detail.get("delivery_status"), "published")
            self.assertTrue(str(detail.get("download_url") or "").endswith("/download"))
            download = self.client.get(f"{self.base_url}{detail['download_url']}")
            self.assertEqual(download.status_code, 200, download.text[:300])
            self.assertGreater(len(download.content), 20)
            preview = self._request("GET", f"/api/artifacts/{artifact_id}/preview")
            self.assertNotEqual(preview.get("preview_kind"), "error", preview)
            raw = json.dumps(preview, ensure_ascii=False)
            self.assertNotIn("/Users/", raw)
            self.assertNotIn("Traceback", raw)
        if not pptx_ready:
            answer = "\n".join(
                str(item.get("content") or "") for item in self._events(events, "answer")
            )
            self.assertIn("PowerPoint", answer)
            self.assertIn("未生成", answer)
            errors = self._events(events, "error")
            if errors:
                public = " ".join(str(item.get("content") or "") for item in errors)
                self.assertIn("PowerPoint", public)
                self.assertNotIn("APP_ARTIFACT_TOOL_ENTRYPOINT", public)

    def test_04_model_config_direct_env_duplicate_disable_and_delete(self) -> None:
        suffix = uuid.uuid4().hex[:10]
        direct_id = f"live-model-{suffix}"
        env_id = f"live-env-{suffix}"
        try:
            direct = self._request(
                "POST", "/api/models", expected=(200,), json={
                    "id": direct_id,
                    "name": "Live direct key model",
                    "provider": "openai_compatible",
                    "model": "fixture-model",
                    "base_url": "https://example.invalid/v1",
                    "api_key_mode": "direct",
                    "api_key": "sk-live-test-redaction",
                },
            )
            self.assertTrue(direct.get("has_api_key"))
            self.assertNotIn("api_key", direct)
            duplicate = self._request(
                "POST", "/api/models", expected=(409,), json={
                    "id": direct_id, "name": "duplicate", "model": "x",
                    "api_key_mode": "direct", "api_key": "secret",
                }
            )
            self.assertIn("already exists", json.dumps(duplicate, ensure_ascii=False))
            env = self._request(
                "POST", "/api/models", json={
                    "id": env_id,
                    "name": "Live env model",
                    "provider": "openai_compatible",
                    "model": "fixture-model",
                    "base_url": "https://example.invalid/v1",
                    "api_key_mode": "env",
                    "api_key_env": "AGENTNEXUS_LIVE_MISSING_KEY",
                },
            )
            self.assertEqual(env.get("api_key_mode"), "env")
            self.assertEqual((env.get("readiness") or {}).get("state"), "needs_config")
            self._request("PUT", f"/api/models/{direct_id}", json={"enabled": False})
            disabled = next(item for item in self._request("GET", "/api/models") if item["id"] == direct_id)
            self.assertFalse(disabled["enabled"])
        finally:
            for model_id in (direct_id, env_id):
                self._request("DELETE", f"/api/models/{model_id}", expected=(200, 404))

    def test_05_skill_lifecycle_package_marketplace_and_invalid_archives(self) -> None:
        suffix = uuid.uuid4().hex[:10]
        skill_id = f"live-skill-{suffix}"
        created_market = None
        try:
            created = self._request(
                "POST", "/api/skills", json={
                    "id": skill_id,
                    "name": "Live 会议纪要 Skill",
                    "description": "live acceptance",
                    "content": "---\nname: live-skill\n---\n整理摘要、决策、风险和行动项。",
                    "enabled": True,
                },
            )
            self.assertEqual(created["id"], skill_id)
            self._request("PUT", f"/api/skills/{skill_id}", json={"enabled": False})
            self.assertFalse(self._request("GET", f"/api/skills/{skill_id}")["enabled"])
            exported = self.client.get(f"{self.base_url}/api/skills/{skill_id}/export")
            self.assertEqual(exported.status_code, 200)
            with zipfile.ZipFile(io.BytesIO(exported.content)) as archive:
                self.assertIn("SKILL.md", archive.namelist())

            installed = self._request(
                "POST", "/api/skills/install/upload",
                files={
                    "file": (
                        "sample_skill.zip",
                        (SAMPLES / "sample_skill.zip").read_bytes(),
                        "application/zip",
                    )
                },
            )
            self.assertTrue(installed.get("id"))
            self.assertTrue(self._request("GET", f"/api/skills/{installed['id']}/files"))

            traversal = io.BytesIO()
            with zipfile.ZipFile(traversal, "w") as archive:
                archive.writestr("../SKILL.md", "---\nname: unsafe\n---\nunsafe")
            self._request(
                "POST", "/api/skills/install/upload", expected=(400,),
                files={"file": ("unsafe.zip", traversal.getvalue(), "application/zip")},
            )
            multiple = io.BytesIO()
            with zipfile.ZipFile(multiple, "w") as archive:
                archive.writestr("one/SKILL.md", "---\nname: one\n---\none")
                archive.writestr("two/SKILL.md", "---\nname: two\n---\ntwo")
            self._request(
                "POST", "/api/skills/install/upload", expected=(400,),
                files={"file": ("multiple.zip", multiple.getvalue(), "application/zip")},
            )

            market = self._request("GET", "/api/marketplace")
            candidate = next((item for item in market.get("skills", []) if not item.get("installed")), None)
            if candidate:
                created_market = candidate["id"]
                installed_market = self._request("POST", f"/api/marketplace/skills/{created_market}/install")
                self.assertEqual(installed_market.get("id"), created_market)
                self._request("PUT", f"/api/skills/{created_market}", json={"enabled": False})
        finally:
            self._request("DELETE", f"/api/skills/{skill_id}", expected=(200, 404))
            if created_market:
                self._request("DELETE", f"/api/skills/{created_market}", expected=(200, 404))

    def test_06_mcp_import_readonly_missing_parameter_disabled_and_remote_errors(self) -> None:
        readonly = self._request(
            "POST", "/api/mcp/import",
            files={
                "file": (
                    "mcp-readonly.json",
                    (SAMPLES / "mcp-readonly.json").read_bytes(),
                    "application/json",
                )
            },
        )
        readonly_id = "agentnexus-sample-readonly"
        self.assertTrue(any(item.get("id") == readonly_id for item in readonly))
        stored = self._request("GET", f"/api/mcp/{readonly_id}")
        self.assertFalse(stored.get("enabled"))
        self.assertNotIn("/Users/", json.dumps(stored, ensure_ascii=False))

        missing = self._request(
            "POST", "/api/mcp/import",
            files={
                "file": (
                    "mcp-missing-parameter.json",
                    (SAMPLES / "mcp-missing-parameter.json").read_bytes(),
                    "application/json",
                )
            },
        )
        missing_id = "agentnexus-sample-missing-parameter"
        self.assertTrue(any(item.get("id") == missing_id for item in missing))
        tools = self._request("GET", f"/api/mcp/{missing_id}/tools")
        self.assertTrue(any(item.get("name") == "lookup_project_status" for item in tools))
        response = self._request(
            "POST", f"/api/mcp/{missing_id}/tools/lookup_project_status/invoke",
            expected=(400,), json={"arguments": {}},
        )
        self.assertIn("project_id", json.dumps(response, ensure_ascii=False))
        self.assertNotIn("Traceback", json.dumps(response, ensure_ascii=False))

        custom_id = f"live-mcp-{uuid.uuid4().hex[:10]}"
        try:
            created = self._request(
                "POST", "/api/mcp", json={
                    "id": custom_id,
                    "name": "Live readonly MCP",
                    "kind": "mcp_http",
                    "enabled": False,
                    "config": {"url": "https://example.invalid/live"},
                    "tools": [{
                        "name": "read_status",
                        "description": "read only fixture",
                        "effect": "read",
                        "annotations": {"readOnlyHint": True},
                        "input_schema": {"type": "object", "properties": {}},
                    }],
                },
            )
            self.assertFalse(created.get("enabled"))
            disabled = self._request(
                "POST", f"/api/mcp/{custom_id}/tools/read_status/invoke",
                expected=(400,), json={"arguments": {}},
            )
            self.assertNotIn("Traceback", json.dumps(disabled, ensure_ascii=False))
            self._request("POST", "/api/mcp/install/url", expected=(400,), json={"url": "https://example.invalid/mcp.json"})
        finally:
            self._request("DELETE", f"/api/mcp/{custom_id}", expected=(200, 404))

    def test_07_expert_team_workspace_memory_and_knowledge_lifecycle(self) -> None:
        suffix = uuid.uuid4().hex[:10]
        agent_ids = [f"live-agent-{suffix}-{role}" for role in ("product", "tech", "security")]
        team_id = f"live-team-{suffix}"
        workspace_id = f"live-workspace-{suffix}"
        kb_id = f"live-kb-{suffix}"
        memory_id = ""
        try:
            for agent_id, name in zip(agent_ids, ("Live 产品体验", "Live 技术实现", "Live 安全审查")):
                created = self._request(
                    "POST", "/api/agents", json={
                        "id": agent_id, "name": name, "model": "deterministic",
                        "skills": ["general_task"], "mcp_servers": [],
                        "permissions": {"read_only": True},
                    },
                )
                self.assertEqual(created["id"], agent_id)
            team = self._request(
                "POST", "/api/expert-teams", expected=(201,), json={
                    "id": team_id,
                    "name": "Live 验收专家团",
                    "description": "live acceptance",
                    "supervisor_agent_id": agent_ids[0],
                    "aggregation_prompt": "合并成员交付并输出统一 P0、P1、P2 结论。",
                    "acceptance": [{"id": "has-conclusion", "title": "必须有统一结论"}],
                    "members": [
                        {"agent_id": agent_ids[1], "role": "技术实现", "member_prompt": "分析技术风险"},
                        {"agent_id": agent_ids[2], "role": "安全审查", "member_prompt": "分析安全风险"},
                    ],
                    "enabled": True,
                },
            )
            self.assertEqual(team["id"], team_id)
            run = self._request(
                "POST", f"/api/expert-teams/{team_id}/runs", expected=(202,), json={
                    "message": "请从技术实现和安全风险两个角度评审 AgentNexus，输出统一结论。",
                    "model_id": "deterministic",
                    "conversation_id": f"live-expert-{suffix}",
                },
            )
            task_id = run["task"]["id"]
            deadline = time.monotonic() + self.timeout
            while time.monotonic() < deadline:
                task = self._request("GET", f"/api/tasks/{task_id}")
                if task.get("status") in {"completed", "failed", "cancelled"}:
                    break
                time.sleep(0.25)
            self.assertEqual(task.get("status"), "completed", task)
            events = self._request("GET", f"/api/tasks/{task_id}/events")
            self.assertTrue(
                self._events(events, "expert_selection")
                or self._events(events, "team_run")
                or any(
                    event.get("type") == "plan"
                    and "专家协作" in str(event.get("title") or "")
                    for event in events
                )
            )
            self._request("PUT", f"/api/expert-teams/{team_id}", json={"enabled": False})
            self._request(
                "POST", f"/api/expert-teams/{team_id}/runs", expected=(400, 404), json={"message": "再次评审", "model_id": "deterministic"}
            )

            workspace = self._request(
                "POST", "/api/workspaces", expected=(201,), json={"id": workspace_id, "name": "Live workspace"}
            )
            self.assertEqual(workspace["id"], workspace_id)
            self._request("PUT", f"/api/workspaces/{workspace_id}", json={"name": "Live workspace updated"})
            self.assertTrue(any(item.get("id") == workspace_id for item in self._request("GET", "/api/workspaces")))

            memory = self._request(
                "POST", "/api/memories", expected=(201,), json={
                    "workspace_id": workspace_id, "scope_type": "user",
                    "title": "live preference", "content": "评审用中文并按 P0、P1、P2 排序。",
                }
            )
            memory_id = memory["id"]
            memory_scope = {"workspace_id": workspace_id}
            self._request("PUT", f"/api/memories/{memory_id}", params=memory_scope, json={"content": "评审用中文并按 P0、P1、P2 排序。", "reason": "live edit"})
            self.assertTrue(self._request("GET", f"/api/memories/{memory_id}/revisions", params=memory_scope))
            self._request("POST", f"/api/memories/{memory_id}/disable", params=memory_scope)
            self._request("POST", f"/api/memories/{memory_id}/enable", params=memory_scope)

            base = self._request(
                "POST", "/api/knowledge-bases", expected=(201,), json={
                    "id": kb_id, "name": "Live knowledge", "workspace_id": workspace_id,
                }
            )
            self.assertEqual(base["id"], kb_id)
            upload = self._upload(SCENARIOS / "knowledge-base-brief.md")
            document = self._request(
                "POST", f"/api/knowledge-bases/{kb_id}/documents/upload", expected=(201,),
                params={"workspace_id": workspace_id},
                json={"upload_id": upload["id"]},
            )
            self.assertIn(document.get("status"), {"indexed", "ready", "completed"})
            self.assertTrue(self._request("GET", f"/api/knowledge-bases/{kb_id}/documents", params={"workspace_id": workspace_id}))
            hits = self._request("GET", "/api/knowledge/search", params={"q": "ORBIT-7391", "base_id": kb_id, "workspace_id": workspace_id})
            self.assertTrue(hits.get("results") or hits.get("items") or hits.get("matches"))
            self._request("PUT", f"/api/knowledge-bases/{kb_id}", params={"workspace_id": workspace_id}, json={"name": "Live knowledge", "enabled": False})
            disabled_hits = self._request(
                "GET", "/api/knowledge/search",
                expected=(200, 404),
                params={"q": "ORBIT-7391", "base_id": kb_id, "workspace_id": workspace_id},
            )
            if isinstance(disabled_hits, dict):
                self.assertFalse(disabled_hits.get("results") or disabled_hits.get("items") or disabled_hits.get("matches"))
        finally:
            if memory_id:
                self._request("DELETE", f"/api/memories/{memory_id}", params={"workspace_id": workspace_id}, expected=(200, 204, 404))
            self._request("DELETE", f"/api/knowledge-bases/{kb_id}", params={"workspace_id": workspace_id}, expected=(200, 204, 404))
            deleted_team = self._request(
                "DELETE", f"/api/expert-teams/{team_id}", expected=(200, 204, 404, 409)
            )
            # A team with an audit trail is intentionally retained for history;
            # the supported cleanup operation is disabling it.
            if isinstance(deleted_team, dict) and deleted_team.get("detail"):
                self._request("PUT", f"/api/expert-teams/{team_id}", json={"enabled": False})
            for agent_id in agent_ids:
                # Agents currently have create/update/list APIs only.  Keep the
                # records so team audit history remains referentially intact;
                # a DELETE response of 405 is the documented capability
                # boundary rather than a failed cleanup.
                self._request("DELETE", f"/api/agents/{agent_id}", expected=(200, 204, 404, 405))
            self._request("DELETE", f"/api/workspaces/{workspace_id}", expected=(200, 204, 404))

    def test_08_automation_webhook_idempotency_and_runtime_stream(self) -> None:
        suffix = uuid.uuid4().hex[:10]
        loop_id = f"live-loop-{suffix}"
        webhook_id = f"live-hook-{suffix}"
        secret = "live-webhook-secret-123456"
        try:
            loop = self._request(
                "POST", "/api/loops", expected=(200,), json={
                    "id": loop_id, "name": "Live once loop", "prompt": "回复自动化验收成功",
                    "agent_id": "general-agent", "model_id": "deterministic",
                    "trigger_type": "once", "once_at": (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
                    "max_runs": 1, "auto_start": False,
                }
            )
            self.assertEqual(loop["status"], "paused")
            self._request("POST", f"/api/loops/{loop_id}/run", expected=(202,))
            deadline = time.monotonic() + self.timeout
            while time.monotonic() < deadline:
                runs = self._request("GET", f"/api/loops/{loop_id}/runs")
                if runs and runs[0].get("status") in {"completed", "failed", "cancelled"}:
                    break
                time.sleep(0.25)
            self.assertTrue(runs)
            self.assertEqual(runs[0].get("status"), "completed", runs)
            self._request("POST", f"/api/loops/{loop_id}/start", expected=(409,))

            hook = self._request(
                "POST", "/api/loops", expected=(200,), json={
                    "id": webhook_id, "name": "Live webhook", "prompt": "回复 webhook 验收成功",
                    "agent_id": "general-agent", "model_id": "deterministic",
                    "trigger_type": "webhook", "webhook_secret": secret,
                    "auto_start": True,
                }
            )
            self.assertEqual(hook["status"], "active")
            body = b'{"source":"live"}'
            timestamp = str(int(time.time()))
            signature = hmac.new(secret.encode(), timestamp.encode() + b"." + body, hashlib.sha256).hexdigest()
            self._request("POST", f"/api/loops/{webhook_id}/webhook", expected=(401,), content=body, headers={"x-automation-timestamp": timestamp, "x-automation-signature": "sha256=bad", "idempotency-key": "bad-signature"})
            first = self._request("POST", f"/api/loops/{webhook_id}/webhook", expected=(202,), content=body, headers={"x-automation-timestamp": timestamp, "x-automation-signature": f"sha256={signature}", "idempotency-key": f"live-{suffix}"})
            second = self._request("POST", f"/api/loops/{webhook_id}/webhook", expected=(202,), content=body, headers={"x-automation-timestamp": timestamp, "x-automation-signature": f"sha256={signature}", "idempotency-key": f"live-{suffix}"})
            self.assertFalse(first.get("duplicate"))
            self.assertTrue(second.get("duplicate"))
            self._request("GET", "/api/notifications")
        finally:
            self._request("POST", f"/api/loops/{webhook_id}/pause", expected=(200, 404))
            self._request("DELETE", f"/api/loops/{webhook_id}", expected=(200, 404))
            self._request("DELETE", f"/api/loops/{loop_id}", expected=(200, 404))

    def test_09_search_disabled_stream_cursor_and_public_error_contract(self) -> None:
        capabilities = self._request("GET", "/api/capabilities")
        self.assertFalse((capabilities.get("web_search") or {}).get("enabled"))
        task, events = self._task("搜索今天的 AI 行业新闻并附来源链接。")
        self.assertIn(task.get("status"), {"completed", "failed"})
        public = json.dumps(events, ensure_ascii=False)
        self.assertNotIn("Traceback", public)
        self.assertNotIn("/Users/", public)
        # The SSE endpoint must expose public events with monotone cursors even
        # when the task has already reached a terminal state.
        task_id = task["id"]
        with self.client.stream("GET", f"{self.base_url}/api/tasks/{task_id}/events/stream", timeout=10.0) as response:
            self.assertEqual(response.status_code, 200)
            chunk = next(response.iter_text())
            self.assertIn("event:", chunk)
            self.assertNotIn("Traceback", chunk)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
