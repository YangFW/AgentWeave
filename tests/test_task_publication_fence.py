from __future__ import annotations

import hashlib
import sqlite3
import tempfile
import unittest
from pathlib import Path

from app import db
from app.services import mcp_gateway as mcp_module
from app.services.agent_runtime import create_task_record
from app.services.goal_spec_service import compile_draft, finalize, public_goal_summary
from app.services.runtime_contract_service import canonical_json_hash
from app.services.task_state import (
    PublicationConflict,
    RunIntakeClosed,
    TaskStateService,
    serialize_checkpoint_state,
)
from app.services.verification_service import CandidateOutput


class TaskPublicationFenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.original_db_path = db.DB_PATH
        self.temp_dir = tempfile.TemporaryDirectory()
        db.DB_PATH = Path(self.temp_dir.name) / "publication-fence.db"
        self.original_artifact_dir = mcp_module.ARTIFACT_DIR
        mcp_module.ARTIFACT_DIR = Path(self.temp_dir.name) / "artifacts"
        mcp_module.ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
        db.init_db()
        self.state = TaskStateService(db.get_conn)

    def tearDown(self) -> None:
        mcp_module.ARTIFACT_DIR = self.original_artifact_dir
        db.DB_PATH = self.original_db_path
        self.temp_dir.cleanup()

    @staticmethod
    def _verification_report(*, passed: bool = True) -> dict:
        if passed:
            return {
                "schema_version": 1,
                "mode": "rules_only",
                "verdict": "rules_passed",
                "passed": True,
                "coverage": "rules_only",
                "semantic_attempted": False,
                "semantic_verified": False,
                "public_reason": "候选结果已经通过独立规则验收。",
                "rules": [
                    {
                        "id": "candidate_bound",
                        "title": "候选内容与验收记录一致",
                        "status": "passed",
                        "public_reason": "候选 Hash 与验收记录一致。",
                        "repair_instruction": None,
                    }
                ],
                "semantic": {
                    "status": "skipped",
                    "public_reason": "本轮规则验收不要求语义复核。",
                    "repair_instructions": [],
                },
                "repair_instructions": [],
            }
        return {
            "schema_version": 1,
            "mode": "rules_only",
            "verdict": "failed",
            "passed": False,
            "coverage": "rules_only",
            "semantic_attempted": False,
            "semantic_verified": False,
            "public_reason": "候选内容未通过独立规则验收。",
            "rules": [
                {
                    "id": "candidate_bound",
                    "title": "候选内容与验收记录一致",
                    "status": "failed",
                    "public_reason": "候选 Hash 与验收记录不一致。",
                    "repair_instruction": "重新生成候选并执行完整验收。",
                }
            ],
            "semantic": {
                "status": "skipped",
                "public_reason": "规则失败后未执行语义复核。",
                "repair_instructions": [],
            },
            "repair_instructions": ["重新生成候选并执行完整验收。"],
        }

    def _verified_fixture(self, *, with_artifact: bool = False) -> dict:
        task = create_task_record(
            "生成经过验收的发布结论",
            "general-agent",
            conversation_id="conv_publication_fence",
        )
        run = self.state.begin_run(task["id"])
        db.update_task_status(task["id"], "running")
        draft = compile_draft(
            task_id=task["id"],
            objective={"statement": "生成经过验收的发布结论", "intent": "analysis"},
        )
        self.state.save_goal_spec(
            task["id"],
            run["id"],
            draft.model_dump(mode="json"),
            public_summary=public_goal_summary(draft),
        )
        confirmed = finalize(draft)
        goal = self.state.save_goal_spec(
            task["id"],
            run["id"],
            confirmed.model_dump(mode="json"),
            public_summary=public_goal_summary(confirmed),
        )
        self.state.update_run_metadata(run["id"], {"goal_spec_id": goal["id"]})
        artifacts: list[dict] = []
        if with_artifact:
            artifact_id = "art_publication_fence"
            artifact_content = "# 已验收结论\n\n这是经过验收的正式文件内容。\n"
            artifact_bytes = artifact_content.encode("utf-8")
            artifact_sha256 = hashlib.sha256(artifact_bytes).hexdigest()
            relative_path = "task/verified.md"
            artifact_path = mcp_module.ARTIFACT_DIR / relative_path
            artifact_path.parent.mkdir(parents=True, exist_ok=True)
            artifact_path.write_bytes(artifact_bytes)
            db.execute(
                """
                INSERT INTO artifacts(
                    id, task_id, run_id, name, kind, path, relative_path,
                    mime_type, size, sha256, version, created_at, delivery_status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, 'pending_verification')
                """,
                (
                    artifact_id,
                    task["id"],
                    run["id"],
                    "verified.md",
                    "md",
                    str(artifact_path),
                    relative_path,
                    "text/markdown",
                    len(artifact_bytes),
                    artifact_sha256,
                    db.utc_now(),
                ),
            )
            artifacts.append(
                {
                    "id": artifact_id,
                    "deliverable_id": "",
                    "task_id": task["id"],
                    "run_id": run["id"],
                    "name": "verified.md",
                    "kind": "md",
                    "mime_type": "text/markdown",
                    "size": len(artifact_bytes),
                    "version": 1,
                    "download_url": f"/api/artifacts/{artifact_id}/download",
                    "content_text": artifact_content,
                    "size_bytes": len(artifact_bytes),
                    "sha256": artifact_sha256,
                    "exists": True,
                    "readable": True,
                    "download_ready": True,
                }
            )
        candidate = CandidateOutput.model_validate(
            {
                "answer": "这是经过验收的正式答案。",
                "artifacts": artifacts,
            }
        ).model_dump(mode="json")
        candidate_sha256 = canonical_json_hash(candidate)
        verification_report = self._verification_report()
        verification = self.state.save_verification_report(
            task["id"],
            run["id"],
            goal["id"],
            verification_report,
            public_report=verification_report,
            candidate_sha256=candidate_sha256,
        )
        return {
            "task": task,
            "run": run,
            "goal": goal,
            "verification": verification,
            "candidate_sha256": candidate_sha256,
            "candidate": candidate,
            "artifacts": artifacts,
        }

    def _commit(self, fixture: dict, **overrides: object) -> dict:
        publication = {
            "task_id": fixture["task"]["id"],
            "run_id": fixture["run"]["id"],
            "goal_spec_id": fixture["goal"]["id"],
            "verification_id": fixture["verification"]["id"],
            "candidate": fixture["candidate"],
            "expected_generation": 0,
            "answer_title": "任务完成",
            "answer_data": {
                "verification_id": fixture["verification"]["id"],
                "delivery_state": "verified",
                "artifacts": fixture["artifacts"],
            },
            "done_title": "已完成",
            "done_content": "结果已经完成原子发布。",
            "done_data": {
                "verification_id": fixture["verification"]["id"],
                "delivery_state": "verified",
            },
            "result": {"summary": "这是经过验收的正式答案。"},
        }
        publication.update(overrides)
        return self.state.commit_verified_publication(**publication)

    def test_publication_closes_intake_and_is_exactly_once(self) -> None:
        fixture = self._verified_fixture(with_artifact=True)

        first = self._commit(fixture)
        second = self._commit(fixture)

        self.assertTrue(first["published"])
        self.assertFalse(first["idempotent"])
        self.assertTrue(second["idempotent"])
        task = db.query_one(
            "SELECT status FROM tasks WHERE id = ?", (fixture["task"]["id"],)
        )
        run = self.state.get_run(fixture["run"]["id"])
        self.assertEqual(task["status"], "completed")
        self.assertEqual(run["status"], "completed")
        self.assertEqual(run["intake_state"], "closed")
        self.assertEqual(
            run["published_verification_id"], fixture["verification"]["id"]
        )
        self.assertRegex(run["publication_hash"], r"^[0-9a-f]{64}$")

    def test_publication_accepts_markdown_storage_kind_for_md_candidate(self) -> None:
        fixture = self._verified_fixture(with_artifact=True)
        artifact_id = fixture["artifacts"][0]["id"]
        db.execute(
            "UPDATE artifacts SET kind = 'markdown' WHERE id = ?",
            (artifact_id,),
        )

        published = self._commit(fixture)

        self.assertTrue(published["published"])
        row = db.query_one("SELECT kind, delivery_status FROM artifacts WHERE id = ?", (artifact_id,))
        self.assertEqual(row["kind"], "markdown")
        self.assertEqual(row["delivery_status"], "published")
        artifact = db.query_one(
            "SELECT delivery_status, verification_id FROM artifacts WHERE id = ?",
            ("art_publication_fence",),
        )
        self.assertEqual(artifact["delivery_status"], "published")
        self.assertEqual(artifact["verification_id"], fixture["verification"]["id"])
        counts = {
            row["type"]: int(row["count"])
            for row in db.query_all(
                "SELECT type, COUNT(*) AS count FROM task_events "
                "WHERE task_id = ? AND type IN ('candidate_verified', 'answer', 'done') "
                "GROUP BY type",
                (fixture["task"]["id"],),
            )
        }
        self.assertEqual(
            counts, {"candidate_verified": 1, "answer": 1, "done": 1}
        )

        competing_client = TaskStateService(db.get_conn)
        command_count_before = len(
            competing_client.list_commands(task_id=fixture["task"]["id"])
        )
        with self.assertRaises(RunIntakeClosed):
            competing_client.enqueue_command(
                fixture["task"]["id"],
                "message",
                run_id=fixture["run"]["id"],
                payload={"message": "发布完成后才到达的要求"},
            )
        self.assertEqual(
            len(competing_client.list_commands(task_id=fixture["task"]["id"])),
            command_count_before,
        )

    def test_idempotent_publication_rejects_modified_delivery_envelope(self) -> None:
        fixture = self._verified_fixture()
        first = self._commit(fixture)
        self.assertFalse(first["idempotent"])

        mutations = {
            "answer_title": {"answer_title": "被替换的答案标题"},
            "answer_data": {
                "answer_data": {
                    "verification_id": fixture["verification"]["id"],
                    "delivery_state": "verified",
                    "artifacts": fixture["artifacts"],
                    "unexpected": "被替换的答案数据",
                }
            },
            "done_title": {"done_title": "被替换的完成标题"},
            "done_content": {"done_content": "被替换的完成说明。"},
            "done_data": {
                "done_data": {
                    "verification_id": fixture["verification"]["id"],
                    "delivery_state": "verified",
                    "unexpected": "被替换的完成数据",
                }
            },
            "result": {
                "result": {
                    "summary": "这是经过验收的正式答案。",
                    "unexpected": "被替换的任务结果",
                }
            },
        }
        for field, override in mutations.items():
            with self.subTest(field=field):
                with self.assertRaisesRegex(
                    PublicationConflict,
                    "publication envelope",
                ):
                    self._commit(fixture, **override)

        exact_retry = self._commit(fixture)
        self.assertTrue(exact_retry["idempotent"])
        event_counts = {
            row["type"]: int(row["count"])
            for row in db.query_all(
                "SELECT type, COUNT(*) AS count FROM task_events "
                "WHERE task_id = ? AND type IN ('candidate_verified', 'answer', 'done') "
                "GROUP BY type",
                (fixture["task"]["id"],),
            )
        }
        self.assertEqual(
            event_counts,
            {"candidate_verified": 1, "answer": 1, "done": 1},
        )

    def test_publication_rejects_every_inconsistent_verification_envelope(self) -> None:
        mutations = {
            "storage_status": lambda verification_id: db.execute(
                "UPDATE task_verifications SET status = 'failed' WHERE id = ?",
                (verification_id,),
            ),
            "authoritative_report": lambda verification_id: db.execute(
                "UPDATE task_verifications SET report_json = ? WHERE id = ?",
                (
                    serialize_checkpoint_state(self._verification_report(passed=False)),
                    verification_id,
                ),
            ),
            "public_report": lambda verification_id: db.execute(
                "UPDATE task_verifications SET public_report_json = ? WHERE id = ?",
                (
                    serialize_checkpoint_state(self._verification_report(passed=False)),
                    verification_id,
                ),
            ),
        }
        for mutation_name, mutate in mutations.items():
            with self.subTest(mutation=mutation_name):
                fixture = self._verified_fixture()
                mutate(fixture["verification"]["id"])

                with self.assertRaises(PublicationConflict):
                    self._commit(fixture)

                task = db.query_one(
                    "SELECT status FROM tasks WHERE id = ?",
                    (fixture["task"]["id"],),
                ) or {}
                run = self.state.get_run(fixture["run"]["id"]) or {}
                self.assertEqual(task.get("status"), "running")
                self.assertEqual(run.get("status"), "running")
                formal_events = db.query_all(
                    "SELECT type FROM task_events WHERE task_id = ? "
                    "AND type IN ('answer', 'done')",
                    (fixture["task"]["id"],),
                )
                self.assertEqual(formal_events, [])

    def test_publication_rejects_semantic_failure_disguised_as_rules_passed(self) -> None:
        fixture = self._verified_fixture()
        forged_report = {
            **self._verification_report(),
            "mode": "semantic_optional",
            "verdict": "rules_passed",
            "passed": True,
            "coverage": "rules_and_semantic",
            "semantic_attempted": True,
            "semantic_verified": True,
            "public_reason": "伪造为只通过规则的正式验收。",
            "semantic": {
                "status": "failed",
                "public_reason": "语义验收实际上没有通过。",
                "repair_instructions": [],
            },
            "repair_instructions": [],
        }
        encoded_report = serialize_checkpoint_state(forged_report)
        db.execute(
            """
            UPDATE task_verifications
            SET status = 'rules_passed', mode = 'semantic_optional',
                report_json = ?, public_report_json = ?
            WHERE id = ?
            """,
            (
                encoded_report,
                encoded_report,
                fixture["verification"]["id"],
            ),
        )

        with self.assertRaises(PublicationConflict):
            self._commit(fixture)

        task = db.query_one(
            "SELECT status FROM tasks WHERE id = ?", (fixture["task"]["id"],)
        ) or {}
        run = self.state.get_run(fixture["run"]["id"]) or {}
        self.assertEqual(task.get("status"), "running")
        self.assertEqual(run.get("status"), "running")
        formal_events = db.query_all(
            "SELECT type FROM task_events WHERE task_id = ? "
            "AND type IN ('answer', 'done')",
            (fixture["task"]["id"],),
        )
        self.assertEqual(formal_events, [])

    def test_resumed_run_can_clarify_its_associated_needs_input_goal(self) -> None:
        task = create_task_record(
            "请按地区生成结论",
            "general-agent",
            conversation_id="conv_resumed_clarification",
        )
        first_run = self.state.begin_run(
            task["id"], activate_task_projection=True
        )
        needs_input = compile_draft(
            task_id=task["id"],
            objective={"statement": "请按地区生成结论", "intent": "analysis"},
            inputs=[
                {
                    "key": "region",
                    "label": "地区",
                    "required": True,
                    "status": "missing",
                    "ask": "请补充需要分析的地区。",
                }
            ],
        )
        original_goal = self.state.save_goal_spec(
            task["id"],
            first_run["id"],
            needs_input.model_dump(mode="json"),
            public_summary=public_goal_summary(needs_input),
        )
        self.state.commit_clarification_completion(
            task_id=task["id"],
            run_id=first_run["id"],
            goal_spec_id=original_goal["id"],
            expected_generation=0,
            clarification="请补充需要分析的地区。",
            missing_information=["region"],
            result={"summary": "请补充需要分析的地区。", "needs_clarification": True},
        )

        resumed = self.state.create_run(
            task["id"], metadata={"trigger": "resume"}
        )
        resumed = self.state.begin_run(
            task["id"],
            run_id=resumed["id"],
            activate_task_projection=True,
        )
        associated_goal = self.state.save_goal_spec(
            task["id"],
            resumed["id"],
            needs_input.model_dump(mode="json"),
            public_summary=public_goal_summary(needs_input),
        )
        self.assertEqual(associated_goal["id"], original_goal["id"])
        self.assertNotEqual(associated_goal["run_id"], resumed["id"])

        completed = self.state.commit_clarification_completion(
            task_id=task["id"],
            run_id=resumed["id"],
            goal_spec_id=associated_goal["id"],
            expected_generation=0,
            clarification="仍需补充地区后才能继续。",
            missing_information=["region"],
            result={"summary": "仍需补充地区后才能继续。", "needs_clarification": True},
        )

        self.assertTrue(completed["completed"])
        self.assertEqual(completed["run"]["status"], "completed")
        self.state.assert_terminal_clean(task_id=task["id"], run_id=resumed["id"])

    def test_publication_rejects_unfinished_nodes_or_commands_and_closes_cleanly(self) -> None:
        fixture = self._verified_fixture()
        pending_node = self.state.create_node(
            fixture["run"]["id"],
            "unfinished",
            "尚未完成的步骤",
            sequence=1,
        )

        with self.assertRaisesRegex(PublicationConflict, "execution nodes"):
            self._commit(fixture)
        self.assertEqual(
            db.query_one(
                "SELECT status FROM tasks WHERE id = ?", (fixture["task"]["id"],)
            )["status"],
            "running",
        )
        self.assertEqual(self.state.get_run(fixture["run"]["id"])["status"], "running")

        self.state.transition_node(pending_node["id"], "cancelled")
        approval = self.state.enqueue_command(
            fixture["task"]["id"],
            "approval",
            run_id=fixture["run"]["id"],
            payload={"approved": True},
        )
        with self.assertRaisesRegex(PublicationConflict, "Runtime input is pending"):
            self._commit(fixture)
        self.assertEqual(self.state.get_command(approval["id"])["status"], "queued")

        self.state.cancel_command(approval["id"])
        published = self._commit(fixture)
        self.assertTrue(published["published"])
        run = self.state.get_run(fixture["run"]["id"])
        self.assertEqual(run["status"], "completed")
        self.assertFalse(
            self.state.list_commands(
                task_id=fixture["task"]["id"],
                run_id=fixture["run"]["id"],
                status="queued",
            )
        )
        with self.assertRaises(RunIntakeClosed):
            self.state.enqueue_command(
                fixture["task"]["id"],
                "approval",
                run_id=fixture["run"]["id"],
                payload={"approved": True},
            )
        with self.assertRaises(PublicationConflict):
            self.state.commit_failure(
                task_id=fixture["task"]["id"],
                run_id=fixture["run"]["id"],
                error={"message": "late failure must lose"},
            )
        self.state.assert_terminal_clean(
            task_id=fixture["task"]["id"],
            run_id=fixture["run"]["id"],
        )

    def test_verified_hash_cannot_publish_a_different_answer(self) -> None:
        fixture = self._verified_fixture()
        fixture["candidate"] = {
            **fixture["candidate"],
            "answer": "这是没有经过本次验收的替换答案。",
        }

        with self.assertRaisesRegex(PublicationConflict, "current candidate"):
            self._commit(fixture)

        task = db.query_one(
            "SELECT status FROM tasks WHERE id = ?", (fixture["task"]["id"],)
        )
        run = self.state.get_run(fixture["run"]["id"])
        self.assertEqual(task["status"], "running")
        self.assertEqual(run["status"], "running")
        self.assertFalse(
            db.query_one(
                "SELECT 1 FROM task_events WHERE task_id = ? "
                "AND type IN ('candidate_verified', 'answer', 'done')",
                (fixture["task"]["id"],),
            )
        )

    def test_verified_artifact_registry_metadata_cannot_change_before_publication(self) -> None:
        fixture = self._verified_fixture(with_artifact=True)
        db.execute(
            "UPDATE artifacts SET name = 'substituted.md' WHERE id = ?",
            (fixture["artifacts"][0]["id"],),
        )

        with self.assertRaisesRegex(PublicationConflict, "metadata"):
            self._commit(fixture)

        artifact = db.query_one(
            "SELECT delivery_status FROM artifacts WHERE id = ?",
            (fixture["artifacts"][0]["id"],),
        )
        self.assertEqual(artifact["delivery_status"], "pending_verification")
        self.assertEqual(
            db.query_all(
                "SELECT type FROM task_events WHERE task_id = ? "
                "AND type IN ('candidate_verified', 'answer', 'done')",
                (fixture["task"]["id"],),
            ),
            [],
        )

    def test_verified_artifact_bytes_cannot_change_before_publication(self) -> None:
        fixture = self._verified_fixture(with_artifact=True)
        artifact = fixture["artifacts"][0]
        path = mcp_module.ARTIFACT_DIR / "task/verified.md"
        path.write_text("文件已在验收后被替换。", encoding="utf-8")

        with self.assertRaisesRegex(PublicationConflict, "bytes changed"):
            self._commit(fixture)

        row = db.query_one(
            "SELECT delivery_status FROM artifacts WHERE id = ?", (artifact["id"],)
        )
        self.assertEqual(row["delivery_status"], "pending_verification")
        self.assertEqual(
            db.query_all(
                "SELECT type FROM task_events WHERE task_id = ? "
                "AND type IN ('candidate_verified', 'answer', 'done')",
                (fixture["task"]["id"],),
            ),
            [],
        )

    def test_accepted_input_makes_old_publication_rollback_without_partial_output(self) -> None:
        fixture = self._verified_fixture(with_artifact=True)
        competing_client = TaskStateService(db.get_conn)
        command = competing_client.enqueue_command(
            fixture["task"]["id"],
            "message",
            run_id=fixture["run"]["id"],
            payload={"message": "发布前增加风险说明"},
        )

        with self.assertRaises(PublicationConflict):
            self._commit(fixture)

        task = db.query_one(
            "SELECT status FROM tasks WHERE id = ?", (fixture["task"]["id"],)
        )
        run = self.state.get_run(fixture["run"]["id"])
        artifact = db.query_one(
            "SELECT delivery_status FROM artifacts WHERE id = ?",
            ("art_publication_fence",),
        )
        self.assertEqual(task["status"], "running")
        self.assertEqual(run["status"], "running")
        self.assertEqual(run["intake_state"], "open")
        self.assertEqual(run["accepted_generation"], 1)
        self.assertEqual(run["applied_generation"], 0)
        self.assertEqual(artifact["delivery_status"], "pending_verification")
        self.assertEqual(
            self.state.get_command(command["id"])["status"], "queued"
        )
        public_events = db.query_all(
            "SELECT type FROM task_events WHERE task_id = ? "
            "AND type IN ('candidate_verified', 'answer', 'done')",
            (fixture["task"]["id"],),
        )
        self.assertEqual(public_events, [])

    def test_late_publication_failure_rolls_back_artifact_events_and_terminal_state(self) -> None:
        fixture = self._verified_fixture(with_artifact=True)
        db.execute(
            """
            CREATE TRIGGER fail_publication_task_completion
            BEFORE UPDATE OF status ON tasks
            WHEN NEW.status = 'completed'
            BEGIN
                SELECT RAISE(ABORT, 'injected publication failure');
            END
            """
        )

        with self.assertRaisesRegex(sqlite3.IntegrityError, "injected publication failure"):
            self._commit(fixture)

        task = db.query_one(
            "SELECT status FROM tasks WHERE id = ?", (fixture["task"]["id"],)
        )
        run = self.state.get_run(fixture["run"]["id"])
        artifact = db.query_one(
            "SELECT delivery_status, verification_id, published_at "
            "FROM artifacts WHERE id = 'art_publication_fence'"
        )
        public_events = db.query_all(
            "SELECT type FROM task_events WHERE task_id = ? "
            "AND type IN ('candidate_verified', 'answer', 'done')",
            (fixture["task"]["id"],),
        )
        self.assertEqual(task["status"], "running")
        self.assertEqual(run["status"], "running")
        self.assertEqual(run["intake_state"], "open")
        self.assertEqual(run["published_verification_id"], "")
        self.assertEqual(run["publication_hash"], "")
        self.assertEqual(artifact["delivery_status"], "pending_verification")
        self.assertEqual(artifact["verification_id"], "")
        self.assertEqual(artifact["published_at"], "")
        self.assertEqual(public_events, [])


if __name__ == "__main__":
    unittest.main()
