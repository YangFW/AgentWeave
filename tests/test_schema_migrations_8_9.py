from __future__ import annotations

import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from app import db
from app.services.task_state import TaskStateService


class SchemaMigrationsEightNineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.original_db_path = db.DB_PATH
        self.temp_dir = tempfile.TemporaryDirectory()
        db.DB_PATH = Path(self.temp_dir.name) / "migration-8-9.db"

    def tearDown(self) -> None:
        db.DB_PATH = self.original_db_path
        self.temp_dir.cleanup()

    @staticmethod
    def _versions() -> list[int]:
        return [
            int(item["version"])
            for item in db.query_all(
                "SELECT version FROM schema_migrations ORDER BY version"
            )
        ]

    @staticmethod
    def _columns(table: str) -> set[str]:
        with closing(db.get_conn()) as conn:
            return {
                str(row[1])
                for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
            }

    @staticmethod
    def _object_exists(name: str, kind: str) -> bool:
        return bool(
            db.query_one(
                "SELECT 1 AS found FROM sqlite_master WHERE type = ? AND name = ?",
                (kind, name),
            )
        )

    def _initialize_through(self, version: int) -> None:
        migrations = tuple(
            item for item in db.SCHEMA_MIGRATIONS if int(item[0]) <= version
        )
        with patch.object(db, "SCHEMA_MIGRATIONS", migrations):
            db.init_db()

    def test_fresh_database_and_repeated_init_install_versions_once(self) -> None:
        db.init_db()
        TaskStateService(db.get_conn)

        first_rows = db.query_all(
            "SELECT version, name, applied_at FROM schema_migrations "
            "WHERE version IN (8, 9, 10) ORDER BY version"
        )
        db.init_db()
        TaskStateService(db.get_conn)
        second_rows = db.query_all(
            "SELECT version, name, applied_at FROM schema_migrations "
            "WHERE version IN (8, 9, 10) ORDER BY version"
        )

        self.assertEqual([item["version"] for item in first_rows], [8, 9, 10])
        self.assertEqual(second_rows, first_rows)
        self.assertIn("tool_effect_id", self._columns("artifacts"))
        self.assertIn("task_run_id", self._columns("loop_runs"))
        self.assertTrue(
            self._object_exists("idx_artifacts_tool_effect", "index")
        )
        self.assertTrue(self._object_exists("tool_effects", "table"))
        self.assertTrue(
            self._object_exists("tool_effect_transitions", "table")
        )
        self.assertTrue(
            self._object_exists("trg_artifacts_pending_run_insert", "trigger")
        )
        self.assertTrue(
            self._object_exists("trg_artifacts_pending_run_update", "trigger")
        )

    def test_v7_database_upgrades_without_rewriting_existing_artifacts(self) -> None:
        self._initialize_through(7)
        TaskStateService(db.get_conn)
        created_at = "2026-08-15T00:00:00+00:00"
        db.execute(
            """
            INSERT INTO artifacts(
                id, task_id, name, kind, path, created_at,
                delivery_status, verification_id, published_at
            ) VALUES (?, ?, ?, ?, ?, ?, 'published', '', ?)
            """,
            (
                "artifact-before-v8",
                "task-before-v8",
                "before.md",
                "md",
                "/nonexistent/before.md",
                created_at,
                created_at,
            ),
        )
        self.assertNotIn("tool_effect_id", self._columns("artifacts"))
        self.assertFalse(self._object_exists("tool_effects", "table"))

        db.init_db()
        row = db.query_one(
            "SELECT * FROM artifacts WHERE id = 'artifact-before-v8'"
        ) or {}

        self.assertEqual(row.get("delivery_status"), "published")
        self.assertEqual(row.get("published_at"), created_at)
        self.assertEqual(row.get("tool_effect_id"), "")
        self.assertEqual(
            [version for version in self._versions() if version in {8, 9}],
            [8, 9],
        )
        self.assertTrue(self._object_exists("tool_effects", "table"))

    def test_v9_database_gets_automation_task_run_binding_in_v10(self) -> None:
        def legacy_automation_schema(conn: sqlite3.Connection) -> None:
            for column, definition in (
                ("attempt", "INTEGER NOT NULL DEFAULT 1"),
                ("trigger_event_id", "TEXT NOT NULL DEFAULT ''"),
                ("input_state_json", "TEXT NOT NULL DEFAULT '{}'"),
                ("output_state_json", "TEXT NOT NULL DEFAULT '{}'"),
                ("diff_json", "TEXT NOT NULL DEFAULT '{}'"),
                ("error_json", "TEXT NOT NULL DEFAULT '{}'"),
            ):
                db._ensure_column(conn, "loop_runs", column, definition)
            db._execute_schema_script(
                conn,
                """
                CREATE TABLE IF NOT EXISTS automation_trigger_events (
                    id TEXT PRIMARY KEY,
                    loop_id TEXT NOT NULL,
                    organization_id TEXT NOT NULL DEFAULT 'local-org',
                    workspace_id TEXT NOT NULL DEFAULT 'default',
                    user_id TEXT NOT NULL DEFAULT 'local-user',
                    trigger_type TEXT NOT NULL DEFAULT 'manual',
                    idempotency_key TEXT NOT NULL DEFAULT '',
                    payload_sha256 TEXT NOT NULL DEFAULT '',
                    payload_ciphertext TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'queued',
                    run_id TEXT NOT NULL DEFAULT '',
                    error TEXT NOT NULL DEFAULT '',
                    received_at TEXT NOT NULL,
                    started_at TEXT NOT NULL DEFAULT '',
                    finished_at TEXT NOT NULL DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS idx_loop_runs_attempt
                    ON loop_runs(loop_id, run_number DESC, attempt DESC);
                CREATE INDEX IF NOT EXISTS idx_automation_trigger_queue
                    ON automation_trigger_events(status, received_at);
                CREATE INDEX IF NOT EXISTS idx_automation_trigger_loop
                    ON automation_trigger_events(loop_id, received_at DESC);
                """,
            )

        legacy_migrations = tuple(
            (version, legacy_automation_schema if version == 4 else migration)
            for version, migration in db.SCHEMA_MIGRATIONS
            if int(version) <= 9
        )
        with patch.object(db, "SCHEMA_MIGRATIONS", legacy_migrations):
            db.init_db()
        self.assertNotIn("task_run_id", self._columns("loop_runs"))

        db.init_db()

        self.assertIn("task_run_id", self._columns("loop_runs"))
        self.assertEqual(
            [version for version in self._versions() if version in {8, 9, 10}],
            [8, 9, 10],
        )

    def test_migration_8_failure_rolls_back_column_index_and_history(self) -> None:
        self._initialize_through(7)
        TaskStateService(db.get_conn)

        def failing_migration(conn: sqlite3.Connection) -> None:
            db._artifact_effect_identity_and_pending_run_fence(conn)
            conn.execute("CREATE TABLE migration_8_partial(id INTEGER)")
            raise RuntimeError("injected migration 8 failure")

        with patch.object(db, "SCHEMA_MIGRATIONS", ((8, failing_migration),)):
            with self.assertRaisesRegex(RuntimeError, "migration 8 failure"):
                db.init_db()

        self.assertNotIn("tool_effect_id", self._columns("artifacts"))
        self.assertFalse(
            self._object_exists("idx_artifacts_tool_effect", "index")
        )
        self.assertFalse(self._object_exists("migration_8_partial", "table"))
        self.assertIsNone(
            db.query_one("SELECT version FROM schema_migrations WHERE version = 8")
        )

    def test_migration_9_failure_rolls_back_all_journal_objects_and_history(self) -> None:
        self._initialize_through(8)

        def failing_migration(conn: sqlite3.Connection) -> None:
            db._tool_effect_journal_schema(conn)
            conn.execute("CREATE TABLE migration_9_partial(id INTEGER)")
            raise RuntimeError("injected migration 9 failure")

        with patch.object(db, "SCHEMA_MIGRATIONS", ((9, failing_migration),)):
            with self.assertRaisesRegex(RuntimeError, "migration 9 failure"):
                db.init_db()

        for name, kind in (
            ("tool_effects", "table"),
            ("tool_effect_transitions", "table"),
            ("idx_tool_effects_task", "index"),
            ("idx_tool_effects_state_lease", "index"),
            ("idx_tool_effect_transitions_effect", "index"),
            ("migration_9_partial", "table"),
        ):
            self.assertFalse(self._object_exists(name, kind), name)
        self.assertIsNone(
            db.query_one("SELECT version FROM schema_migrations WHERE version = 9")
        )

    def test_runner_preserves_existing_outer_transaction_authority(self) -> None:
        self._initialize_through(7)
        with closing(db.get_conn()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("CREATE TABLE caller_owned_change(id INTEGER)")
            with patch.object(
                db,
                "SCHEMA_MIGRATIONS",
                ((8, db._artifact_effect_identity_and_pending_run_fence),),
            ):
                db._apply_schema_migrations(conn)

            self.assertTrue(conn.in_transaction)
            self.assertIn(
                "tool_effect_id",
                {
                    str(row[1])
                    for row in conn.execute("PRAGMA table_info(artifacts)").fetchall()
                },
            )
            self.assertIsNotNone(
                conn.execute(
                    "SELECT version FROM schema_migrations WHERE version = 8"
                ).fetchone()
            )
            conn.rollback()

        self.assertFalse(self._object_exists("caller_owned_change", "table"))
        self.assertNotIn("tool_effect_id", self._columns("artifacts"))
        self.assertIsNone(
            db.query_one("SELECT version FROM schema_migrations WHERE version = 8")
        )

    def test_legacy_executescript_and_compound_trigger_remain_atomic(self) -> None:
        self._initialize_through(8)

        def failing_legacy_style_migration(conn: sqlite3.Connection) -> None:
            conn.executescript(
                """
                CREATE TABLE migration_source(value TEXT NOT NULL);
                CREATE TABLE migration_audit(value TEXT NOT NULL);
                CREATE TRIGGER migration_compound_trigger
                AFTER INSERT ON migration_source
                BEGIN
                    INSERT INTO migration_audit(value) VALUES ('first:' || NEW.value);
                    INSERT INTO migration_audit(value) VALUES ('second:' || NEW.value);
                END;
                CREATE INDEX migration_audit_value ON migration_audit(value);
                """
            )
            conn.execute("INSERT INTO migration_source(value) VALUES ('probe')")
            count = conn.execute(
                "SELECT COUNT(*) FROM migration_audit"
            ).fetchone()[0]
            if int(count) != 2:
                raise AssertionError("compound trigger was split incorrectly")
            raise RuntimeError("injected legacy executescript failure")

        with patch.object(
            db, "SCHEMA_MIGRATIONS", ((9, failing_legacy_style_migration),)
        ):
            with self.assertRaisesRegex(RuntimeError, "executescript failure"):
                db.init_db()

        for name, kind in (
            ("migration_source", "table"),
            ("migration_audit", "table"),
            ("migration_compound_trigger", "trigger"),
            ("migration_audit_value", "index"),
        ):
            self.assertFalse(self._object_exists(name, kind), name)
        self.assertIsNone(
            db.query_one("SELECT version FROM schema_migrations WHERE version = 9")
        )


if __name__ == "__main__":
    unittest.main()
