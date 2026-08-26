"""Additional live checks for TEST_PLAN items not covered by the core suites.

This file deliberately keeps the plan and immutable fixtures untouched.  It
uses the running local service, creates only disposable resources, and records
only public API evidence (never secrets or server paths).
"""

from __future__ import annotations

import io
import json
import time
import uuid
import unittest
import zipfile
from pathlib import Path
from typing import Any

import httpx


ROOT = Path(__file__).resolve().parents[1]
SAMPLES = ROOT / "tests" / "fixtures" / "agentnexus_samples"
SCENARIOS = SAMPLES / "scenarios"


class PlanCompletenessLiveTests(unittest.TestCase):
    base_url = "http://127.0.0.1:8000"
    timeout = 30.0

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
            raise unittest.SkipTest(f"本地服务未启动：{exc}") from exc

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
            f"{method} {path}: {response.text[:1000]}",
        )
        if not response.content:
            return None
        return response.json() if "json" in response.headers.get("content-type", "") else response.content

    def _task(
        self,
        message: str,
        *,
        conversation_id: str | None = None,
        model_id: str = "deterministic",
        workspace: str = "default",
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        created = self._request(
            "POST",
            "/api/tasks",
            json={
                "message": message,
                "agent_id": "general-agent",
                "model_id": model_id,
                "conversation_id": conversation_id or f"live-completeness-{uuid.uuid4().hex}",
                "workspace": workspace,
            },
        )
        task_id = created["id"]
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            task = self._request("GET", f"/api/tasks/{task_id}")
            if task.get("status") in {"completed", "failed", "cancelled"}:
                return task, self._request("GET", f"/api/tasks/{task_id}/events")
            time.sleep(0.15)
        self.fail(f"任务 {task_id} 未在 {self.timeout:g} 秒内结束")

    @staticmethod
    def _event(events: list[dict[str, Any]], event_type: str) -> list[dict[str, Any]]:
        return [item for item in events if item.get("type") == event_type]

    def test_10_model_validation_test_status_and_secret_redaction(self) -> None:
        suffix = uuid.uuid4().hex[:10]
        direct_id = f"live-model-edge-{suffix}"
        env_id = f"live-model-env-edge-{suffix}"
        try:
            invalid_url = self.client.post(
                f"{self.base_url}/api/models",
                json={
                    "id": f"bad-url-{suffix}",
                    "name": "bad url",
                    "model": "fixture",
                    "api_key_mode": "direct",
                    "api_key": "sk-never-return-this",
                    "base_url": "ftp://invalid.example/v1",
                },
            )
            self.assertIn(invalid_url.status_code, (400, 422))
            self.assertNotIn("sk-never-return-this", invalid_url.text)

            direct = self._request(
                "POST",
                "/api/models",
                json={
                    "id": direct_id,
                    "name": "Live invalid key",
                    "provider": "openai_compatible",
                    "model": "fixture-model",
                    "base_url": "https://example.invalid/v1",
                    "api_key_mode": "direct",
                    "api_key": "sk-live-secret-edge",
                },
            )
            self.assertTrue(direct.get("has_api_key"))
            self.assertNotIn("api_key", direct)
            self.assertNotIn("sk-live-secret-edge", json.dumps(direct, ensure_ascii=False))

            tested = self._request(
                "POST", f"/api/models/{direct_id}/test", expected=(400,)
            )
            self.assertIsNotNone(tested)
            listed = next(item for item in self._request("GET", "/api/models") if item["id"] == direct_id)
            self.assertEqual((listed.get("last_test") or {}).get("status"), "fail")
            self.assertNotIn("sk-live-secret-edge", json.dumps(listed, ensure_ascii=False))

            env = self._request(
                "POST",
                "/api/models",
                json={
                    "id": env_id,
                    "name": "Live missing env",
                    "provider": "openai_compatible",
                    "model": "fixture-model",
                    "base_url": "https://example.invalid/v1",
                    "api_key_mode": "env",
                    "api_key_env": "AGENTNEXUS_COMPLETENESS_MISSING_KEY",
                },
            )
            self.assertEqual((env.get("readiness") or {}).get("state"), "needs_config")
            self.assertIn("AGENTNEXUS_COMPLETENESS_MISSING_KEY", (env.get("readiness") or {}).get("detail", ""))
            deterministic_delete = self._request(
                "DELETE", "/api/models/deterministic", expected=(400,)
            )
            self.assertIn("不能删除", json.dumps(deterministic_delete, ensure_ascii=False))
        finally:
            for model_id in (direct_id, env_id):
                self._request("DELETE", f"/api/models/{model_id}", expected=(200, 404))

    def test_11_conversation_boundary_runtime_children_and_cancel(self) -> None:
        conversation = f"live-context-{uuid.uuid4().hex}"
        first, first_events = self._task(
            "解释一下 Transformer 的自注意力机制。", conversation_id=conversation
        )
        second, second_events = self._task(
            "用上面的内容举一个电商推荐的例子。", conversation_id=conversation
        )
        isolated, isolated_events = self._task("上面的内容是什么？")
        self.assertEqual(first.get("status"), "completed")
        self.assertEqual(second.get("status"), "completed")
        self.assertEqual(isolated.get("status"), "completed")
        self.assertTrue(self._event(second_events, "intent"))
        self.assertTrue(self._event(second_events, "plan"))
        self.assertTrue(self._event(second_events, "verification_result"))
        self.assertFalse(
            any(
                (event.get("data") or {}).get("server_id") == "weather"
                for event in self._event(second_events, "tool_call")
            )
        )
        messages = self._request("GET", f"/api/conversations/{conversation}/messages")
        self.assertGreaterEqual(len(messages.get("messages") or []), 4)
        self.assertNotEqual(first.get("conversation_id"), isolated.get("conversation_id"))

        runtime = self._request("GET", f"/api/tasks/{second['id']}/runtime")
        self.assertTrue(runtime.get("runs"))
        nodes = runtime.get("nodes") or []
        self.assertTrue(nodes)
        # Public runtime data is at most two levels while still exposing the
        # selected model/Skill/MCP capability calls separately.
        self.assertLessEqual(max((str(item.get("node_key", "")).count(":" ) for item in nodes), default=0), 2)
        trace = runtime.get("trace_summary") or {}
        self.assertIn("verification_state", trace)
        self.assertEqual(trace.get("verification_state"), "passed")

        cancellable = self._request(
            "POST",
            "/api/tasks",
            json={
                "message": "请执行一个可取消的离线流程。",
                "agent_id": "general-agent",
                "model_id": "deterministic",
                "conversation_id": f"live-cancel-{uuid.uuid4().hex}",
            },
        )
        cancel = self._request(
            "POST", f"/api/tasks/{cancellable['id']}/cancel", expected=(202, 409), json={"reason": "live test"}
        )
        if cancel.get("ok"):
            deadline = time.monotonic() + self.timeout
            while time.monotonic() < deadline:
                current = self._request("GET", f"/api/tasks/{cancellable['id']}")
                if current.get("status") in {"completed", "failed", "cancelled"}:
                    break
                time.sleep(0.1)
            self.assertIn(current.get("status"), {"completed", "cancelled"})

    def test_12_skill_path_mcp_shapes_and_install_failure_boundaries(self) -> None:
        # The same fixture is intentionally installed through the local-path
        # route as well as the ZIP route covered by the extended plan tests.
        path_install = self._request(
            "POST",
            "/api/skills/install/path",
            expected=(200, 400),
            json={"path": str(SAMPLES / "sample_skill"), "enabled": True},
        )
        if path_install.get("id"):
            self.assertEqual(path_install.get("id"), "meeting_notes_sample")
        else:
            # Local path installation is intentionally disabled unless the
            # administrator configures APP_SKILL_LOCAL_ROOTS.  The public
            # response is the expected conditional capability boundary.
            self.assertIn("APP_SKILL_LOCAL_ROOTS", json.dumps(path_install, ensure_ascii=False))
        self.assertTrue(self._request("GET", "/api/skills/meeting_notes_sample/files"))
        self.assertTrue(
            any(item.get("id") == "meeting_notes_sample" for item in self._request("GET", "/api/skills"))
        )
        remote_skill = self._request(
            "POST",
            "/api/skills/install/url",
            expected=(400,),
            json={"url": "https://example.invalid/agentnexus-skill.zip"},
        )
        self.assertNotIn("Traceback", json.dumps(remote_skill, ensure_ascii=False))

        mcp_id = f"live-shape-mcp-{uuid.uuid4().hex[:10]}"
        try:
            config = {
                "mcpServers": {
                    mcp_id: {
                        "name": "Live local stdio shape",
                        "command": "python3",
                        "args": ["-c", "print('fixture')"],
                        "enabled": False,
                        "tools": [
                            {
                                "name": "read_fixture",
                                "description": "read only",
                                "annotations": {"readOnlyHint": True},
                                "inputSchema": {"type": "object", "properties": {}},
                            }
                        ],
                    }
                }
            }
            imported = self._request(
                "POST",
                "/api/mcp/import",
                files={"file": ("mcp-shape.json", json.dumps(config).encode(), "application/json")},
            )
            self.assertTrue(any(item.get("id") == mcp_id for item in imported))
            server = self._request("GET", f"/api/mcp/{mcp_id}")
            self.assertFalse(server.get("enabled"))
            self.assertNotIn("/Users/", json.dumps(server, ensure_ascii=False))
            self.assertTrue(self._request("GET", f"/api/mcp/{mcp_id}/tools"))
            disabled = self._request(
                "POST",
                f"/api/mcp/{mcp_id}/tools/read_fixture/invoke",
                expected=(400,),
                json={"arguments": {}},
            )
            self.assertNotIn("Traceback", json.dumps(disabled, ensure_ascii=False))
            malformed = self._request(
                "POST",
                "/api/mcp/import",
                expected=(400,),
                files={"file": ("bad.json", b"{not-json", "application/json")},
            )
            self.assertNotIn("Traceback", json.dumps(malformed, ensure_ascii=False))
        finally:
            self._request("DELETE", f"/api/mcp/{mcp_id}", expected=(200, 404))

    def test_13_workspace_isolation_and_controlled_artifact_access(self) -> None:
        suffix = uuid.uuid4().hex[:10]
        workspace_a = f"live-a-{suffix}"
        workspace_b = f"live-b-{suffix}"
        base_id = f"live-isolated-kb-{suffix}"
        self._request("POST", "/api/workspaces", expected=(201,), json={"id": workspace_a, "name": "Live A"})
        self._request("POST", "/api/workspaces", expected=(201,), json={"id": workspace_b, "name": "Live B"})
        try:
            base = self._request(
                "POST",
                "/api/knowledge-bases",
                expected=(201,),
                json={"id": base_id, "name": "Live isolated KB", "workspace_id": workspace_a, "visibility": "private"},
            )
            self.assertEqual(base.get("workspace_id"), workspace_a)
            upload = self._request(
                "POST",
                "/api/uploads",
                files={
                    "file": (
                        "knowledge-base-brief.md",
                        (SCENARIOS / "knowledge-base-brief.md").read_bytes(),
                        "text/markdown",
                    )
                },
            )
            indexed = self._request(
                "POST",
                f"/api/knowledge-bases/{base_id}/documents/upload",
                expected=(201,),
                params={"workspace_id": workspace_a},
                json={"upload_id": upload["id"]},
            )
            self.assertIn(indexed.get("status"), {"indexed", "ready", "completed"})
            foreign = self._request(
                "GET",
                "/api/knowledge/search",
                expected=(200, 404),
                params={"q": "ORBIT-7391", "base_id": base_id, "workspace_id": workspace_b},
            )
            if isinstance(foreign, dict):
                self.assertFalse(foreign.get("results") or foreign.get("items") or foreign.get("matches"))

            invalid_artifact = self._request(
                "GET", "/api/artifacts/not-a-real-artifact/download", expected=(404,)
            )
            self.assertNotIn("/Users/", json.dumps(invalid_artifact, ensure_ascii=False))
            traversal = self.client.get(f"{self.base_url}/api/artifacts/../.env.local/download")
            self.assertIn(traversal.status_code, (404, 400))
            self.assertNotIn("APP_ALLOW_OUTBOUND_NETWORK", traversal.text)
        finally:
            self._request("DELETE", f"/api/knowledge-bases/{base_id}", params={"workspace_id": workspace_a}, expected=(200, 204, 404))
            self._request("DELETE", f"/api/workspaces/{workspace_a}", expected=(200, 204, 404))
            self._request("DELETE", f"/api/workspaces/{workspace_b}", expected=(200, 204, 404))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
