from __future__ import annotations

import json
import re
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Any, Callable, Iterator, Mapping

from app import db
from app.services.context_service import ExecutionScope
from app.services.model_defaults import configured_default_model_id


WORKSPACE_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS workspaces (
    id TEXT PRIMARY KEY,
    organization_id TEXT NOT NULL DEFAULT 'local-org',
    owner_user_id TEXT NOT NULL DEFAULT 'local-user',
    name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    default_agent_id TEXT NOT NULL DEFAULT 'general-agent',
    default_model_id TEXT NOT NULL DEFAULT 'deterministic',
    settings_json TEXT NOT NULL DEFAULT '{}',
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_workspaces_scope
    ON workspaces(organization_id, owner_user_id, enabled, updated_at DESC);
"""


class WorkspaceError(RuntimeError):
    pass


class WorkspaceNotFoundError(WorkspaceError):
    pass


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _clean_text(value: Any, *, field: str, allow_empty: bool = True, max_length: int = 10_000) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field} must be a string")
    result = value.strip()
    if not result and not allow_empty:
        raise ValueError(f"{field} cannot be empty")
    if len(result) > max_length:
        raise ValueError(f"{field} is too long")
    if any(ord(character) < 32 and character not in "\n\r\t" for character in result):
        raise ValueError(f"{field} cannot contain control characters")
    return result


def _clean_id(value: Any, *, field: str) -> str:
    result = _clean_text(value, field=field, allow_empty=False, max_length=80)
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]+", result):
        raise ValueError(f"{field} must start with a letter or digit and contain only letters, digits, '_', '-' or '.'")
    return result


def _settings(value: Mapping[str, Any] | None) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise TypeError("settings must be an object")
    text = json.dumps(dict(value), ensure_ascii=False)
    if len(text) > 20_000:
        raise ValueError("settings is too large")
    return dict(value)


class WorkspaceService:
    def __init__(
        self,
        connection: sqlite3.Connection | Callable[[], sqlite3.Connection] | None = None,
        *,
        clock: Callable[[], str] | None = None,
        auto_init: bool = True,
    ) -> None:
        self._shared_connection = connection if isinstance(connection, sqlite3.Connection) else None
        self._connection_factory = connection if callable(connection) else db.get_conn
        self._clock = clock or _utc_now
        self._lock = threading.RLock()
        if auto_init:
            self.init_schema()

    def init_schema(self) -> None:
        with self._connection(write=True) as conn:
            db._execute_schema_script(conn, WORKSPACE_SCHEMA_SQL)
            self._ensure_default(conn)

    def _connection(self, *, write: bool = False) -> Iterator[sqlite3.Connection]:
        class _Context:
            def __init__(inner_self, outer: WorkspaceService) -> None:
                inner_self.outer = outer
                inner_self.conn: sqlite3.Connection | None = None
                inner_self.owns = False
                inner_self.original_row_factory: Any = None

            def __enter__(inner_self) -> sqlite3.Connection:
                outer = inner_self.outer
                outer._lock.acquire()
                conn = outer._shared_connection or outer._connection_factory()
                inner_self.conn = conn
                inner_self.owns = outer._shared_connection is None
                inner_self.original_row_factory = conn.row_factory
                conn.row_factory = sqlite3.Row
                if write:
                    conn.execute("BEGIN IMMEDIATE")
                return conn

            def __exit__(inner_self, exc_type: Any, exc: Any, tb: Any) -> None:
                assert inner_self.conn is not None
                try:
                    if write:
                        if exc_type is None:
                            inner_self.conn.commit()
                        else:
                            inner_self.conn.rollback()
                finally:
                    if inner_self.owns:
                        inner_self.conn.close()
                    else:
                        inner_self.conn.row_factory = inner_self.original_row_factory
                    inner_self.outer._lock.release()

        return _Context(self)

    def _ensure_default(self, conn: sqlite3.Connection) -> None:
        if conn.execute("SELECT 1 FROM workspaces WHERE id = 'default'").fetchone():
            return
        now = self._clock()
        conn.execute(
            """
            INSERT INTO workspaces(
                id, organization_id, owner_user_id, name, description,
                default_agent_id, default_model_id, settings_json, enabled,
                created_at, updated_at
            ) VALUES ('default', 'local-org', 'local-user', '默认项目', '系统默认工作区，兼容既有数据。', 'general-agent', ?, '{}', 1, ?, ?)
            """,
            (configured_default_model_id(), now, now),
        )

    @staticmethod
    def _scope(value: ExecutionScope | Mapping[str, Any] | None) -> ExecutionScope:
        return ExecutionScope.normalise(value)

    @staticmethod
    def _public(row: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "id": row["id"],
            "organization_id": row["organization_id"],
            "owner_user_id": row["owner_user_id"],
            "name": row["name"],
            "description": row["description"],
            "default_agent_id": row["default_agent_id"],
            "default_model_id": row["default_model_id"],
            "settings": db.json_loads(row.get("settings_json"), {}),
            "enabled": bool(row["enabled"]),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def list_workspaces(self, execution_scope: ExecutionScope | Mapping[str, Any] | None, *, include_disabled: bool = True) -> list[dict[str, Any]]:
        scope = self._scope(execution_scope)
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT * FROM workspaces WHERE organization_id = ? ORDER BY updated_at DESC, id",
                (scope.organization_id,),
            ).fetchall()
        items = [self._public(dict(row)) for row in rows]
        if not include_disabled:
            items = [item for item in items if item["enabled"]]
        return items

    def get_workspace(self, workspace_id: str, execution_scope: ExecutionScope | Mapping[str, Any] | None) -> dict[str, Any] | None:
        workspace_id = _clean_id(workspace_id, field="workspace_id")
        scope = self._scope(execution_scope)
        with self._connection() as conn:
            row = conn.execute(
                "SELECT * FROM workspaces WHERE id = ? AND organization_id = ?",
                (workspace_id, scope.organization_id),
            ).fetchone()
        return self._public(dict(row)) if row else None

    def _require_workspace(self, conn: sqlite3.Connection, workspace_id: str, scope: ExecutionScope) -> dict[str, Any]:
        row = conn.execute(
            "SELECT * FROM workspaces WHERE id = ? AND organization_id = ?",
            (workspace_id, scope.organization_id),
        ).fetchone()
        if row is None:
            raise WorkspaceNotFoundError("项目不存在或当前实例不可见")
        return dict(row)

    def create_workspace(
        self,
        execution_scope: ExecutionScope | Mapping[str, Any] | None,
        *,
        workspace_id: str,
        name: str,
        description: str = "",
        default_agent_id: str = "general-agent",
        default_model_id: str = "deterministic",
        settings: Mapping[str, Any] | None = None,
        enabled: bool = True,
    ) -> dict[str, Any]:
        scope = self._scope(execution_scope)
        workspace_id = _clean_id(workspace_id, field="workspace_id")
        name = _clean_text(name, field="name", allow_empty=False, max_length=120)
        description = _clean_text(description, field="description", max_length=2000)
        default_agent_id = _clean_id(default_agent_id, field="default_agent_id")
        default_model_id = _clean_id(default_model_id, field="default_model_id")
        settings_json = json.dumps(_settings(settings), ensure_ascii=False)
        if not isinstance(enabled, bool):
            raise TypeError("enabled must be a boolean")
        now = self._clock()
        with self._connection(write=True) as conn:
            conn.execute(
                """
                INSERT INTO workspaces(
                    id, organization_id, owner_user_id, name, description,
                    default_agent_id, default_model_id, settings_json, enabled,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    workspace_id,
                    scope.organization_id,
                    scope.user_id,
                    name,
                    description,
                    default_agent_id,
                    default_model_id,
                    settings_json,
                    int(enabled),
                    now,
                    now,
                ),
            )
            row = conn.execute("SELECT * FROM workspaces WHERE id = ?", (workspace_id,)).fetchone()
        return self._public(dict(row or {}))

    def update_workspace(
        self,
        workspace_id: str,
        execution_scope: ExecutionScope | Mapping[str, Any] | None,
        *,
        name: str,
        description: str = "",
        default_agent_id: str = "general-agent",
        default_model_id: str = "deterministic",
        settings: Mapping[str, Any] | None = None,
        enabled: bool = True,
    ) -> dict[str, Any]:
        workspace_id = _clean_id(workspace_id, field="workspace_id")
        scope = self._scope(execution_scope)
        name = _clean_text(name, field="name", allow_empty=False, max_length=120)
        description = _clean_text(description, field="description", max_length=2000)
        default_agent_id = _clean_id(default_agent_id, field="default_agent_id")
        default_model_id = _clean_id(default_model_id, field="default_model_id")
        settings_json = json.dumps(_settings(settings), ensure_ascii=False)
        if not isinstance(enabled, bool):
            raise TypeError("enabled must be a boolean")
        now = self._clock()
        with self._connection(write=True) as conn:
            self._require_workspace(conn, workspace_id, scope)
            conn.execute(
                """
                UPDATE workspaces
                SET name = ?, description = ?, default_agent_id = ?,
                    default_model_id = ?, settings_json = ?, enabled = ?, updated_at = ?
                WHERE id = ? AND organization_id = ?
                """,
                (
                    name,
                    description,
                    default_agent_id,
                    default_model_id,
                    settings_json,
                    int(enabled),
                    now,
                    workspace_id,
                    scope.organization_id,
                ),
            )
            row = conn.execute("SELECT * FROM workspaces WHERE id = ?", (workspace_id,)).fetchone()
        return self._public(dict(row or {}))

    def delete_workspace(
        self,
        workspace_id: str,
        execution_scope: ExecutionScope | Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        workspace_id = _clean_id(workspace_id, field="workspace_id")
        if workspace_id == "default":
            raise WorkspaceError("默认项目不能删除")
        scope = self._scope(execution_scope)
        with self._connection(write=True) as conn:
            self._require_workspace(conn, workspace_id, scope)
            conn.execute(
                "UPDATE workspaces SET enabled = 0, updated_at = ? WHERE id = ? AND organization_id = ?",
                (self._clock(), workspace_id, scope.organization_id),
            )
        return {"id": workspace_id, "deleted": True, "soft_deleted": True}
