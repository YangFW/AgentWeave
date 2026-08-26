"""Small interface-level smoke checks for a running local AgentNexus service.

The comprehensive checklist remains in ``docs/TEST_PLAN.md``.  This script is
deliberately independent of the unit-test database and only creates disposable
conversation tasks in the running service.  Start the service first, then run:

    .venv/bin/python -m unittest tests.test_plan_live_smoke -v

Set ``AGENTNEXUS_BASE_URL`` to test another local instance.
"""

from __future__ import annotations

import os
import time
import unittest
from typing import Any

import httpx


class LivePlanSmokeTests(unittest.TestCase):
    base_url = os.getenv("AGENTNEXUS_BASE_URL", "http://127.0.0.1:8000").rstrip("/")

    @classmethod
    def setUpClass(cls) -> None:
        cls.client = httpx.Client(timeout=20.0)
        try:
            response = cls.client.get(f"{cls.base_url}/api/health")
            response.raise_for_status()
        except Exception as exc:  # pragma: no cover - depends on local service
            cls.client.close()
            raise unittest.SkipTest(f"本地 AgentNexus 服务未启动：{exc}") from exc

    @classmethod
    def tearDownClass(cls) -> None:
        cls.client.close()

    def _create_and_wait(self, message: str, conversation_id: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        response = self.client.post(
            f"{self.base_url}/api/tasks",
            json={
                "message": message,
                "agent_id": "general-agent",
                "model_id": "deterministic",
                "conversation_id": conversation_id,
            },
        )
        response.raise_for_status()
        task = response.json()
        task_id = task["id"]
        for _ in range(160):
            current = self.client.get(f"{self.base_url}/api/tasks/{task_id}").json()
            if current.get("status") in {"completed", "failed", "cancelled"}:
                events = self.client.get(f"{self.base_url}/api/tasks/{task_id}/events").json()
                return current, events
            time.sleep(0.15)
        self.fail(f"任务 {task_id} 在 24 秒内未结束")

    def test_capabilities_and_sample_catalog_are_reachable(self) -> None:
        for endpoint in ("/api/capabilities", "/api/models", "/api/skills", "/api/mcp"):
            response = self.client.get(f"{self.base_url}{endpoint}")
            self.assertEqual(response.status_code, 200, endpoint)

    def test_weather_context_is_preserved_across_rounds(self) -> None:
        conversation = f"live-weather-{time.time_ns()}"
        first, first_events = self._create_and_wait("今天天气怎么样？", conversation)
        self.assertIn(first.get("status"), {"completed", "failed"})
        self.assertTrue(any(event.get("type") == "clarification" for event in first_events))

        second, second_events = self._create_and_wait("宁波", conversation)
        self.assertEqual(second.get("status"), "completed")
        self.assertTrue(any(event.get("type") == "tool_call" for event in second_events))
        self.assertTrue(any(event.get("type") == "tool_result" for event in second_events))

        third, third_events = self._create_and_wait("明天呢？", conversation)
        self.assertEqual(third.get("status"), "completed")
        tool_results = [event for event in third_events if event.get("type") == "tool_result"]
        self.assertTrue(tool_results)
        self.assertTrue(
            any(
                (event.get("data") or {}).get("server_id") == "weather"
                and (event.get("data") or {}).get("tool_name") == "forecast"
                for event in tool_results
            )
        )

    def test_multiple_document_formats_publish_each_available_artifact(self) -> None:
        task, events = self._create_and_wait(
            "请生成一份关于 AgentNexus 测试的 Word 和 Markdown 文档，并提供下载。",
            f"live-docs-{time.time_ns()}",
        )
        self.assertEqual(task.get("status"), "completed")
        artifacts = task.get("artifacts") or []
        self.assertEqual({str(item.get("kind")) for item in artifacts}, {"docx", "md"})
        self.assertTrue(all(str(item.get("download_url") or "").endswith("/download") for item in artifacts))
        plan = next(event for event in events if event.get("type") == "plan")
        plan_data = (plan.get("data") or {}).get("plan") or {}
        self.assertEqual(plan_data.get("output_formats"), ["docx", "md"])
        check = next(event for event in events if event.get("type") == "output_check")
        self.assertTrue((check.get("data") or {}).get("passed"))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
