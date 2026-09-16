from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from app import db
from app import main as main_module
from app.services.agent_runtime import create_task_record
from app.services.goal_spec_service import compile_draft, finalize, public_goal_summary
from app.services.task_state import TaskStateService


class TaskRuntimeApiTests(unittest.TestCase):
    """HTTP acceptance tests for the durable task-control contract.

    These tests deliberately create task/run state directly and replace the
    background runtime worker.  The API layer therefore remains under test
    without making a model request or depending on worker scheduling timing.
    """

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_db_path = db.DB_PATH
        self.original_app_db_path = os.environ.get("APP_DB_PATH")
        self.original_upload_dir = main_module.UPLOAD_DIR
        self.db_path = Path(self.temp_dir.name) / "task-runtime-api.db"
        os.environ["APP_DB_PATH"] = str(self.db_path)
        db.DB_PATH = self.db_path
        main_module.UPLOAD_DIR = Path(self.temp_dir.name) / "uploads"
        db.init_db()
        self.state = TaskStateService(db.get_conn)

        self.runtime_patch = patch.object(
            main_module.runtime, "run_task", new_callable=AsyncMock
        )
        self.scheduler_start_patch = patch.object(
            main_module.loop_scheduler, "start", return_value=None
        )
        self.scheduler_stop_patch = patch.object(
            main_module.loop_scheduler, "stop", new_callable=AsyncMock
        )
        self.run_task = self.runtime_patch.start()
        self.scheduler_start_patch.start()
        self.scheduler_stop_patch.start()
        self.client_context = TestClient(main_module.app)
        self.client = self.client_context.__enter__()

    @unittest.skipUnless(os.getenv("APP_TEST_REDIS_URL"), "需要独立测试 Redis")
    def test_redis_sse_replays_database_events_without_notification(self):
        task = create_task_record("事件补发", "general-agent")
        first = db.insert_event(task["id"], "answer", "回答", "first")
        second = db.insert_event(task["id"], "answer", "回答", "second")
        db.update_task_status(task["id"], "completed")
        # 消息已落库且从未 publish，仍须按游标补发，不能依赖 Pub/Sub 历史。
        with patch.dict(os.environ, {"REDIS_URL": os.environ["APP_TEST_REDIS_URL"]}):
            result = self.client.get(f"/api/tasks/{task['id']}/events/stream", params={"after_id": first})
        self.assertEqual(result.status_code, 200)
        self.assertIn(f"id: {second}\n", result.text)
        self.assertNotIn(f"id: {first}\n", result.text)
        self.assertIn('second', result.text)

    def test_attachment_limits_reject_without_creating_partial_task(self):
        before = len(db.query_all('SELECT id FROM tasks'))
        response = self.client.post('/api/tasks', json={'message': 'test', 'attachment_ids': [f'upload-{index}' for index in range(11)]})
        self.assertEqual(response.status_code, 422)
        response = self.client.post('/api/tasks', json={'message': 'test', 'attachment_ids': ['same-upload', 'same-upload']})
        self.assertEqual(response.status_code, 422)
        response = self.client.post('/api/tasks', json={'message': 'test', 'attachment_ids': ['missing-upload']})
        self.assertEqual(response.status_code, 404)
        for index in range(2):
            db.execute('INSERT INTO uploads(id,name,path,size,created_at) VALUES(?,?,?,?,?)', (f'large-{index}', 'large.txt', 'unused', 30 * 1024 * 1024, db.utc_now()))
        response = self.client.post('/api/tasks', json={'message': 'test', 'attachment_ids': ['large-0', 'large-1']})
        self.assertEqual(response.status_code, 413)
        self.assertEqual(len(db.query_all('SELECT id FROM tasks')), before)

    def test_task_run_and_outbox_rollback_as_one_submission(self):
        for table in ('task_runs', 'dispatch_outbox'):
            with self.subTest(table=table):
                before = {name: len(db.query_all(f'SELECT * FROM {name}')) for name in ('tasks', 'task_runs', 'dispatch_outbox')}
                db.execute(f"CREATE TRIGGER reject_submission BEFORE INSERT ON {table} BEGIN SELECT RAISE(ABORT,'test rollback'); END")
                try:
                    with patch.dict(os.environ, {'REDIS_URL': 'redis://127.0.0.1:1/0'}):
                        with self.assertRaises(sqlite3.IntegrityError):
                            self.client.post('/api/tasks', json={'message': 'test atomic submission', 'model_id': 'deterministic'})
                finally:
                    db.execute('DROP TRIGGER reject_submission')
                after = {name: len(db.query_all(f'SELECT * FROM {name}')) for name in before}
                self.assertEqual(after, before)

    def test_submission_key_reuses_task_and_rejects_conflicting_payload(self):
        headers = {'Idempotency-Key': 'stable-submission'}
        payload = {'message': '你好', 'model_id': 'deterministic'}
        with patch.object(main_module, '_schedule_runtime') as schedule:
            first = self.client.post('/api/tasks', json=payload, headers=headers)
            second = self.client.post('/api/tasks', json=payload, headers=headers)
            conflict = self.client.post('/api/tasks', json={**payload, 'message': 'different'}, headers=headers)
        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(second.status_code, 200, second.text)
        self.assertEqual(first.json()['id'], second.json()['id'])
        self.assertEqual(first.json()['run']['id'], second.json()['run']['id'])
        self.assertEqual(conflict.status_code, 409)
        schedule.assert_called_once()
        self.assertEqual(len(db.query_all('SELECT * FROM task_submissions')), 1)

    def test_task_and_event_expose_contract_version(self):
        task = create_task_record('versioned task','general-agent')
        db.insert_event(task['id'],'answer','回答','versioned answer')
        task_response = self.client.get('/api/tasks/'+task['id'])
        events = self.client.get(f"/api/tasks/{task['id']}/events").json()
        self.assertEqual(task_response.json()['schema_version'],1)
        self.assertEqual(events[0]['schema_version'],1)
        self.assertEqual(task_response.json()['status'],'queued')

    def tearDown(self) -> None:
        self.client_context.__exit__(None, None, None)
        self.scheduler_stop_patch.stop()
        self.scheduler_start_patch.stop()
        self.runtime_patch.stop()
        db.DB_PATH = self.original_db_path
        main_module.UPLOAD_DIR = self.original_upload_dir
        if self.original_app_db_path is None:
            os.environ.pop("APP_DB_PATH", None)
        else:
            os.environ["APP_DB_PATH"] = self.original_app_db_path
        self.temp_dir.cleanup()

    def _create_task(self, *, status: str = "queued") -> str:
        task = create_task_record(
            "验证可靠任务运行接口",
            "general-agent",
            conversation_id="conv_runtime_api",
        )
        db.execute(
            "UPDATE tasks SET status = ?, updated_at = ? WHERE id = ?",
            (status, db.utc_now(), task["id"]),
        )
        return str(task["id"])

    def _finish_run_with_checkpoint(
        self, task_id: str, *, status: str = "failed"
    ) -> tuple[dict, dict]:
        run = self.state.begin_run(task_id)
        node = self.state.create_node(run["id"], "prepare", "准备执行")
        self.state.start_node(node["id"])
        self.state.finish_node(node["id"], output={"prepared": True})
        checkpoint = self.state.create_checkpoint(
            run["id"],
            {"cursor": "after-prepare", "completed_nodes": [node["id"]]},
            node_id=node["id"],
            reason="safe boundary",
        )
        self.state.finish_run(
            run["id"], status=status, error={"message": "fixture interruption"}
        )
        db.update_task_status(task_id, status, result={"fixture": True})
        return run, checkpoint

    def assertFriendly4xx(self, response) -> None:  # noqa: N802 - unittest style
        self.assertGreaterEqual(response.status_code, 400, response.text)
        self.assertLess(response.status_code, 500, response.text)
        body = response.json()
        self.assertIn("detail", body, body)
        self.assertTrue(str(body["detail"]).strip(), body)

    def test_runtime_projects_runs_nodes_checkpoints_commands_and_active_run(self) -> None:
        task_id = self._create_task(status="running")
        run = self.state.begin_run(task_id)
        node = self.state.create_node(
            run["id"],
            "tool:filesystem.read",
            "读取工作区文件",
            kind="mcp",
            input_data={"path": "README.md"},
        )
        checkpoint = self.state.create_checkpoint(
            run["id"], {"cursor": 1}, node_id=node["id"], reason="before tool"
        )
        command = self.state.enqueue_command(
            task_id,
            "message",
            run_id=run["id"],
            payload={"message": "输出时补充验收结论"},
        )

        response = self.client.get(f"/api/tasks/{task_id}/runtime")

        self.assertEqual(response.status_code, 200, response.text)
        runtime = response.json()
        self.assertTrue(
            {"runs", "nodes", "checkpoints", "commands", "active_run"}.issubset(runtime)
        )
        self.assertEqual([item["id"] for item in runtime["runs"]], [run["id"]])
        self.assertEqual([item["id"] for item in runtime["nodes"]], [node["id"]])
        self.assertEqual(
            [item["id"] for item in runtime["checkpoints"]], [checkpoint["id"]]
        )
        self.assertEqual([item["id"] for item in runtime["commands"]], [command["id"]])
        self.assertEqual(runtime["active_run"]["id"], run["id"])

    def test_runtime_projects_current_public_capability_node(self) -> None:
        task_id = self._create_task(status="running")
        run = self.state.begin_run(task_id)
        root = self.state.create_node(
            run["id"], "goal:execute", "执行任务", kind="phase",
            metadata={"logical_id": "execute", "last_message": "正在执行工具"},
        )
        self.state.start_node(root["id"])
        child = self.state.create_node(
            run["id"], "goal:tool:web-search.search", "web-search.search",
            parent_node_id=root["id"], kind="mcp",
            metadata={
                "logical_id": "tool:web-search.search",
                "last_message": "正在调用 web-search.search",
                "api_key": "must-not-leak",
            },
        )
        self.state.start_node(child["id"])

        response = self.client.get(f"/api/tasks/{task_id}/runtime")

        self.assertEqual(response.status_code, 200, response.text)
        runtime = response.json()
        self.assertEqual(runtime["current_node"]["id"], child["id"])
        self.assertEqual(runtime["current_node"]["capability"]["type"], "mcp")
        self.assertEqual(runtime["current_node"]["capability"]["id"], "web-search.search")
        self.assertEqual(runtime["current_node"]["status_message"], "正在调用 web-search.search")
        self.assertEqual(runtime["trace_summary"]["current_node"]["id"], child["id"])
        self.assertEqual(runtime["trace_summary"]["node_status_counts"]["running"], 2)
        self.assertEqual(
            runtime["trace_summary"]["capabilities"][0]["id"],
            "web-search.search",
        )
        self.assertNotIn("must-not-leak", response.text)

        self.state.finish_node(child["id"], output={"summary": "搜索完成"})
        fallback = self.client.get(f"/api/tasks/{task_id}/runtime")
        self.assertEqual(fallback.status_code, 200, fallback.text)
        self.assertEqual(fallback.json()["current_node"]["id"], root["id"])

    def test_runtime_exposes_only_public_goal_verification_and_two_level_nodes(self) -> None:
        task_id = self._create_task(status="running")
        run = self.state.begin_run(task_id)
        root = self.state.create_node(run["id"], "execute", "执行", input_data={"secret": "raw-input"})
        child = self.state.create_node(
            run["id"], "tool:safe.read", "读取工具", parent_node_id=root["id"],
            input_data={"api_key": "never-public"},
        )
        draft = compile_draft(
            task_id=task_id,
            objective={"statement": "生成验收结论", "intent": "analysis"},
            inputs=[
                {
                    "key": "private_source",
                    "label": "私有来源",
                    "required": True,
                    "status": "provided",
                    "value": "private-input-value",
                    "provenance": [
                        {
                            "source_type": "user_message",
                            "source_id": task_id,
                            "field": "message",
                            "excerpt": "private-provenance-excerpt",
                        }
                    ],
                }
            ],
        )
        self.state.save_goal_spec(
            task_id, run["id"], draft.model_dump(mode="json"),
            public_summary=public_goal_summary(draft),
        )
        confirmed = finalize(draft)
        goal = self.state.save_goal_spec(
            task_id, run["id"], confirmed.model_dump(mode="json"),
            public_summary=public_goal_summary(confirmed),
        )
        public_report = {
            "schema_version": 1,
            "mode": "rules_only",
            "verdict": "rules_passed",
            "passed": True,
            "coverage": "rules_only",
            "semantic_attempted": False,
            "semantic_verified": False,
            "public_reason": "规则验收通过。",
            "rules": [
                {"id": "response", "title": "结果存在", "status": "passed", "public_reason": "已生成结果。"}
            ],
            "semantic": {"status": "skipped", "public_reason": "本轮未要求语义复核。", "repair_instructions": []},
            "repair_instructions": [],
            "judge_prompt": "never expose judge prompt",
        }
        self.state.save_verification_report(
            task_id, run["id"], goal["id"],
            {**public_report, "internal_reasoning": "never expose judge reasoning"},
            public_report=public_report,
            verifier_model_id="private-judge-model",
            candidate_sha256="a" * 64,
            evidence_sha256="b" * 64,
        )

        response = self.client.get(f"/api/tasks/{task_id}/runtime")

        self.assertEqual(response.status_code, 200, response.text)
        runtime = response.json()
        self.assertEqual(runtime["active_goal"]["id"], goal["id"])
        self.assertEqual(runtime["active_goal"]["summary"]["objective"]["statement"], "生成验收结论")
        self.assertNotIn("inputs", runtime["active_goal"]["summary"])
        self.assertEqual(runtime["verification"]["state"], "passed")
        self.assertEqual(runtime["verification"]["public_report"]["public_reason"], "规则验收通过。")
        self.assertEqual(runtime["trace_summary"]["verification_state"], "passed")
        self.assertEqual(runtime["trace_summary"]["goal"]["objective"], "生成验收结论")
        self.assertEqual(runtime["trace_summary"]["goal"]["status"], "confirmed")
        self.assertEqual(len(runtime["node_tree"]), 1)
        self.assertEqual(runtime["node_tree"][0]["id"], root["id"])
        self.assertEqual(runtime["node_tree"][0]["children"][0]["id"], child["id"])
        self.assertEqual(runtime["node_tree"][0]["children"][0]["children"], [])
        for forbidden in (
            "private-input-value", "private-provenance-excerpt", "raw-input", "never-public", "judge_prompt",
            "internal_reasoning", "private-judge-model", "candidate_sha256",
            "evidence_sha256", "spec_json", "report_json",
        ):
            self.assertNotIn(forbidden, response.text)

    def test_runtime_trace_summary_counts_public_artifacts_and_capabilities(self) -> None:
        task_id = self._create_task(status="completed")
        run = self.state.begin_run(task_id)
        skill = self.state.create_node(
            run["id"],
            "skill:word_document",
            "Word 文档 Skill",
            kind="skill",
            metadata={"logical_id": "skill:word_document", "secret": "must-not-leak"},
        )
        self.state.start_node(skill["id"])
        self.state.finish_node(skill["id"], output={"summary": "Skill 完成"})
        tool = self.state.create_node(
            run["id"],
            "tool:report.generate_document",
            "report.generate_document",
            kind="mcp",
            metadata={"logical_id": "tool:report.generate_document"},
        )
        self.state.start_node(tool["id"])
        self.state.finish_node(tool["id"], output={"summary": "文档生成完成"})
        self.state.finish_run(run["id"], status="completed")
        db.update_task_status(
            task_id,
            "completed",
            result={"summary": "已生成文档"},
            artifacts=[
                {
                    "id": "artifact-public",
                    "kind": "docx",
                    "name": "result.docx",
                    "relative_path": "result.docx",
                    "download_url": "/api/artifacts/artifact-public/download",
                    "delivery_status": "published",
                    "private_note": "must-not-leak",
                }
            ],
        )
        db.execute(
            """
            INSERT INTO artifacts(
                id, task_id, run_id, workspace_id, name, kind, mime_type, size,
                path, relative_path, sha256, metadata_json, created_at,
                delivery_status, verification_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "artifact-public",
                task_id,
                run["id"],
                "default",
                "result.docx",
                "docx",
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                12,
                "/private/artifacts/result.docx",
                "result.docx",
                "0" * 64,
                "{}",
                db.utc_now(),
                "published",
                "verification-public",
            ),
        )
        main_module.emit(
            task_id,
            "knowledge",
            "已检索项目知识库",
            "本次使用 2 个知识片段。",
            {
                "knowledge_base_ids": ["kb_trace"],
                "matches": [
                    {
                        "chunk_id": "chunk_trace_1",
                        "document_id": "doc_trace",
                        "knowledge_base_id": "kb_trace",
                        "document_name": "trace-source.md",
                        "ordinal": 0,
                        "content": "must-not-leak",
                    },
                    {
                        "chunk_id": "chunk_trace_2",
                        "document_id": "doc_trace",
                        "knowledge_base_id": "kb_trace",
                        "document_name": "trace-source.md",
                        "ordinal": 1,
                    },
                ],
            },
        )

        response = self.client.get(f"/api/tasks/{task_id}/runtime")

        self.assertEqual(response.status_code, 200, response.text)
        summary = response.json()["trace_summary"]
        self.assertEqual(summary["task_status"], "completed")
        self.assertEqual(summary["artifacts"]["total"], 1)
        self.assertEqual(summary["artifacts"]["published"], 1)
        self.assertEqual(summary["artifacts"]["formats"], ["docx"])
        self.assertEqual(summary["artifacts"]["items"][0]["name"], "result.docx")
        self.assertEqual(summary["artifacts"]["items"][0]["download_url"], "/api/artifacts/artifact-public/download")
        self.assertEqual(summary["artifacts"]["items"][0]["preview_url"], "/api/artifacts/artifact-public/preview")
        self.assertEqual(summary["knowledge"]["match_count"], 2)
        self.assertEqual(summary["knowledge"]["knowledge_base_ids"], ["kb_trace"])
        self.assertEqual(summary["knowledge"]["documents"][0]["document_name"], "trace-source.md")
        capability_ids = {item["id"] for item in summary["capabilities"]}
        self.assertEqual(capability_ids, {"word_document", "report.generate_document"})
        calls = {item["id"]: item for item in summary["capability_calls"]}
        self.assertEqual(set(calls), {"word_document", "report.generate_document"})
        self.assertEqual(calls["word_document"]["type"], "skill")
        self.assertEqual(calls["report.generate_document"]["type"], "mcp")
        self.assertEqual(calls["word_document"]["output_summary"], "Skill 完成")
        self.assertEqual(calls["report.generate_document"]["output_summary"], "文档生成完成")
        self.assertEqual(calls["word_document"]["status"], "completed")
        self.assertNotIn("must-not-leak", response.text)
        self.assertNotIn("/private/artifacts/result.docx", response.text)
        self.assertNotIn("relative_path", response.text)

    def test_message_command_is_persisted_for_the_active_run(self) -> None:
        task_id = self._create_task(status="running")
        run = self.state.begin_run(task_id)

        response = self.client.post(
            f"/api/tasks/{task_id}/commands",
            json={
                "type": "message",
                "payload": {"message": "请把结果同时整理为 Markdown 文档"},
            },
        )

        self.assertIn(response.status_code, {200, 201, 202}, response.text)
        commands = self.state.list_commands(task_id=task_id)
        self.assertEqual(len(commands), 1)
        self.assertEqual(commands[0]["type"], "message")
        self.assertEqual(commands[0]["run_id"], run["id"])
        self.assertEqual(
            commands[0]["payload"]["message"],
            "请把结果同时整理为 Markdown 文档",
        )

    def test_cancel_endpoint_enqueues_one_deduplicated_cancel_request(self) -> None:
        task_id = self._create_task(status="running")
        run = self.state.begin_run(task_id)

        first = self.client.post(
            f"/api/tasks/{task_id}/cancel", json={"reason": "用户主动取消"}
        )
        second = self.client.post(
            f"/api/tasks/{task_id}/cancel", json={"reason": "重复点击"}
        )

        self.assertIn(first.status_code, {200, 201, 202}, first.text)
        self.assertIn(second.status_code, {200, 201, 202}, second.text)
        cancel_commands = self.state.list_commands(
            task_id=task_id, command_types=["cancel"]
        )
        self.assertEqual(len(cancel_commands), 1)
        self.assertEqual(cancel_commands[0]["run_id"], run["id"])
        self.assertEqual(cancel_commands[0]["status"], "queued")

    def test_retry_creates_a_second_run_for_the_same_task(self) -> None:
        task_id = self._create_task()
        first_run, _ = self._finish_run_with_checkpoint(task_id, status="failed")

        response = self.client.post(f"/api/tasks/{task_id}/retry", json={})

        self.assertIn(response.status_code, {200, 201, 202}, response.text)
        runs = sorted(self.state.list_runs(task_id=task_id), key=lambda item: item["attempt"])
        self.assertEqual(len(runs), 2)
        self.assertEqual(runs[0]["id"], first_run["id"])
        self.assertEqual([item["attempt"] for item in runs], [1, 2])
        self.assertEqual(runs[1]["task_id"], task_id)
        self.assertEqual(runs[1]["resumed_from_checkpoint_id"], "")
        self.assertIn(runs[1]["status"], {"queued", "running"})

    def test_resume_uses_the_latest_checkpoint_and_creates_a_new_attempt(self) -> None:
        task_id = self._create_task()
        _, checkpoint = self._finish_run_with_checkpoint(task_id, status="cancelled")

        response = self.client.post(f"/api/tasks/{task_id}/resume", json={})

        self.assertIn(response.status_code, {200, 201, 202}, response.text)
        runs = sorted(self.state.list_runs(task_id=task_id), key=lambda item: item["attempt"])
        self.assertEqual(len(runs), 2)
        self.assertEqual(runs[1]["attempt"], 2)
        self.assertEqual(runs[1]["resumed_from_checkpoint_id"], checkpoint["id"])
        self.assertIn(runs[1]["status"], {"queued", "running"})

    def test_restore_checkpoint_audits_restore_and_associates_the_new_run(self) -> None:
        task_id = self._create_task()
        _, checkpoint = self._finish_run_with_checkpoint(task_id, status="failed")

        response = self.client.post(
            f"/api/tasks/{task_id}/checkpoints/{checkpoint['id']}/restore", json={}
        )

        self.assertIn(response.status_code, {200, 201, 202}, response.text)
        restored = self.state.get_checkpoint(checkpoint["id"])
        self.assertIsNotNone(restored)
        self.assertEqual(restored["restore_count"], 1)
        self.assertTrue(restored["restored_at"])
        runs = sorted(self.state.list_runs(task_id=task_id), key=lambda item: item["attempt"])
        self.assertEqual(len(runs), 2)
        self.assertEqual(runs[1]["resumed_from_checkpoint_id"], checkpoint["id"])

    def test_resume_and_restore_responses_never_return_checkpoint_state(self) -> None:
        for endpoint_kind in ("resume", "restore"):
            with self.subTest(endpoint_kind=endpoint_kind):
                task_id = self._create_task()
                run = self.state.begin_run(task_id)
                checkpoint = self.state.create_checkpoint(
                    run["id"],
                    {
                        "goal_spec": {"objective": "private-full-spec"},
                        "completed_tools": {"tool-hash": {"secret_result": "raw-tool-result"}},
                        "agent": {"system_prompt": "private-system-prompt"},
                    },
                )
                self.state.finish_run(run["id"], status="failed")
                db.update_task_status(task_id, "failed")
                path = (
                    f"/api/tasks/{task_id}/resume"
                    if endpoint_kind == "resume"
                    else f"/api/tasks/{task_id}/checkpoints/{checkpoint['id']}/restore"
                )

                response = self.client.post(path, json={})

                self.assertIn(response.status_code, {200, 201, 202}, response.text)
                self.assertEqual(response.json()["checkpoint"]["id"], checkpoint["id"])
                for forbidden in (
                    "state", "state_json", "private-full-spec", "raw-tool-result",
                    "private-system-prompt", "completed_tools", "system_prompt",
                ):
                    self.assertNotIn(forbidden, response.text)

    def test_invalid_task_commands_and_state_transitions_return_friendly_4xx(self) -> None:
        missing = self.client.get("/api/tasks/task_missing/runtime")
        self.assertEqual(missing.status_code, 404, missing.text)
        self.assertFriendly4xx(missing)

        task_id = self._create_task(status="running")
        self.state.begin_run(task_id)
        self.assertFriendly4xx(
            self.client.post(
                f"/api/tasks/{task_id}/commands",
                json={"type": "launch_missiles", "payload": {}},
            )
        )
        self.assertFriendly4xx(
            self.client.post(f"/api/tasks/{task_id}/retry", json={})
        )
        self.assertFriendly4xx(
            self.client.post(
                f"/api/tasks/{task_id}/checkpoints/tcp_missing/restore", json={}
            )
        )

    def test_task_responses_redact_attachment_storage_paths_and_internal_json(self) -> None:
        attachment = {
            "id": "upl_public_contract",
            "name": "资料.docx",
            "content_type": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "size": 1234,
            "path": "/private/server/uploads/secret-folder/资料.docx",
            "created_at": db.utc_now(),
        }
        task = create_task_record(
            "读取附件并整理结论",
            "general-agent",
            attachments=[attachment],
            conversation_id="conv_public_contract",
        )

        listed = self.client.get("/api/tasks")
        fetched = self.client.get(f"/api/tasks/{task['id']}")

        self.assertEqual(listed.status_code, 200, listed.text)
        self.assertEqual(fetched.status_code, 200, fetched.text)
        for body in (listed.text, fetched.text):
            self.assertNotIn(attachment["path"], body)
            self.assertNotIn("attachments_json", body)
            self.assertNotIn("artifacts_json", body)
            self.assertNotIn("result_json", body)
        public_task = fetched.json()
        self.assertEqual(public_task["attachments"][0]["id"], attachment["id"])
        self.assertEqual(public_task["attachments"][0]["name"], attachment["name"])
        self.assertNotIn("path", public_task["attachments"][0])

    def test_task_event_responses_remove_internal_json_and_nested_paths(self) -> None:
        task_id = self._create_task(status="running")
        internal_path = "/private/server/artifacts/secret.docx"
        main_module.emit(
            task_id,
            "tool_result",
            "工具完成",
            "已生成文件",
            {
                "artifact": {
                    "id": "art_public_event",
                    "name": "result.docx",
                    "kind": "docx",
                    "path": internal_path,
                    "relative_path": "task/run/result.docx",
                    "download_url": "/api/artifacts/art_public_event/download",
                }
            },
        )

        fetched = self.client.get(f"/api/tasks/{task_id}")
        events = self.client.get(f"/api/tasks/{task_id}/events")

        self.assertEqual(fetched.status_code, 200, fetched.text)
        self.assertEqual(events.status_code, 200, events.text)
        for body in (fetched.text, events.text):
            self.assertNotIn(internal_path, body)
            self.assertNotIn("relative_path", body)
            self.assertNotIn("data_json", body)
        public_artifact = events.json()[0]["data"]["artifact"]
        self.assertEqual(public_artifact["id"], "art_public_event")
        self.assertEqual(public_artifact["delivery_status"], "unavailable")
        self.assertNotIn("download_url", public_artifact)
        self.assertNotIn("preview_url", public_artifact)

    def test_pending_and_rejected_artifacts_do_not_expose_download_or_preview_links(self) -> None:
        task_id = self._create_task(status="running")
        for artifact_id, delivery_status in (
            ("art_pending_event", "pending_verification"),
            ("art_rejected_event", "rejected"),
        ):
            main_module.emit(
                task_id,
                "tool_result",
                "工具完成",
                "文件已生成，等待验收",
                {
                    "artifact": {
                        "id": artifact_id,
                        "task_id": task_id,
                        "name": f"{artifact_id}.docx",
                        "kind": "docx",
                        "delivery_status": delivery_status,
                    }
                },
            )

        response = self.client.get(f"/api/tasks/{task_id}/events")

        self.assertEqual(response.status_code, 200, response.text)
        artifacts = [item["data"]["artifact"] for item in response.json()]
        self.assertEqual(
            [item.get("delivery_status") for item in artifacts],
            ["pending_verification", "rejected"],
        )
        for artifact in artifacts:
            self.assertNotIn("download_url", artifact)
            self.assertNotIn("preview_url", artifact)

    def test_error_events_never_expose_raw_exception_text_in_http_or_sse(self) -> None:
        task_id = self._create_task(status="failed")
        secret = "Authorization=Bearer SECRET_TOKEN_123"
        private_path = "/private/run/system_prompt.txt"
        main_module.emit(
            task_id,
            "error",
            f"任务失败 {secret}",
            f"供应商异常：{secret}；内部路径：{private_path}",
            {
                "error_type": "ProviderFailure",
                "exception": f"{secret} {private_path}",
            },
        )

        listed = self.client.get(f"/api/tasks/{task_id}/events")
        fetched = self.client.get(f"/api/tasks/{task_id}")
        streamed = self.client.get(f"/api/tasks/{task_id}/events/stream")

        self.assertEqual(listed.status_code, 200, listed.text)
        public_error = listed.json()[0]
        self.assertEqual(public_error["type"], "error")
        self.assertTrue(public_error["content"].strip())
        for response in (listed, fetched, streamed):
            self.assertNotIn("SECRET_TOKEN_123", response.text)
            self.assertNotIn("Authorization", response.text)
            self.assertNotIn(private_path, response.text)
            self.assertNotIn("ProviderFailure", response.text)

    def test_model_network_policy_error_is_publicly_classified_as_model_unavailable(self) -> None:
        task_id = self._create_task(status="failed")
        main_module.emit(
            task_id,
            "error",
            "任务失败",
            "模型 API主机不在 APP_MODEL_HOST_ALLOWLIST 中",
            {"error_type": "RuntimeError"},
        )

        listed = self.client.get(f"/api/tasks/{task_id}/events")

        self.assertEqual(listed.status_code, 200, listed.text)
        public_error = listed.json()[0]
        self.assertEqual(public_error["title"], "模型暂时不可用")
        self.assertIn("模型暂时不可用", public_error["content"])
        self.assertNotIn("APP_MODEL_HOST_ALLOWLIST", public_error["content"])

    def test_failed_task_result_nodes_and_progress_never_expose_raw_exception_text(self) -> None:
        task_id = self._create_task(status="running")
        run = self.state.begin_run(task_id)
        node = self.state.create_node(run["id"], "execute", "执行工具")
        self.state.start_node(node["id"])
        secret = "Authorization=Bearer NODE_SECRET_456"
        private_path = "/private/platform/workspace/internal.json"
        raw_error = f"ProviderFailure: {secret}; path={private_path}"
        self.state.fail_node(node["id"], {"message": raw_error})
        self.state.finish_run(
            run["id"], status="failed", error={"message": raw_error}
        )
        db.update_task_status(task_id, "failed", result={"error": raw_error})
        main_module.emit(
            task_id,
            "plan_progress",
            "执行失败",
            raw_error,
            {"status": "failed", "node_id": "execute"},
        )

        responses = (
            self.client.get("/api/tasks"),
            self.client.get(f"/api/tasks/{task_id}"),
            self.client.get(f"/api/tasks/{task_id}/runtime"),
            self.client.get(f"/api/tasks/{task_id}/events"),
            self.client.get(f"/api/tasks/{task_id}/events/stream"),
        )

        for response in responses:
            self.assertEqual(response.status_code, 200, response.text)
            self.assertNotIn("NODE_SECRET_456", response.text)
            self.assertNotIn("Authorization", response.text)
            self.assertNotIn(private_path, response.text)
            self.assertNotIn("ProviderFailure", response.text)
        task = responses[1].json()
        runtime = responses[2].json()
        self.assertEqual(task["result"], {"error_code": "task_execution_failed"})
        self.assertIn("该执行步骤未完成", runtime["nodes"][0]["error_summary"])

    def test_event_stream_resumes_from_header_or_explicit_cursor(self) -> None:
        task_id = self._create_task(status="completed")
        first_id = main_module.emit(task_id, "start", "开始", "第一条")
        second_id = main_module.emit(task_id, "progress", "进度", "第二条")
        third_id = main_module.emit(task_id, "done", "完成", "第三条")

        by_header = self.client.get(
            f"/api/tasks/{task_id}/events/stream",
            headers={"Last-Event-ID": str(first_id)},
        )
        by_cursor = self.client.get(
            f"/api/tasks/{task_id}/events/stream?cursor={second_id}",
            headers={"Last-Event-ID": str(first_id)},
        )

        self.assertEqual(by_header.status_code, 200, by_header.text)
        self.assertNotIn(f"id: {first_id}\n", by_header.text)
        self.assertIn(f"id: {second_id}\n", by_header.text)
        self.assertIn(f"id: {third_id}\n", by_header.text)
        self.assertNotIn(f"id: {second_id}\n", by_cursor.text)
        self.assertIn(f"id: {third_id}\n", by_cursor.text)
        self.assertIn('event: task_status', by_cursor.text)
        self.assertIn('"terminal": true', by_cursor.text)

    def test_waiting_approval_closes_stream_without_marking_task_terminal(self) -> None:
        task_id = self._create_task(status="waiting_approval")
        event_id = main_module.emit(
            task_id, "approval_required", "等待确认", "请确认后继续"
        )

        response = self.client.get(f"/api/tasks/{task_id}/events/stream")

        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn(f"id: {event_id}\n", response.text)
        self.assertIn('"status": "waiting_approval"', response.text)
        self.assertIn('"terminal": false', response.text)

    def test_event_feeds_never_expose_private_reasoning_events(self) -> None:
        task_id = self._create_task(status="completed")
        private_id = main_module.emit(
            task_id, "reasoning", "内部推理", "不应发给浏览器"
        )
        public_id = main_module.emit(task_id, "answer", "回答", "公开结果")

        listed = self.client.get(f"/api/tasks/{task_id}/events")
        task = self.client.get(f"/api/tasks/{task_id}")
        streamed = self.client.get(f"/api/tasks/{task_id}/events/stream")

        self.assertEqual([item["id"] for item in listed.json()], [public_id])
        self.assertEqual([item["id"] for item in task.json()["events"]], [public_id])
        self.assertNotIn(f"id: {private_id}\n", streamed.text)
        self.assertNotIn("不应发给浏览器", streamed.text)
        self.assertIn(f"id: {public_id}\n", streamed.text)

    def test_event_feeds_are_default_deny_and_project_known_payloads(self) -> None:
        task_id = self._create_task(status="completed")
        main_module.emit(
            task_id,
            "agent",
            "已选择 Agent",
            "安全智能体",
            {
                "agent": {
                    "id": "safe-agent",
                    "name": "安全智能体",
                    "system_prompt": "private-agent-system-prompt",
                }
            },
        )
        main_module.emit(
            task_id,
            "plan",
            "执行计划",
            "公开计划",
            {
                "plan": {
                    "goal": "生成报告",
                    "steps": ["生成"],
                    "nodes": [{"id": "execute", "title": "生成", "status": "pending"}],
                    "allowed_tools": [
                        {"server_id": "report", "tool_name": "generate", "schema_hash": "private-schema-hash"}
                    ],
                    "internal_prompt": "private-plan-prompt",
                }
            },
        )
        main_module.emit(
            task_id,
            "verification_result",
            "验收通过",
            "公开验收结论",
            {
                "verification_id": "verification-public",
                "candidate_sha256": "private-candidate-hash",
                "report": {
                    "schema_version": 1,
                    "mode": "rules_only",
                    "verdict": "rules_passed",
                    "passed": True,
                    "coverage": "rules_only",
                    "semantic_attempted": False,
                    "semantic_verified": False,
                    "public_reason": "规则通过。",
                    "rules": [],
                    "semantic": {"status": "skipped", "public_reason": "未执行语义复核。"},
                    "repair_instructions": [],
                    "judge_prompt": "private-judge-prompt",
                    "internal_reasoning": "private-judge-reasoning",
                },
            },
        )
        main_module.emit(
            task_id,
            "plan_check",
            "工具调用前校验未通过",
            "已阻止偏离计划的工具调用。",
            {
                "tool": "weather.forecast",
                "passed": False,
                "reason": "tool_not_in_plan",
                "source": "execution_plan",
                "plan_id": "plan-public",
                "private_plan_detail": "must-not-leak",
            },
        )
        main_module.emit(
            task_id,
            "knowledge",
            "已检索项目知识库",
            "本次使用 1 个知识片段。",
            {
                "knowledge_base_ids": ["kb_public"],
                "matches": [
                    {
                        "chunk_id": "chunk_public",
                        "document_id": "doc_public",
                        "knowledge_base_id": "kb_public",
                        "document_name": "handbook.md",
                        "ordinal": 2,
                        "content": "private-knowledge-content",
                        "source_path": "/private/source/path/handbook.md",
                    }
                ],
            },
        )
        main_module.emit(
            task_id,
            "policy_decision",
            "策略校验",
            "private-policy-summary",
            {"patches": [{"path": "/tool/arguments", "value": "private-policy-patch"}]},
        )
        main_module.emit(
            task_id,
            "future_internal_event",
            "未知内部事件",
            "private-unknown-event",
            {"secret": "private-unknown-payload"},
        )

        listed = self.client.get(f"/api/tasks/{task_id}/events")
        fetched = self.client.get(f"/api/tasks/{task_id}")
        streamed = self.client.get(f"/api/tasks/{task_id}/events/stream")

        self.assertEqual(listed.status_code, 200, listed.text)
        self.assertEqual([item["type"] for item in listed.json()], ["agent", "plan", "verification_result", "plan_check", "knowledge"])
        agent = listed.json()[0]
        self.assertEqual(agent["data"]["agent"], {"id": "safe-agent", "name": "安全智能体"})
        self.assertEqual(listed.json()[1]["data"]["plan"]["goal"], "生成报告")
        self.assertEqual(
            listed.json()[2]["data"]["report"]["public_reason"], "规则通过。"
        )
        plan_check = listed.json()[3]
        self.assertEqual(plan_check["data"]["reason"], "tool_not_in_plan")
        self.assertEqual(plan_check["data"]["source"], "execution_plan")
        self.assertFalse(plan_check["data"]["passed"])
        knowledge = listed.json()[4]
        self.assertEqual(knowledge["data"]["knowledge_base_ids"], ["kb_public"])
        self.assertEqual(knowledge["data"]["matches"][0]["document_name"], "handbook.md")
        self.assertEqual(knowledge["data"]["matches"][0]["ordinal"], 2)
        for body in (listed.text, fetched.text, streamed.text):
            for forbidden in (
                "private-agent-system-prompt", "system_prompt", "private-schema-hash",
                "schema_hash", "private-plan-prompt", "private-candidate-hash",
                "candidate_sha256", "private-judge-prompt", "private-judge-reasoning",
                "judge_prompt", "internal_reasoning", "private-policy-summary",
                "private-policy-patch", "patches", "private-unknown-event",
                "private-unknown-payload", "future_internal_event",
                "private-knowledge-content", "source_path", "/private/source/path",
                "private_plan_detail", "must-not-leak",
            ):
                self.assertNotIn(forbidden, body)

    def test_conversation_history_exposes_errors_as_structured_non_answer_messages(self) -> None:
        task_id = self._create_task(status="failed")
        main_module.emit(task_id, "error", "任务失败", "private failure detail")

        response = self.client.get("/api/conversations/conv_runtime_api/messages")

        self.assertEqual(response.status_code, 200, response.text)
        messages = response.json()["messages"]
        self.assertEqual(messages[0], {
            "role": "user", "content": "验证可靠任务运行接口", "task_id": task_id
        })
        self.assertEqual(messages[1]["role"], "system")
        self.assertEqual(messages[1]["message_type"], "error")
        self.assertEqual(messages[1]["title"], "任务未完成")
        self.assertEqual(messages[1]["content"], "任务执行未完成。请检查模型、参数或工具配置后重试。")
        self.assertEqual(messages[1]["task_id"], task_id)
        self.assertNotIn("private failure detail", response.text)

    def test_event_stream_rejects_invalid_cursor_and_missing_task(self) -> None:
        task_id = self._create_task(status="completed")

        negative = self.client.get(
            f"/api/tasks/{task_id}/events/stream?cursor=-1"
        )
        invalid_header = self.client.get(
            f"/api/tasks/{task_id}/events/stream",
            headers={"Last-Event-ID": "not-a-number"},
        )
        missing = self.client.get("/api/tasks/task_missing/events/stream")

        for response in (negative, invalid_header):
            self.assertEqual(response.status_code, 400, response.text)
            self.assertEqual(response.json()["detail"], "事件游标必须是非负整数")
        self.assertEqual(missing.status_code, 404, missing.text)

    def test_create_task_response_uses_the_same_public_attachment_contract(self) -> None:
        upload_id = "upl_create_contract"
        attachment_path = "/private/server/uploads/create-contract.docx"
        db.execute(
            "INSERT INTO uploads(id, name, content_type, size, path, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (
                upload_id,
                "create-contract.docx",
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                4321,
                attachment_path,
                db.utc_now(),
            ),
        )

        response = self.client.post(
            "/api/tasks",
            json={
                "message": "读取附件",
                "agent_id": "general-agent",
                "model_id": "deterministic",
                "attachment_ids": [upload_id],
            },
        )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertNotIn(attachment_path, response.text)
        self.assertNotIn("attachments_json", response.text)
        self.assertNotIn("result_json", response.text)
        self.assertNotIn("error_json", response.text)
        self.assertNotIn("metadata_json", response.text)
        self.assertEqual(response.json()["attachments"][0]["id"], upload_id)
        self.assertNotIn("path", response.json()["attachments"][0])
        self.assertEqual(response.json()["attachments"][0]["context_status"]["state"], "ready")
        self.assertTrue(response.json()["attachments"][0]["context_status"]["extractable"])
        self.assertIn("result", response.json()["run"])
        self.assertIn("metadata", response.json()["run"])

    def test_upload_response_exposes_context_status_without_storage_path(self) -> None:
        supported = self.client.post(
            "/api/uploads",
            files={"file": ("notes.md", b"# Notes\nAgentNexus upload context", "text/markdown")},
        )
        unsupported = self.client.post(
            "/api/uploads",
            files={"file": ("archive.bin", b"\x00\x01\x02", "application/octet-stream")},
        )

        self.assertEqual(supported.status_code, 200, supported.text)
        self.assertEqual(unsupported.status_code, 200, unsupported.text)
        self.assertNotIn(str(main_module.UPLOAD_DIR), supported.text)
        self.assertNotIn("path", supported.json())
        self.assertEqual(supported.json()["context_status"]["state"], "ready")
        self.assertTrue(supported.json()["context_status"]["extractable"])
        self.assertEqual(unsupported.json()["context_status"]["state"], "unsupported")
        self.assertFalse(unsupported.json()["context_status"]["extractable"])

    def test_create_task_rejects_enabled_but_unready_model_before_runtime(self) -> None:
        created = self.client.post(
            "/api/models",
            json={
                "id": "unready-model",
                "name": "Unready Model",
                "provider": "openai_compatible",
                "model": "demo-model",
                "base_url": "https://example.invalid/v1",
                "api_key_mode": "env",
                "api_key_env": "MISSING_TASK_MODEL_KEY",
                "enabled": True,
            },
        )
        self.assertEqual(created.status_code, 200, created.text)
        self.assertEqual(created.json()["readiness"]["state"], "needs_config")

        response = self.client.post(
            "/api/tasks",
            json={
                "message": "你好",
                "agent_id": "general-agent",
                "model_id": "unready-model",
            },
        )

        self.assertEqual(response.status_code, 400, response.text)
        self.assertIn("所选模型暂不可用", response.json()["detail"])
        self.run_task.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
