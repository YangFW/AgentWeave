"""Live acceptance checks mapped to ``docs/TEST_PLAN.md``.

These checks use the running local service and the immutable scenario fixtures.
They intentionally do not alter the test plan or fixture contents.  Temporary
resources created by lifecycle checks are deleted in ``finally`` blocks.

Run after starting the service::

    .venv/bin/python -m unittest tests.test_plan_live_full -v

The suite skips only when a test's documented configuration prerequisite is
absent (for example, no online model or no enabled expert team).  A failed
task with an internal stack trace is treated as a test failure.
"""

from __future__ import annotations

import os
import time
import unittest
import uuid
from pathlib import Path
from typing import Any

import httpx


ROOT = Path(__file__).resolve().parents[1]
SCENARIOS = ROOT / "tests" / "fixtures" / "agentnexus_samples" / "scenarios"


class LiveAcceptanceTests(unittest.TestCase):
    base_url = os.getenv("AGENTNEXUS_BASE_URL", "http://127.0.0.1:8000").rstrip("/")
    timeout = float(os.getenv("AGENTNEXUS_LIVE_TIMEOUT", "120"))

    @classmethod
    def setUpClass(cls) -> None:
        cls.client = httpx.Client(timeout=30.0)
        try:
            response = cls.client.get(f"{cls.base_url}/api/health")
            response.raise_for_status()
            if not response.json().get("ok"):
                raise RuntimeError("健康检查未返回 ok=true")
        except Exception as exc:  # pragma: no cover - local prerequisite
            cls.client.close()
            raise unittest.SkipTest(f"本地 AgentNexus 服务未启动：{exc}") from exc

    @classmethod
    def tearDownClass(cls) -> None:
        cls.client.close()

    def _request(self, method: str, path: str, *, expected: tuple[int, ...] = (200,), **kwargs: Any) -> Any:
        response = self.client.request(method, f"{self.base_url}{path}", **kwargs)
        self.assertIn(response.status_code, expected, f"{method} {path}: {response.text[:800]}")
        if not response.content:
            return None
        return response.json()

    def _upload(self, filename: str) -> dict[str, Any]:
        path = SCENARIOS / filename
        self.assertTrue(path.is_file(), path)
        with path.open("rb") as handle:
            response = self.client.post(
                f"{self.base_url}/api/uploads",
                files={"file": (path.name, handle, "text/markdown")},
            )
        self.assertEqual(response.status_code, 200, response.text[:800])
        item = response.json()
        self.assertEqual(item.get("name"), path.name)
        context_status = item.get("context_status") or {}
        self.assertEqual(context_status.get("state"), "ready")
        self.assertTrue(context_status.get("extractable"))
        return item

    def _ready_model(self) -> str:
        models = self._request("GET", "/api/models")
        for model in models:
            if model.get("id") != "deterministic" and model.get("enabled") and (model.get("readiness") or {}).get("state") == "ready":
                return str(model["id"])
        self.skipTest("没有配置就绪的在线模型；复杂创作场景按测试前置条件跳过")

    def _wait_task(self, task_id: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            task = self._request("GET", f"/api/tasks/{task_id}")
            if task.get("status") in {"completed", "failed", "cancelled"}:
                return task, self._request("GET", f"/api/tasks/{task_id}/events")
            time.sleep(0.25)
        self.fail(f"任务 {task_id} 超过 {self.timeout:g} 秒未结束")

    def _task(self, message: str, conversation_id: str, *, model_id: str = "deterministic", attachments: list[str] | None = None, **extra: Any) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        payload: dict[str, Any] = {
            "message": message,
            "agent_id": "general-agent",
            "model_id": model_id,
            "conversation_id": conversation_id,
        }
        if attachments:
            payload["attachment_ids"] = attachments
        payload.update(extra)
        created = self._request("POST", "/api/tasks", json=payload)
        return self._wait_task(created["id"])

    @staticmethod
    def _events(events: list[dict[str, Any]], kind: str) -> list[dict[str, Any]]:
        return [event for event in events if event.get("type") == kind]

    def test_00_health_capabilities_and_sample_catalog(self) -> None:
        capabilities = self._request("GET", "/api/capabilities")
        self.assertTrue(capabilities["outbound_network"]["enabled"])
        self.assertTrue(capabilities["file_upload"]["supported"])
        self.assertIn("markdown", capabilities["document_output"]["formats"])
        for filename in (
            "weather-travel-brief.md",
            "transformer-learning.md",
            "codex-alternative-prd.md",
            "meeting-notes.md",
            "knowledge-base-brief.md",
            "expert-review-brief.md",
            "artifact-request.md",
        ):
            self.assertTrue((SCENARIOS / filename).is_file(), filename)

    def test_N01_weather_context_and_N03_missing_city_cancel(self) -> None:
        conversation = f"live-accept-weather-{uuid.uuid4().hex}"
        first, first_events = self._task("今天天气怎么样？", conversation)
        self.assertEqual(first.get("status"), "completed")
        self.assertTrue(self._events(first_events, "clarification"))

        second, second_events = self._task("宁波", conversation)
        self.assertEqual(second.get("status"), "completed")
        self.assertTrue(self._events(second_events, "tool_call"))
        self.assertTrue(self._events(second_events, "tool_result"))

        third, third_events = self._task("明天呢？", conversation)
        self.assertEqual(third.get("status"), "completed")
        self.assertTrue(any((event.get("data") or {}).get("tool_name") == "forecast" for event in self._events(third_events, "tool_result")))

        cancel_conversation = f"live-accept-cancel-{uuid.uuid4().hex}"
        missing, missing_events = self._task("查一下天气。", cancel_conversation)
        self.assertEqual(missing.get("status"), "completed")
        self.assertTrue(self._events(missing_events, "clarification"))
        cancelled, cancelled_events = self._task("算了，不查了。", cancel_conversation)
        self.assertEqual(cancelled.get("status"), "completed")
        self.assertFalse(self._events(cancelled_events, "tool_call"))

    def test_N02_weather_to_travel_target_switch_and_pptx_boundary(self) -> None:
        conversation = f"live-accept-travel-{uuid.uuid4().hex}"
        source = self._upload("weather-travel-brief.md")
        task, events = self._task(
            "帮我写个旅行计划，明天从杭州自驾去宁波的，然后给这个旅行计划生成一个 PPT 和 Word 文档，供我下载。",
            conversation,
            model_id=self._ready_model(),
            attachments=[source["id"]],
        )
        capabilities = self._request("GET", "/api/capabilities")
        pptx_ready = bool((capabilities.get("document_output") or {}).get("pptx_configured"))
        if pptx_ready:
            self.assertEqual(task.get("status"), "completed")
            self.assertEqual({item.get("kind") for item in task.get("artifacts") or []}, {"docx", "pptx"})
        else:
            self.assertEqual(task.get("status"), "failed")
            errors = self._events(events, "error")
            self.assertTrue(errors)
            public_text = " ".join(str(event.get("content") or "") for event in errors)
            self.assertIn("PowerPoint", public_text)
            self.assertNotIn("Traceback", public_text)
        tool_calls = self._events(events, "tool_call")
        self.assertFalse(any((event.get("data") or {}).get("server_id") == "weather" for event in tool_calls), "旅行目标不应因历史上下文再次调用天气工具")

    def test_N04_markdown_structure_reuse_and_streaming(self) -> None:
        model_id = self._ready_model()
        conversation = f"live-accept-markdown-{uuid.uuid4().hex}"
        source = self._upload("transformer-learning.md")
        first, first_events = self._task(
            "帮我写个 Transformer 的入门学习文档，Markdown 格式给我。",
            conversation,
            model_id=model_id,
            attachments=[source["id"]],
        )
        self.assertEqual(first.get("status"), "completed")
        self.assertIn("md", {str(item.get("kind")) for item in first.get("artifacts") or []})
        self.assertTrue(self._events(first_events, "answer_delta"))
        self.assertTrue(any((event.get("data") or {}).get("passed") for event in self._events(first_events, "output_check")))

        structure = self._upload("codex-alternative-prd.md")
        second, second_events = self._task(
            "按照上面的文档格式和结构，帮我写一份关于 Codex 软件平替开发的项目需求文档。",
            conversation,
            model_id=model_id,
            attachments=[structure["id"]],
        )
        self.assertEqual(second.get("status"), "completed")
        answer = "\n".join(str(event.get("content") or "") for event in self._events(second_events, "answer"))
        self.assertIn("AgentNexus", answer)
        self.assertNotIn("Attention(Q, K, V)", answer)

    def test_upload_all_scenario_documents_and_conversation_messages(self) -> None:
        conversation = f"live-accept-upload-{uuid.uuid4().hex}"
        uploads = [self._upload(path.name) for path in sorted(SCENARIOS.glob("*.md"))]
        self.assertGreaterEqual(len(uploads), 7)
        task, _ = self._task(
            "请总结我上传的资料，列出关键结论、风险和待办。",
            conversation,
            attachments=[item["id"] for item in uploads[:7]],
        )
        self.assertEqual(task.get("status"), "completed")
        messages = self._request("GET", f"/api/conversations/{conversation}/messages")
        self.assertGreaterEqual(len(messages.get("messages") or []), 2)

    def test_skill_mcp_model_and_marketplace_read_paths(self) -> None:
        skills = self._request("GET", "/api/skills")
        self.assertTrue(any(item.get("id") == "general_task" for item in skills))
        mcp = self._request("GET", "/api/mcp")
        self.assertTrue(any(item.get("id") == "weather" for item in mcp))
        models = self._request("GET", "/api/models")
        self.assertTrue(any(item.get("id") == "deterministic" for item in models))
        for item in models:
            if item.get("id") != "deterministic":
                self.assertNotIn("api_key", item)
                self.assertIn("has_api_key", item)
        marketplace = self._request("GET", "/api/marketplace")
        self.assertIsInstance(marketplace, (dict, list))

    def test_knowledge_base_memory_workspace_and_cleanup(self) -> None:
        suffix = uuid.uuid4().hex[:10]
        base_id = f"live-kb-{suffix}"
        workspace_id = f"live-ws-{suffix}"
        memory_id = None
        try:
            workspace = self._request("POST", "/api/workspaces", expected=(201,), json={"id": workspace_id, "name": "Live acceptance workspace"})
            self.assertEqual(workspace.get("id"), workspace_id)
            memory = self._request("POST", "/api/memories", expected=(201,), json={"scope_type": "user", "title": "live acceptance", "content": "验收结果使用中文并按 P0、P1、P2 排序。"})
            memory_id = memory.get("id")
            self.assertTrue(memory_id)
            base = self._request("POST", "/api/knowledge-bases", expected=(201,), json={"id": base_id, "name": "Live acceptance KB"})
            upload = self._upload("knowledge-base-brief.md")
            document = self._request("POST", f"/api/knowledge-bases/{base_id}/documents/upload", expected=(201,), json={"upload_id": upload["id"]})
            self.assertIn(document.get("status"), {"indexed", "ready", "completed"})
            hits = self._request("GET", "/api/knowledge/search", params={"q": "ORBIT-7391", "base_id": base_id, "organization_id": "local-org", "workspace_id": "default", "user_id": "local-user"})
            self.assertTrue(hits.get("results") or hits.get("items") or hits.get("matches"))
        finally:
            if memory_id:
                self._request("DELETE", f"/api/memories/{memory_id}", expected=(200, 204))
            self._request("DELETE", f"/api/knowledge-bases/{base_id}", expected=(200, 204))
            self._request("DELETE", f"/api/workspaces/{workspace_id}", expected=(200, 204))

    def test_expert_mode_selection_or_configuration_message(self) -> None:
        teams = self._request("GET", "/api/expert-teams")
        enabled = [team for team in teams if team.get("enabled")]
        if not enabled:
            self.skipTest("没有启用专家团，属于 TEST_PLAN 的配置前置条件")
        task, events = self._task(
            "请从产品体验、技术实现和安全治理三个角度评审附件，并汇总 P0、P1、P2。",
            f"live-accept-expert-{uuid.uuid4().hex}",
            model_id=self._ready_model(),
            attachments=[self._upload("expert-review-brief.md")["id"]],
            executor_type="team",
        )
        self.assertIn(task.get("status"), {"completed", "failed"})
        self.assertTrue(self._events(events, "expert_selection") or self._events(events, "plan"))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
