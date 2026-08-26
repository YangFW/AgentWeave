from __future__ import annotations

import hashlib
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from app import db
from app import main as main_app
from app.services import mcp_gateway as mcp_module
from app.services.agent_runtime import create_task_record
from app.services.goal_spec_service import compile_draft, finalize, public_goal_summary
from app.services.runtime_contract_service import canonical_json_hash
from app.services.task_state import TaskStateService
from app.services.verification_service import CandidateOutput


class ArtifactDeliveryMigrationTests(unittest.TestCase):
    LEGACY_APPLIED_AT = "2026-01-01T00:00:00+00:00"

    def setUp(self) -> None:
        self.original_db_path = db.DB_PATH
        self.original_artifact_dir = mcp_module.ARTIFACT_DIR
        self.original_main_artifact_dir = main_app.ARTIFACT_DIR
        self.original_artifact_env = os.environ.get("APP_ARTIFACT_DIR")
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        db.DB_PATH = root / "legacy-platform.db"
        mcp_module.ARTIFACT_DIR = root / "artifacts"
        main_app.ARTIFACT_DIR = mcp_module.ARTIFACT_DIR
        mcp_module.ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
        os.environ["APP_ARTIFACT_DIR"] = str(mcp_module.ARTIFACT_DIR)

    def tearDown(self) -> None:
        if self.original_artifact_env is None:
            os.environ.pop("APP_ARTIFACT_DIR", None)
        else:
            os.environ["APP_ARTIFACT_DIR"] = self.original_artifact_env
        main_app.ARTIFACT_DIR = self.original_main_artifact_dir
        mcp_module.ARTIFACT_DIR = self.original_artifact_dir
        db.DB_PATH = self.original_db_path
        self.temp_dir.cleanup()

    def _reset_migration_history(self, conn: sqlite3.Connection) -> None:
        conn.execute("DELETE FROM schema_migrations")
        for version in range(1, 7):
            conn.execute(
                """
                INSERT INTO schema_migrations(version, name, applied_at)
                VALUES (?, ?, ?)
                """,
                (version, f"legacy_migration_{version}", self.LEGACY_APPLIED_AT),
            )

    def _create_legacy_artifact_fixture(
        self,
        *,
        current_columns: bool = False,
        delivery_status: str = "published",
        verification_id: str = "",
        published_at: str = "",
    ) -> tuple[Path, str]:
        # Build every unrelated table through the normal initializer, then
        # replace only Artifact storage with the historical shape.  This keeps
        # the fixture representative without copying the whole old schema.
        db.init_db()
        content = b"# Legacy artifact\n\nThe historical download remains available.\n"
        artifact_path = mcp_module.ARTIFACT_DIR / "legacy" / "legacy.md"
        artifact_path.parent.mkdir(parents=True, exist_ok=True)
        artifact_path.write_bytes(content)
        created_at = "2025-12-31T08:30:00+00:00"
        with closing(db.get_conn()) as conn, conn:
            conn.execute("DROP TABLE artifacts")
            common_schema = """
                id TEXT PRIMARY KEY,
                task_id TEXT NOT NULL,
                name TEXT NOT NULL,
                kind TEXT NOT NULL,
                path TEXT NOT NULL,
                created_at TEXT NOT NULL,
                run_id TEXT NOT NULL DEFAULT '',
                workspace_id TEXT NOT NULL DEFAULT 'default',
                relative_path TEXT NOT NULL DEFAULT '',
                mime_type TEXT NOT NULL DEFAULT 'application/octet-stream',
                size INTEGER NOT NULL DEFAULT 0,
                sha256 TEXT NOT NULL DEFAULT '',
                version INTEGER NOT NULL DEFAULT 1,
                metadata_json TEXT NOT NULL DEFAULT '{}'
            """
            if current_columns:
                common_schema += """,
                    delivery_status TEXT NOT NULL DEFAULT 'published',
                    verification_id TEXT NOT NULL DEFAULT '',
                    published_at TEXT NOT NULL DEFAULT ''
                """
            conn.execute(f"CREATE TABLE artifacts ({common_schema})")
            values = (
                "art_legacy_delivery",
                "task_legacy_delivery",
                "legacy.md",
                "md",
                str(artifact_path),
                created_at,
                "",
                "default",
                "legacy/legacy.md",
                "text/markdown",
                len(content),
                hashlib.sha256(content).hexdigest(),
                3,
                '{"legacy":true}',
            )
            if current_columns:
                conn.execute(
                    """
                    INSERT INTO artifacts(
                        id, task_id, name, kind, path, created_at, run_id,
                        workspace_id, relative_path, mime_type, size, sha256,
                        version, metadata_json, delivery_status,
                        verification_id, published_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (*values, delivery_status, verification_id, published_at),
                )
            else:
                conn.execute(
                    """
                    INSERT INTO artifacts(
                        id, task_id, name, kind, path, created_at, run_id,
                        workspace_id, relative_path, mime_type, size, sha256,
                        version, metadata_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    values,
                )
            self._reset_migration_history(conn)
            conn.commit()
        return artifact_path, created_at

    @staticmethod
    def _passed_report() -> dict:
        return {
            "schema_version": 1,
            "mode": "rules_only",
            "verdict": "rules_passed",
            "passed": True,
            "coverage": "rules_only",
            "semantic_attempted": False,
            "semantic_verified": False,
            "public_reason": "升级库中的新候选已通过规则验收。",
            "rules": [
                {
                    "id": "delivery",
                    "title": "产物可交付",
                    "status": "passed",
                    "public_reason": "文件存在且 Hash 匹配。",
                    "repair_instruction": None,
                }
            ],
            "semantic": {
                "status": "skipped",
                "public_reason": "本轮不要求语义复核。",
                "repair_instructions": [],
            },
            "repair_instructions": [],
        }

    def test_legacy_v1_artifacts_upgrade_once_and_remain_downloadable(self) -> None:
        artifact_path, created_at = self._create_legacy_artifact_fixture()

        db.init_db()
        first_v7 = db.query_one(
            "SELECT * FROM schema_migrations WHERE version = 7"
        )
        db.init_db()
        second_v7 = db.query_one(
            "SELECT * FROM schema_migrations WHERE version = 7"
        )

        with closing(db.get_conn()) as conn, conn:
            columns = {
                row[1]: row
                for row in conn.execute("PRAGMA table_info(artifacts)").fetchall()
            }
        self.assertTrue(
            {"delivery_status", "verification_id", "published_at"}.issubset(
                columns
            )
        )
        self.assertEqual(columns["delivery_status"][3], 1)
        self.assertEqual(columns["verification_id"][3], 1)
        self.assertEqual(columns["published_at"][3], 1)
        row = db.query_one(
            "SELECT * FROM artifacts WHERE id = 'art_legacy_delivery'"
        ) or {}
        self.assertEqual(row.get("delivery_status"), "published")
        self.assertEqual(row.get("verification_id"), "")
        self.assertEqual(row.get("published_at"), created_at)
        self.assertEqual(row.get("version"), 3)
        self.assertEqual(row.get("metadata_json"), '{"legacy":true}')
        self.assertEqual(
            db.query_one("SELECT * FROM schema_migrations WHERE version = 1"),
            {
                "version": 1,
                "name": "legacy_migration_1",
                "applied_at": self.LEGACY_APPLIED_AT,
            },
        )
        self.assertEqual(first_v7, second_v7)
        self.assertIsNotNone(first_v7)

        listed = main_app.list_artifacts(task_id="task_legacy_delivery")
        self.assertEqual([item["id"] for item in listed], ["art_legacy_delivery"])
        detail = main_app.get_artifact("art_legacy_delivery")
        self.assertEqual(detail["delivery_status"], "published")
        response = main_app.download_artifact("art_legacy_delivery")
        self.assertEqual(Path(response.path).resolve(), artifact_path.resolve())
        self.assertEqual(Path(response.path).read_bytes(), artifact_path.read_bytes())

    def test_existing_pending_state_is_not_reclassified_during_v7_upgrade(self) -> None:
        self._create_legacy_artifact_fixture(
            current_columns=True,
            delivery_status="pending_verification",
            verification_id="tvr_existing",
            published_at="",
        )

        db.init_db()
        row = db.query_one(
            "SELECT * FROM artifacts WHERE id = 'art_legacy_delivery'"
        ) or {}

        self.assertEqual(row.get("delivery_status"), "pending_verification")
        self.assertEqual(row.get("verification_id"), "tvr_existing")
        self.assertEqual(row.get("published_at"), "")
        self.assertIsNotNone(
            db.query_one("SELECT * FROM schema_migrations WHERE version = 7")
        )

    def test_upgraded_database_supports_verified_atomic_publication(self) -> None:
        self._create_legacy_artifact_fixture()
        db.init_db()
        state = TaskStateService(db.get_conn)
        task = create_task_record(
            "在升级后的数据库中生成新产物",
            "general-agent",
            conversation_id="conv_upgraded_artifact_publication",
        )
        run = state.begin_run(task["id"], activate_task_projection=True)
        draft = compile_draft(
            task_id=task["id"],
            objective={
                "statement": "在升级后的数据库中生成新产物",
                "intent": "document_generation",
            },
        )
        state.save_goal_spec(
            task["id"],
            run["id"],
            draft.model_dump(mode="json"),
            public_summary=public_goal_summary(draft),
        )
        goal_spec = finalize(draft)
        goal = state.save_goal_spec(
            task["id"],
            run["id"],
            goal_spec.model_dump(mode="json"),
            public_summary=public_goal_summary(goal_spec),
        )

        content = b"# Verified after migration\n"
        artifact_id = "art_verified_after_v7"
        relative_path = "upgraded/verified.md"
        artifact_path = mcp_module.ARTIFACT_DIR / relative_path
        artifact_path.parent.mkdir(parents=True, exist_ok=True)
        artifact_path.write_bytes(content)
        digest = hashlib.sha256(content).hexdigest()
        db.execute(
            """
            INSERT INTO artifacts(
                id, task_id, run_id, name, kind, path, relative_path,
                mime_type, size, sha256, version, created_at,
                delivery_status
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
                len(content),
                digest,
                db.utc_now(),
            ),
        )
        artifact = {
            "id": artifact_id,
            "deliverable_id": "",
            "task_id": task["id"],
            "run_id": run["id"],
            "name": "verified.md",
            "kind": "md",
            "mime_type": "text/markdown",
            "size": len(content),
            "version": 1,
            "download_url": f"/api/artifacts/{artifact_id}/download",
            "content_text": content.decode("utf-8"),
            "size_bytes": len(content),
            "sha256": digest,
            "exists": True,
            "readable": True,
            "download_ready": True,
        }
        candidate = CandidateOutput.model_validate(
            {"answer": "升级后的新产物已经生成并通过验收。", "artifacts": [artifact]}
        ).model_dump(mode="json")
        report = self._passed_report()
        verification = state.save_verification_report(
            task["id"],
            run["id"],
            goal["id"],
            report,
            public_report=report,
            candidate_sha256=canonical_json_hash(candidate),
        )
        commit_args = {
            "task_id": task["id"],
            "run_id": run["id"],
            "goal_spec_id": goal["id"],
            "verification_id": verification["id"],
            "candidate": candidate,
            "expected_generation": 0,
            "answer_title": "任务完成",
            "answer_data": {},
            "done_title": "已完成",
            "done_content": "产物已经完成原子发布。",
            "done_data": {},
            "result": {"summary": "升级后的新产物已经生成并通过验收。"},
        }

        first = state.commit_verified_publication(**commit_args)
        second = state.commit_verified_publication(**commit_args)

        self.assertTrue(first["published"])
        self.assertFalse(first["idempotent"])
        self.assertTrue(second["idempotent"])
        state.assert_terminal_clean(task_id=task["id"], run_id=run["id"])
        row = db.query_one("SELECT * FROM artifacts WHERE id = ?", (artifact_id,)) or {}
        self.assertEqual(row.get("delivery_status"), "published")
        self.assertEqual(row.get("verification_id"), verification["id"])
        self.assertTrue(row.get("published_at"))
        response = main_app.download_artifact(artifact_id)
        self.assertEqual(Path(response.path).read_bytes(), content)


if __name__ == "__main__":
    unittest.main()
