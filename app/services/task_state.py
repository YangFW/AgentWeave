from __future__ import annotations

import base64
import hashlib
import json
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Mapping, Sequence

from app import db
from app.services.runtime_contract_service import canonical_json_hash
from app.services.verification_service import CandidateOutput, VerificationReport


_ARTIFACT_FORMAT_ALIASES = {
    "markdown": "md",
    "word": "docx",
    "powerpoint": "pptx",
    "ppt": "pptx",
    "excel": "xlsx",
    "htm": "html",
}


def _normalise_artifact_kind(value: Any) -> str:
    raw = str(value or "").strip().lower().lstrip(".")
    return _ARTIFACT_FORMAT_ALIASES.get(raw, raw)


RUN_STATUSES = frozenset(
    {"queued", "running", "paused", "waiting_approval", "completed", "failed", "cancelled"}
)
TERMINAL_RUN_STATUSES = frozenset({"completed", "failed", "cancelled"})
ACTIVE_RUN_STATUSES = frozenset({"running", "paused", "waiting_approval"})
RUN_TRANSITIONS: Mapping[str, frozenset[str]] = {
    "queued": frozenset({"running", "cancelled"}),
    "running": frozenset({"paused", "waiting_approval", "completed", "failed", "cancelled"}),
    "paused": frozenset({"running", "failed", "cancelled"}),
    "waiting_approval": frozenset({"running", "completed", "failed", "cancelled"}),
    "completed": frozenset(),
    "failed": frozenset(),
    "cancelled": frozenset(),
}

NODE_STATUSES = frozenset({"pending", "running", "completed", "failed", "skipped", "cancelled"})
TERMINAL_NODE_STATUSES = frozenset({"completed", "failed", "skipped", "cancelled"})
NODE_TRANSITIONS: Mapping[str, frozenset[str]] = {
    "pending": frozenset({"running", "skipped", "cancelled"}),
    "running": frozenset({"completed", "failed", "cancelled"}),
    "completed": frozenset(),
    "failed": frozenset(),
    "skipped": frozenset(),
    "cancelled": frozenset(),
}

COMMAND_STATUSES = frozenset({"queued", "claimed", "completed", "failed", "cancelled"})
COMMAND_TRANSITIONS: Mapping[str, frozenset[str]] = {
    "queued": frozenset({"claimed", "cancelled"}),
    "claimed": frozenset({"queued", "completed", "failed", "cancelled"}),
    "completed": frozenset(),
    "failed": frozenset(),
    "cancelled": frozenset(),
}

TASK_STATE_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS task_runs (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    attempt INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued'
        CHECK (status IN ('queued', 'running', 'paused', 'waiting_approval', 'completed', 'failed', 'cancelled')),
    current_node_id TEXT NOT NULL DEFAULT '',
    resumed_from_checkpoint_id TEXT NOT NULL DEFAULT '',
    result_json TEXT NOT NULL DEFAULT '{}',
    error_json TEXT NOT NULL DEFAULT '{}',
    metadata_json TEXT NOT NULL DEFAULT '{}',
    intake_state TEXT NOT NULL DEFAULT 'open'
        CHECK (intake_state IN ('open', 'closed')),
    accepted_generation INTEGER NOT NULL DEFAULT 0,
    applied_generation INTEGER NOT NULL DEFAULT 0,
    published_verification_id TEXT NOT NULL DEFAULT '',
    publication_hash TEXT NOT NULL DEFAULT '',
    intake_closed_at TEXT NOT NULL DEFAULT '',
    started_at TEXT NOT NULL DEFAULT '',
    finished_at TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (task_id, attempt)
);

CREATE TABLE IF NOT EXISTS task_nodes (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    node_key TEXT NOT NULL,
    parent_node_id TEXT,
    title TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'step',
    sequence INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'running', 'completed', 'failed', 'skipped', 'cancelled')),
    input_json TEXT NOT NULL DEFAULT '{}',
    output_json TEXT NOT NULL DEFAULT '{}',
    error_json TEXT NOT NULL DEFAULT '{}',
    metadata_json TEXT NOT NULL DEFAULT '{}',
    started_at TEXT NOT NULL DEFAULT '',
    finished_at TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (run_id, node_key),
    FOREIGN KEY (run_id) REFERENCES task_runs(id) ON DELETE CASCADE,
    FOREIGN KEY (parent_node_id) REFERENCES task_nodes(id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS task_checkpoints (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    node_id TEXT,
    sequence INTEGER NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    state_json TEXT NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    restored_at TEXT NOT NULL DEFAULT '',
    restore_count INTEGER NOT NULL DEFAULT 0,
    last_restore_metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    UNIQUE (run_id, sequence),
    FOREIGN KEY (run_id) REFERENCES task_runs(id) ON DELETE CASCADE,
    FOREIGN KEY (node_id) REFERENCES task_nodes(id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS task_commands (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    run_id TEXT,
    command_type TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'queued'
        CHECK (status IN ('queued', 'claimed', 'completed', 'failed', 'cancelled')),
    priority INTEGER NOT NULL DEFAULT 0,
    intake_generation INTEGER NOT NULL DEFAULT 0,
    available_at TEXT NOT NULL,
    worker_id TEXT NOT NULL DEFAULT '',
    result_json TEXT NOT NULL DEFAULT '{}',
    error_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    claimed_at TEXT NOT NULL DEFAULT '',
    completed_at TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL,
    FOREIGN KEY (run_id) REFERENCES task_runs(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS task_goal_specs (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    schema_version TEXT NOT NULL,
    status TEXT NOT NULL,
    spec_hash TEXT NOT NULL,
    supersedes_id TEXT NOT NULL DEFAULT '',
    spec_json TEXT NOT NULL,
    public_summary_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    UNIQUE (run_id, version),
    FOREIGN KEY (run_id) REFERENCES task_runs(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS task_run_goal_specs (
    run_id TEXT NOT NULL,
    goal_spec_id TEXT NOT NULL,
    attached_at TEXT NOT NULL,
    PRIMARY KEY (run_id, goal_spec_id),
    FOREIGN KEY (run_id) REFERENCES task_runs(id) ON DELETE CASCADE,
    FOREIGN KEY (goal_spec_id) REFERENCES task_goal_specs(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS task_verifications (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    goal_spec_id TEXT NOT NULL,
    attempt INTEGER NOT NULL,
    mode TEXT NOT NULL,
    status TEXT NOT NULL,
    report_json TEXT NOT NULL,
    public_report_json TEXT NOT NULL DEFAULT '{}',
    verifier_model_id TEXT NOT NULL DEFAULT '',
    candidate_sha256 TEXT NOT NULL,
    evidence_sha256 TEXT NOT NULL DEFAULT '',
    intake_generation INTEGER NOT NULL DEFAULT 0,
    repaired_from_id TEXT NOT NULL DEFAULT '',
    started_at TEXT NOT NULL,
    finished_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (run_id, goal_spec_id, attempt),
    FOREIGN KEY (run_id) REFERENCES task_runs(id) ON DELETE CASCADE,
    FOREIGN KEY (goal_spec_id) REFERENCES task_goal_specs(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_task_runs_task_attempt
    ON task_runs(task_id, attempt DESC);
CREATE INDEX IF NOT EXISTS idx_task_runs_status
    ON task_runs(status, updated_at);
CREATE INDEX IF NOT EXISTS idx_task_nodes_run_sequence
    ON task_nodes(run_id, sequence);
CREATE INDEX IF NOT EXISTS idx_task_checkpoints_run_sequence
    ON task_checkpoints(run_id, sequence DESC);
CREATE INDEX IF NOT EXISTS idx_task_commands_queue
    ON task_commands(status, available_at, priority DESC, created_at);
CREATE INDEX IF NOT EXISTS idx_task_commands_task
    ON task_commands(task_id, run_id, created_at);
CREATE INDEX IF NOT EXISTS idx_task_goal_specs_task_run_version
    ON task_goal_specs(task_id, run_id, version DESC);
CREATE INDEX IF NOT EXISTS idx_task_run_goal_specs_goal
    ON task_run_goal_specs(goal_spec_id, run_id);
CREATE INDEX IF NOT EXISTS idx_task_verifications_task_run_attempt
    ON task_verifications(task_id, run_id, attempt DESC);
"""

_TYPE_KEY = "__task_state_type__"
_UNSET = object()


class TaskStateError(RuntimeError):
    """Base error raised by the persistent task-state layer."""


class ActiveRunConflict(TaskStateError):
    """Raised when another Run already owns execution for the same Task."""

    def __init__(self, task_id: str, run_id: str) -> None:
        super().__init__(f"Task {task_id} already has active run {run_id}")
        self.task_id = task_id
        self.run_id = run_id


class RunIntakeClosed(TaskStateError):
    """Raised when a command loses the race with a terminal publication."""

    def __init__(self, task_id: str, run_id: str) -> None:
        super().__init__(
            f"Run {run_id} for task {task_id} no longer accepts runtime input"
        )
        self.task_id = task_id
        self.run_id = run_id


class PublicationConflict(TaskStateError):
    """A verified candidate cannot be linearized against the current intake."""

    def __init__(
        self,
        reason: str,
        *,
        pending_command_types: Iterable[str] = (),
    ) -> None:
        super().__init__(reason)
        self.reason = reason
        self.pending_command_types = tuple(
            dict.fromkeys(str(item) for item in pending_command_types if str(item))
        )


class StateNotFoundError(TaskStateError):
    """Raised when a requested run, node, checkpoint, or command does not exist."""


class InvalidStateTransition(TaskStateError):
    """Raised when a persisted state-machine transition is not allowed."""

    def __init__(self, entity: str, entity_id: str, old_status: str, new_status: str) -> None:
        super().__init__(f"{entity} {entity_id} cannot transition from {old_status!r} to {new_status!r}")
        self.entity = entity
        self.entity_id = entity_id
        self.old_status = old_status
        self.new_status = new_status


class TaskCancellationRequested(TaskStateError):
    """Cooperative-cancellation signal raised at safe execution boundaries."""

    def __init__(self, task_id: str, run_id: str | None = None) -> None:
        suffix = f" (run {run_id})" if run_id else ""
        super().__init__(f"Cancellation requested for task {task_id}{suffix}")
        self.task_id = task_id
        self.run_id = run_id


def _encode_state(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, datetime):
        return {_TYPE_KEY: "datetime", "value": value.isoformat()}
    if isinstance(value, date):
        return {_TYPE_KEY: "date", "value": value.isoformat()}
    if isinstance(value, Path):
        return {_TYPE_KEY: "path", "value": str(value)}
    if isinstance(value, uuid.UUID):
        return {_TYPE_KEY: "uuid", "value": str(value)}
    if isinstance(value, Decimal):
        return {_TYPE_KEY: "decimal", "value": str(value)}
    if isinstance(value, bytes):
        return {_TYPE_KEY: "bytes", "value": base64.b64encode(value).decode("ascii")}
    if isinstance(value, tuple):
        return {_TYPE_KEY: "tuple", "items": [_encode_state(item) for item in value]}
    if isinstance(value, (set, frozenset)):
        items = [_encode_state(item) for item in value]
        items.sort(key=lambda item: json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
        return {_TYPE_KEY: "set", "items": items}
    if isinstance(value, Mapping):
        if all(isinstance(key, str) for key in value) and _TYPE_KEY not in value:
            return {str(key): _encode_state(item) for key, item in value.items()}
        return {
            _TYPE_KEY: "mapping",
            "items": [[_encode_state(key), _encode_state(item)] for key, item in value.items()],
        }
    if isinstance(value, list):
        return [_encode_state(item) for item in value]
    raise TypeError(f"Checkpoint state contains unsupported value: {type(value).__name__}")


def _decode_state(value: Any) -> Any:
    if isinstance(value, list):
        return [_decode_state(item) for item in value]
    if not isinstance(value, dict):
        return value
    marker = value.get(_TYPE_KEY)
    if not marker:
        return {key: _decode_state(item) for key, item in value.items()}
    if marker == "datetime":
        return datetime.fromisoformat(value["value"])
    if marker == "date":
        return date.fromisoformat(value["value"])
    if marker == "path":
        return Path(value["value"])
    if marker == "uuid":
        return uuid.UUID(value["value"])
    if marker == "decimal":
        return Decimal(value["value"])
    if marker == "bytes":
        return base64.b64decode(value["value"])
    if marker == "tuple":
        return tuple(_decode_state(item) for item in value.get("items", []))
    if marker == "set":
        return set(_decode_state(item) for item in value.get("items", []))
    if marker == "mapping":
        return {_decode_state(key): _decode_state(item) for key, item in value.get("items", [])}
    raise ValueError(f"Unknown checkpoint state type marker: {marker}")


def serialize_checkpoint_state(value: Any) -> str:
    """Serialize execution state as inspectable JSON without using unsafe pickle."""

    return json.dumps(_encode_state(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def deserialize_checkpoint_state(value: str | bytes | None) -> Any:
    """Restore state created by :func:`serialize_checkpoint_state`."""

    if value is None or value == "":
        return None
    if isinstance(value, bytes):
        value = value.decode("utf-8")
    return _decode_state(json.loads(value))


def init_schema(conn: sqlite3.Connection | None = None) -> None:
    """Create the task-state schema, using the platform database by default."""

    owns_connection = conn is None
    connection = conn or db.get_conn()
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.executescript(TASK_STATE_SCHEMA_SQL)
        verification_columns = {
            row[1]
            for row in connection.execute(
                "PRAGMA table_info(task_verifications)"
            ).fetchall()
        }
        if "evidence_sha256" not in verification_columns:
            connection.execute(
                "ALTER TABLE task_verifications "
                "ADD COLUMN evidence_sha256 TEXT NOT NULL DEFAULT ''"
            )
        run_columns = {
            row[1]
            for row in connection.execute("PRAGMA table_info(task_runs)").fetchall()
        }
        added_intake_fence = "intake_state" not in run_columns
        for column, definition in (
            ("intake_state", "TEXT NOT NULL DEFAULT 'open'"),
            ("accepted_generation", "INTEGER NOT NULL DEFAULT 0"),
            ("applied_generation", "INTEGER NOT NULL DEFAULT 0"),
            ("published_verification_id", "TEXT NOT NULL DEFAULT ''"),
            ("publication_hash", "TEXT NOT NULL DEFAULT ''"),
            ("intake_closed_at", "TEXT NOT NULL DEFAULT ''"),
        ):
            if column not in run_columns:
                connection.execute(
                    f"ALTER TABLE task_runs ADD COLUMN {column} {definition}"  # noqa: S608 - fixed migration identifiers
                )
        command_columns = {
            row[1]
            for row in connection.execute("PRAGMA table_info(task_commands)").fetchall()
        }
        if "intake_generation" not in command_columns:
            connection.execute(
                "ALTER TABLE task_commands "
                "ADD COLUMN intake_generation INTEGER NOT NULL DEFAULT 0"
            )
        verification_columns = {
            row[1]
            for row in connection.execute(
                "PRAGMA table_info(task_verifications)"
            ).fetchall()
        }
        if "intake_generation" not in verification_columns:
            connection.execute(
                "ALTER TABLE task_verifications "
                "ADD COLUMN intake_generation INTEGER NOT NULL DEFAULT 0"
            )
        # A terminal legacy run must never begin accepting input simply because
        # the intake fence columns were added after it finished.  Run this only
        # during that actual column migration: doing it on every startup would
        # silently mask late corruption before the audited invariant sweep can
        # observe and repair it.
        if added_intake_fence:
            connection.execute(
                """
                UPDATE task_runs
                SET intake_state = 'closed',
                    intake_closed_at = CASE
                        WHEN intake_closed_at = ''
                            THEN COALESCE(NULLIF(finished_at, ''), updated_at)
                        ELSE intake_closed_at
                    END
                WHERE status IN ('completed', 'failed', 'cancelled')
                """
            )
        connection.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_task_commands_run_generation
            ON task_commands(run_id, intake_generation)
            WHERE run_id IS NOT NULL
              AND command_type IN ('message', 'cancel')
              AND intake_generation > 0
            """
        )
        # Databases created by the first task-state revision stored the creator
        # run directly on GoalSpec rows.  Preserve that history while adding the
        # explicit many-run association used by resume/retry.
        connection.execute(
            """
            INSERT OR IGNORE INTO task_run_goal_specs(run_id, goal_spec_id, attached_at)
            SELECT run_id, id, created_at FROM task_goal_specs
            """
        )
        # The core database creates ``artifacts`` before this service creates
        # ``task_runs`` on a fresh install.  Install the cross-table trigger
        # only now that both sides of the ownership check are present.
        db._ensure_artifact_pending_run_fence(connection)
        connection.commit()
    finally:
        if owns_connection:
            connection.close()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


def _row_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {key: row[key] for key in row.keys()}


def _json_object(value: Any, *, field: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise TypeError(f"{field} must be a mapping")
    return dict(value)


def _normalise_time(value: str | datetime | None, fallback: str) -> str:
    if value is None or value == "":
        return fallback
    if isinstance(value, datetime):
        parsed = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc).isoformat()
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"Invalid ISO timestamp: {value}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat()


class TaskStateService:
    """Durable run, node, checkpoint, and command state for the Agent runtime.

    The service is independent from ``AgentRuntime`` so lifecycle state can be
    persisted without changing the user-facing task projection.
    """

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

    @contextmanager
    def _connection(self, *, write: bool = False) -> Iterator[sqlite3.Connection]:
        with self._lock:
            conn = self._shared_connection or self._connection_factory()
            owns_connection = self._shared_connection is None
            original_row_factory = conn.row_factory
            conn.row_factory = sqlite3.Row
            try:
                conn.execute("PRAGMA foreign_keys = ON")
                if write:
                    conn.execute("BEGIN IMMEDIATE")
                yield conn
                if write:
                    conn.commit()
            except Exception:
                if write:
                    conn.rollback()
                raise
            finally:
                if owns_connection:
                    conn.close()
                else:
                    conn.row_factory = original_row_factory

    @contextmanager
    def transaction(
        self, *, write: bool = False
    ) -> Iterator[sqlite3.Connection]:
        """Share one core-state transaction with same-database orchestrators.

        Services that own extension tables (for example expert-team runs) can
        use this narrow boundary to commit their rows and the core Task/Run
        projection under the same lock and rollback scope.  The connection
        factory remains private so callers cannot bypass lifecycle handling.
        """

        with self._connection(write=write) as conn:
            yield conn

    def _now(self) -> str:
        return self._clock()

    def init_schema(self) -> None:
        if self._shared_connection is not None:
            init_schema(self._shared_connection)
            self.sweep_reconciled_terminal_invariants()
            return
        conn = self._connection_factory()
        try:
            init_schema(conn)
        finally:
            conn.close()
        # Compatibility bootstrap fallback: ``main.on_startup`` calls this
        # method before dispatching queued or interrupted work.  Keeping the
        # idempotent sweep here also protects deployments whose bootstrap has
        # not yet adopted an explicit sweep call, and closes residue that may
        # have landed before the new database trigger existed.
        self.sweep_reconciled_terminal_invariants()

    # -- Runs -------------------------------------------------------------

    def create_run(
        self,
        task_id: str,
        *,
        run_id: str | None = None,
        metadata: Mapping[str, Any] | None = None,
        resumed_from_checkpoint_id: str | None = None,
    ) -> dict[str, Any]:
        if not task_id.strip():
            raise ValueError("task_id cannot be empty")
        run_id = run_id or _new_id("trun")
        now = self._now()
        metadata_json = serialize_checkpoint_state(_json_object(metadata, field="metadata"))
        with self._connection(write=True) as conn:
            checkpoint_id = self._validate_resume_checkpoint(
                conn, task_id, resumed_from_checkpoint_id
            )
            attempt = int(
                conn.execute(
                    "SELECT COALESCE(MAX(attempt), 0) + 1 FROM task_runs WHERE task_id = ?",
                    (task_id,),
                ).fetchone()[0]
            )
            conn.execute(
                """
                INSERT INTO task_runs(
                    id, task_id, attempt, status, resumed_from_checkpoint_id,
                    metadata_json, created_at, updated_at
                ) VALUES (?, ?, ?, 'queued', ?, ?, ?, ?)
                """,
                (run_id, task_id, attempt, checkpoint_id, metadata_json, now, now),
            )
            row = conn.execute("SELECT * FROM task_runs WHERE id = ?", (run_id,)).fetchone()
        return self._serialize_run(_row_dict(row) or {})

    def begin_run(
        self,
        task_id: str,
        *,
        run_id: str | None = None,
        metadata: Mapping[str, Any] | None = None,
        resumed_from_checkpoint_id: str | None = None,
        activate_task_projection: bool = False,
        task_result: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not task_id.strip():
            raise ValueError("task_id cannot be empty")
        now = self._now()
        with self._connection(write=True) as conn:
            row: sqlite3.Row | None = None
            fresh_attempt = False
            if run_id:
                row = conn.execute("SELECT * FROM task_runs WHERE id = ?", (run_id,)).fetchone()
                if row is not None and row["task_id"] != task_id:
                    raise TaskStateError(f"Run {run_id} belongs to task {row['task_id']}, not {task_id}")
            else:
                row = conn.execute(
                    "SELECT * FROM task_runs WHERE task_id = ? AND status = 'queued' ORDER BY attempt LIMIT 1",
                    (task_id,),
                ).fetchone()
            if row is None:
                fresh_attempt = True
                run_id = run_id or _new_id("trun")
                checkpoint_id = self._validate_resume_checkpoint(
                    conn, task_id, resumed_from_checkpoint_id
                )
                attempt = int(
                    conn.execute(
                        "SELECT COALESCE(MAX(attempt), 0) + 1 FROM task_runs WHERE task_id = ?",
                        (task_id,),
                    ).fetchone()[0]
                )
                conn.execute(
                    """
                    INSERT INTO task_runs(
                        id, task_id, attempt, status, resumed_from_checkpoint_id,
                        metadata_json, started_at, created_at, updated_at
                    ) VALUES (?, ?, ?, 'running', ?, ?, ?, ?, ?)
                    """,
                    (
                        run_id,
                        task_id,
                        attempt,
                        checkpoint_id,
                        serialize_checkpoint_state(_json_object(metadata, field="metadata")),
                        now,
                        now,
                        now,
                    ),
                )
            else:
                run_id = str(row["id"])
                fresh_attempt = str(row["status"] or "") == "queued"
                self._assert_transition("run", run_id, row["status"], "running", RUN_TRANSITIONS)
                checkpoint_id = str(row["resumed_from_checkpoint_id"] or "")
                if resumed_from_checkpoint_id:
                    checkpoint_id = self._validate_resume_checkpoint(
                        conn, task_id, resumed_from_checkpoint_id
                    )
                merged_metadata = deserialize_checkpoint_state(row["metadata_json"]) or {}
                if metadata is not None:
                    merged_metadata.update(_json_object(metadata, field="metadata"))
                conn.execute(
                    """
                    UPDATE task_runs
                    SET status = 'running', resumed_from_checkpoint_id = ?, metadata_json = ?,
                        started_at = CASE WHEN started_at = '' THEN ? ELSE started_at END,
                        finished_at = '', updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        checkpoint_id,
                        serialize_checkpoint_state(merged_metadata),
                        now,
                        now,
                        run_id,
                    ),
                )
            active = conn.execute(
                """
                SELECT id FROM task_runs
                WHERE task_id = ? AND id != ? AND status IN ('running', 'paused', 'waiting_approval')
                LIMIT 1
                """,
                (task_id, run_id),
            ).fetchone()
            if active is not None:
                raise ActiveRunConflict(task_id, str(active["id"]))
            if activate_task_projection:
                # Starting an Agent attempt is one durable state transition:
                # the selected Run and its public Task projection must become
                # active in the same transaction.  In particular, checkpoint
                # decoding happens only *after* this commit, so a corrupt
                # checkpoint can never leave Task=completed / Run=running.
                task = conn.execute(
                    "SELECT status FROM tasks WHERE id = ?", (task_id,)
                ).fetchone()
                if task is None:
                    raise StateNotFoundError(f"task {task_id} was not found")
                task_status = str(task["status"] or "")
                allowed_task_statuses = (
                    {"queued", "completed", "failed", "cancelled"}
                    if fresh_attempt
                    else {"running", "waiting_approval"}
                )
                if task_status not in allowed_task_statuses:
                    raise PublicationConflict(
                        "Task projection cannot enter the selected running attempt"
                    )
                if fresh_attempt:
                    activated_result = serialize_checkpoint_state(
                        _json_object(task_result, field="task_result")
                    ) if task_result is not None else "{}"
                    task_update = conn.execute(
                        """
                        UPDATE tasks
                        SET status = 'running', result_json = ?, artifacts_json = '[]',
                            updated_at = ?
                        WHERE id = ? AND status = ?
                        """,
                        (activated_result, now, task_id, task_status),
                    )
                elif task_result is not None:
                    task_update = conn.execute(
                        """
                        UPDATE tasks SET status = 'running', result_json = ?, updated_at = ?
                        WHERE id = ? AND status = ?
                        """,
                        (
                            serialize_checkpoint_state(
                                _json_object(task_result, field="task_result")
                            ),
                            now,
                            task_id,
                            task_status,
                        ),
                    )
                else:
                    task_update = conn.execute(
                        """
                        UPDATE tasks SET status = 'running', updated_at = ?
                        WHERE id = ? AND status = ?
                        """,
                        (now, task_id, task_status),
                    )
                if task_update.rowcount != 1:
                    raise PublicationConflict(
                        "Task projection activation lost its start CAS"
                    )
            result = conn.execute("SELECT * FROM task_runs WHERE id = ?", (run_id,)).fetchone()
        return self._serialize_run(_row_dict(result) or {})

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        with self._connection() as conn:
            row = conn.execute("SELECT * FROM task_runs WHERE id = ?", (run_id,)).fetchone()
        return self._serialize_run(_row_dict(row)) if row else None

    def list_runs(
        self,
        *,
        task_id: str | None = None,
        status: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        if status is not None and status not in RUN_STATUSES:
            raise ValueError(f"Unknown run status: {status}")
        clauses: list[str] = []
        params: list[Any] = []
        if task_id is not None:
            clauses.append("task_id = ?")
            params.append(task_id)
        if status is not None:
            clauses.append("status = ?")
            params.append(status)
        sql = "SELECT * FROM task_runs"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY created_at DESC, attempt DESC LIMIT ?"
        params.append(max(1, int(limit)))
        with self._connection() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [self._serialize_run(_row_dict(row) or {}) for row in rows]

    def transition_run(
        self,
        run_id: str,
        status: str,
        *,
        result: Mapping[str, Any] | None = None,
        error: Mapping[str, Any] | None = None,
        metadata: Mapping[str, Any] | None = None,
        current_node_id: str | None | object = _UNSET,
    ) -> dict[str, Any]:
        if status not in RUN_STATUSES:
            raise ValueError(f"Unknown run status: {status}")
        now = self._now()
        with self._connection(write=True) as conn:
            row = self._require_row(conn, "task_runs", run_id, "run")
            self._assert_transition("run", run_id, row["status"], status, RUN_TRANSITIONS)
            merged_metadata = deserialize_checkpoint_state(row["metadata_json"]) or {}
            if metadata is not None:
                merged_metadata.update(_json_object(metadata, field="metadata"))
            values: dict[str, Any] = {
                "status": status,
                "metadata_json": serialize_checkpoint_state(merged_metadata),
                "updated_at": now,
            }
            if result is not None:
                values["result_json"] = serialize_checkpoint_state(_json_object(result, field="result"))
            if error is not None:
                values["error_json"] = serialize_checkpoint_state(_json_object(error, field="error"))
            if current_node_id is not _UNSET:
                node_id = str(current_node_id or "")
                if node_id:
                    self._validate_node_run(conn, node_id, run_id)
                values["current_node_id"] = node_id
            if status == "running":
                values["started_at"] = row["started_at"] or now
                values["finished_at"] = ""
            elif status in TERMINAL_RUN_STATUSES:
                if status == "completed":
                    pending_runtime_commands = conn.execute(
                        """
                        SELECT command_type FROM task_commands
                        WHERE task_id = ? AND (run_id = ? OR run_id IS NULL)
                          AND command_type IN ('message', 'cancel')
                          AND status IN ('queued', 'claimed')
                        ORDER BY intake_generation, created_at, id
                        """,
                        (row["task_id"], run_id),
                    ).fetchall()
                    if pending_runtime_commands:
                        raise PublicationConflict(
                            "Low-level completion cannot discard pending runtime input",
                            pending_command_types=[
                                str(item["command_type"])
                                for item in pending_runtime_commands
                            ],
                        )
                    if int(row["accepted_generation"] or 0) != int(
                        row["applied_generation"] or 0
                    ):
                        raise PublicationConflict(
                            "Low-level completion cannot advance unapplied input generations"
                        )
                values["finished_at"] = now
                values["current_node_id"] = ""
                values["intake_state"] = "closed"
                values["intake_closed_at"] = now
                if status != "completed":
                    values["applied_generation"] = int(
                        row["accepted_generation"] or 0
                    )

                # ``finish_run`` remains a low-level compatibility API for
                # recovery, expert-team and control paths.  It may close
                # non-runtime residue, but a successful completion must never
                # acknowledge or discard user input that was not applied.
                if status == "completed":
                    conn.execute(
                        """
                        UPDATE task_nodes
                        SET status = 'completed', finished_at = ?, updated_at = ?
                        WHERE run_id = ? AND status = 'running'
                        """,
                        (now, now, run_id),
                    )
                    skipped_output = serialize_checkpoint_state(
                        {"summary": "运行已结束，本节点未再执行"}
                    )
                    conn.execute(
                        """
                        UPDATE task_nodes
                        SET status = 'skipped', output_json = ?, finished_at = ?, updated_at = ?
                        WHERE run_id = ? AND status = 'pending'
                        """,
                        (skipped_output, now, now, run_id),
                    )
                elif status == "failed":
                    node_error = serialize_checkpoint_state(
                        _json_object(error, field="error")
                    )
                    conn.execute(
                        """
                        UPDATE task_nodes
                        SET status = 'failed', error_json = ?, finished_at = ?, updated_at = ?
                        WHERE run_id = ? AND status = 'running'
                        """,
                        (node_error, now, now, run_id),
                    )
                    cancelled_output = serialize_checkpoint_state(
                        {"summary": "运行失败，后续节点未执行"}
                    )
                    conn.execute(
                        """
                        UPDATE task_nodes
                        SET status = 'cancelled', output_json = ?, finished_at = ?, updated_at = ?
                        WHERE run_id = ? AND status = 'pending'
                        """,
                        (cancelled_output, now, now, run_id),
                    )
                else:
                    cancelled_output = serialize_checkpoint_state(
                        {"summary": "运行已取消"}
                    )
                    conn.execute(
                        """
                        UPDATE task_nodes
                        SET status = 'cancelled', output_json = ?, finished_at = ?, updated_at = ?
                        WHERE run_id = ? AND status IN ('pending', 'running')
                        """,
                        (cancelled_output, now, now, run_id),
                    )

                command_result = serialize_checkpoint_state(
                    {
                        "cancelled": status != "failed",
                        "reason": f"run_terminal_{status}",
                    }
                )
                command_error = serialize_checkpoint_state(
                    _json_object(error, field="error")
                )
                command_status = "failed" if status == "failed" else "cancelled"
                conn.execute(
                    """
                    UPDATE task_commands
                    SET status = ?, result_json = ?, error_json = ?,
                        completed_at = ?, updated_at = ?
                    WHERE task_id = ? AND (run_id = ? OR run_id IS NULL)
                      AND status IN ('queued', 'claimed')
                    """,
                    (
                        command_status,
                        command_result,
                        command_error,
                        now,
                        now,
                        row["task_id"],
                        run_id,
                    ),
                )

                artifact_table = conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'artifacts'"
                ).fetchone()
                if artifact_table is not None:
                    artifact_columns = {
                        str(item[1])
                        for item in conn.execute(
                            "PRAGMA table_info(artifacts)"
                        ).fetchall()
                    }
                    if {
                        "run_id",
                        "delivery_status",
                        "verification_id",
                        "published_at",
                    }.issubset(artifact_columns):
                        conn.execute(
                            """
                            UPDATE artifacts
                            SET delivery_status = 'rejected', verification_id = '', published_at = ''
                            WHERE task_id = ? AND run_id = ?
                              AND delivery_status = 'pending_verification'
                            """,
                            (row["task_id"], run_id),
                        )
            assignments = ", ".join(f"{key} = ?" for key in values)
            conn.execute(
                f"UPDATE task_runs SET {assignments} WHERE id = ?",  # noqa: S608 - fixed column names
                [*values.values(), run_id],
            )
            updated = conn.execute("SELECT * FROM task_runs WHERE id = ?", (run_id,)).fetchone()
        return self._serialize_run(_row_dict(updated) or {})

    def finish_run(
        self,
        run_id: str,
        *,
        status: str = "completed",
        result: Mapping[str, Any] | None = None,
        error: Mapping[str, Any] | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        if status not in TERMINAL_RUN_STATUSES:
            raise ValueError("finish_run status must be completed, failed, or cancelled")
        return self.transition_run(
            run_id,
            status,
            result=result,
            error=error,
            metadata=metadata,
        )

    def update_run_metadata(
        self, run_id: str, metadata: Mapping[str, Any], *, merge: bool = True
    ) -> dict[str, Any]:
        with self._connection(write=True) as conn:
            row = self._require_row(conn, "task_runs", run_id, "run")
            value = deserialize_checkpoint_state(row["metadata_json"]) or {} if merge else {}
            value.update(_json_object(metadata, field="metadata"))
            conn.execute(
                "UPDATE task_runs SET metadata_json = ?, updated_at = ? WHERE id = ?",
                (serialize_checkpoint_state(value), self._now(), run_id),
            )
            updated = conn.execute("SELECT * FROM task_runs WHERE id = ?", (run_id,)).fetchone()
        return self._serialize_run(_row_dict(updated) or {})

    def reconcile_legacy_terminal_projection(
        self,
        run_id: str,
        *,
        reason: str = "检测到历史终态任务仍保留未结束运行，已按任务终态安全收敛",
    ) -> dict[str, Any]:
        """Close one non-terminal legacy Run whose Task is already terminal.

        Older runtime paths could commit the public Task projection before the
        matching Run transaction existed. A process exit in that split-state
        window leaves a terminal Task (and its published answer/artifacts)
        beside an apparently active Run. Restart recovery must not execute that
        Run again: doing so can duplicate side effects or overwrite an answer
        the user already received.

        The terminal Task is therefore authoritative. In one transaction we
        close the orphan Run to the same status, close active nodes, cancel
        unfinished commands, reject only that Run's unverified artifacts, and
        append an audit event. The Task row is deliberately never updated, so
        its result, artifact projection, timestamps, and scope remain exactly
        as they were.

        A marker is not treated as permanent proof.  Older code (or a process
        that started before the original transaction committed) could append
        residue after the marker was written.  Repeated startup therefore
        verifies the latest reconciled attempt and, when necessary, performs
        another atomic cleanup with a distinct repair audit event.  A later
        retry owns task-scoped commands, so historical attempts never consume
        those commands during the sweep.
        """

        if not str(run_id or "").strip():
            raise ValueError("run_id cannot be empty")
        reason = str(reason or "").strip()
        if not reason:
            raise ValueError("reason cannot be empty")
        now = self._now()

        with self._connection(write=True) as conn:
            run = self._require_row(conn, "task_runs", run_id, "run")
            task_id = str(run["task_id"])
            task = conn.execute(
                "SELECT * FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
            if task is None:
                raise StateNotFoundError(f"task {task_id} was not found")

            task_status = str(task["status"] or "")
            run_status = str(run["status"] or "")
            run_metadata = deserialize_checkpoint_state(run["metadata_json"]) or {}
            reconciliation = run_metadata.get(
                "legacy_terminal_projection_reconciled"
            )
            later_attempt = conn.execute(
                "SELECT 1 FROM task_runs WHERE task_id = ? AND attempt > ? LIMIT 1",
                (task_id, int(run["attempt"] or 0)),
            ).fetchone()
            has_later_attempt = later_attempt is not None
            repairing_marker = bool(
                run_status in TERMINAL_RUN_STATUSES
                and task_status in TERMINAL_RUN_STATUSES
                and isinstance(reconciliation, Mapping)
                and reconciliation.get("reconciled") is True
            )

            if run_status in TERMINAL_RUN_STATUSES and not repairing_marker:
                return {
                    "reconciled": False,
                    "idempotent": True,
                    "reason": "run_already_terminal",
                    "task_status": task_status,
                    "previous_run_status": run_status,
                    "closed_node_ids": [],
                    "cancelled_command_ids": [],
                    "rejected_artifact_ids": [],
                    "run": self._serialize_run(_row_dict(run) or {}),
                }

            if not repairing_marker and run_status not in ACTIVE_RUN_STATUSES:
                return {
                    "reconciled": False,
                    "idempotent": False,
                    "reason": "run_not_active",
                    "task_status": task_status,
                    "previous_run_status": run_status,
                    "closed_node_ids": [],
                    "cancelled_command_ids": [],
                    "rejected_artifact_ids": [],
                    "run": self._serialize_run(_row_dict(run) or {}),
                }

            if task_status not in TERMINAL_RUN_STATUSES:
                return {
                    "reconciled": False,
                    "idempotent": run_status in TERMINAL_RUN_STATUSES,
                    "reason": (
                        "terminal_run_is_historical"
                        if run_status in TERMINAL_RUN_STATUSES
                        else "task_not_terminal"
                    ),
                    "task_status": task_status,
                    "previous_run_status": run_status,
                    "closed_node_ids": [],
                    "cancelled_command_ids": [],
                    "rejected_artifact_ids": [],
                    "run": self._serialize_run(_row_dict(run) or {}),
                }

            closed_nodes = conn.execute(
                "SELECT id, status, metadata_json FROM task_nodes "
                "WHERE run_id = ? AND status IN ('pending', 'running') "
                "ORDER BY sequence, id",
                (run_id,),
            ).fetchall()
            command_scope = (
                "(run_id = ? OR run_id IS NULL)"
                if not has_later_attempt
                else "run_id = ?"
            )
            unfinished_commands = conn.execute(
                "SELECT id FROM task_commands WHERE task_id = ? AND "
                + command_scope
                + " AND status IN ('queued', 'claimed') ORDER BY created_at, id",
                (task_id, run_id),
            ).fetchall()
            cancelled_command_ids = [
                str(command["id"]) for command in unfinished_commands
            ]

            rejected_artifact_ids: list[str] = []
            artifact_table = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' "
                "AND name = 'artifacts'"
            ).fetchone()
            if artifact_table is not None:
                artifact_columns = {
                    str(item[1])
                    for item in conn.execute("PRAGMA table_info(artifacts)").fetchall()
                }
                if {
                    "run_id",
                    "delivery_status",
                    "verification_id",
                    "published_at",
                }.issubset(artifact_columns):
                    rejected_artifacts = conn.execute(
                        "SELECT id FROM artifacts WHERE task_id = ? AND run_id = ? "
                        "AND delivery_status = 'pending_verification' "
                        "ORDER BY created_at, id",
                        (task_id, run_id),
                    ).fetchall()
                    rejected_artifact_ids = [
                        str(artifact["id"]) for artifact in rejected_artifacts
                    ]

            aligned_generation = max(
                int(run["accepted_generation"] or 0),
                int(run["applied_generation"] or 0),
            )
            projection_dirty = bool(
                run_status != task_status
                or str(run["current_node_id"] or "")
                or str(run["intake_state"] or "") != "closed"
                or not str(run["intake_closed_at"] or "")
                or not str(run["finished_at"] or "")
                or int(run["accepted_generation"] or 0)
                != int(run["applied_generation"] or 0)
            )
            if (
                repairing_marker
                and not closed_nodes
                and not cancelled_command_ids
                and not rejected_artifact_ids
                and not projection_dirty
            ):
                return {
                    "reconciled": True,
                    "repaired": False,
                    "idempotent": True,
                    "task_status": task_status,
                    "previous_run_status": str(
                        reconciliation.get("previous_run_status") or ""
                    ),
                    "closed_node_ids": [],
                    "cancelled_command_ids": [],
                    "rejected_artifact_ids": [],
                    "run": self._serialize_run(_row_dict(run) or {}),
                }

            closed_node_ids: list[str] = []
            for node in closed_nodes:
                node_metadata = (
                    deserialize_checkpoint_state(node["metadata_json"]) or {}
                )
                node_metadata["legacy_terminal_projection_reconciled"] = {
                    "reconciled": True,
                    "repair": repairing_marker,
                    "previous_status": str(node["status"] or ""),
                    "task_status": task_status,
                    "reconciled_at": now,
                }
                cursor = conn.execute(
                    "UPDATE task_nodes SET status = 'cancelled', metadata_json = ?, "
                    "finished_at = ?, updated_at = ? WHERE id = ? "
                    "AND status IN ('pending', 'running')",
                    (
                        serialize_checkpoint_state(node_metadata),
                        now,
                        now,
                        node["id"],
                    ),
                )
                if cursor.rowcount != 1:
                    raise PublicationConflict(
                        "Legacy terminal reconciliation lost its node CAS"
                    )
                closed_node_ids.append(str(node["id"]))

            command_reason = (
                "legacy_terminal_projection_repaired"
                if repairing_marker
                else "legacy_terminal_projection_reconciled"
            )
            command_result = serialize_checkpoint_state(
                {
                    "cancelled": True,
                    "reason": command_reason,
                    "task_status": task_status,
                }
            )
            if cancelled_command_ids:
                cancelled = conn.execute(
                    "UPDATE task_commands SET status = 'cancelled', "
                    "result_json = ?, completed_at = ?, updated_at = ? "
                    "WHERE task_id = ? AND "
                    + command_scope
                    + " AND status IN ('queued', 'claimed')",
                    (command_result, now, now, task_id, run_id),
                )
                if cancelled.rowcount != len(cancelled_command_ids):
                    raise PublicationConflict(
                        "Legacy terminal reconciliation lost its command CAS"
                    )

            if rejected_artifact_ids:
                rejected = conn.execute(
                    "UPDATE artifacts SET delivery_status = 'rejected', "
                    "verification_id = '', published_at = '' "
                    "WHERE task_id = ? AND run_id = ? "
                    "AND delivery_status = 'pending_verification'",
                    (task_id, run_id),
                )
                if rejected.rowcount != len(rejected_artifact_ids):
                    raise PublicationConflict(
                        "Legacy terminal reconciliation lost its artifact CAS"
                    )

            if repairing_marker:
                marker = dict(reconciliation)
                marker.update(
                    {
                        "reconciled": True,
                        "task_status": task_status,
                        "last_repaired_at": now,
                        "repair_count": int(marker.get("repair_count") or 0) + 1,
                        "last_repair_closed_node_count": len(closed_node_ids),
                        "last_repair_cancelled_command_count": len(
                            cancelled_command_ids
                        ),
                        "last_repair_rejected_artifact_count": len(
                            rejected_artifact_ids
                        ),
                    }
                )
            else:
                marker = {
                    "reconciled": True,
                    "previous_run_status": run_status,
                    "task_status": task_status,
                    "reconciled_at": now,
                    "repair_count": 0,
                    "closed_node_count": len(closed_node_ids),
                    "cancelled_command_count": len(cancelled_command_ids),
                    "rejected_artifact_count": len(rejected_artifact_ids),
                }
            run_metadata["legacy_terminal_projection_reconciled"] = marker
            updated_run_cursor = conn.execute(
                """
                UPDATE task_runs
                SET status = ?, current_node_id = '', metadata_json = ?,
                    intake_state = 'closed', accepted_generation = ?,
                    applied_generation = ?,
                    intake_closed_at = CASE
                        WHEN intake_closed_at = '' THEN ? ELSE intake_closed_at END,
                    finished_at = CASE
                        WHEN finished_at = '' THEN ? ELSE finished_at END,
                    updated_at = ?
                WHERE id = ? AND task_id = ? AND status = ?
                """,
                (
                    task_status,
                    serialize_checkpoint_state(run_metadata),
                    aligned_generation,
                    aligned_generation,
                    now,
                    now,
                    now,
                    run_id,
                    task_id,
                    run_status,
                ),
            )
            if updated_run_cursor.rowcount != 1:
                raise PublicationConflict(
                    "Legacy terminal reconciliation lost its Run CAS"
                )

            event_cursor = conn.execute(
                """INSERT INTO task_events(
                       task_id, ts, type, title, content, data_json
                   ) VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    task_id,
                    now,
                    (
                        "legacy_terminal_projection_repaired"
                        if repairing_marker
                        else "legacy_terminal_projection_reconciled"
                    ),
                    (
                        "已修复历史运行残留"
                        if repairing_marker
                        else "已收敛历史运行状态"
                    ),
                    reason,
                    serialize_checkpoint_state(
                        {
                            "run_id": run_id,
                            "previous_run_status": run_status,
                            "task_status": task_status,
                            "repair": repairing_marker,
                            "projection_repaired": projection_dirty,
                            "closed_node_ids": closed_node_ids,
                            "cancelled_command_ids": cancelled_command_ids,
                            "rejected_artifact_ids": rejected_artifact_ids,
                        }
                    ),
                ),
            )
            updated_run = conn.execute(
                "SELECT * FROM task_runs WHERE id = ?", (run_id,)
            ).fetchone()

        return {
            "reconciled": True,
            "repaired": repairing_marker,
            "idempotent": False,
            "task_status": task_status,
            "previous_run_status": str(
                marker.get("previous_run_status") or run_status
            ),
            "event_id": int(event_cursor.lastrowid),
            "closed_node_ids": closed_node_ids,
            "cancelled_command_ids": cancelled_command_ids,
            "rejected_artifact_ids": rejected_artifact_ids,
            "run": self._serialize_run(_row_dict(updated_run) or {}),
        }

    def sweep_reconciled_terminal_invariants(self) -> dict[str, Any]:
        """Recheck latest reconciled terminal attempts before dispatch starts.

        Only the latest attempt for a Task may own task-scoped commands.  Old
        reconciled attempts are intentionally excluded when an explicit retry
        exists, including a queued retry whose Task projection still reflects
        the previous terminal result.
        """

        empty = {
            "checked_run_ids": [],
            "repaired_run_ids": [],
            "outcomes": [],
        }
        with self._connection() as conn:
            has_tasks = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'tasks'"
            ).fetchone()
            if has_tasks is None:
                return empty
            rows = conn.execute(
                """
                SELECT r.id, r.metadata_json
                FROM task_runs AS r
                JOIN tasks AS t ON t.id = r.task_id
                WHERE r.status IN ('completed', 'failed', 'cancelled')
                  AND t.status IN ('completed', 'failed', 'cancelled')
                  AND NOT EXISTS (
                      SELECT 1 FROM task_runs AS newer
                      WHERE newer.task_id = r.task_id
                        AND newer.attempt > r.attempt
                  )
                ORDER BY r.created_at, r.attempt, r.id
                """
            ).fetchall()
        checked_run_ids: list[str] = []
        repaired_run_ids: list[str] = []
        outcomes: list[dict[str, Any]] = []
        for row in rows:
            metadata = deserialize_checkpoint_state(row["metadata_json"]) or {}
            marker = (
                metadata.get("legacy_terminal_projection_reconciled")
                if isinstance(metadata, Mapping)
                else None
            )
            if not isinstance(marker, Mapping) or marker.get("reconciled") is not True:
                continue
            run_id = str(row["id"])
            checked_run_ids.append(run_id)
            outcome = self.reconcile_legacy_terminal_projection(
                run_id,
                reason=(
                    "平台启动时复核历史终态运行，检测并清理晚到的持久化残留"
                ),
            )
            outcomes.append(outcome)
            if outcome.get("repaired") is True:
                repaired_run_ids.append(run_id)
        return {
            "checked_run_ids": checked_run_ids,
            "repaired_run_ids": repaired_run_ids,
            "outcomes": outcomes,
        }

    def recover_interrupted_attempt(
        self,
        run_id: str,
        *,
        reason: str = "平台服务重启，已创建恢复尝试",
        error_type: str = "ServiceRestart",
        metadata: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Atomically replace an interrupted attempt with one queued retry.

        Restart recovery is an intake hand-off, not a terminal failure of the
        user's Task.  Closing the old Run through ``finish_run`` used to fail
        every queued/claimed command as a side effect.  A message or cancel
        accepted immediately before shutdown could therefore disappear.

        This transaction closes the interrupted attempt, creates the retry at
        the latest safe checkpoint, transfers every unfinished run-bound
        command (releasing dead-worker claims), and moves the Task projection
        to ``queued`` as one indivisible operation.  Durable approval proofs
        are copied to the retry metadata and, when present, the committed Task
        result is retained as its activation result.
        """

        if not str(run_id or "").strip():
            raise ValueError("run_id cannot be empty")
        reason = str(reason or "").strip()
        error_type = str(error_type or "").strip()
        if not reason or not error_type:
            raise ValueError("reason and error_type cannot be empty")
        recovery_metadata = _json_object(metadata, field="metadata")
        now = self._now()

        with self._connection(write=True) as conn:
            old_run = self._require_row(conn, "task_runs", run_id, "run")
            task_id = str(old_run["task_id"])

            # A repeated startup may revisit an already closed source attempt.
            # Return the exact durable child instead of creating a third Run.
            if str(old_run["status"] or "") in TERMINAL_RUN_STATUSES:
                for candidate in conn.execute(
                    "SELECT * FROM task_runs WHERE task_id = ? AND attempt > ? "
                    "ORDER BY attempt",
                    (task_id, int(old_run["attempt"] or 0)),
                ).fetchall():
                    candidate_metadata = (
                        deserialize_checkpoint_state(candidate["metadata_json"]) or {}
                    )
                    if str(candidate_metadata.get("previous_run_id") or "") == run_id:
                        return {
                            "recovered": True,
                            "idempotent": True,
                            "previous_run_id": run_id,
                            "checkpoint_id": str(
                                candidate["resumed_from_checkpoint_id"] or ""
                            ),
                            "transferred_command_ids": [],
                            "run": self._serialize_run(
                                _row_dict(candidate) or {}
                            ),
                        }
                raise PublicationConflict(
                    "Interrupted Run is terminal without a durable recovery child"
                )

            if str(old_run["status"] or "") not in {"running", "paused"}:
                raise PublicationConflict(
                    "Only running or paused attempts can enter restart recovery"
                )
            if str(old_run["intake_state"] or "open") != "open":
                raise PublicationConflict("Interrupted Run intake is already closed")
            if str(old_run["published_verification_id"] or ""):
                raise PublicationConflict(
                    "A published attempt cannot be replaced by restart recovery"
                )

            task = conn.execute(
                "SELECT * FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
            if task is None:
                raise StateNotFoundError(f"task {task_id} was not found")
            if str(task["status"] or "") not in {"running", "queued"}:
                raise PublicationConflict(
                    "Task projection is not eligible for restart recovery"
                )
            other_active = conn.execute(
                "SELECT id FROM task_runs WHERE task_id = ? AND id != ? "
                "AND status IN ('running', 'paused', 'waiting_approval') LIMIT 1",
                (task_id, run_id),
            ).fetchone()
            if other_active is not None:
                raise ActiveRunConflict(task_id, str(other_active["id"]))

            checkpoint = conn.execute(
                "SELECT id FROM task_checkpoints WHERE run_id = ? "
                "ORDER BY sequence DESC LIMIT 1",
                (run_id,),
            ).fetchone()
            checkpoint_id = str(checkpoint["id"] if checkpoint else "")
            new_run_id = _new_id("trun")
            attempt = int(
                conn.execute(
                    "SELECT COALESCE(MAX(attempt), 0) + 1 FROM task_runs "
                    "WHERE task_id = ?",
                    (task_id,),
                ).fetchone()[0]
            )
            old_metadata = (
                deserialize_checkpoint_state(old_run["metadata_json"]) or {}
            )
            old_result = deserialize_checkpoint_state(task["result_json"]) or {}
            durable_metadata: dict[str, Any] = {}
            for key, value in old_metadata.items():
                if (
                    key in {
                        "agent_id",
                        "workspace",
                        "organization_id",
                        "user_id",
                        "executor_type",
                        "executor_id",
                        "effective_permissions",
                        "permission_source",
                    }
                    or key.endswith("_decision")
                    or key.endswith("_decisions")
                ):
                    durable_metadata[key] = value
            durable_metadata.update(recovery_metadata)
            durable_metadata.update(
                {
                    "recovered_after_restart": True,
                    "previous_run_id": run_id,
                }
            )
            if any(
                key in old_result
                for key in (
                    "policy_approval_decision",
                    "policy_approval_decisions",
                    "skill_recommendation_decision",
                    "approval_resolution",
                    "skip_skill_recommendations",
                )
            ):
                durable_metadata["recovery_activation_result"] = old_result

            command_rows = conn.execute(
                "SELECT id, command_type FROM task_commands "
                "WHERE task_id = ? AND run_id = ? "
                "AND status IN ('queued', 'claimed') ORDER BY created_at, id",
                (task_id, run_id),
            ).fetchall()
            transferred_command_ids = [str(row["id"]) for row in command_rows]
            pending_runtime_types = sorted(
                {
                    str(row["command_type"])
                    for row in command_rows
                    if str(row["command_type"]) in {"message", "cancel"}
                }
            )
            if pending_runtime_types:
                # ``run_task`` uses this durable marker to reconcile steering
                # before re-evaluating task-created policy on the recovered
                # branch.  Without it an old rejected policy proof could win
                # before the newer user message is applied.
                durable_metadata["recovery_pending_runtime_input"] = True
                durable_metadata["recovery_pending_command_types"] = (
                    pending_runtime_types
                )

            original_accepted = int(old_run["accepted_generation"] or 0)
            original_applied = int(old_run["applied_generation"] or 0)
            conn.execute(
                """
                INSERT INTO task_runs(
                    id, task_id, attempt, status, resumed_from_checkpoint_id,
                    metadata_json, accepted_generation, applied_generation,
                    created_at, updated_at
                ) VALUES (?, ?, ?, 'queued', ?, ?, ?, ?, ?, ?)
                """,
                (
                    new_run_id,
                    task_id,
                    attempt,
                    checkpoint_id,
                    serialize_checkpoint_state(durable_metadata),
                    original_accepted,
                    original_applied,
                    now,
                    now,
                ),
            )

            conn.execute(
                """
                UPDATE task_commands
                SET run_id = ?, status = 'queued', worker_id = '', claimed_at = '',
                    completed_at = '', updated_at = ?
                WHERE task_id = ? AND run_id = ?
                  AND status IN ('queued', 'claimed')
                """,
                (new_run_id, now, task_id, run_id),
            )

            error_payload = serialize_checkpoint_state(
                {"message": reason, "error_type": error_type}
            )
            interrupted_metadata = dict(old_metadata)
            interrupted_metadata.update(
                {
                    "interrupted": True,
                    "recovery_run_id": new_run_id,
                    "transferred_command_ids": transferred_command_ids,
                }
            )
            old_update = conn.execute(
                """
                UPDATE task_runs
                SET status = 'failed', error_json = ?, metadata_json = ?,
                    current_node_id = '', intake_state = 'closed',
                    intake_closed_at = ?, accepted_generation = applied_generation,
                    finished_at = ?, updated_at = ?
                WHERE id = ? AND status IN ('running', 'paused')
                  AND intake_state = 'open'
                """,
                (
                    error_payload,
                    serialize_checkpoint_state(interrupted_metadata),
                    now,
                    now,
                    now,
                    run_id,
                ),
            )
            if old_update.rowcount != 1:
                raise PublicationConflict(
                    "Interrupted Run lost its restart-recovery CAS"
                )
            for node in conn.execute(
                "SELECT id, metadata_json FROM task_nodes "
                "WHERE run_id = ? AND status = 'running'",
                (run_id,),
            ).fetchall():
                node_metadata = (
                    deserialize_checkpoint_state(node["metadata_json"]) or {}
                )
                node_metadata["interrupted"] = True
                conn.execute(
                    "UPDATE task_nodes SET status = 'failed', error_json = ?, "
                    "metadata_json = ?, finished_at = ?, updated_at = ? "
                    "WHERE id = ? AND status = 'running'",
                    (
                        error_payload,
                        serialize_checkpoint_state(node_metadata),
                        now,
                        now,
                        node["id"],
                    ),
                )
            conn.execute(
                "UPDATE task_nodes SET status = 'cancelled', output_json = ?, "
                "finished_at = ?, updated_at = ? "
                "WHERE run_id = ? AND status = 'pending'",
                (
                    serialize_checkpoint_state(
                        {"summary": "旧运行已中断，将从安全检查点恢复"}
                    ),
                    now,
                    now,
                    run_id,
                ),
            )
            artifact_table = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' "
                "AND name = 'artifacts'"
            ).fetchone()
            if artifact_table is not None:
                artifact_columns = {
                    str(item[1])
                    for item in conn.execute("PRAGMA table_info(artifacts)").fetchall()
                }
                if {
                    "run_id",
                    "delivery_status",
                    "verification_id",
                    "published_at",
                }.issubset(artifact_columns):
                    conn.execute(
                        "UPDATE artifacts SET delivery_status = 'rejected', "
                        "verification_id = '', published_at = '' "
                        "WHERE task_id = ? AND run_id = ? "
                        "AND delivery_status = 'pending_verification'",
                        (task_id, run_id),
                    )

            task_update = conn.execute(
                "UPDATE tasks SET status = 'queued', updated_at = ? "
                "WHERE id = ? AND status IN ('running', 'queued')",
                (now, task_id),
            )
            if task_update.rowcount != 1:
                raise PublicationConflict(
                    "Task projection lost its restart-recovery CAS"
                )
            event_cursor = conn.execute(
                """
                INSERT INTO task_events(task_id, ts, type, title, content, data_json)
                VALUES (?, ?, 'recovery_scheduled', ?, ?, ?)
                """,
                (
                    task_id,
                    now,
                    "已安排服务重启恢复",
                    (
                        "将从最近安全检查点创建新的运行尝试。"
                        if checkpoint_id
                        else "未找到检查点，将从任务起点重新执行。"
                    ),
                    serialize_checkpoint_state(
                        {
                            "previous_run_id": run_id,
                            "run_id": new_run_id,
                            "checkpoint_id": checkpoint_id,
                            "transferred_command_ids": transferred_command_ids,
                        }
                    ),
                ),
            )
            new_run = conn.execute(
                "SELECT * FROM task_runs WHERE id = ?", (new_run_id,)
            ).fetchone()

        return {
            "recovered": True,
            "idempotent": False,
            "previous_run_id": run_id,
            "checkpoint_id": checkpoint_id,
            "event_id": int(event_cursor.lastrowid),
            "transferred_command_ids": transferred_command_ids,
            "run": self._serialize_run(_row_dict(new_run) or {}),
        }

    def delete_run(self, run_id: str) -> bool:
        with self._connection(write=True) as conn:
            row = conn.execute("SELECT status FROM task_runs WHERE id = ?", (run_id,)).fetchone()
            if row is None:
                return False
            if row["status"] in ACTIVE_RUN_STATUSES:
                raise TaskStateError("An active run must be finished or cancelled before deletion")
            conn.execute("DELETE FROM task_runs WHERE id = ?", (run_id,))
        return True

    # -- Nodes ------------------------------------------------------------

    def create_node(
        self,
        run_id: str,
        node_key: str,
        title: str,
        *,
        node_id: str | None = None,
        parent_node_id: str | None = None,
        kind: str = "step",
        sequence: int | None = None,
        input_data: Mapping[str, Any] | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not node_key.strip() or not title.strip():
            raise ValueError("node_key and title cannot be empty")
        node_id = node_id or _new_id("tnode")
        now = self._now()
        with self._connection(write=True) as conn:
            run = self._require_row(conn, "task_runs", run_id, "run")
            if run["status"] in TERMINAL_RUN_STATUSES:
                raise TaskStateError(f"Cannot add a node to terminal run {run_id}")
            if parent_node_id:
                self._validate_node_run(conn, parent_node_id, run_id)
            if sequence is None:
                sequence = int(
                    conn.execute(
                        "SELECT COALESCE(MAX(sequence), 0) + 1 FROM task_nodes WHERE run_id = ?",
                        (run_id,),
                    ).fetchone()[0]
                )
            conn.execute(
                """
                INSERT INTO task_nodes(
                    id, run_id, task_id, node_key, parent_node_id, title, kind,
                    sequence, input_json, metadata_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    node_id,
                    run_id,
                    run["task_id"],
                    node_key,
                    parent_node_id,
                    title,
                    kind or "step",
                    int(sequence),
                    serialize_checkpoint_state(_json_object(input_data, field="input_data")),
                    serialize_checkpoint_state(_json_object(metadata, field="metadata")),
                    now,
                    now,
                ),
            )
            row = conn.execute("SELECT * FROM task_nodes WHERE id = ?", (node_id,)).fetchone()
        return self._serialize_node(_row_dict(row) or {})

    def get_node(self, node_id: str) -> dict[str, Any] | None:
        with self._connection() as conn:
            row = conn.execute("SELECT * FROM task_nodes WHERE id = ?", (node_id,)).fetchone()
        return self._serialize_node(_row_dict(row)) if row else None

    def list_nodes(self, run_id: str, *, parent_node_id: str | None | object = _UNSET) -> list[dict[str, Any]]:
        params: list[Any] = [run_id]
        sql = "SELECT * FROM task_nodes WHERE run_id = ?"
        if parent_node_id is not _UNSET:
            if parent_node_id is None:
                sql += " AND parent_node_id IS NULL"
            else:
                sql += " AND parent_node_id = ?"
                params.append(parent_node_id)
        sql += " ORDER BY sequence, created_at"
        with self._connection() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [self._serialize_node(_row_dict(row) or {}) for row in rows]

    def transition_node(
        self,
        node_id: str,
        status: str,
        *,
        output: Mapping[str, Any] | None = None,
        error: Mapping[str, Any] | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        if status not in NODE_STATUSES:
            raise ValueError(f"Unknown node status: {status}")
        now = self._now()
        with self._connection(write=True) as conn:
            row = self._require_row(conn, "task_nodes", node_id, "node")
            self._assert_transition("node", node_id, row["status"], status, NODE_TRANSITIONS)
            if status == "running":
                run = self._require_row(conn, "task_runs", row["run_id"], "run")
                if run["status"] != "running":
                    raise TaskStateError(f"Cannot start a node while run {run['id']} is {run['status']}")
            merged_metadata = deserialize_checkpoint_state(row["metadata_json"]) or {}
            if metadata is not None:
                merged_metadata.update(_json_object(metadata, field="metadata"))
            values: dict[str, Any] = {
                "status": status,
                "metadata_json": serialize_checkpoint_state(merged_metadata),
                "updated_at": now,
            }
            if output is not None:
                values["output_json"] = serialize_checkpoint_state(_json_object(output, field="output"))
            if error is not None:
                values["error_json"] = serialize_checkpoint_state(_json_object(error, field="error"))
            if status == "running":
                values["started_at"] = row["started_at"] or now
            if status in TERMINAL_NODE_STATUSES:
                values["finished_at"] = now
            assignments = ", ".join(f"{key} = ?" for key in values)
            conn.execute(
                f"UPDATE task_nodes SET {assignments} WHERE id = ?",  # noqa: S608 - fixed column names
                [*values.values(), node_id],
            )
            if status == "running":
                conn.execute(
                    "UPDATE task_runs SET current_node_id = ?, updated_at = ? WHERE id = ?",
                    (node_id, now, row["run_id"]),
                )
            elif status in TERMINAL_NODE_STATUSES:
                conn.execute(
                    """
                    UPDATE task_runs SET current_node_id = '', updated_at = ?
                    WHERE id = ? AND current_node_id = ?
                    """,
                    (now, row["run_id"], node_id),
                )
            updated = conn.execute("SELECT * FROM task_nodes WHERE id = ?", (node_id,)).fetchone()
        return self._serialize_node(_row_dict(updated) or {})

    def start_node(self, node_id: str, *, metadata: Mapping[str, Any] | None = None) -> dict[str, Any]:
        return self.transition_node(node_id, "running", metadata=metadata)

    def finish_node(
        self,
        node_id: str,
        *,
        output: Mapping[str, Any] | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        return self.transition_node(node_id, "completed", output=output, metadata=metadata)

    def fail_node(
        self,
        node_id: str,
        error: Mapping[str, Any],
        *,
        metadata: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        return self.transition_node(node_id, "failed", error=error, metadata=metadata)

    def skip_node(
        self, node_id: str, *, metadata: Mapping[str, Any] | None = None
    ) -> dict[str, Any]:
        return self.transition_node(node_id, "skipped", metadata=metadata)

    def update_node_metadata(
        self, node_id: str, metadata: Mapping[str, Any], *, merge: bool = True
    ) -> dict[str, Any]:
        with self._connection(write=True) as conn:
            row = self._require_row(conn, "task_nodes", node_id, "node")
            value = deserialize_checkpoint_state(row["metadata_json"]) or {} if merge else {}
            value.update(_json_object(metadata, field="metadata"))
            conn.execute(
                "UPDATE task_nodes SET metadata_json = ?, updated_at = ? WHERE id = ?",
                (serialize_checkpoint_state(value), self._now(), node_id),
            )
            updated = conn.execute("SELECT * FROM task_nodes WHERE id = ?", (node_id,)).fetchone()
        return self._serialize_node(_row_dict(updated) or {})

    def update_node_definition(
        self,
        node_id: str,
        *,
        title: str | None = None,
        kind: str | None = None,
        sequence: int | None = None,
    ) -> dict[str, Any]:
        """Update display-only node definition fields without changing state."""

        values: dict[str, Any] = {"updated_at": self._now()}
        if title is not None:
            if not title.strip():
                raise ValueError("title cannot be empty")
            values["title"] = title.strip()
        if kind is not None:
            values["kind"] = kind.strip() or "step"
        if sequence is not None:
            values["sequence"] = int(sequence)
        with self._connection(write=True) as conn:
            self._require_row(conn, "task_nodes", node_id, "node")
            assignments = ", ".join(f"{key} = ?" for key in values)
            conn.execute(
                f"UPDATE task_nodes SET {assignments} WHERE id = ?",  # noqa: S608 - fixed column names
                [*values.values(), node_id],
            )
            updated = conn.execute("SELECT * FROM task_nodes WHERE id = ?", (node_id,)).fetchone()
        return self._serialize_node(_row_dict(updated) or {})

    def delete_node(self, node_id: str) -> bool:
        with self._connection(write=True) as conn:
            row = conn.execute("SELECT status FROM task_nodes WHERE id = ?", (node_id,)).fetchone()
            if row is None:
                return False
            if row["status"] == "running":
                raise TaskStateError("A running node cannot be deleted")
            conn.execute("DELETE FROM task_nodes WHERE id = ?", (node_id,))
        return True

    # -- Checkpoints ------------------------------------------------------

    def create_checkpoint(
        self,
        run_id: str,
        state: Any,
        *,
        checkpoint_id: str | None = None,
        node_id: str | None = None,
        reason: str = "",
        metadata: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        checkpoint_id = checkpoint_id or _new_id("tcp")
        state_json = serialize_checkpoint_state(state)
        now = self._now()
        with self._connection(write=True) as conn:
            run = self._require_row(conn, "task_runs", run_id, "run")
            if node_id:
                self._validate_node_run(conn, node_id, run_id)
            sequence = int(
                conn.execute(
                    "SELECT COALESCE(MAX(sequence), 0) + 1 FROM task_checkpoints WHERE run_id = ?",
                    (run_id,),
                ).fetchone()[0]
            )
            conn.execute(
                """
                INSERT INTO task_checkpoints(
                    id, task_id, run_id, node_id, sequence, reason, state_json,
                    metadata_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    checkpoint_id,
                    run["task_id"],
                    run_id,
                    node_id,
                    sequence,
                    reason,
                    state_json,
                    serialize_checkpoint_state(_json_object(metadata, field="metadata")),
                    now,
                ),
            )
            row = conn.execute(
                "SELECT * FROM task_checkpoints WHERE id = ?", (checkpoint_id,)
            ).fetchone()
        return self._serialize_checkpoint(_row_dict(row) or {}, include_state=True)

    def get_checkpoint(self, checkpoint_id: str, *, include_state: bool = True) -> dict[str, Any] | None:
        with self._connection() as conn:
            row = conn.execute(
                "SELECT * FROM task_checkpoints WHERE id = ?", (checkpoint_id,)
            ).fetchone()
        return self._serialize_checkpoint(_row_dict(row), include_state=include_state) if row else None

    def latest_checkpoint(self, run_id: str, *, include_state: bool = True) -> dict[str, Any] | None:
        with self._connection() as conn:
            row = conn.execute(
                "SELECT * FROM task_checkpoints WHERE run_id = ? ORDER BY sequence DESC LIMIT 1",
                (run_id,),
            ).fetchone()
        return self._serialize_checkpoint(_row_dict(row), include_state=include_state) if row else None

    def list_checkpoints(
        self,
        *,
        run_id: str | None = None,
        task_id: str | None = None,
        include_state: bool = False,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if run_id is not None:
            clauses.append("run_id = ?")
            params.append(run_id)
        if task_id is not None:
            clauses.append("task_id = ?")
            params.append(task_id)
        sql = "SELECT * FROM task_checkpoints"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY created_at DESC, sequence DESC LIMIT ?"
        params.append(max(1, int(limit)))
        with self._connection() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [
            self._serialize_checkpoint(_row_dict(row) or {}, include_state=include_state)
            for row in rows
        ]

    def restore_checkpoint(
        self,
        checkpoint_id: str,
        *,
        restore_metadata: Mapping[str, Any] | None = None,
        mark_restored: bool = True,
    ) -> dict[str, Any]:
        with self._connection(write=mark_restored) as conn:
            row = self._require_row(conn, "task_checkpoints", checkpoint_id, "checkpoint")
            if mark_restored:
                now = self._now()
                conn.execute(
                    """
                    UPDATE task_checkpoints
                    SET restored_at = ?, restore_count = restore_count + 1,
                        last_restore_metadata_json = ?
                    WHERE id = ?
                    """,
                    (
                        now,
                        serialize_checkpoint_state(
                            _json_object(restore_metadata, field="restore_metadata")
                        ),
                        checkpoint_id,
                    ),
                )
                row = conn.execute(
                    "SELECT * FROM task_checkpoints WHERE id = ?", (checkpoint_id,)
                ).fetchone()
        return self._serialize_checkpoint(_row_dict(row) or {}, include_state=True)

    def delete_checkpoint(self, checkpoint_id: str) -> bool:
        with self._connection(write=True) as conn:
            cursor = conn.execute("DELETE FROM task_checkpoints WHERE id = ?", (checkpoint_id,))
        return cursor.rowcount > 0

    # -- Goal specifications and verification reports --------------------

    def save_goal_spec(
        self,
        task_id: str,
        run_id: str,
        spec: Mapping[str, Any],
        *,
        public_summary: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Persist one immutable GoalSpec version.

        GoalSpec identity is task-scoped.  Saving the same immutable version
        from a resumed run attaches that run to the existing record; it never
        clones or rewrites the contract authorised for earlier tool calls.
        """

        payload = _json_object(spec, field="spec")
        # New callers persist the strict domain model without storage-envelope
        # fields.  Validate its task identity and canonical seal at the boundary;
        # legacy pre-1.0 fixtures remain readable through the compatibility path.
        typed_goal = "goal_id" in payload
        if typed_goal:
            from app.services.goal_spec_service import ensure_goal_spec

            validated = ensure_goal_spec(payload)
            if validated.task_id != task_id:
                raise TaskStateError(
                    f"GoalSpec belongs to task {validated.task_id}, not {task_id}"
                )
            payload = validated.model_dump(mode="json")
        try:
            version = int(payload.get("version") or 0)
        except (TypeError, ValueError) as exc:
            raise ValueError("GoalSpec version must be a positive integer") from exc
        if version <= 0:
            raise ValueError("GoalSpec version must be a positive integer")
        schema_version = str(payload.get("schema_version") or "").strip()
        if not schema_version:
            raise ValueError("GoalSpec schema_version cannot be empty")
        status = str(payload.get("status") or "confirmed").strip()
        canonical = serialize_checkpoint_state(payload)
        calculated_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        spec_hash = str(payload.get("spec_hash") or calculated_hash).strip()
        # GoalSpec 1.0 deliberately separates the stable goal lineage
        # (``goal_id``) from a persisted version record.  Older callers supplied
        # an explicit row ``id``; newer typed GoalSpecs do not.  Generate a
        # deterministic task-scoped row id so checkpoint replay and cross-run
        # resume both reference the very same immutable version record.
        spec_id = str(payload.get("id") or "").strip()
        if not spec_id:
            goal_id = str(payload.get("goal_id") or "").strip()
            if not goal_id:
                raise ValueError("GoalSpec goal_id cannot be empty")
            digest = hashlib.sha256(
                f"{task_id}:{goal_id}:{version}:{spec_hash}".encode("utf-8")
            ).hexdigest()[:32]
            spec_id = f"tgs_{digest}"
        supersedes_id = str(payload.get("supersedes_id") or "").strip()
        supersedes_ref = payload.get("supersedes")
        summary = _json_object(public_summary, field="public_summary")
        now = self._now()

        with self._connection(write=True) as conn:
            run = self._require_row(conn, "task_runs", run_id, "run")
            if run["task_id"] != task_id:
                raise TaskStateError(f"Run {run_id} does not belong to task {task_id}")
            existing = conn.execute(
                "SELECT * FROM task_goal_specs WHERE task_id = ? AND version = ?",
                (task_id, version),
            ).fetchone()
            if existing is not None:
                if existing["id"] != spec_id or existing["spec_hash"] != spec_hash:
                    raise TaskStateError(
                        f"GoalSpec version {version} for task {task_id} already has different content"
                    )
                conn.execute(
                    """
                    INSERT OR IGNORE INTO task_run_goal_specs(run_id, goal_spec_id, attached_at)
                    VALUES (?, ?, ?)
                    """,
                    (run_id, existing["id"], now),
                )
                return self._serialize_goal_spec(_row_dict(existing) or {})
            if not supersedes_id and isinstance(supersedes_ref, Mapping):
                try:
                    superseded_version = int(supersedes_ref.get("version") or 0)
                except (TypeError, ValueError) as exc:
                    raise ValueError("GoalSpec supersedes.version must be a positive integer") from exc
                if superseded_version <= 0:
                    raise ValueError("GoalSpec supersedes.version must be a positive integer")
                superseded = conn.execute(
                    "SELECT * FROM task_goal_specs WHERE task_id = ? AND version = ?",
                    (task_id, superseded_version),
                ).fetchone()
                if superseded is None:
                    raise TaskStateError(
                        "A GoalSpec revision must supersede a version persisted for the same task"
                    )
                expected_hash = str(supersedes_ref.get("spec_hash") or "").strip()
                if expected_hash and superseded["spec_hash"] != expected_hash:
                    raise TaskStateError("GoalSpec supersedes hash does not match the persisted version")
                supersedes_id = str(superseded["id"])
            if supersedes_id:
                superseded = self._require_row(
                    conn, "task_goal_specs", supersedes_id, "goal_spec"
                )
                if superseded["task_id"] != task_id:
                    raise TaskStateError("A GoalSpec can only supersede a version from the same task")
            conn.execute(
                """
                INSERT INTO task_goal_specs(
                    id, task_id, run_id, version, schema_version, status,
                    spec_hash, supersedes_id, spec_json, public_summary_json,
                    created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    spec_id,
                    task_id,
                    run_id,
                    version,
                    schema_version,
                    status,
                    spec_hash,
                    supersedes_id,
                    canonical,
                    serialize_checkpoint_state(summary),
                    now,
                ),
            )
            row = conn.execute(
                "SELECT * FROM task_goal_specs WHERE id = ?", (spec_id,)
            ).fetchone()
            conn.execute(
                """
                INSERT INTO task_run_goal_specs(run_id, goal_spec_id, attached_at)
                VALUES (?, ?, ?)
                """,
                (run_id, spec_id, now),
            )
        return self._serialize_goal_spec(_row_dict(row) or {})

    def get_goal_spec(self, goal_spec_id: str) -> dict[str, Any] | None:
        with self._connection() as conn:
            row = conn.execute(
                "SELECT * FROM task_goal_specs WHERE id = ?", (goal_spec_id,)
            ).fetchone()
        return self._serialize_goal_spec(_row_dict(row)) if row else None

    def list_goal_specs(
        self,
        *,
        task_id: str | None = None,
        run_id: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if task_id is not None:
            clauses.append("g.task_id = ?")
            params.append(task_id)
        if run_id is not None:
            clauses.append("a.run_id = ?")
            params.append(run_id)
        sql = "SELECT DISTINCT g.* FROM task_goal_specs AS g"
        if run_id is not None:
            sql += " JOIN task_run_goal_specs AS a ON a.goal_spec_id = g.id"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY g.version DESC, g.created_at DESC LIMIT ?"
        params.append(max(1, int(limit)))
        with self._connection() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [self._serialize_goal_spec(_row_dict(row) or {}) for row in rows]

    def latest_goal_spec(
        self, *, task_id: str | None = None, run_id: str | None = None
    ) -> dict[str, Any] | None:
        items = self.list_goal_specs(task_id=task_id, run_id=run_id, limit=1)
        return items[0] if items else None

    def save_verification_report(
        self,
        task_id: str,
        run_id: str,
        goal_spec_id: str,
        report: Mapping[str, Any],
        *,
        public_report: Mapping[str, Any] | None = None,
        verifier_model_id: str = "",
        candidate_sha256: str,
        evidence_sha256: str = "",
        intake_generation: int | None = None,
        repaired_from_id: str = "",
        report_id: str = "",
        attempt: int | None = None,
        status: str = "",
        started_at: str = "",
        finished_at: str = "",
    ) -> dict[str, Any]:
        """Persist a public-safe verification report without model prompts or CoT.

        ``report`` is the strict domain report.  Record identity, attempt and
        timestamps are storage-envelope fields and therefore accepted
        separately.  Legacy callers that embedded them in ``report`` remain
        compatible while the runtime can persist an untouched typed model.
        """

        payload = _json_object(report, field="report")
        report_id = str(report_id or payload.get("id") or "").strip()
        try:
            requested_attempt = int(
                attempt if attempt is not None else payload.get("attempt") or 0
            )
        except (TypeError, ValueError) as exc:
            raise ValueError("Verification attempt must be a non-negative integer") from exc
        if requested_attempt < 0:
            raise ValueError("Verification attempt must be a non-negative integer")
        mode = str(payload.get("mode") or "rules_only").strip()
        status = str(status or payload.get("verdict") or payload.get("status") or "inconclusive").strip()
        if len(candidate_sha256) != 64 or any(
            character not in "0123456789abcdef" for character in candidate_sha256.lower()
        ):
            raise ValueError("candidate_sha256 must be a hexadecimal SHA-256 digest")
        evidence_sha256 = str(evidence_sha256 or "").lower()
        if not evidence_sha256:
            evidence_sha256 = hashlib.sha256(b"{}").hexdigest()
        if len(evidence_sha256) != 64 or any(
            character not in "0123456789abcdef" for character in evidence_sha256
        ):
            raise ValueError("evidence_sha256 must be a hexadecimal SHA-256 digest")
        now = self._now()
        started_at = str(started_at or payload.get("started_at") or now)
        finished_at = str(finished_at or payload.get("finished_at") or now)
        public_payload = _json_object(public_report, field="public_report")

        with self._connection(write=True) as conn:
            run = self._require_row(conn, "task_runs", run_id, "run")
            if run["task_id"] != task_id:
                raise TaskStateError(f"Run {run_id} does not belong to task {task_id}")
            verification_generation = int(
                run["applied_generation"]
                if intake_generation is None
                else intake_generation
            )
            if verification_generation < 0:
                raise ValueError("Verification intake_generation cannot be negative")
            if verification_generation != int(run["applied_generation"] or 0):
                raise TaskStateError(
                    "Verification must bind to the run's applied input generation"
                )
            goal = self._require_row(
                conn, "task_goal_specs", goal_spec_id, "goal_spec"
            )
            if goal["task_id"] != task_id:
                raise TaskStateError("Verification GoalSpec does not belong to this task")
            attached = conn.execute(
                """
                SELECT 1 FROM task_run_goal_specs
                WHERE run_id = ? AND goal_spec_id = ?
                """,
                (run_id, goal_spec_id),
            ).fetchone()
            if attached is None:
                raise TaskStateError("Verification GoalSpec is not attached to this run")
            if requested_attempt == 0:
                same_candidate = conn.execute(
                    """
                    SELECT * FROM task_verifications
                    WHERE run_id = ? AND goal_spec_id = ?
                      AND candidate_sha256 = ? AND evidence_sha256 = ?
                      AND mode = ? AND verifier_model_id = ?
                      AND intake_generation = ?
                    ORDER BY attempt DESC LIMIT 1
                    """,
                    (
                        run_id,
                        goal_spec_id,
                        candidate_sha256.lower(),
                        evidence_sha256,
                        mode,
                        verifier_model_id,
                        verification_generation,
                    ),
                ).fetchone()
                if same_candidate is not None:
                    return self._serialize_verification(_row_dict(same_candidate) or {})
                requested_attempt = int(
                    conn.execute(
                        """
                        SELECT COALESCE(MAX(attempt), 0) + 1
                        FROM task_verifications WHERE run_id = ? AND goal_spec_id = ?
                        """,
                        (run_id, goal_spec_id),
                    ).fetchone()[0]
                )
            if not report_id:
                digest = hashlib.sha256(
                    f"{run_id}:{goal_spec_id}:{requested_attempt}:{candidate_sha256.lower()}:{evidence_sha256}:{mode}:{verifier_model_id}".encode(
                        "utf-8"
                    )
                ).hexdigest()[:32]
                report_id = f"tvr_{digest}"
            if repaired_from_id:
                repaired_from = self._require_row(
                    conn, "task_verifications", repaired_from_id, "verification"
                )
                if (
                    repaired_from["task_id"] != task_id
                    or repaired_from["goal_spec_id"] != goal_spec_id
                    or int(repaired_from["attempt"]) >= requested_attempt
                ):
                    raise TaskStateError(
                        "repaired_from_id must reference an earlier verification for the same GoalSpec"
                    )
            existing = conn.execute(
                """
                SELECT * FROM task_verifications
                WHERE run_id = ? AND goal_spec_id = ? AND attempt = ?
                """,
                (run_id, goal_spec_id, requested_attempt),
            ).fetchone()
            if existing is not None:
                if (
                    existing["id"] != report_id
                    or existing["candidate_sha256"] != candidate_sha256.lower()
                    or existing["evidence_sha256"] != evidence_sha256
                    or existing["mode"] != mode
                    or existing["verifier_model_id"] != verifier_model_id
                    or int(existing["intake_generation"] or 0)
                    != verification_generation
                ):
                    raise TaskStateError(
                        f"Verification attempt {requested_attempt} for {goal_spec_id} already has different content"
                    )
                return self._serialize_verification(_row_dict(existing) or {})
            conn.execute(
                """
                INSERT INTO task_verifications(
                    id, task_id, run_id, goal_spec_id, attempt, mode, status,
                    report_json, public_report_json, verifier_model_id,
                    candidate_sha256, evidence_sha256, repaired_from_id,
                    intake_generation, started_at, finished_at,
                    created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    report_id,
                    task_id,
                    run_id,
                    goal_spec_id,
                    requested_attempt,
                    mode,
                    status,
                    serialize_checkpoint_state(payload),
                    serialize_checkpoint_state(public_payload),
                    verifier_model_id,
                    candidate_sha256.lower(),
                    evidence_sha256,
                    repaired_from_id,
                    verification_generation,
                    started_at,
                    finished_at,
                    now,
                ),
            )
            row = conn.execute(
                "SELECT * FROM task_verifications WHERE id = ?", (report_id,)
            ).fetchone()
        return self._serialize_verification(_row_dict(row) or {})

    def list_verifications(
        self,
        *,
        task_id: str | None = None,
        run_id: str | None = None,
        goal_spec_id: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        for column, value in (
            ("task_id", task_id),
            ("run_id", run_id),
            ("goal_spec_id", goal_spec_id),
        ):
            if value is not None:
                clauses.append(f"{column} = ?")
                params.append(value)
        sql = "SELECT * FROM task_verifications"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY attempt DESC, created_at DESC LIMIT ?"
        params.append(max(1, int(limit)))
        with self._connection() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [self._serialize_verification(_row_dict(row) or {}) for row in rows]

    @staticmethod
    def _candidate_artifact_file(row: sqlite3.Row) -> Path:
        """Resolve one registered Artifact through the shared storage guard."""

        from app.services import mcp_gateway as mcp_module

        relative_path = str(row["relative_path"] or "").strip()
        if not relative_path:
            legacy = Path(str(row["path"] or "")).resolve(strict=True)
            root = mcp_module.ARTIFACT_DIR.resolve(strict=True)
            relative_path = legacy.relative_to(root).as_posix()
        return mcp_module.resolve_artifact_path(relative_path)

    def _validate_candidate_artifacts(
        self,
        conn: sqlite3.Connection,
        *,
        task_id: str,
        run_id: str,
        artifacts: Sequence[Mapping[str, Any]],
        expected_status: str,
        verification_id: str = "",
    ) -> list[sqlite3.Row]:
        """Bind verified Artifact metadata to registry rows and actual bytes."""

        if not artifacts:
            return []
        selected_ids = [str(item.get("id") or "").strip() for item in artifacts]
        placeholders = ",".join("?" for _ in selected_ids)
        rows = conn.execute(
            f"SELECT * FROM artifacts WHERE id IN ({placeholders})",  # noqa: S608
            selected_ids,
        ).fetchall()
        by_id = {str(row["id"]): row for row in rows}
        if set(by_id) != set(selected_ids):
            raise PublicationConflict("A verified artifact no longer exists")

        for artifact in artifacts:
            artifact_id = str(artifact.get("id") or "").strip()
            row = by_id[artifact_id]
            if (
                row["task_id"] != task_id
                or row["run_id"] != run_id
                or row["delivery_status"] != expected_status
            ):
                raise PublicationConflict(
                    "A verified artifact has invalid ownership or delivery state"
                )
            if verification_id and row["verification_id"] != verification_id:
                raise PublicationConflict(
                    "A published artifact refers to a different verification"
                )

            row_size = int(row["size"] or 0)
            row_hash = str(row["sha256"] or "").lower()
            expected_url = f"/api/artifacts/{artifact_id}/download"
            metadata_matches = (
                str(artifact.get("task_id") or "") == task_id
                and str(artifact.get("run_id") or "") == run_id
                and str(artifact.get("name") or "") == str(row["name"] or "")
                and _normalise_artifact_kind(artifact.get("kind"))
                == _normalise_artifact_kind(row["kind"])
                and str(artifact.get("mime_type") or "")
                == str(row["mime_type"] or "")
                and int(artifact.get("size") or 0) == row_size
                and int(artifact.get("size_bytes") or 0) == row_size
                and int(artifact.get("version") or 0) == int(row["version"] or 0)
                and str(artifact.get("sha256") or "").lower() == row_hash
                and str(artifact.get("download_url") or "") == expected_url
                and artifact.get("exists") is True
                and artifact.get("readable") is True
                and artifact.get("download_ready") is True
            )
            if not metadata_matches or len(row_hash) != 64:
                raise PublicationConflict(
                    "Verified artifact metadata no longer matches the candidate"
                )

            try:
                path = self._candidate_artifact_file(row)
                actual_size = path.stat().st_size
                digest = hashlib.sha256()
                with path.open("rb") as stream:
                    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                        digest.update(chunk)
                actual_hash = digest.hexdigest()
            except (OSError, RuntimeError, ValueError) as exc:
                raise PublicationConflict(
                    "Verified artifact file is unavailable or outside storage"
                ) from exc
            if actual_size != row_size or actual_hash != row_hash:
                raise PublicationConflict(
                    "Verified artifact bytes changed before publication"
                )
        return rows

    def commit_verified_publication(
        self,
        *,
        task_id: str,
        run_id: str,
        goal_spec_id: str,
        verification_id: str,
        candidate: Mapping[str, Any],
        expected_generation: int,
        answer_title: str,
        answer_data: Mapping[str, Any],
        done_title: str,
        done_content: str,
        done_data: Mapping[str, Any],
        result: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Atomically close intake and publish one independently verified result.

        This is the linearization point shared with runtime message/cancel
        intake.  Whichever transaction obtains SQLite's write lock first wins:
        an accepted input advances the generation and makes this commit stale;
        a successful publication closes intake before a later command can be
        accepted.  No answer, done event, task completion or artifact exposure
        is visible unless every write commits together.
        """

        if expected_generation < 0:
            raise ValueError("expected_generation cannot be negative")
        candidate_model = CandidateOutput.model_validate(candidate)
        candidate_payload = candidate_model.model_dump(mode="json")
        candidate_sha256 = canonical_json_hash(candidate_payload)
        answer = candidate_model.answer
        public_artifacts = [
            item.model_dump(mode="json") for item in candidate_model.artifacts
        ]
        selected_ids = [
            str(item.get("id") or "").strip()
            for item in public_artifacts
        ]
        if any(not item for item in selected_ids):
            raise ValueError("Every published artifact must have an id")
        if len(selected_ids) != len(set(selected_ids)):
            raise ValueError("A publication cannot contain duplicate artifact ids")
        answer_title_value = str(answer_title or "")
        answer_data_payload = _json_object(answer_data, field="answer_data")
        done_title_value = str(done_title or "")
        done_content_value = str(done_content or "")
        done_data_payload = _json_object(done_data, field="done_data")
        result_payload = _json_object(result, field="result")
        publication_hash = canonical_json_hash(
            _encode_state(
                {
                    "schema_version": 1,
                    "task_id": task_id,
                    "run_id": run_id,
                    "goal_spec_id": goal_spec_id,
                    "verification_id": verification_id,
                    "expected_generation": int(expected_generation),
                    "candidate": candidate_payload,
                    "answer_title": answer_title_value,
                    "answer_data": answer_data_payload,
                    "done_title": done_title_value,
                    "done_content": done_content_value,
                    "done_data": done_data_payload,
                    "result": result_payload,
                }
            )
        )
        authoritative_answer_data = dict(answer_data_payload)
        authoritative_answer_data.update(
            {
                "artifacts": public_artifacts,
                "verification_id": verification_id,
                "candidate_sha256": candidate_sha256,
                "delivery_state": "verified",
            }
        )
        authoritative_done_data = dict(done_data_payload)
        authoritative_done_data.update(
            {
                "verification_id": verification_id,
                "candidate_sha256": candidate_sha256,
                "delivery_state": "verified",
            }
        )
        authoritative_result = dict(result_payload)
        authoritative_result.update(
            {
                "summary": answer,
                "verification_id": verification_id,
                "candidate_sha256": candidate_sha256,
            }
        )
        now = self._now()
        with self._connection(write=True) as conn:
            run = self._require_row(conn, "task_runs", run_id, "run")
            if run["task_id"] != task_id:
                raise TaskStateError(f"Run {run_id} does not belong to task {task_id}")

            verification = self._require_row(
                conn, "task_verifications", verification_id, "verification"
            )
            if (
                verification["task_id"] != task_id
                or verification["run_id"] != run_id
                or verification["goal_spec_id"] != goal_spec_id
                or verification["candidate_sha256"] != candidate_sha256
                or int(verification["intake_generation"] or 0)
                != expected_generation
            ):
                raise PublicationConflict(
                    "Verification does not match the current candidate, goal or input generation"
                )
            try:
                authoritative_report = VerificationReport.model_validate(
                    deserialize_checkpoint_state(verification["report_json"]) or {}
                )
                public_report = VerificationReport.model_validate(
                    deserialize_checkpoint_state(verification["public_report_json"])
                    or {}
                )
            except (TypeError, ValueError) as exc:
                raise PublicationConflict(
                    "Verification report is not a valid typed publication verdict"
                ) from exc
            authoritative_payload = authoritative_report.model_dump(mode="json")
            public_payload = public_report.model_dump(mode="json")
            if (
                str(verification["status"] or "")
                != authoritative_report.verdict
                or str(verification["mode"] or "") != authoritative_report.mode
                or not authoritative_report.passed
                or authoritative_report.verdict not in {"passed", "rules_passed"}
                or public_payload != authoritative_payload
            ):
                raise PublicationConflict(
                    "Verification status, authoritative verdict and public verdict disagree"
                )

            task = conn.execute(
                "SELECT * FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
            if task is None:
                raise StateNotFoundError(f"task {task_id} was not found")

            published_verification_id = str(
                run["published_verification_id"] or ""
            )
            if (
                run["status"] == "completed"
                and str(run["intake_state"] or "") == "closed"
                and published_verification_id == verification_id
            ):
                if task["status"] != "completed":
                    raise PublicationConflict(
                        "Publication fence is closed but task completion is inconsistent"
                    )
                if str(run["publication_hash"] or "") != publication_hash:
                    raise PublicationConflict(
                        "Idempotent publication envelope differs from the original publication"
                    )
                active_node = conn.execute(
                    """
                    SELECT 1 FROM task_nodes
                    WHERE run_id = ? AND status IN ('pending', 'running')
                    LIMIT 1
                    """,
                    (run_id,),
                ).fetchone()
                active_command = conn.execute(
                    """
                    SELECT 1 FROM task_commands
                    WHERE task_id = ? AND (run_id = ? OR run_id IS NULL)
                      AND status IN ('queued', 'claimed')
                    LIMIT 1
                    """,
                    (task_id, run_id),
                ).fetchone()
                pending_artifact = conn.execute(
                    """
                    SELECT 1 FROM artifacts
                    WHERE task_id = ? AND run_id = ?
                      AND delivery_status = 'pending_verification'
                    LIMIT 1
                    """,
                    (task_id, run_id),
                ).fetchone()
                if (
                    active_node is not None
                    or active_command is not None
                    or pending_artifact is not None
                    or int(run["accepted_generation"] or 0)
                    != int(run["applied_generation"] or 0)
                ):
                    raise PublicationConflict(
                        "Published run contains unfinished terminal residue"
                    )
                self._validate_candidate_artifacts(
                    conn,
                    task_id=task_id,
                    run_id=run_id,
                    artifacts=public_artifacts,
                    expected_status="published",
                    verification_id=verification_id,
                )
                stored_artifacts = deserialize_checkpoint_state(
                    task["artifacts_json"]
                ) or []
                answer_event = conn.execute(
                    """
                    SELECT title, content, data_json FROM task_events
                    WHERE task_id = ? AND type = 'answer'
                    ORDER BY id DESC LIMIT 1
                    """,
                    (task_id,),
                ).fetchone()
                done_event = conn.execute(
                    """
                    SELECT title, content, data_json FROM task_events
                    WHERE task_id = ? AND type = 'done'
                    ORDER BY id DESC LIMIT 1
                    """,
                    (task_id,),
                ).fetchone()
                answer_event_data = (
                    deserialize_checkpoint_state(answer_event["data_json"]) or {}
                    if answer_event is not None
                    else {}
                )
                done_event_data = (
                    deserialize_checkpoint_state(done_event["data_json"]) or {}
                    if done_event is not None
                    else {}
                )
                if (
                    stored_artifacts != public_artifacts
                    or deserialize_checkpoint_state(task["result_json"])
                    != authoritative_result
                    or deserialize_checkpoint_state(run["result_json"])
                    != authoritative_result
                    or answer_event is None
                    or answer_event["title"] != answer_title_value
                    or answer_event["content"] != answer
                    or answer_event_data != authoritative_answer_data
                    or done_event is None
                    or done_event["title"] != done_title_value
                    or done_event["content"] != done_content_value
                    or done_event_data != authoritative_done_data
                ):
                    raise PublicationConflict(
                        "Idempotent publication envelope differs from stored output"
                    )
                return {
                    "published": True,
                    "idempotent": True,
                    "run": self._serialize_run(_row_dict(run) or {}),
                }
            if published_verification_id:
                raise PublicationConflict(
                    "Run was already published with a different verification"
                )
            if run["status"] != "running" or task["status"] != "running":
                raise PublicationConflict(
                    "Task and run must both be running at publication time"
                )
            if str(run["intake_state"] or "open") != "open":
                raise PublicationConflict("Runtime input is already closed")

            accepted = int(run["accepted_generation"] or 0)
            applied = int(run["applied_generation"] or 0)
            if not (
                accepted == applied == int(expected_generation)
            ):
                raise PublicationConflict(
                    "Verified candidate is stale because newer runtime input exists"
                )
            active_nodes = conn.execute(
                """
                SELECT id, status FROM task_nodes
                WHERE run_id = ? AND status IN ('pending', 'running')
                ORDER BY sequence, id
                """,
                (run_id,),
            ).fetchall()
            if active_nodes:
                raise PublicationConflict(
                    "Verified candidate cannot publish while execution nodes are unfinished"
                )
            pending_commands = conn.execute(
                """
                SELECT command_type FROM task_commands
                WHERE task_id = ? AND (run_id = ? OR run_id IS NULL)
                  AND status IN ('queued', 'claimed')
                ORDER BY intake_generation, created_at, id
                """,
                (task_id, run_id),
            ).fetchall()
            if pending_commands:
                raise PublicationConflict(
                    "Runtime input is pending and must be applied before publication",
                    pending_command_types=[
                        str(item["command_type"]) for item in pending_commands
                    ],
                )

            active_goal = conn.execute(
                """
                SELECT g.id, g.spec_hash, g.version
                FROM task_goal_specs AS g
                JOIN task_run_goal_specs AS a ON a.goal_spec_id = g.id
                WHERE a.run_id = ?
                ORDER BY g.version DESC, g.created_at DESC
                LIMIT 1
                """,
                (run_id,),
            ).fetchone()
            if active_goal is None or active_goal["id"] != goal_spec_id:
                raise PublicationConflict(
                    "Publication GoalSpec is not the active contract for this run"
                )
            run_metadata = deserialize_checkpoint_state(run["metadata_json"]) or {}
            metadata_goal_id = str(run_metadata.get("goal_spec_id") or "")
            if metadata_goal_id and metadata_goal_id != goal_spec_id:
                raise PublicationConflict(
                    "Run metadata refers to a different active GoalSpec"
                )

            self._validate_candidate_artifacts(
                conn,
                task_id=task_id,
                run_id=run_id,
                artifacts=public_artifacts,
                expected_status="pending_verification",
            )

            if selected_ids:
                placeholders = ",".join("?" for _ in selected_ids)
                conn.execute(
                    f"""
                    UPDATE artifacts
                    SET delivery_status = 'rejected', verification_id = ?, published_at = ''
                    WHERE task_id = ? AND run_id = ?
                      AND delivery_status = 'pending_verification'
                      AND id NOT IN ({placeholders})
                    """,  # noqa: S608 - placeholders are generated; values are bound
                    (verification_id, task_id, run_id, *selected_ids),
                )
                published = conn.execute(
                    f"""
                    UPDATE artifacts
                    SET delivery_status = 'published', verification_id = ?, published_at = ?
                    WHERE task_id = ? AND run_id = ?
                      AND delivery_status = 'pending_verification'
                      AND id IN ({placeholders})
                    """,  # noqa: S608 - placeholders are generated; values are bound
                    (verification_id, now, task_id, run_id, *selected_ids),
                )
                if published.rowcount != len(selected_ids):
                    raise PublicationConflict(
                        "Not every verified artifact was published atomically"
                    )
            else:
                conn.execute(
                    """
                    UPDATE artifacts
                    SET delivery_status = 'rejected', verification_id = ?, published_at = ''
                    WHERE task_id = ? AND run_id = ?
                      AND delivery_status = 'pending_verification'
                    """,
                    (verification_id, task_id, run_id),
                )

            event_ids: dict[str, int] = {}
            for event_type, title, content, data in (
                (
                    "candidate_verified",
                    "候选结果已验收",
                    str(public_report.public_reason or "最终验收通过。"),
                    {
                        "verification_id": verification_id,
                        "candidate_sha256": candidate_sha256,
                        "delivery_state": "verified",
                    },
                ),
                (
                    "answer",
                    answer_title_value,
                    answer,
                    authoritative_answer_data,
                ),
                (
                    "done",
                    done_title_value,
                    done_content_value,
                    authoritative_done_data,
                ),
            ):
                cursor = conn.execute(
                    """
                    INSERT INTO task_events(task_id, ts, type, title, content, data_json)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        task_id,
                        now,
                        event_type,
                        title,
                        content,
                        serialize_checkpoint_state(data),
                    ),
                )
                event_ids[event_type] = int(cursor.lastrowid)

            serialized_result = serialize_checkpoint_state(
                _json_object(authoritative_result, field="result")
            )
            serialized_artifacts = serialize_checkpoint_state(public_artifacts)
            task_update = conn.execute(
                """
                UPDATE tasks
                SET status = 'completed', result_json = ?, artifacts_json = ?,
                    updated_at = ?
                WHERE id = ? AND status = 'running'
                """,
                (serialized_result, serialized_artifacts, now, task_id),
            )
            if task_update.rowcount != 1:
                raise PublicationConflict("Task completion lost its publication CAS")
            run_update = conn.execute(
                """
                UPDATE task_runs
                SET status = 'completed', result_json = ?, current_node_id = '',
                    intake_state = 'closed', intake_closed_at = ?,
                    published_verification_id = ?, publication_hash = ?,
                    finished_at = ?, updated_at = ?
                WHERE id = ? AND status = 'running' AND intake_state = 'open'
                  AND accepted_generation = ? AND applied_generation = ?
                """,
                (
                    serialized_result,
                    now,
                    verification_id,
                    publication_hash,
                    now,
                    now,
                    run_id,
                    expected_generation,
                    expected_generation,
                ),
            )
            if run_update.rowcount != 1:
                raise PublicationConflict("Run completion lost its publication CAS")
            updated_run = conn.execute(
                "SELECT * FROM task_runs WHERE id = ?", (run_id,)
            ).fetchone()
        return {
            "published": True,
            "idempotent": False,
            "event_ids": event_ids,
            "run": self._serialize_run(_row_dict(updated_run) or {}),
        }

    def commit_clarification_completion(
        self,
        *,
        task_id: str,
        run_id: str,
        goal_spec_id: str,
        expected_generation: int,
        clarification: str,
        missing_information: Sequence[str],
        result: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Atomically publish a non-answer clarification and close the run.

        A clarification is a terminal response for the current task attempt,
        but it is not a verified formal answer.  It still shares the same
        runtime-input generation fence as formal publication so a last-moment
        user message can never be stranded behind a completed task.
        """

        if expected_generation < 0:
            raise ValueError("expected_generation cannot be negative")
        message = str(clarification or "").strip()
        if not message:
            raise ValueError("clarification cannot be empty")
        missing = [
            str(item).strip()
            for item in missing_information
            if str(item).strip()
        ]
        now = self._now()
        with self._connection(write=True) as conn:
            run = self._require_row(conn, "task_runs", run_id, "run")
            if run["task_id"] != task_id:
                raise TaskStateError(
                    f"Run {run_id} does not belong to task {task_id}"
                )
            task = conn.execute(
                "SELECT * FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
            if task is None:
                raise StateNotFoundError(f"task {task_id} was not found")

            run_metadata = deserialize_checkpoint_state(run["metadata_json"]) or {}
            if (
                run["status"] == "completed"
                and str(run["intake_state"] or "") == "closed"
                and task["status"] == "completed"
                and run_metadata.get("completion_kind") == "clarification"
                and run_metadata.get("completion_goal_spec_id") == goal_spec_id
            ):
                residue = conn.execute(
                    """
                    SELECT
                      EXISTS(SELECT 1 FROM task_nodes
                             WHERE run_id = ? AND status IN ('pending', 'running')) AS active_nodes,
                      EXISTS(SELECT 1 FROM task_commands
                             WHERE task_id = ? AND (run_id = ? OR run_id IS NULL)
                               AND status IN ('queued', 'claimed')) AS active_commands,
                      EXISTS(SELECT 1 FROM artifacts
                             WHERE task_id = ? AND run_id = ?
                               AND delivery_status = 'pending_verification') AS pending_artifacts
                    """,
                    (run_id, task_id, run_id, task_id, run_id),
                ).fetchone()
                if (
                    residue is None
                    or any(int(residue[key] or 0) for key in residue.keys())
                    or int(run["accepted_generation"] or 0)
                    != int(run["applied_generation"] or 0)
                ):
                    raise PublicationConflict(
                        "Clarification completion contains unfinished terminal residue"
                    )
                return {
                    "completed": True,
                    "idempotent": True,
                    "run": self._serialize_run(_row_dict(run) or {}),
                }
            if str(run["published_verification_id"] or ""):
                raise PublicationConflict(
                    "A formally published run cannot become a clarification"
                )
            if run["status"] != "running" or task["status"] != "running":
                raise PublicationConflict(
                    "Task and run must both be running at clarification time"
                )
            if str(run["intake_state"] or "open") != "open":
                raise PublicationConflict("Runtime input is already closed")

            accepted = int(run["accepted_generation"] or 0)
            applied = int(run["applied_generation"] or 0)
            if accepted != applied or applied != int(expected_generation):
                raise PublicationConflict(
                    "Clarification is stale because newer runtime input exists"
                )
            pending_commands = conn.execute(
                """
                SELECT command_type FROM task_commands
                WHERE task_id = ? AND run_id = ?
                  AND command_type IN ('message', 'cancel')
                  AND status IN ('queued', 'claimed')
                ORDER BY intake_generation, created_at, id
                """,
                (task_id, run_id),
            ).fetchall()
            if pending_commands:
                raise PublicationConflict(
                    "Runtime input is pending and must be applied before clarification",
                    pending_command_types=[
                        str(item["command_type"]) for item in pending_commands
                    ],
                )

            goal = self._require_row(
                conn, "task_goal_specs", goal_spec_id, "goal spec"
            )
            if (
                goal["task_id"] != task_id
                or goal["status"] != "needs_input"
            ):
                raise PublicationConflict(
                    "Clarification must reference this run's needs_input GoalSpec"
                )
            active_goal = conn.execute(
                """
                SELECT g.id
                FROM task_goal_specs AS g
                JOIN task_run_goal_specs AS a ON a.goal_spec_id = g.id
                WHERE a.run_id = ?
                ORDER BY g.version DESC, g.created_at DESC
                LIMIT 1
                """,
                (run_id,),
            ).fetchone()
            if active_goal is None or active_goal["id"] != goal_spec_id:
                raise PublicationConflict(
                    "Clarification GoalSpec is not the active contract for this run"
                )

            serialized_result = serialize_checkpoint_state(
                _json_object(result, field="result")
            )
            completed_node_output = serialize_checkpoint_state(
                {
                    "summary": "已确认需要补充必要参数",
                    "completion_kind": "clarification",
                }
            )
            skipped_node_output = serialize_checkpoint_state(
                {
                    "summary": "等待用户补充参数，本轮不继续执行",
                    "completion_kind": "clarification",
                }
            )
            conn.execute(
                """
                UPDATE task_nodes
                SET status = 'completed', output_json = ?, finished_at = ?, updated_at = ?
                WHERE run_id = ? AND status = 'running'
                """,
                (completed_node_output, now, now, run_id),
            )
            conn.execute(
                """
                UPDATE task_nodes
                SET status = 'skipped', output_json = ?, finished_at = ?, updated_at = ?
                WHERE run_id = ? AND status = 'pending'
                """,
                (skipped_node_output, now, now, run_id),
            )
            conn.execute(
                """
                UPDATE artifacts
                SET delivery_status = 'rejected', verification_id = '', published_at = ''
                WHERE task_id = ? AND run_id = ?
                  AND delivery_status = 'pending_verification'
                """,
                (task_id, run_id),
            )
            stale_command_result = serialize_checkpoint_state(
                {
                    "cancelled": True,
                    "reason": "run_completed_with_clarification",
                }
            )
            conn.execute(
                """
                UPDATE task_commands
                SET status = 'cancelled', result_json = ?, completed_at = ?, updated_at = ?
                WHERE task_id = ? AND (run_id = ? OR run_id IS NULL)
                  AND status IN ('queued', 'claimed')
                """,
                (stale_command_result, now, now, task_id, run_id),
            )

            event_ids: dict[str, int] = {}
            for event_type, title, content, data in (
                (
                    "clarification",
                    "需要补充信息",
                    message,
                    {
                        "missing_information": missing,
                        "goal_spec_id": goal_spec_id,
                        "goal_spec_version": int(goal["version"]),
                        "delivery_state": "awaiting_input",
                        "formal_answer": False,
                    },
                ),
                (
                    "done",
                    "等待补充",
                    "收到补充信息后会继续当前任务。",
                    {
                        "goal_spec_id": goal_spec_id,
                        "goal_spec_version": int(goal["version"]),
                        "delivery_state": "awaiting_input",
                        "formal_answer": False,
                    },
                ),
            ):
                cursor = conn.execute(
                    """
                    INSERT INTO task_events(task_id, ts, type, title, content, data_json)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        task_id,
                        now,
                        event_type,
                        title,
                        content,
                        serialize_checkpoint_state(data),
                    ),
                )
                event_ids[event_type] = int(cursor.lastrowid)

            task_update = conn.execute(
                """
                UPDATE tasks
                SET status = 'completed', result_json = ?, artifacts_json = '[]',
                    updated_at = ?
                WHERE id = ? AND status = 'running'
                """,
                (serialized_result, now, task_id),
            )
            if task_update.rowcount != 1:
                raise PublicationConflict("Task clarification lost its completion CAS")
            run_metadata.update(
                {
                    "completion_kind": "clarification",
                    "completion_goal_spec_id": goal_spec_id,
                }
            )
            run_update = conn.execute(
                """
                UPDATE task_runs
                SET status = 'completed', result_json = ?, metadata_json = ?,
                    current_node_id = '', intake_state = 'closed', intake_closed_at = ?,
                    finished_at = ?, updated_at = ?
                WHERE id = ? AND status = 'running' AND intake_state = 'open'
                  AND accepted_generation = ? AND applied_generation = ?
                """,
                (
                    serialized_result,
                    serialize_checkpoint_state(run_metadata),
                    now,
                    now,
                    now,
                    run_id,
                    expected_generation,
                    expected_generation,
                ),
            )
            if run_update.rowcount != 1:
                raise PublicationConflict("Run clarification lost its completion CAS")
            updated_run = conn.execute(
                "SELECT * FROM task_runs WHERE id = ?", (run_id,)
            ).fetchone()
        return {
            "completed": True,
            "idempotent": False,
            "event_ids": event_ids,
            "run": self._serialize_run(_row_dict(updated_run) or {}),
        }

    def commit_platform_command_completion(
        self,
        *,
        task_id: str,
        run_id: str,
        command_kind: str,
        expected_generation: int,
        answer_title: str,
        answer: str,
        answer_data: Mapping[str, Any] | None = None,
        done_title: str = "已完成",
        done_content: str = "平台指令已处理。",
        done_data: Mapping[str, Any] | None = None,
        result: Mapping[str, Any] | None = None,
        transaction_effect: Callable[
            [sqlite3.Connection], Mapping[str, Any] | None
        ]
        | None = None,
        operation_key: str = "",
    ) -> dict[str, Any]:
        """Atomically apply and publish one deterministic platform command.

        Memory and registry commands do not produce a model candidate, but
        they still share the runtime intake and terminal-state fence.  Their
        optional SQLite-only business effect, public events, Task projection
        and Run therefore commit together.  The effect is invoked only after
        every input-generation/CAS check and must not perform network I/O or
        open another database connection.
        """

        kind = str(command_kind or "").strip()
        content = str(answer or "").strip()
        if not kind:
            raise ValueError("command_kind cannot be empty")
        if not content and transaction_effect is None:
            raise ValueError("answer cannot be empty")
        normalized_operation_key = str(operation_key or "").strip()
        if transaction_effect is not None and not normalized_operation_key:
            raise ValueError("transactional platform effects require operation_key")
        if expected_generation < 0:
            raise ValueError("expected_generation cannot be negative")
        answer_title_value = str(answer_title or "")
        answer_payload = _json_object(answer_data, field="answer_data")
        done_title_value = str(done_title or "")
        done_content_value = str(done_content or "")
        done_payload = _json_object(done_data, field="done_data")
        result_payload = _json_object(result, field="result")
        extra_events: list[dict[str, Any]] = []
        static_completion_hash = ""
        if transaction_effect is None:
            result_payload.setdefault("summary", content)
            static_completion_hash = canonical_json_hash(
                {
                    "command_kind": kind,
                    "answer_title": answer_title_value,
                    "answer": content,
                    "answer_data": answer_payload,
                    "done_title": done_title_value,
                    "done_content": done_content_value,
                    "done_data": done_payload,
                    "result": result_payload,
                }
            )
        request_hash = (
            canonical_json_hash(
                {
                    "schema": "platform-command-effect-request/1.0",
                    "task_id": task_id,
                    "run_id": run_id,
                    "command_kind": kind,
                    "operation_key": normalized_operation_key,
                }
            )
            if transaction_effect is not None
            else ""
        )
        now = self._now()
        with self._connection(write=True) as conn:
            run = self._require_row(conn, "task_runs", run_id, "run")
            if run["task_id"] != task_id:
                raise TaskStateError(
                    f"Run {run_id} does not belong to task {task_id}"
                )
            task = conn.execute(
                "SELECT * FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
            if task is None:
                raise StateNotFoundError(f"task {task_id} was not found")
            metadata = deserialize_checkpoint_state(run["metadata_json"]) or {}

            same_completed_request = (
                metadata.get("platform_command_request_hash") == request_hash
                if transaction_effect is not None
                else metadata.get("platform_command_hash")
                == static_completion_hash
            )
            if (
                run["status"] == "completed"
                and task["status"] == "completed"
                and str(run["intake_state"] or "") == "closed"
                and metadata.get("completion_kind") == "platform_command"
                and metadata.get("platform_command_kind") == kind
                and same_completed_request
            ):
                self.assert_terminal_clean(task_id=task_id, run_id=run_id)
                return {
                    "completed": True,
                    "idempotent": True,
                    "run": self._serialize_run(_row_dict(run) or {}),
                }

            if str(run["published_verification_id"] or ""):
                raise PublicationConflict(
                    "A formally published run cannot become a platform command"
                )
            if run["status"] != "running" or task["status"] != "running":
                raise PublicationConflict(
                    "Task and run must both be running at platform-command completion"
                )
            if str(run["intake_state"] or "open") != "open":
                raise PublicationConflict("Runtime input is already closed")
            pending_runtime_commands = conn.execute(
                """
                SELECT command_type FROM task_commands
                WHERE task_id = ? AND (run_id = ? OR run_id IS NULL)
                  AND command_type IN ('message', 'cancel')
                  AND status IN ('queued', 'claimed')
                ORDER BY intake_generation, created_at, id
                """,
                (task_id, run_id),
            ).fetchall()
            if pending_runtime_commands:
                raise PublicationConflict(
                    "Runtime input is pending and must be applied before platform-command completion",
                    pending_command_types=[
                        str(item["command_type"])
                        for item in pending_runtime_commands
                    ],
                )
            accepted = int(run["accepted_generation"] or 0)
            applied = int(run["applied_generation"] or 0)
            if accepted != applied or applied != int(expected_generation):
                raise PublicationConflict(
                    "Platform command is stale because newer runtime input exists"
                )

            if transaction_effect is not None:
                effect_value = transaction_effect(conn)
                effect = (
                    _json_object(effect_value, field="transaction_effect result")
                    if effect_value is not None
                    else {}
                )
                if "answer_title" in effect:
                    answer_title_value = str(effect.get("answer_title") or "")
                if "answer" in effect:
                    content = str(effect.get("answer") or "").strip()
                if "answer_data" in effect:
                    answer_payload = _json_object(
                        effect.get("answer_data"), field="effect.answer_data"
                    )
                if "done_title" in effect:
                    done_title_value = str(effect.get("done_title") or "")
                if "done_content" in effect:
                    done_content_value = str(effect.get("done_content") or "")
                if "done_data" in effect:
                    done_payload = _json_object(
                        effect.get("done_data"), field="effect.done_data"
                    )
                if "result" in effect:
                    result_payload = _json_object(
                        effect.get("result"), field="effect.result"
                    )
                raw_events = effect.get("events", [])
                if not isinstance(raw_events, Sequence) or isinstance(
                    raw_events, (str, bytes)
                ):
                    raise TypeError("effect.events must be a sequence")
                for index, raw_event in enumerate(raw_events):
                    event = _json_object(
                        raw_event, field=f"effect.events[{index}]"
                    )
                    event_type = str(event.get("type") or "").strip()
                    event_title = str(event.get("title") or "").strip()
                    if not event_type or not event_title:
                        raise ValueError(
                            "effect events require non-empty type and title"
                        )
                    extra_events.append(
                        {
                            "type": event_type,
                            "title": event_title,
                            "content": str(event.get("content") or ""),
                            "data": _json_object(
                                event.get("data"), field="effect event data"
                            ),
                        }
                    )
            if not content:
                raise ValueError("answer cannot be empty")
            result_payload.setdefault("summary", content)
            completion_hash = canonical_json_hash(
                {
                    "command_kind": kind,
                    "answer_title": answer_title_value,
                    "answer": content,
                    "answer_data": answer_payload,
                    "done_title": done_title_value,
                    "done_content": done_content_value,
                    "done_data": done_payload,
                    "result": result_payload,
                }
            )

            completed_node_output = serialize_checkpoint_state(
                {
                    "summary": "平台指令已完成",
                    "completion_kind": "platform_command",
                    "command_kind": kind,
                }
            )
            skipped_node_output = serialize_checkpoint_state(
                {
                    "summary": "平台指令已直接完成，后续节点无需执行",
                    "completion_kind": "platform_command",
                    "command_kind": kind,
                }
            )
            conn.execute(
                """
                UPDATE task_nodes
                SET status = 'completed', output_json = ?, finished_at = ?, updated_at = ?
                WHERE run_id = ? AND status = 'running'
                """,
                (completed_node_output, now, now, run_id),
            )
            conn.execute(
                """
                UPDATE task_nodes
                SET status = 'skipped', output_json = ?, finished_at = ?, updated_at = ?
                WHERE run_id = ? AND status = 'pending'
                """,
                (skipped_node_output, now, now, run_id),
            )
            conn.execute(
                """
                UPDATE artifacts
                SET delivery_status = 'rejected', verification_id = '', published_at = ''
                WHERE task_id = ? AND run_id = ?
                  AND delivery_status = 'pending_verification'
                """,
                (task_id, run_id),
            )
            stale_command_result = serialize_checkpoint_state(
                {
                    "cancelled": True,
                    "reason": "run_completed_with_platform_command",
                }
            )
            conn.execute(
                """
                UPDATE task_commands
                SET status = 'cancelled', result_json = ?, completed_at = ?, updated_at = ?
                WHERE task_id = ? AND (run_id = ? OR run_id IS NULL)
                  AND status IN ('queued', 'claimed')
                """,
                (stale_command_result, now, now, task_id, run_id),
            )

            event_ids: dict[str, int] = {}
            event_envelopes = [
                (
                    item["type"],
                    item["title"],
                    item["content"],
                    item["data"],
                )
                for item in extra_events
            ]
            event_envelopes.extend(
                [
                    (
                        "answer",
                        answer_title_value or "平台指令已处理",
                        content,
                        answer_payload,
                    ),
                    (
                        "done",
                        done_title_value or "已完成",
                        done_content_value,
                        done_payload,
                    ),
                ]
            )
            for event_type, title, event_content, data in event_envelopes:
                cursor = conn.execute(
                    """
                    INSERT INTO task_events(task_id, ts, type, title, content, data_json)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        task_id,
                        now,
                        event_type,
                        title,
                        event_content,
                        serialize_checkpoint_state(data),
                    ),
                )
                event_ids[event_type] = int(cursor.lastrowid)

            serialized_result = serialize_checkpoint_state(result_payload)
            task_update = conn.execute(
                """
                UPDATE tasks
                SET status = 'completed', result_json = ?, artifacts_json = '[]',
                    updated_at = ?
                WHERE id = ? AND status = 'running'
                """,
                (serialized_result, now, task_id),
            )
            if task_update.rowcount != 1:
                raise PublicationConflict(
                    "Task completion lost its platform-command CAS"
                )
            metadata.update(
                {
                    "completion_kind": "platform_command",
                    "platform_command_kind": kind,
                    "platform_command_hash": completion_hash,
                    "platform_command_request_hash": request_hash,
                }
            )
            run_update = conn.execute(
                """
                UPDATE task_runs
                SET status = 'completed', result_json = ?, metadata_json = ?,
                    current_node_id = '', intake_state = 'closed', intake_closed_at = ?,
                    applied_generation = ?, finished_at = ?, updated_at = ?
                WHERE id = ? AND status = 'running' AND intake_state = 'open'
                  AND accepted_generation = ? AND applied_generation = ?
                """,
                (
                    serialized_result,
                    serialize_checkpoint_state(metadata),
                    now,
                    expected_generation,
                    now,
                    now,
                    run_id,
                    expected_generation,
                    expected_generation,
                ),
            )
            if run_update.rowcount != 1:
                raise PublicationConflict(
                    "Run completion lost its platform-command CAS"
                )
            updated_run = conn.execute(
                "SELECT * FROM task_runs WHERE id = ?", (run_id,)
            ).fetchone()
        return {
            "completed": True,
            "idempotent": False,
            "event_ids": event_ids,
            "run": self._serialize_run(_row_dict(updated_run) or {}),
        }

    def get_policy_approval_decision(
        self,
        task_id: str,
        approval_id: str,
    ) -> dict[str, Any] | None:
        """Return the durable decision proof for one exact policy request."""

        if not task_id.strip() or not approval_id.strip():
            raise ValueError("task_id and approval_id cannot be empty")
        with self._connection() as conn:
            task = conn.execute(
                "SELECT result_json FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
            if task is not None:
                result = deserialize_checkpoint_state(task["result_json"]) or {}
                decisions = result.get("policy_approval_decisions", {})
                if isinstance(decisions, Mapping):
                    proof = decisions.get(approval_id)
                    if isinstance(proof, Mapping):
                        return dict(proof)

            for row in conn.execute(
                "SELECT metadata_json FROM task_runs WHERE task_id = ? "
                "ORDER BY attempt DESC",
                (task_id,),
            ).fetchall():
                metadata = deserialize_checkpoint_state(row["metadata_json"]) or {}
                decisions = metadata.get("policy_approval_decisions", {})
                if not isinstance(decisions, Mapping):
                    continue
                proof = decisions.get(approval_id)
                if isinstance(proof, Mapping):
                    return dict(proof)

            has_events = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'task_events'"
            ).fetchone()
            if has_events is not None:
                for row in conn.execute(
                    "SELECT data_json FROM task_events "
                    "WHERE task_id = ? AND type = 'approval' ORDER BY id DESC",
                    (task_id,),
                ).fetchall():
                    data = deserialize_checkpoint_state(row["data_json"]) or {}
                    if (
                        isinstance(data, Mapping)
                        and str(data.get("approval_id") or "") == approval_id
                        and str(data.get("decision") or "")
                        in {"approved", "rejected"}
                    ):
                        return dict(data)
        return None

    def commit_policy_approval_request(
        self,
        *,
        task_id: str,
        run_id: str,
        approval_id: str,
        result: Mapping[str, Any],
        title: str,
        content: str,
        data: Mapping[str, Any],
        node_summary: str = "当前执行正在等待用户审批",
    ) -> dict[str, Any]:
        """Atomically expose a policy approval and pause its Task/Run.

        The public event must never be observable without both projections in
        ``waiting_approval``.  The active node remains resumable, but receives
        an inspectable waiting marker in the same transaction.
        """

        if not approval_id.strip():
            raise ValueError("approval_id cannot be empty")
        if not title.strip():
            raise ValueError("approval title cannot be empty")
        result_payload = _json_object(result, field="result")
        event_data = _json_object(data, field="data")
        result_payload["pending_action"] = "policy_approval"
        result_payload["policy_approval_id"] = approval_id
        event_data.update({"approval_id": approval_id, "run_id": run_id})
        now = self._now()

        with self._connection(write=True) as conn:
            run = self._require_row(conn, "task_runs", run_id, "run")
            if run["task_id"] != task_id:
                raise TaskStateError(
                    f"Run {run_id} does not belong to task {task_id}"
                )
            task = conn.execute(
                "SELECT * FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
            if task is None:
                raise StateNotFoundError(f"task {task_id} was not found")
            metadata = deserialize_checkpoint_state(run["metadata_json"]) or {}
            pending = metadata.get("pending_policy_approval")

            if task["status"] == "waiting_approval" and run["status"] == "waiting_approval":
                if not isinstance(pending, Mapping) or str(
                    pending.get("approval_id") or ""
                ) != approval_id:
                    raise PublicationConflict(
                        "Task/Run are waiting for a different policy approval"
                    )
                stored_result = deserialize_checkpoint_state(task["result_json"]) or {}
                event = conn.execute(
                    "SELECT id FROM task_events "
                    "WHERE task_id = ? AND type = 'approval_required' ORDER BY id DESC",
                    (task_id,),
                ).fetchone()
                if (
                    str(stored_result.get("policy_approval_id") or "")
                    != approval_id
                    or event is None
                ):
                    raise PublicationConflict(
                        "Policy approval waiting state is missing its durable projection"
                    )
                return {
                    "waiting": True,
                    "idempotent": True,
                    "event_id": int(event["id"]),
                    "run": self._serialize_run(_row_dict(run) or {}),
                }

            if task["status"] != "running" or run["status"] != "running":
                raise PublicationConflict(
                    "Task and run must both be running before policy approval"
                )
            if str(run["intake_state"] or "open") != "open":
                raise PublicationConflict("Runtime input is already closed")
            if str(run["published_verification_id"] or ""):
                raise PublicationConflict(
                    "A published run cannot request policy approval"
                )
            pending_runtime_commands = conn.execute(
                """
                SELECT command_type FROM task_commands
                WHERE task_id = ? AND (run_id = ? OR run_id IS NULL)
                  AND command_type IN ('message', 'cancel')
                  AND status IN ('queued', 'claimed')
                ORDER BY intake_generation, created_at, id
                """,
                (task_id, run_id),
            ).fetchall()
            if pending_runtime_commands:
                raise PublicationConflict(
                    "Runtime input supersedes the policy approval request",
                    pending_command_types=[
                        str(item["command_type"])
                        for item in pending_runtime_commands
                    ],
                )
            if int(run["accepted_generation"] or 0) != int(
                run["applied_generation"] or 0
            ):
                raise PublicationConflict(
                    "Policy approval is stale because newer input exists"
                )

            task_result = deserialize_checkpoint_state(task["result_json"]) or {}
            task_result.update(result_payload)
            request_proof = {
                "approval_id": approval_id,
                "event": str(result_payload.get("policy_event") or ""),
                "requested_at": now,
            }
            metadata["pending_policy_approval"] = request_proof

            for node in conn.execute(
                "SELECT id, output_json, metadata_json FROM task_nodes "
                "WHERE run_id = ? AND status = 'running'",
                (run_id,),
            ).fetchall():
                output = deserialize_checkpoint_state(node["output_json"]) or {}
                node_metadata = (
                    deserialize_checkpoint_state(node["metadata_json"]) or {}
                )
                output.update(
                    {
                        "summary": node_summary,
                        "approval_id": approval_id,
                        "approval_status": "waiting",
                    }
                )
                node_metadata["pending_policy_approval_id"] = approval_id
                conn.execute(
                    "UPDATE task_nodes SET output_json = ?, metadata_json = ?, "
                    "updated_at = ? WHERE id = ? AND status = 'running'",
                    (
                        serialize_checkpoint_state(output),
                        serialize_checkpoint_state(node_metadata),
                        now,
                        node["id"],
                    ),
                )

            cursor = conn.execute(
                """
                INSERT INTO task_events(task_id, ts, type, title, content, data_json)
                VALUES (?, ?, 'approval_required', ?, ?, ?)
                """,
                (
                    task_id,
                    now,
                    title,
                    content,
                    serialize_checkpoint_state(event_data),
                ),
            )
            task_update = conn.execute(
                "UPDATE tasks SET status = 'waiting_approval', result_json = ?, "
                "updated_at = ? WHERE id = ? AND status = 'running'",
                (serialize_checkpoint_state(task_result), now, task_id),
            )
            run_update = conn.execute(
                "UPDATE task_runs SET status = 'waiting_approval', result_json = ?, "
                "metadata_json = ?, updated_at = ? "
                "WHERE id = ? AND status = 'running' AND intake_state = 'open'",
                (
                    serialize_checkpoint_state(task_result),
                    serialize_checkpoint_state(metadata),
                    now,
                    run_id,
                ),
            )
            if task_update.rowcount != 1 or run_update.rowcount != 1:
                raise PublicationConflict(
                    "Policy approval request lost its Task/Run CAS"
                )
            updated_run = conn.execute(
                "SELECT * FROM task_runs WHERE id = ?", (run_id,)
            ).fetchone()
        return {
            "waiting": True,
            "idempotent": False,
            "event_id": int(cursor.lastrowid),
            "run": self._serialize_run(_row_dict(updated_run) or {}),
        }

    def commit_policy_approval_decision(
        self,
        *,
        task_id: str,
        run_id: str,
        approval_id: str,
        worker_id: str,
    ) -> dict[str, Any] | None:
        """Atomically consume one policy decision and resume its Task/Run."""

        if not approval_id.strip() or not worker_id.strip():
            raise ValueError("approval_id and worker_id cannot be empty")
        now = self._now()
        with self._connection(write=True) as conn:
            run = self._require_row(conn, "task_runs", run_id, "run")
            if run["task_id"] != task_id:
                raise TaskStateError(
                    f"Run {run_id} does not belong to task {task_id}"
                )
            task = conn.execute(
                "SELECT * FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
            if task is None:
                raise StateNotFoundError(f"task {task_id} was not found")
            metadata = deserialize_checkpoint_state(run["metadata_json"]) or {}
            decisions = metadata.get("policy_approval_decisions", {})
            if not isinstance(decisions, Mapping):
                raise PublicationConflict("Run policy approval proof is malformed")
            existing = decisions.get(approval_id)
            if isinstance(existing, Mapping):
                return {
                    "decided": True,
                    "idempotent": True,
                    "approved": bool(existing.get("approved")),
                    "decision": str(existing.get("decision") or ""),
                    "proof": dict(existing),
                    "run": self._serialize_run(_row_dict(run) or {}),
                }
            pending = metadata.get("pending_policy_approval")
            if (
                task["status"] != "waiting_approval"
                or run["status"] != "waiting_approval"
                or not isinstance(pending, Mapping)
                or str(pending.get("approval_id") or "") != approval_id
            ):
                raise PublicationConflict(
                    "Task/Run are not waiting for this policy approval"
                )

            command = conn.execute(
                """
                SELECT * FROM task_commands
                WHERE task_id = ? AND (run_id = ? OR run_id IS NULL)
                  AND command_type = 'approval'
                  AND status IN ('queued', 'claimed')
                ORDER BY created_at, id LIMIT 1
                """,
                (task_id, run_id),
            ).fetchone()
            if command is None:
                return None
            payload = deserialize_checkpoint_state(command["payload_json"]) or {}
            if not isinstance(payload.get("approved"), bool):
                raise TaskStateError("Policy approval command requires a boolean decision")
            command_approval_id = str(payload.get("approval_id") or "")
            if command_approval_id and command_approval_id != approval_id:
                raise PublicationConflict(
                    "Queued approval command belongs to another policy request"
                )
            approved = bool(payload["approved"])
            decision = "approved" if approved else "rejected"
            note = str(payload.get("note") or "").strip()
            proof = {
                "approval_id": approval_id,
                "approved": approved,
                "decision": decision,
                "command_id": str(command["id"]),
                "decided_at": now,
                "request": dict(pending),
            }
            proof["proof_hash"] = canonical_json_hash(proof)

            task_result = deserialize_checkpoint_state(task["result_json"]) or {}
            task_decisions = task_result.get("policy_approval_decisions", {})
            if not isinstance(task_decisions, Mapping):
                raise PublicationConflict("Task policy approval proof is malformed")
            task_decisions = dict(task_decisions)
            task_decisions[approval_id] = proof
            task_result["policy_approval_decisions"] = task_decisions
            task_result["policy_approval_decision"] = proof
            task_result.pop("pending_action", None)
            task_result.pop("policy_approval_id", None)
            task_result["summary"] = note or (
                "用户已批准策略要求的操作。"
                if approved
                else "用户已拒绝策略要求的操作。"
            )

            metadata = dict(metadata)
            run_decisions = dict(decisions)
            run_decisions[approval_id] = proof
            metadata["policy_approval_decisions"] = run_decisions
            metadata["last_policy_approval_decision"] = proof
            metadata.pop("pending_policy_approval", None)

            command_result = serialize_checkpoint_state(proof)
            command_update = conn.execute(
                """
                UPDATE task_commands
                SET status = 'completed', worker_id = ?,
                    claimed_at = CASE WHEN claimed_at = '' THEN ? ELSE claimed_at END,
                    result_json = ?, completed_at = ?, updated_at = ?
                WHERE id = ? AND status IN ('queued', 'claimed')
                """,
                (
                    worker_id,
                    now,
                    command_result,
                    now,
                    now,
                    command["id"],
                ),
            )
            if command_update.rowcount != 1:
                raise PublicationConflict("Policy approval command lost its CAS")
            conn.execute(
                """
                UPDATE task_commands
                SET status = 'cancelled', result_json = ?, completed_at = ?, updated_at = ?
                WHERE task_id = ? AND (run_id = ? OR run_id IS NULL)
                  AND command_type = 'approval' AND id != ?
                  AND status IN ('queued', 'claimed')
                """,
                (
                    serialize_checkpoint_state(
                        {
                            "cancelled": True,
                            "reason": "policy_approval_already_decided",
                            "approval_id": approval_id,
                            "authoritative_command_id": str(command["id"]),
                        }
                    ),
                    now,
                    now,
                    task_id,
                    run_id,
                    command["id"],
                ),
            )

            for node in conn.execute(
                "SELECT id, output_json, metadata_json FROM task_nodes "
                "WHERE run_id = ? AND status = 'running'",
                (run_id,),
            ).fetchall():
                output = deserialize_checkpoint_state(node["output_json"]) or {}
                node_metadata = (
                    deserialize_checkpoint_state(node["metadata_json"]) or {}
                )
                output.update(
                    {
                        "summary": task_result["summary"],
                        "approval_id": approval_id,
                        "approval_status": decision,
                    }
                )
                node_metadata.pop("pending_policy_approval_id", None)
                node_metadata["last_policy_approval_id"] = approval_id
                conn.execute(
                    "UPDATE task_nodes SET output_json = ?, metadata_json = ?, "
                    "updated_at = ? WHERE id = ? AND status = 'running'",
                    (
                        serialize_checkpoint_state(output),
                        serialize_checkpoint_state(node_metadata),
                        now,
                        node["id"],
                    ),
                )

            cursor = conn.execute(
                """
                INSERT INTO task_events(task_id, ts, type, title, content, data_json)
                VALUES (?, ?, 'approval', ?, ?, ?)
                """,
                (
                    task_id,
                    now,
                    "审批通过" if approved else "审批拒绝",
                    note
                    or (
                        "继续执行已审批的操作。"
                        if approved
                        else "已记录用户拒绝，受限操作不会执行。"
                    ),
                    serialize_checkpoint_state(proof),
                ),
            )
            task_update = conn.execute(
                "UPDATE tasks SET status = 'running', result_json = ?, updated_at = ? "
                "WHERE id = ? AND status = 'waiting_approval'",
                (serialize_checkpoint_state(task_result), now, task_id),
            )
            run_update = conn.execute(
                "UPDATE task_runs SET status = 'running', result_json = ?, "
                "metadata_json = ?, updated_at = ? "
                "WHERE id = ? AND status = 'waiting_approval' "
                "AND intake_state = 'open'",
                (
                    serialize_checkpoint_state(task_result),
                    serialize_checkpoint_state(metadata),
                    now,
                    run_id,
                ),
            )
            if task_update.rowcount != 1 or run_update.rowcount != 1:
                raise PublicationConflict(
                    "Policy approval decision lost its Task/Run CAS"
                )
            updated_run = conn.execute(
                "SELECT * FROM task_runs WHERE id = ?", (run_id,)
            ).fetchone()
        return {
            "decided": True,
            "idempotent": False,
            "approved": approved,
            "decision": decision,
            "proof": proof,
            "event_id": int(cursor.lastrowid),
            "run": self._serialize_run(_row_dict(updated_run) or {}),
        }

    def supersede_policy_approval_wait_for_runtime_input(
        self,
        *,
        task_id: str,
        run_id: str,
        command_ids: Sequence[str],
    ) -> dict[str, Any]:
        """Atomically release an obsolete policy wait for claimed messages.

        A runtime message changes the goal generation and therefore invalidates
        an approval request for the old operation. The messages remain claimed
        until the replacement GoalSpec and plan are durable; this transaction
        only records the supersession, cancels stale approval decisions, and
        returns Task/Run to ``running`` so steering can rebuild the goal.
        """

        normalized_ids = tuple(
            dict.fromkeys(str(item or "").strip() for item in command_ids)
        )
        normalized_ids = tuple(item for item in normalized_ids if item)
        if not task_id.strip() or not run_id.strip() or not normalized_ids:
            raise ValueError("task_id, run_id and command_ids cannot be empty")
        now = self._now()
        with self._connection(write=True) as conn:
            run = self._require_row(conn, "task_runs", run_id, "run")
            if run["task_id"] != task_id:
                raise TaskStateError(
                    f"Run {run_id} does not belong to task {task_id}"
                )
            task = conn.execute(
                "SELECT * FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
            if task is None:
                raise StateNotFoundError(f"task {task_id} was not found")
            placeholders = ",".join("?" for _ in normalized_ids)
            messages = conn.execute(
                f"""
                SELECT id, intake_generation FROM task_commands
                WHERE id IN ({placeholders}) AND task_id = ? AND run_id = ?
                  AND command_type = 'message' AND status = 'claimed'
                ORDER BY intake_generation, created_at, id
                """,  # noqa: S608 - placeholder count only; values stay bound
                (*normalized_ids, task_id, run_id),
            ).fetchall()
            if len(messages) != len(normalized_ids):
                raise PublicationConflict(
                    "Policy wait supersession requires the exact claimed messages"
                )
            generations = [
                int(message["intake_generation"] or 0) for message in messages
            ]
            if (
                not generations
                or min(generations) <= int(run["applied_generation"] or 0)
                or max(generations) > int(run["accepted_generation"] or 0)
            ):
                raise PublicationConflict(
                    "Policy wait supersession has an invalid input generation"
                )

            metadata = deserialize_checkpoint_state(run["metadata_json"]) or {}
            pending = metadata.get("pending_policy_approval")
            if not isinstance(pending, Mapping):
                return {
                    "superseded": False,
                    "idempotent": True,
                    "run": self._serialize_run(_row_dict(run) or {}),
                }
            if task["status"] != "waiting_approval" or run["status"] != "waiting_approval":
                raise PublicationConflict(
                    "Pending policy approval is not in a waiting Task/Run"
                )

            approval_id = str(pending.get("approval_id") or "")
            proof = {
                "approval_id": approval_id,
                "decision": "superseded",
                "superseded": True,
                "reason": "runtime_input_changed_goal",
                "command_ids": list(normalized_ids),
                "input_generations": generations,
                "superseded_at": now,
                "request": dict(pending),
            }
            proof["proof_hash"] = canonical_json_hash(proof)
            supersessions = metadata.get("policy_approval_supersessions", {})
            if not isinstance(supersessions, Mapping):
                raise PublicationConflict(
                    "Run policy approval supersession proof is malformed"
                )
            supersessions = dict(supersessions)
            supersessions[approval_id or proof["proof_hash"]] = proof
            metadata["policy_approval_supersessions"] = supersessions
            metadata["last_policy_approval_supersession"] = proof
            metadata.pop("pending_policy_approval", None)

            task_result = deserialize_checkpoint_state(task["result_json"]) or {}
            task_result.pop("pending_action", None)
            task_result.pop("policy_approval_id", None)
            task_result.pop("policy_event", None)
            task_result.pop("policy_evaluation", None)
            task_result.pop("approval_request", None)
            task_result["policy_approval_superseded"] = proof
            task_result["summary"] = "旧审批请求已被新的用户要求取代。"

            cancelled_result = serialize_checkpoint_state(
                {
                    "cancelled": True,
                    "reason": "policy_approval_superseded_by_runtime_input",
                    "approval_id": approval_id,
                    "supersession_hash": proof["proof_hash"],
                }
            )
            conn.execute(
                """
                UPDATE task_commands
                SET status = 'cancelled', result_json = ?, completed_at = ?,
                    updated_at = ?
                WHERE task_id = ? AND (run_id = ? OR run_id IS NULL)
                  AND command_type = 'approval'
                  AND status IN ('queued', 'claimed')
                """,
                (cancelled_result, now, now, task_id, run_id),
            )

            for node in conn.execute(
                "SELECT id, output_json, metadata_json FROM task_nodes "
                "WHERE run_id = ? AND status = 'running'",
                (run_id,),
            ).fetchall():
                output = deserialize_checkpoint_state(node["output_json"]) or {}
                node_metadata = (
                    deserialize_checkpoint_state(node["metadata_json"]) or {}
                )
                output.update(
                    {
                        "summary": task_result["summary"],
                        "approval_id": approval_id,
                        "approval_status": "superseded",
                    }
                )
                node_metadata.pop("pending_policy_approval_id", None)
                node_metadata["last_policy_approval_supersession_hash"] = proof[
                    "proof_hash"
                ]
                conn.execute(
                    "UPDATE task_nodes SET output_json = ?, metadata_json = ?, "
                    "updated_at = ? WHERE id = ? AND status = 'running'",
                    (
                        serialize_checkpoint_state(output),
                        serialize_checkpoint_state(node_metadata),
                        now,
                        node["id"],
                    ),
                )

            cursor = conn.execute(
                """
                INSERT INTO task_events(task_id, ts, type, title, content, data_json)
                VALUES (?, ?, 'approval', ?, ?, ?)
                """,
                (
                    task_id,
                    now,
                    "旧审批请求已失效",
                    "收到新的用户要求，旧目标对应的审批不会继续执行。",
                    serialize_checkpoint_state(proof),
                ),
            )
            task_update = conn.execute(
                "UPDATE tasks SET status = 'running', result_json = ?, updated_at = ? "
                "WHERE id = ? AND status = 'waiting_approval'",
                (serialize_checkpoint_state(task_result), now, task_id),
            )
            run_update = conn.execute(
                "UPDATE task_runs SET status = 'running', result_json = ?, "
                "metadata_json = ?, updated_at = ? "
                "WHERE id = ? AND status = 'waiting_approval' "
                "AND intake_state = 'open'",
                (
                    serialize_checkpoint_state(task_result),
                    serialize_checkpoint_state(metadata),
                    now,
                    run_id,
                ),
            )
            if task_update.rowcount != 1 or run_update.rowcount != 1:
                raise PublicationConflict(
                    "Policy approval supersession lost its Task/Run CAS"
                )
            updated_run = conn.execute(
                "SELECT * FROM task_runs WHERE id = ?", (run_id,)
            ).fetchone()
        return {
            "superseded": True,
            "idempotent": False,
            "proof": proof,
            "event_id": int(cursor.lastrowid),
            "run": self._serialize_run(_row_dict(updated_run) or {}),
        }

    def commit_skill_recommendation_request(
        self,
        *,
        task_id: str,
        run_id: str,
        approval_id: str,
        recommendation_id: str,
        result: Mapping[str, Any],
        title: str,
        content: str,
        data: Mapping[str, Any],
        recommendation_fingerprint: Mapping[str, Any] | None = None,
        node_summary: str = "能力推荐已生成，正在等待用户确认",
    ) -> dict[str, Any]:
        """Atomically expose one Skill recommendation and pause its Task/Run.

        A recommendation is executable platform state, not just UI copy.  The
        prompt, pending action, Run metadata and waiting projections therefore
        share one transaction.  A message/cancel that wins the intake race
        prevents the stale recommendation from becoming visible.
        """

        approval_id = str(approval_id or "").strip()
        recommendation_id = str(recommendation_id or "").strip()
        if not approval_id or not recommendation_id:
            raise ValueError("approval_id and recommendation_id cannot be empty")
        if not str(title or "").strip():
            raise ValueError("approval title cannot be empty")
        result_payload = _json_object(result, field="result")
        result_payload.update(
            {
                "pending_action": "install_recommended_skill",
                "recommendation_id": recommendation_id,
                "skill_recommendation_approval_id": approval_id,
                "summary": str(content or ""),
            }
        )
        event_data = _json_object(data, field="data")
        fingerprint = _json_object(
            recommendation_fingerprint,
            field="recommendation_fingerprint",
        )
        if not fingerprint:
            # Legacy/internal callers that only exercise the state machine
            # still receive an explicit weak identity.  Production runtime
            # requests always bind the full catalog package hash.
            fingerprint = {
                "schema": "builtin-skill-package/legacy",
                "id": recommendation_id,
            }
        event_data.update(
            {
                "approval_id": approval_id,
                "recommendation_id": recommendation_id,
                "recommendation_fingerprint": fingerprint,
                "run_id": run_id,
            }
        )
        request_hash = canonical_json_hash(
            {
                "schema": "skill-recommendation-request/1.0",
                "task_id": task_id,
                "run_id": run_id,
                "approval_id": approval_id,
                "recommendation_id": recommendation_id,
                "recommendation_fingerprint": fingerprint,
                "result": result_payload,
                "event": {
                    "title": str(title),
                    "content": str(content or ""),
                    "data": event_data,
                },
            }
        )
        now = self._now()
        with self._connection(write=True) as conn:
            run = self._require_row(conn, "task_runs", run_id, "run")
            if run["task_id"] != task_id:
                raise TaskStateError(
                    f"Run {run_id} does not belong to task {task_id}"
                )
            task = conn.execute(
                "SELECT * FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
            if task is None:
                raise StateNotFoundError(f"task {task_id} was not found")
            metadata = deserialize_checkpoint_state(run["metadata_json"]) or {}
            pending = metadata.get("pending_skill_recommendation")

            if (
                task["status"] == "waiting_approval"
                and run["status"] == "waiting_approval"
            ):
                if (
                    not isinstance(pending, Mapping)
                    or str(pending.get("approval_id") or "") != approval_id
                    or str(pending.get("recommendation_id") or "")
                    != recommendation_id
                    or str(pending.get("request_hash") or "") != request_hash
                ):
                    raise PublicationConflict(
                        "Task/Run are waiting for a different Skill recommendation"
                    )
                event_id = int(pending.get("event_id") or 0)
                event = conn.execute(
                    "SELECT id FROM task_events WHERE id = ? AND task_id = ? "
                    "AND type = 'approval_required'",
                    (event_id, task_id),
                ).fetchone()
                stored_result = (
                    deserialize_checkpoint_state(task["result_json"]) or {}
                )
                if (
                    event is None
                    or str(stored_result.get("recommendation_id") or "")
                    != recommendation_id
                    or str(
                        stored_result.get("skill_recommendation_approval_id") or ""
                    )
                    != approval_id
                ):
                    raise PublicationConflict(
                        "Skill recommendation waiting state is incomplete"
                    )
                return {
                    "waiting": True,
                    "idempotent": True,
                    "event_id": event_id,
                    "run": self._serialize_run(_row_dict(run) or {}),
                }

            if task["status"] != "running" or run["status"] != "running":
                raise PublicationConflict(
                    "Task and run must both be running before a Skill recommendation"
                )
            if str(run["intake_state"] or "open") != "open":
                raise PublicationConflict("Runtime input is already closed")
            if str(run["published_verification_id"] or ""):
                raise PublicationConflict(
                    "A published run cannot request a Skill installation"
                )
            pending_runtime_commands = conn.execute(
                """
                SELECT command_type FROM task_commands
                WHERE task_id = ? AND (run_id = ? OR run_id IS NULL)
                  AND command_type IN ('message', 'cancel')
                  AND status IN ('queued', 'claimed')
                ORDER BY intake_generation, created_at, id
                """,
                (task_id, run_id),
            ).fetchall()
            if pending_runtime_commands:
                raise PublicationConflict(
                    "Runtime input supersedes the Skill recommendation request",
                    pending_command_types=[
                        str(item["command_type"])
                        for item in pending_runtime_commands
                    ],
                )
            if int(run["accepted_generation"] or 0) != int(
                run["applied_generation"] or 0
            ):
                raise PublicationConflict(
                    "Skill recommendation is stale because newer input exists"
                )

            task_result = deserialize_checkpoint_state(task["result_json"]) or {}
            task_result.update(result_payload)
            for node in conn.execute(
                "SELECT id, output_json, metadata_json FROM task_nodes "
                "WHERE run_id = ? AND status = 'running'",
                (run_id,),
            ).fetchall():
                output = deserialize_checkpoint_state(node["output_json"]) or {}
                node_metadata = (
                    deserialize_checkpoint_state(node["metadata_json"]) or {}
                )
                output.update(
                    {
                        "summary": node_summary,
                        "approval_id": approval_id,
                        "approval_status": "waiting",
                    }
                )
                node_metadata["pending_skill_recommendation_id"] = approval_id
                conn.execute(
                    "UPDATE task_nodes SET status = 'completed', output_json = ?, "
                    "metadata_json = ?, finished_at = ?, updated_at = ? "
                    "WHERE id = ? AND status = 'running'",
                    (
                        serialize_checkpoint_state(output),
                        serialize_checkpoint_state(node_metadata),
                        now,
                        now,
                        node["id"],
                    ),
                )

            cursor = conn.execute(
                """
                INSERT INTO task_events(task_id, ts, type, title, content, data_json)
                VALUES (?, ?, 'approval_required', ?, ?, ?)
                """,
                (
                    task_id,
                    now,
                    str(title),
                    str(content or ""),
                    serialize_checkpoint_state(event_data),
                ),
            )
            request_proof = {
                "approval_id": approval_id,
                "recommendation_id": recommendation_id,
                "recommendation_fingerprint": fingerprint,
                "request_hash": request_hash,
                "event_id": int(cursor.lastrowid),
                "requested_at": now,
            }
            metadata = dict(metadata)
            metadata["pending_skill_recommendation"] = request_proof
            task_update = conn.execute(
                "UPDATE tasks SET status = 'waiting_approval', result_json = ?, "
                "updated_at = ? WHERE id = ? AND status = 'running'",
                (serialize_checkpoint_state(task_result), now, task_id),
            )
            run_update = conn.execute(
                "UPDATE task_runs SET status = 'waiting_approval', result_json = ?, "
                "metadata_json = ?, current_node_id = '', updated_at = ? "
                "WHERE id = ? AND status = 'running' AND intake_state = 'open'",
                (
                    serialize_checkpoint_state(task_result),
                    serialize_checkpoint_state(metadata),
                    now,
                    run_id,
                ),
            )
            if task_update.rowcount != 1 or run_update.rowcount != 1:
                raise PublicationConflict(
                    "Skill recommendation request lost its Task/Run CAS"
                )
            updated_run = conn.execute(
                "SELECT * FROM task_runs WHERE id = ?", (run_id,)
            ).fetchone()
        return {
            "waiting": True,
            "idempotent": False,
            "event_id": int(cursor.lastrowid),
            "run": self._serialize_run(_row_dict(updated_run) or {}),
        }

    def commit_skill_recommendation_decision(
        self,
        *,
        task_id: str,
        run_id: str,
        command_id: str,
        approval_id: str,
        recommendation_id: str,
        approved: bool,
        note: str,
        result: Mapping[str, Any],
        events: Sequence[Mapping[str, Any]],
        transaction_effect: Callable[
            [sqlite3.Connection], Mapping[str, Any] | None
        ]
        | None = None,
        superseded_events: Sequence[Mapping[str, Any]] = (),
    ) -> dict[str, Any]:
        """Commit a recommendation decision and optional registry mutation.

        The approval command, Skill rows, public events and durable decision
        proof commit together.  The Task/Run intentionally remain waiting until
        ``begin_run`` atomically reacquires this same Run.  If runtime input won
        first, the decision is recorded as superseded and the Skill effect is
        not invoked.
        """

        command_id = str(command_id or "").strip()
        approval_id = str(approval_id or "").strip()
        recommendation_id = str(recommendation_id or "").strip()
        if not command_id or not approval_id or not recommendation_id:
            raise ValueError(
                "command_id, approval_id and recommendation_id cannot be empty"
            )
        result_payload = _json_object(result, field="result")

        def normalize_events(
            values: Sequence[Mapping[str, Any]], field: str
        ) -> list[dict[str, Any]]:
            normalized: list[dict[str, Any]] = []
            for index, item in enumerate(values):
                event = _json_object(item, field=f"{field}[{index}]")
                event_type = str(event.get("type") or "").strip()
                title = str(event.get("title") or "").strip()
                if not event_type or not title:
                    raise ValueError("recommendation events require type and title")
                normalized.append(
                    {
                        "type": event_type,
                        "title": title,
                        "content": str(event.get("content") or ""),
                        "data": _json_object(
                            event.get("data"), field="recommendation event data"
                        ),
                    }
                )
            return normalized

        normal_events = normalize_events(events, "events")
        stale_events = normalize_events(superseded_events, "superseded_events")
        now = self._now()
        with self._connection(write=True) as conn:
            run = self._require_row(conn, "task_runs", run_id, "run")
            if run["task_id"] != task_id:
                raise TaskStateError(
                    f"Run {run_id} does not belong to task {task_id}"
                )
            task = conn.execute(
                "SELECT * FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
            if task is None:
                raise StateNotFoundError(f"task {task_id} was not found")
            command = self._require_row(
                conn, "task_commands", command_id, "command"
            )
            if (
                command["task_id"] != task_id
                or command["run_id"] not in {None, run_id}
                or command["command_type"] != "approval"
            ):
                raise TaskStateError(
                    "Approval command does not belong to this recommendation Run"
                )
            payload = deserialize_checkpoint_state(command["payload_json"]) or {}
            if payload.get("approved") is not approved:
                raise PublicationConflict(
                    "Approval command decision does not match the continuation"
                )
            if str(payload.get("note") or "") != str(note or ""):
                raise PublicationConflict(
                    "Approval command note does not match the continuation"
                )
            command_approval_id = str(payload.get("approval_id") or "")
            if command_approval_id and command_approval_id != approval_id:
                raise PublicationConflict(
                    "Approval command belongs to another recommendation request"
                )
            metadata = deserialize_checkpoint_state(run["metadata_json"]) or {}
            decisions = metadata.get("skill_recommendation_decisions", {})
            if not isinstance(decisions, Mapping):
                raise PublicationConflict(
                    "Run Skill recommendation decision proof is malformed"
                )
            existing = decisions.get(command_id)
            if command["status"] == "completed" and isinstance(existing, Mapping):
                if (
                    bool(existing.get("approved")) is not approved
                    or str(existing.get("recommendation_id") or "")
                    != recommendation_id
                    or str(existing.get("approval_id") or "") != approval_id
                ):
                    raise PublicationConflict(
                        "Completed recommendation command has conflicting proof"
                    )
                stored_result = (
                    deserialize_checkpoint_state(task["result_json"]) or {}
                )
                return {
                    "decided": True,
                    "idempotent": True,
                    "superseded": bool(existing.get("superseded")),
                    "proof": dict(existing),
                    "result": stored_result,
                    "run": self._serialize_run(_row_dict(run) or {}),
                }
            if command["status"] not in {"queued", "claimed"}:
                raise PublicationConflict(
                    "Approval command is no longer available for this recommendation"
                )
            pending = metadata.get("pending_skill_recommendation")
            if (
                task["status"] != "waiting_approval"
                or run["status"] != "waiting_approval"
                or not isinstance(pending, Mapping)
                or str(pending.get("approval_id") or "") != approval_id
                or str(pending.get("recommendation_id") or "")
                != recommendation_id
            ):
                raise PublicationConflict(
                    "Task/Run are not waiting for this Skill recommendation"
                )
            if str(run["intake_state"] or "open") != "open":
                raise PublicationConflict("Runtime input is already closed")

            pending_runtime_commands = conn.execute(
                """
                SELECT command_type FROM task_commands
                WHERE task_id = ? AND (run_id = ? OR run_id IS NULL)
                  AND command_type IN ('message', 'cancel')
                  AND status IN ('queued', 'claimed')
                ORDER BY intake_generation, created_at, id
                """,
                (task_id, run_id),
            ).fetchall()
            pending_types = [
                str(item["command_type"]) for item in pending_runtime_commands
            ]
            superseded = bool(pending_types)
            selected_events = stale_events if superseded else normal_events
            effect_result: dict[str, Any] = {}
            if approved and not superseded:
                if transaction_effect is None:
                    raise ValueError(
                        "Approved Skill recommendation requires a transaction effect"
                    )
                effect_value = transaction_effect(conn)
                if effect_value is not None:
                    effect_result = _json_object(
                        effect_value, field="transaction_effect result"
                    )
                updates = _json_object(
                    effect_result.get("result_updates"),
                    field="effect.result_updates",
                )
                result_payload.update(updates)
                raw_effect_events = effect_result.get("events", [])
                if not isinstance(raw_effect_events, Sequence) or isinstance(
                    raw_effect_events, (str, bytes)
                ):
                    raise TypeError("effect.events must be a sequence")
                selected_events.extend(
                    normalize_events(raw_effect_events, "effect.events")
                )

            result_payload.pop("pending_action", None)
            result_payload.pop("recommendation_id", None)
            result_payload.pop("skill_recommendation_approval_id", None)
            result_payload.pop("summary", None)
            decision = "approved" if approved else "rejected"
            proof = {
                "action": "install_recommended_skill",
                "approval_id": approval_id,
                "recommendation_id": recommendation_id,
                "approved": approved,
                "decision": decision,
                "command_id": command_id,
                "superseded": superseded,
                "pending_command_types": sorted(set(pending_types)),
                "decided_at": now,
                "request": dict(pending),
            }
            proof["proof_hash"] = canonical_json_hash(proof)
            result_payload.update(
                {
                    "approval": decision,
                    "skill_recommendation_decision": proof,
                    "skip_skill_recommendations": True,
                }
            )
            if superseded:
                result_payload["approval_resolution"] = (
                    "superseded_by_runtime_input"
                )

            metadata = dict(metadata)
            stored_decisions = dict(decisions)
            stored_decisions[command_id] = proof
            metadata["skill_recommendation_decisions"] = stored_decisions
            metadata["skill_recommendation_decision"] = proof
            metadata.pop("pending_skill_recommendation", None)
            for node in conn.execute(
                "SELECT id, output_json, metadata_json FROM task_nodes "
                "WHERE run_id = ? AND metadata_json LIKE ?",
                (run_id, f'%"pending_skill_recommendation_id":"{approval_id}"%'),
            ).fetchall():
                output = deserialize_checkpoint_state(node["output_json"]) or {}
                node_metadata = (
                    deserialize_checkpoint_state(node["metadata_json"]) or {}
                )
                output.update(
                    {
                        "approval_id": approval_id,
                        "approval_status": (
                            "superseded" if superseded else decision
                        ),
                    }
                )
                node_metadata.pop("pending_skill_recommendation_id", None)
                node_metadata["last_skill_recommendation_id"] = approval_id
                conn.execute(
                    "UPDATE task_nodes SET output_json = ?, metadata_json = ?, "
                    "updated_at = ? WHERE id = ?",
                    (
                        serialize_checkpoint_state(output),
                        serialize_checkpoint_state(node_metadata),
                        now,
                        node["id"],
                    ),
                )

            for event in selected_events:
                event_data = dict(event["data"])
                event_data.setdefault("approval_id", approval_id)
                event_data.setdefault("recommendation_id", recommendation_id)
                event_data.setdefault("command_id", command_id)
                event_data.setdefault("superseded", superseded)
                conn.execute(
                    """
                    INSERT INTO task_events(task_id, ts, type, title, content, data_json)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        task_id,
                        now,
                        event["type"],
                        event["title"],
                        event["content"],
                        serialize_checkpoint_state(event_data),
                    ),
                )
            command_update = conn.execute(
                """
                UPDATE task_commands
                SET status = 'completed', worker_id = ?,
                    claimed_at = CASE WHEN claimed_at = '' THEN ? ELSE claimed_at END,
                    result_json = ?, completed_at = ?, updated_at = ?
                WHERE id = ? AND status IN ('queued', 'claimed')
                """,
                (
                    f"approval:{run_id}",
                    now,
                    serialize_checkpoint_state(proof),
                    now,
                    now,
                    command_id,
                ),
            )
            if command_update.rowcount != 1:
                raise PublicationConflict(
                    "Skill recommendation approval command lost its CAS"
                )
            serialized_result = serialize_checkpoint_state(result_payload)
            task_update = conn.execute(
                "UPDATE tasks SET result_json = ?, updated_at = ? "
                "WHERE id = ? AND status = 'waiting_approval'",
                (serialized_result, now, task_id),
            )
            run_update = conn.execute(
                "UPDATE task_runs SET result_json = ?, metadata_json = ?, "
                "updated_at = ? WHERE id = ? AND status = 'waiting_approval' "
                "AND intake_state = 'open'",
                (
                    serialized_result,
                    serialize_checkpoint_state(metadata),
                    now,
                    run_id,
                ),
            )
            if task_update.rowcount != 1 or run_update.rowcount != 1:
                raise PublicationConflict(
                    "Skill recommendation decision lost its Task/Run CAS"
                )
            updated_run = conn.execute(
                "SELECT * FROM task_runs WHERE id = ?", (run_id,)
            ).fetchone()
        return {
            "decided": True,
            "idempotent": False,
            "superseded": superseded,
            "proof": proof,
            "result": result_payload,
            "run": self._serialize_run(_row_dict(updated_run) or {}),
        }

    def commit_approval_resolution(
        self,
        *,
        task_id: str,
        run_id: str,
        command_id: str,
        decision: str,
        note: str,
        approval_id: str,
        expected_generation: int,
        result: Mapping[str, Any],
        events: Sequence[Mapping[str, Any]],
        superseded_result: Mapping[str, Any] | None = None,
        superseded_events: Sequence[Mapping[str, Any]] = (),
    ) -> dict[str, Any]:
        """Atomically close a generic approval that has no resumable action.

        Approval rejection and an approved-but-unsupported action are terminal
        responses, but only if no newer runtime input has won the intake race.
        Their public events, Task projection, Run state and intake fence must
        therefore share one transaction.  A pending message/cancel makes the
        resolution stale and remains available for normal runtime handling.
        """

        command_id = str(command_id or "").strip()
        if not command_id:
            raise ValueError("command_id cannot be empty")
        normalized_decision = str(decision or "").strip()
        if normalized_decision not in {"rejected", "unsupported"}:
            raise ValueError("decision must be rejected or unsupported")
        if expected_generation < 0:
            raise ValueError("expected_generation cannot be negative")
        normalized_note = str(note or "")
        normalized_approval_id = str(approval_id or "").strip()
        result_payload = _json_object(result, field="result")
        superseded_payload = _json_object(
            superseded_result, field="superseded_result"
        )
        normalized_events: list[dict[str, Any]] = []
        for index, item in enumerate(events):
            event = _json_object(item, field=f"events[{index}]")
            event_type = str(event.get("type") or "").strip()
            title = str(event.get("title") or "").strip()
            content = str(event.get("content") or "")
            if not event_type or not title:
                raise ValueError("approval resolution events require type and title")
            normalized_events.append(
                {
                    "type": event_type,
                    "title": title,
                    "content": content,
                    "data": _json_object(event.get("data"), field="event.data"),
                }
            )
        if not normalized_events:
            raise ValueError("approval resolution requires at least one event")
        normalized_superseded_events: list[dict[str, Any]] = []
        for index, item in enumerate(superseded_events):
            event = _json_object(item, field=f"superseded_events[{index}]")
            event_type = str(event.get("type") or "").strip()
            title = str(event.get("title") or "").strip()
            if not event_type or not title:
                raise ValueError("approval resolution events require type and title")
            normalized_superseded_events.append(
                {
                    "type": event_type,
                    "title": title,
                    "content": str(event.get("content") or ""),
                    "data": _json_object(event.get("data"), field="event.data"),
                }
            )
        resolution_hash = canonical_json_hash(
            {
                "schema": "generic-approval-resolution/1.0",
                "command_id": command_id,
                "decision": normalized_decision,
                "note": normalized_note,
                "approval_id": normalized_approval_id,
                "expected_generation": expected_generation,
                "result": result_payload,
                "events": normalized_events,
                "superseded_result": superseded_payload,
                "superseded_events": normalized_superseded_events,
            }
        )
        serialized_result = serialize_checkpoint_state(result_payload)
        now = self._now()
        with self._connection(write=True) as conn:
            run = self._require_row(conn, "task_runs", run_id, "run")
            if run["task_id"] != task_id:
                raise TaskStateError(
                    f"Run {run_id} does not belong to task {task_id}"
                )
            task = conn.execute(
                "SELECT * FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
            if task is None:
                raise StateNotFoundError(f"task {task_id} was not found")
            command = self._require_row(
                conn, "task_commands", command_id, "command"
            )
            if (
                command["task_id"] != task_id
                or command["run_id"] not in {None, run_id}
                or command["command_type"] != "approval"
            ):
                raise TaskStateError(
                    "Approval command does not belong to this approval Run"
                )
            command_payload = (
                deserialize_checkpoint_state(command["payload_json"]) or {}
            )
            expected_approved = normalized_decision == "unsupported"
            if command_payload.get("approved") is not expected_approved:
                raise PublicationConflict(
                    "Approval command decision does not match the resolution"
                )
            if str(command_payload.get("note") or "") != normalized_note:
                raise PublicationConflict(
                    "Approval command note does not match the resolution"
                )
            command_approval_id = str(command_payload.get("approval_id") or "")
            if (
                normalized_approval_id
                and command_approval_id
                and command_approval_id != normalized_approval_id
            ):
                raise PublicationConflict(
                    "Approval command belongs to another approval request"
                )
            metadata = deserialize_checkpoint_state(run["metadata_json"]) or {}
            if (
                command["status"] == "completed"
                and metadata.get("approval_resolution_command_id") == command_id
                and metadata.get("approval_decision") == normalized_decision
            ):
                proof = deserialize_checkpoint_state(command["result_json"]) or {}
                if not isinstance(proof, Mapping):
                    raise PublicationConflict(
                        "Completed approval command is missing its durable proof"
                    )
                stored_hash = str(
                    proof.get("resolution_hash")
                    or metadata.get("approval_resolution_hash")
                    or ""
                )
                superseded = bool(proof.get("superseded"))
                # A superseded resolution intentionally replaces the Task
                # projection with its continuation result.  Reconstructing
                # the call after a process restart therefore starts from that
                # committed result, not the pre-decision result used to build
                # the original hash.  The command payload and durable proof
                # remain the replay authority on this branch.
                if (
                    not superseded
                    and stored_hash
                    and stored_hash != resolution_hash
                ):
                    raise PublicationConflict(
                        "Completed approval resolution does not match this replay"
                    )
                if superseded:
                    if (
                        task["status"] != "waiting_approval"
                        or run["status"] not in {"paused", "waiting_approval"}
                        or str(run["intake_state"] or "open") != "open"
                    ):
                        raise PublicationConflict(
                            "Superseded approval continuation no longer owns a waiting Run"
                        )
                else:
                    self.assert_terminal_clean(task_id=task_id, run_id=run_id)
                stored_result = (
                    deserialize_checkpoint_state(task["result_json"]) or {}
                )
                return {
                    "completed": not superseded,
                    "idempotent": True,
                    "superseded": superseded,
                    "proof": dict(proof),
                    "result": stored_result,
                    "event_ids": [],
                    "run": self._serialize_run(_row_dict(run) or {}),
                }
            if command["status"] not in {"queued", "claimed"}:
                raise PublicationConflict(
                    "Approval command is no longer available for resolution"
                )
            if str(run["published_verification_id"] or ""):
                raise PublicationConflict(
                    "A formally published run cannot become an approval resolution"
                )
            if run["status"] not in {"paused", "waiting_approval"} or task[
                "status"
            ] != "waiting_approval":
                raise PublicationConflict(
                    "Task and run must both be waiting at approval resolution"
                )
            if str(run["intake_state"] or "open") != "open":
                raise PublicationConflict("Runtime input is already closed")

            pending_runtime_commands = conn.execute(
                """
                SELECT command_type FROM task_commands
                WHERE task_id = ? AND (run_id = ? OR run_id IS NULL)
                  AND command_type IN ('message', 'cancel')
                  AND status IN ('queued', 'claimed')
                ORDER BY intake_generation, created_at, id
                """,
                (task_id, run_id),
            ).fetchall()
            if pending_runtime_commands:
                if not superseded_payload or not normalized_superseded_events:
                    raise PublicationConflict(
                        "Runtime input is pending and supersedes the approval resolution",
                        pending_command_types=[
                            str(item["command_type"])
                            for item in pending_runtime_commands
                        ],
                    )
                pending_types = sorted(
                    {str(item["command_type"]) for item in pending_runtime_commands}
                )
                proof = {
                    "action": "generic_approval",
                    "command_id": command_id,
                    "decision": normalized_decision,
                    "approved": expected_approved,
                    "note": normalized_note,
                    "approval_id": normalized_approval_id or command_approval_id,
                    "superseded": True,
                    "pending_command_types": pending_types,
                    "resolution_hash": resolution_hash,
                    "decided_at": now,
                }
                proof["proof_hash"] = canonical_json_hash(proof)
                superseded_payload["approval_resolution_proof"] = proof
                for event in normalized_superseded_events:
                    data = dict(event["data"])
                    data.update(proof)
                    conn.execute(
                        """
                        INSERT INTO task_events(task_id, ts, type, title, content, data_json)
                        VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        (
                            task_id,
                            now,
                            event["type"],
                            event["title"],
                            event["content"],
                            serialize_checkpoint_state(data),
                        ),
                    )
                command_update = conn.execute(
                    """
                    UPDATE task_commands
                    SET status = 'completed', worker_id = ?,
                        claimed_at = CASE WHEN claimed_at = '' THEN ? ELSE claimed_at END,
                        result_json = ?, completed_at = ?, updated_at = ?
                    WHERE id = ? AND status IN ('queued', 'claimed')
                    """,
                    (
                        f"approval:{run_id}",
                        now,
                        serialize_checkpoint_state(proof),
                        now,
                        now,
                        command_id,
                    ),
                )
                if command_update.rowcount != 1:
                    raise PublicationConflict(
                        "Superseded approval command lost its CAS"
                    )
                metadata.update(
                    {
                        "approval_decision": normalized_decision,
                        "approval_resolution_command_id": command_id,
                        "approval_resolution_proof": proof,
                    }
                )
                serialized_superseded = serialize_checkpoint_state(
                    superseded_payload
                )
                task_update = conn.execute(
                    "UPDATE tasks SET result_json = ?, updated_at = ? "
                    "WHERE id = ? AND status = 'waiting_approval'",
                    (serialized_superseded, now, task_id),
                )
                run_update = conn.execute(
                    "UPDATE task_runs SET result_json = ?, metadata_json = ?, "
                    "updated_at = ? WHERE id = ? "
                    "AND status IN ('paused', 'waiting_approval') "
                    "AND intake_state = 'open'",
                    (
                        serialized_superseded,
                        serialize_checkpoint_state(metadata),
                        now,
                        run_id,
                    ),
                )
                if task_update.rowcount != 1 or run_update.rowcount != 1:
                    raise PublicationConflict(
                        "Superseded approval lost its Task/Run CAS"
                    )
                updated_run = conn.execute(
                    "SELECT * FROM task_runs WHERE id = ?", (run_id,)
                ).fetchone()
                return {
                    "completed": False,
                    "idempotent": False,
                    "superseded": True,
                    "proof": proof,
                    "result": superseded_payload,
                    "pending_command_types": pending_types,
                    "event_ids": [],
                    "run": self._serialize_run(_row_dict(updated_run) or {}),
                }
            accepted = int(run["accepted_generation"] or 0)
            applied = int(run["applied_generation"] or 0)
            if accepted != applied or applied != int(expected_generation):
                raise PublicationConflict(
                    "Approval resolution is stale because newer runtime input exists"
                )

            completed_node_output = serialize_checkpoint_state(
                {
                    "summary": "审批决定已处理",
                    "completion_kind": "approval_resolution",
                    "decision": normalized_decision,
                }
            )
            skipped_node_output = serialize_checkpoint_state(
                {
                    "summary": "审批决定已结束任务，后续节点无需执行",
                    "completion_kind": "approval_resolution",
                    "decision": normalized_decision,
                }
            )
            conn.execute(
                """
                UPDATE task_nodes
                SET status = 'completed', output_json = ?, finished_at = ?, updated_at = ?
                WHERE run_id = ? AND status = 'running'
                """,
                (completed_node_output, now, now, run_id),
            )
            conn.execute(
                """
                UPDATE task_nodes
                SET status = 'skipped', output_json = ?, finished_at = ?, updated_at = ?
                WHERE run_id = ? AND status = 'pending'
                """,
                (skipped_node_output, now, now, run_id),
            )
            conn.execute(
                """
                UPDATE artifacts
                SET delivery_status = 'rejected', verification_id = '', published_at = ''
                WHERE task_id = ? AND run_id = ?
                  AND delivery_status = 'pending_verification'
                """,
                (task_id, run_id),
            )
            stale_command_result = serialize_checkpoint_state(
                {
                    "cancelled": True,
                    "reason": "run_completed_with_approval_resolution",
                }
            )
            conn.execute(
                """
                UPDATE task_commands
                SET status = 'cancelled', result_json = ?, completed_at = ?, updated_at = ?
                WHERE task_id = ? AND (run_id = ? OR run_id IS NULL)
                  AND id != ? AND status IN ('queued', 'claimed')
                """,
                (stale_command_result, now, now, task_id, run_id, command_id),
            )

            command_proof = {
                "action": "generic_approval",
                "command_id": command_id,
                "decision": normalized_decision,
                "approved": expected_approved,
                "note": normalized_note,
                "approval_id": normalized_approval_id or command_approval_id,
                "superseded": False,
                "resolution_hash": resolution_hash,
                "decided_at": now,
            }
            command_proof["proof_hash"] = canonical_json_hash(command_proof)
            command_update = conn.execute(
                """
                UPDATE task_commands
                SET status = 'completed', worker_id = ?,
                    claimed_at = CASE WHEN claimed_at = '' THEN ? ELSE claimed_at END,
                    result_json = ?, completed_at = ?, updated_at = ?
                WHERE id = ? AND status IN ('queued', 'claimed')
                """,
                (
                    f"approval:{run_id}",
                    now,
                    serialize_checkpoint_state(command_proof),
                    now,
                    now,
                    command_id,
                ),
            )
            if command_update.rowcount != 1:
                raise PublicationConflict("Approval command lost its terminal CAS")

            event_ids: list[int] = []
            for event in normalized_events:
                cursor = conn.execute(
                    """
                    INSERT INTO task_events(task_id, ts, type, title, content, data_json)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        task_id,
                        now,
                        event["type"],
                        event["title"],
                        event["content"],
                        serialize_checkpoint_state(event["data"]),
                    ),
                )
                event_ids.append(int(cursor.lastrowid))

            task_update = conn.execute(
                """
                UPDATE tasks SET status = 'completed', result_json = ?, updated_at = ?
                WHERE id = ? AND status = 'waiting_approval'
                """,
                (serialized_result, now, task_id),
            )
            if task_update.rowcount != 1:
                raise PublicationConflict(
                    "Task completion lost its approval-resolution CAS"
                )
            metadata.update(
                {
                    "completion_kind": "approval_resolution",
                    "approval_decision": normalized_decision,
                    "approval_resolution_hash": resolution_hash,
                    "approval_resolution_command_id": command_id,
                    "approval_resolution_proof": command_proof,
                }
            )
            run_update = conn.execute(
                """
                UPDATE task_runs
                SET status = 'completed', result_json = ?, metadata_json = ?,
                    current_node_id = '', intake_state = 'closed', intake_closed_at = ?,
                    finished_at = ?, updated_at = ?
                WHERE id = ? AND status IN ('paused', 'waiting_approval')
                  AND intake_state = 'open'
                  AND accepted_generation = ? AND applied_generation = ?
                """,
                (
                    serialized_result,
                    serialize_checkpoint_state(metadata),
                    now,
                    now,
                    now,
                    run_id,
                    expected_generation,
                    expected_generation,
                ),
            )
            if run_update.rowcount != 1:
                raise PublicationConflict(
                    "Run completion lost its approval-resolution CAS"
                )
            updated_run = conn.execute(
                "SELECT * FROM task_runs WHERE id = ?", (run_id,)
            ).fetchone()
        return {
            "completed": True,
            "idempotent": False,
            "superseded": False,
            "event_ids": event_ids,
            "run": self._serialize_run(_row_dict(updated_run) or {}),
        }

    def commit_cancellation(
        self,
        *,
        task_id: str,
        run_id: str,
        result: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Atomically acknowledge cancellation and close all runtime intake.

        A message that commits before this transaction is explicitly cancelled;
        one that arrives after it is rejected by the closed intake fence.  No
        accepted generation can therefore remain queued behind a cancelled run.
        """

        now = self._now()
        cancellation_result = {
            "cancelled": True,
            **(_json_object(result, field="result") if result is not None else {}),
        }
        serialized_result = serialize_checkpoint_state(cancellation_result)
        with self._connection(write=True) as conn:
            run = self._require_row(conn, "task_runs", run_id, "run")
            if run["task_id"] != task_id:
                raise TaskStateError(
                    f"Run {run_id} does not belong to task {task_id}"
                )
            task = conn.execute(
                "SELECT * FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
            if task is None:
                raise StateNotFoundError(f"task {task_id} was not found")

            if run["status"] == "cancelled" and task["status"] == "cancelled":
                pending = conn.execute(
                    """
                    SELECT 1 FROM task_commands
                    WHERE task_id = ? AND (run_id = ? OR run_id IS NULL)
                      AND status IN ('queued', 'claimed')
                    LIMIT 1
                    """,
                    (task_id, run_id),
                ).fetchone()
                active_node = conn.execute(
                    """
                    SELECT 1 FROM task_nodes
                    WHERE run_id = ? AND status IN ('pending', 'running')
                    LIMIT 1
                    """,
                    (run_id,),
                ).fetchone()
                pending_artifact = conn.execute(
                    """
                    SELECT 1 FROM artifacts
                    WHERE task_id = ? AND run_id = ?
                      AND delivery_status = 'pending_verification'
                    LIMIT 1
                    """,
                    (task_id, run_id),
                ).fetchone()
                if (
                    pending is not None
                    or active_node is not None
                    or pending_artifact is not None
                    or str(run["intake_state"] or "") != "closed"
                    or int(run["accepted_generation"] or 0)
                    != int(run["applied_generation"] or 0)
                ):
                    raise PublicationConflict(
                        "Cancelled run has inconsistent pending runtime input"
                    )
                return {
                    "cancelled": True,
                    "idempotent": True,
                    "run": self._serialize_run(_row_dict(run) or {}),
                }
            if run["status"] not in ACTIVE_RUN_STATUSES or task["status"] not in {
                "running",
                "waiting_approval",
            }:
                raise PublicationConflict(
                    "Task and run must be active at cancellation time"
                )
            if str(run["intake_state"] or "open") != "open":
                raise PublicationConflict("Runtime input is already closed")
            if str(run["published_verification_id"] or ""):
                raise PublicationConflict("A published run cannot be cancelled")

            cancel_rows = conn.execute(
                """
                SELECT id FROM task_commands
                WHERE task_id = ? AND (run_id = ? OR run_id IS NULL)
                  AND command_type = 'cancel'
                  AND status IN ('queued', 'claimed')
                ORDER BY created_at, id
                """,
                (task_id, run_id),
            ).fetchall()
            if not cancel_rows:
                raise PublicationConflict("No pending cancellation request exists")

            cancelled_message_ids = [
                str(row["id"])
                for row in conn.execute(
                    """
                    SELECT id FROM task_commands
                    WHERE task_id = ? AND run_id = ?
                      AND command_type = 'message'
                      AND status IN ('queued', 'claimed')
                    ORDER BY intake_generation, created_at, id
                    """,
                    (task_id, run_id),
                ).fetchall()
            ]
            command_cancel_result = serialize_checkpoint_state(
                {
                    "cancelled": True,
                    "reason": "task_cancelled_before_message_application",
                }
            )
            conn.execute(
                """
                UPDATE task_commands
                SET status = 'cancelled', result_json = ?, completed_at = ?, updated_at = ?
                WHERE task_id = ? AND run_id = ?
                  AND command_type = 'message'
                  AND status IN ('queued', 'claimed')
                """,
                (command_cancel_result, now, now, task_id, run_id),
            )
            cancel_result = serialize_checkpoint_state({"cancelled": True})
            conn.execute(
                """
                UPDATE task_commands
                SET status = 'completed', result_json = ?,
                    worker_id = CASE WHEN worker_id = '' THEN 'cancellation-fence' ELSE worker_id END,
                    claimed_at = CASE WHEN claimed_at = '' THEN ? ELSE claimed_at END,
                    completed_at = ?, updated_at = ?
                WHERE task_id = ? AND (run_id = ? OR run_id IS NULL)
                  AND command_type = 'cancel'
                  AND status IN ('queued', 'claimed')
                """,
                (cancel_result, now, now, now, task_id, run_id),
            )
            stale_command_result = serialize_checkpoint_state(
                {
                    "cancelled": True,
                    "reason": "task_cancelled_before_command_application",
                }
            )
            conn.execute(
                """
                UPDATE task_commands
                SET status = 'cancelled', result_json = ?, completed_at = ?, updated_at = ?
                WHERE task_id = ? AND (run_id = ? OR run_id IS NULL)
                  AND status IN ('queued', 'claimed')
                """,
                (stale_command_result, now, now, task_id, run_id),
            )
            node_result = serialize_checkpoint_state(
                {"summary": "任务已由用户取消"}
            )
            conn.execute(
                """
                UPDATE task_nodes
                SET status = 'cancelled', output_json = ?, finished_at = ?, updated_at = ?
                WHERE run_id = ? AND status IN ('pending', 'running')
                """,
                (node_result, now, now, run_id),
            )
            conn.execute(
                """
                UPDATE artifacts
                SET delivery_status = 'rejected', verification_id = '', published_at = ''
                WHERE task_id = ? AND run_id = ?
                  AND delivery_status = 'pending_verification'
                """,
                (task_id, run_id),
            )
            conn.execute(
                """
                INSERT INTO task_events(task_id, ts, type, title, content, data_json)
                VALUES (?, ?, 'cancelled', '任务已取消', ?, ?)
                """,
                (
                    task_id,
                    now,
                    "已在安全执行边界停止当前任务。",
                    serialize_checkpoint_state(
                        {
                            "run_id": run_id,
                            "cancel_command_ids": [
                                str(row["id"]) for row in cancel_rows
                            ],
                            "cancelled_message_ids": cancelled_message_ids,
                        }
                    ),
                ),
            )
            task_update = conn.execute(
                """
                UPDATE tasks
                SET status = 'cancelled', result_json = ?, artifacts_json = '[]',
                    updated_at = ?
                WHERE id = ? AND status IN ('running', 'waiting_approval')
                """,
                (serialized_result, now, task_id),
            )
            if task_update.rowcount != 1:
                raise PublicationConflict("Task cancellation lost its terminal CAS")
            run_metadata = deserialize_checkpoint_state(run["metadata_json"]) or {}
            run_metadata["completion_kind"] = "cancelled"
            accepted_generation = int(run["accepted_generation"] or 0)
            run_update = conn.execute(
                """
                UPDATE task_runs
                SET status = 'cancelled', result_json = ?, metadata_json = ?,
                    current_node_id = '', intake_state = 'closed', intake_closed_at = ?,
                    applied_generation = ?, finished_at = ?, updated_at = ?
                WHERE id = ? AND status IN ('running', 'paused', 'waiting_approval')
                  AND intake_state = 'open' AND accepted_generation = ?
                """,
                (
                    serialized_result,
                    serialize_checkpoint_state(run_metadata),
                    now,
                    accepted_generation,
                    now,
                    now,
                    run_id,
                    accepted_generation,
                ),
            )
            if run_update.rowcount != 1:
                raise PublicationConflict("Run cancellation lost its terminal CAS")
            updated_run = conn.execute(
                "SELECT * FROM task_runs WHERE id = ?", (run_id,)
            ).fetchone()
        return {
            "cancelled": True,
            "idempotent": False,
            "cancel_command_ids": [str(row["id"]) for row in cancel_rows],
            "cancelled_message_ids": cancelled_message_ids,
            "run": self._serialize_run(_row_dict(updated_run) or {}),
        }

    def commit_failure(
        self,
        *,
        task_id: str,
        run_id: str,
        error: Mapping[str, Any],
        result: Mapping[str, Any] | None = None,
        transaction_effect: Callable[[sqlite3.Connection], Any] | None = None,
    ) -> dict[str, Any]:
        """Atomically fail a run and remove every unfinished runtime residue.

        A terminal failure is a publication boundary just like success,
        clarification and cancellation.  Task, Run, nodes, commands, input
        generations and pending artifacts therefore move together in one
        transaction; a process crash can never expose ``Task=failed`` while an
        active Run is still recoverable.
        """

        now = self._now()
        error_payload = _json_object(error, field="error")
        message = str(error_payload.get("message") or "任务执行失败")
        task_result = (
            _json_object(result, field="result")
            if result is not None
            else {"error": message}
        )
        serialized_error = serialize_checkpoint_state(error_payload)
        serialized_result = serialize_checkpoint_state(task_result)
        with self._connection(write=True) as conn:
            run = self._require_row(conn, "task_runs", run_id, "run")
            if run["task_id"] != task_id:
                raise TaskStateError(
                    f"Run {run_id} does not belong to task {task_id}"
                )
            task = conn.execute(
                "SELECT * FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
            if task is None:
                raise StateNotFoundError(f"task {task_id} was not found")

            if run["status"] == "failed" and task["status"] == "failed":
                residue = conn.execute(
                    """
                    SELECT
                      EXISTS(SELECT 1 FROM task_nodes
                             WHERE run_id = ? AND status IN ('pending', 'running')) AS active_nodes,
                      EXISTS(SELECT 1 FROM task_commands
                             WHERE task_id = ? AND (run_id = ? OR run_id IS NULL)
                               AND status IN ('queued', 'claimed')) AS active_commands,
                      EXISTS(SELECT 1 FROM artifacts
                             WHERE task_id = ? AND run_id = ?
                               AND delivery_status = 'pending_verification') AS pending_artifacts
                    """,
                    (run_id, task_id, run_id, task_id, run_id),
                ).fetchone()
                if (
                    residue is None
                    or any(int(residue[key] or 0) for key in residue.keys())
                    or str(run["intake_state"] or "") != "closed"
                    or int(run["accepted_generation"] or 0)
                    != int(run["applied_generation"] or 0)
                ):
                    raise PublicationConflict(
                        "Failed run contains unfinished terminal residue"
                    )
                if transaction_effect is not None:
                    transaction_effect(conn)
                return {
                    "failed": True,
                    "idempotent": True,
                    "run": self._serialize_run(_row_dict(run) or {}),
                }

            if str(run["published_verification_id"] or ""):
                raise PublicationConflict("A published run cannot become failed")
            if run["status"] not in ACTIVE_RUN_STATUSES | {"queued"} or task["status"] not in {
                "queued",
                "running",
                "waiting_approval",
                "failed",
            }:
                raise PublicationConflict(
                    "Task and run must be active at failure time"
                )
            if str(run["intake_state"] or "open") != "open":
                raise PublicationConflict("Runtime input is already closed")

            active_commands = conn.execute(
                """
                SELECT id, command_type FROM task_commands
                WHERE task_id = ? AND (run_id = ? OR run_id IS NULL)
                  AND status IN ('queued', 'claimed')
                ORDER BY created_at, id
                """,
                (task_id, run_id),
            ).fetchall()
            conn.execute(
                """
                UPDATE task_commands
                SET status = 'failed', error_json = ?, completed_at = ?, updated_at = ?
                WHERE task_id = ? AND (run_id = ? OR run_id IS NULL)
                  AND status IN ('queued', 'claimed')
                """,
                (serialized_error, now, now, task_id, run_id),
            )

            conn.execute(
                """
                UPDATE task_nodes
                SET status = 'failed', error_json = ?, finished_at = ?, updated_at = ?
                WHERE run_id = ? AND status = 'running'
                """,
                (serialized_error, now, now, run_id),
            )
            pending_node_output = serialize_checkpoint_state(
                {
                    "summary": "运行已失败，后续节点未执行",
                    "completion_kind": "failed",
                }
            )
            conn.execute(
                """
                UPDATE task_nodes
                SET status = 'cancelled', output_json = ?, finished_at = ?, updated_at = ?
                WHERE run_id = ? AND status = 'pending'
                """,
                (pending_node_output, now, now, run_id),
            )
            conn.execute(
                """
                UPDATE artifacts
                SET delivery_status = 'rejected', verification_id = '', published_at = ''
                WHERE task_id = ? AND run_id = ?
                  AND delivery_status = 'pending_verification'
                """,
                (task_id, run_id),
            )

            failed_command_ids = [str(row["id"]) for row in active_commands]
            cursor = conn.execute(
                """
                INSERT INTO task_events(task_id, ts, type, title, content, data_json)
                VALUES (?, ?, 'error', '任务失败', ?, ?)
                """,
                (
                    task_id,
                    now,
                    message,
                    serialize_checkpoint_state(
                        {
                            "run_id": run_id,
                            "error_type": str(error_payload.get("error_type") or ""),
                            "failed_command_ids": failed_command_ids,
                        }
                    ),
                ),
            )
            task_update = conn.execute(
                """
                UPDATE tasks
                SET status = 'failed', result_json = ?, artifacts_json = '[]', updated_at = ?
                WHERE id = ? AND status IN ('queued', 'running', 'waiting_approval', 'failed')
                """,
                (serialized_result, now, task_id),
            )
            if task_update.rowcount != 1:
                raise PublicationConflict("Task failure lost its terminal CAS")

            run_metadata = deserialize_checkpoint_state(run["metadata_json"]) or {}
            run_metadata["completion_kind"] = "failed"
            accepted_generation = int(run["accepted_generation"] or 0)
            run_update = conn.execute(
                """
                UPDATE task_runs
                SET status = 'failed', result_json = ?, error_json = ?, metadata_json = ?,
                    current_node_id = '', intake_state = 'closed', intake_closed_at = ?,
                    applied_generation = ?,
                    started_at = CASE WHEN started_at = '' THEN ? ELSE started_at END,
                    finished_at = ?, updated_at = ?
                WHERE id = ? AND status IN ('queued', 'running', 'paused', 'waiting_approval')
                  AND intake_state = 'open' AND accepted_generation = ?
                """,
                (
                    serialized_result,
                    serialized_error,
                    serialize_checkpoint_state(run_metadata),
                    now,
                    accepted_generation,
                    now,
                    now,
                    now,
                    run_id,
                    accepted_generation,
                ),
            )
            if run_update.rowcount != 1:
                raise PublicationConflict("Run failure lost its terminal CAS")
            if transaction_effect is not None:
                transaction_effect(conn)
            updated_run = conn.execute(
                "SELECT * FROM task_runs WHERE id = ?", (run_id,)
            ).fetchone()
        return {
            "failed": True,
            "idempotent": False,
            "event_id": int(cursor.lastrowid),
            "failed_command_ids": failed_command_ids,
            "run": self._serialize_run(_row_dict(updated_run) or {}),
        }

    def assert_terminal_clean(
        self,
        *,
        task_id: str,
        run_id: str,
    ) -> dict[str, Any]:
        """Return a terminal Run only when every durable invariant is closed.

        Runtime terminal commits race legitimately.  A caller that loses the
        write lock may accept the winner only after this independent read
        proves that Task/Run agree and no node, command, generation or pending
        Artifact was stranded.
        """

        with self._connection() as conn:
            run = self._require_row(conn, "task_runs", run_id, "run")
            if run["task_id"] != task_id:
                raise TaskStateError(
                    f"Run {run_id} does not belong to task {task_id}"
                )
            task = conn.execute(
                "SELECT status FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
            if task is None:
                raise StateNotFoundError(f"task {task_id} was not found")
            run_status = str(run["status"] or "")
            task_status = str(task["status"] or "")
            if (
                run_status not in TERMINAL_RUN_STATUSES
                or task_status != run_status
                or str(run["intake_state"] or "") != "closed"
                or not str(run["intake_closed_at"] or "")
                or not str(run["finished_at"] or "")
                or str(run["current_node_id"] or "")
                or int(run["accepted_generation"] or 0)
                != int(run["applied_generation"] or 0)
            ):
                raise PublicationConflict(
                    "Task and run do not share one clean terminal state"
                )
            residue = conn.execute(
                """
                SELECT
                  EXISTS(SELECT 1 FROM task_nodes
                         WHERE run_id = ? AND status IN ('pending', 'running')) AS active_nodes,
                  EXISTS(SELECT 1 FROM task_commands
                         WHERE task_id = ? AND (run_id = ? OR run_id IS NULL)
                           AND status IN ('queued', 'claimed')) AS active_commands,
                  EXISTS(SELECT 1 FROM artifacts
                         WHERE task_id = ? AND run_id = ?
                           AND delivery_status = 'pending_verification') AS pending_artifacts
                """,
                (run_id, task_id, run_id, task_id, run_id),
            ).fetchone()
            if residue is None or any(
                int(residue[key] or 0) for key in residue.keys()
            ):
                raise PublicationConflict(
                    "Terminal run contains unfinished durable residue"
                )
        return self._serialize_run(_row_dict(run) or {})

    # -- Commands ---------------------------------------------------------

    def enqueue_command(
        self,
        task_id: str,
        command_type: str,
        *,
        payload: Mapping[str, Any] | None = None,
        run_id: str | None = None,
        priority: int = 0,
        available_at: str | datetime | None = None,
        command_id: str | None = None,
        deduplicate: bool = False,
    ) -> dict[str, Any]:
        if not task_id.strip() or not command_type.strip():
            raise ValueError("task_id and command_type cannot be empty")
        command_type = command_type.strip().lower()
        command_id = command_id or _new_id("tcmd")
        now = self._now()
        due_at = _normalise_time(available_at, now)
        with self._connection(write=True) as conn:
            run: sqlite3.Row | None = None
            run_metadata: dict[str, Any] = {}
            if run_id:
                run = self._require_row(conn, "task_runs", run_id, "run")
                decoded_run_metadata = deserialize_checkpoint_state(
                    run["metadata_json"]
                ) or {}
                if isinstance(decoded_run_metadata, Mapping):
                    run_metadata = dict(decoded_run_metadata)
                if run["task_id"] != task_id:
                    raise TaskStateError(f"Run {run_id} does not belong to task {task_id}")
                # Every run-bound command shares the same terminal intake
                # fence.  Previously only message/cancel commands checked it,
                # so an approval could be queued after a run had already
                # completed and permanently violate terminal-state
                # invariants.
                if (
                    str(run["intake_state"] or "open") != "open"
                    or str(run["status"] or "") in TERMINAL_RUN_STATUSES
                ):
                    raise RunIntakeClosed(task_id, str(run_id))
                if command_type == "approval":
                    pending = run_metadata.get("pending_policy_approval")
                    policy_decisions = run_metadata.get(
                        "policy_approval_decisions", {}
                    )
                    if not isinstance(pending, Mapping) and isinstance(
                        policy_decisions, Mapping
                    ) and policy_decisions:
                        raise PublicationConflict(
                            "The policy approval decision has already been committed"
                        )
                    if isinstance(pending, Mapping):
                        task = conn.execute(
                            "SELECT status FROM tasks WHERE id = ?", (task_id,)
                        ).fetchone()
                        if (
                            task is None
                            or str(task["status"] or "") != "waiting_approval"
                            or str(run["status"] or "") != "waiting_approval"
                        ):
                            raise PublicationConflict(
                                "Task/Run are not accepting a policy approval decision"
                            )
                        requested_approval_id = str(
                            (_json_object(payload, field="payload")).get(
                                "approval_id"
                            )
                            or ""
                        )
                        pending_approval_id = str(
                            pending.get("approval_id") or ""
                        )
                        if (
                            requested_approval_id
                            and requested_approval_id != pending_approval_id
                        ):
                            raise PublicationConflict(
                                "Approval command does not match the pending policy request"
                            )
            else:
                # Every task-scoped command participates in the terminal
                # fence, not only message/cancel.  Otherwise a late approval,
                # continuation, or orchestrator command could be appended to
                # an already published Task and make the terminal projection
                # permanently unclean.  Pure task-state deployments may omit
                # the public ``tasks`` table, so the compatibility path below
                # applies the fence only when that table is present.
                has_task_table = conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'tasks'"
                ).fetchone() is not None
                task = (
                    conn.execute(
                        "SELECT status FROM tasks WHERE id = ?", (task_id,)
                    ).fetchone()
                    if has_task_table
                    else None
                )
                latest_run = conn.execute(
                    """
                    SELECT id, status, intake_state FROM task_runs
                    WHERE task_id = ?
                    ORDER BY attempt DESC
                    LIMIT 1
                    """,
                    (task_id,),
                ).fetchone()
                if task is not None and str(task["status"] or "") in {
                    "completed",
                    "failed",
                    "cancelled",
                }:
                    raise RunIntakeClosed(
                        task_id,
                        str(latest_run["id"]) if latest_run is not None else "",
                    )
                if command_type in {"message", "cancel"}:
                    # Task-scoped runtime input also observes a closed latest
                    # Run even when the legacy Task projection is not terminal.
                    if latest_run is not None and (
                        str(latest_run["intake_state"] or "open") != "open"
                        or str(latest_run["status"] or "")
                        in TERMINAL_RUN_STATUSES
                    ):
                        raise RunIntakeClosed(task_id, str(latest_run["id"]))
            if deduplicate:
                if run_id is None:
                    duplicate = conn.execute(
                        """
                        SELECT * FROM task_commands
                        WHERE task_id = ? AND run_id IS NULL AND command_type = ?
                          AND status IN ('queued', 'claimed')
                        ORDER BY created_at LIMIT 1
                        """,
                        (task_id, command_type),
                    ).fetchone()
                else:
                    duplicate = conn.execute(
                        """
                        SELECT * FROM task_commands
                        WHERE task_id = ? AND run_id = ? AND command_type = ?
                          AND status IN ('queued', 'claimed')
                        ORDER BY created_at LIMIT 1
                        """,
                        (task_id, run_id, command_type),
                    ).fetchone()
                if duplicate is not None:
                    if command_type == "approval" and isinstance(
                        run_metadata.get("pending_policy_approval"),
                        Mapping,
                    ):
                        existing_payload = deserialize_checkpoint_state(
                            duplicate["payload_json"]
                        ) or {}
                        new_payload = _json_object(payload, field="payload")
                        if existing_payload.get("approved") != new_payload.get(
                            "approved"
                        ):
                            raise PublicationConflict(
                                "A conflicting policy approval decision is already queued"
                            )
                    return self._serialize_command(_row_dict(duplicate) or {})
            intake_generation = 0
            runtime_input = bool(
                run_id and command_type in {"message", "cancel"}
            )
            if runtime_input:
                assert run is not None
                has_task_table = conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'tasks'"
                ).fetchone() is not None
                task = (
                    conn.execute(
                        "SELECT status FROM tasks WHERE id = ?", (task_id,)
                    ).fetchone()
                    if has_task_table
                    else None
                )
                if (
                    str(run["intake_state"] or "open") != "open"
                    or str(run["status"] or "") in TERMINAL_RUN_STATUSES
                    or (
                        has_task_table
                        and (
                            task is None
                            or str(task["status"] or "")
                            in {"completed", "failed", "cancelled"}
                        )
                    )
                ):
                    raise RunIntakeClosed(task_id, str(run_id))
                intake_generation = int(run["accepted_generation"] or 0) + 1
                if intake_generation <= int(run["applied_generation"] or 0):
                    raise TaskStateError(
                        "Runtime input generation cannot move behind the applied generation"
                    )
                conn.execute(
                    """
                    UPDATE task_runs
                    SET accepted_generation = ?, updated_at = ?
                    WHERE id = ? AND intake_state = 'open'
                      AND accepted_generation = ?
                    """,
                    (
                        intake_generation,
                        now,
                        run_id,
                        int(run["accepted_generation"] or 0),
                    ),
                )
            conn.execute(
                """
                INSERT INTO task_commands(
                    id, task_id, run_id, command_type, payload_json, status,
                    priority, intake_generation, available_at, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, 'queued', ?, ?, ?, ?, ?)
                """,
                (
                    command_id,
                    task_id,
                    run_id,
                    command_type,
                    serialize_checkpoint_state(_json_object(payload, field="payload")),
                    int(priority),
                    intake_generation,
                    due_at,
                    now,
                    now,
                ),
            )
            has_event_table = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'task_events'"
            ).fetchone() is not None
            if runtime_input and has_event_table:
                public_payload = _json_object(payload, field="payload")
                content = str(
                    public_payload.get("message")
                    or public_payload.get("reason")
                    or ""
                )
                conn.execute(
                    """
                    INSERT INTO task_events(task_id, ts, type, title, content, data_json)
                    VALUES (?, ?, 'command_queued', ?, ?, ?)
                    """,
                    (
                        task_id,
                        now,
                        "已加入运行中指令"
                        if command_type == "message"
                        else "已收到取消请求",
                        content,
                        serialize_checkpoint_state(
                            {
                                "command_id": command_id,
                                "run_id": run_id,
                                "intake_generation": intake_generation,
                            }
                        ),
                    ),
                )
            row = conn.execute("SELECT * FROM task_commands WHERE id = ?", (command_id,)).fetchone()
        return self._serialize_command(_row_dict(row) or {})

    def request_cancel(
        self,
        task_id: str,
        *,
        run_id: str | None = None,
        reason: str = "",
        requested_by: str = "",
    ) -> dict[str, Any]:
        return self.enqueue_command(
            task_id,
            "cancel",
            run_id=run_id,
            priority=100,
            payload={"reason": reason, "requested_by": requested_by},
            deduplicate=True,
        )

    def get_command(self, command_id: str) -> dict[str, Any] | None:
        with self._connection() as conn:
            row = conn.execute("SELECT * FROM task_commands WHERE id = ?", (command_id,)).fetchone()
        return self._serialize_command(_row_dict(row)) if row else None

    def list_commands(
        self,
        *,
        task_id: str | None = None,
        run_id: str | None | object = _UNSET,
        status: str | None = None,
        command_types: Iterable[str] | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        if status is not None and status not in COMMAND_STATUSES:
            raise ValueError(f"Unknown command status: {status}")
        clauses: list[str] = []
        params: list[Any] = []
        if task_id is not None:
            clauses.append("task_id = ?")
            params.append(task_id)
        if run_id is not _UNSET:
            if run_id is None:
                clauses.append("run_id IS NULL")
            else:
                clauses.append("run_id = ?")
                params.append(run_id)
        if status is not None:
            clauses.append("status = ?")
            params.append(status)
        types = [item.strip().lower() for item in command_types or [] if item.strip()]
        if types:
            clauses.append("command_type IN (" + ",".join("?" for _ in types) + ")")
            params.extend(types)
        sql = "SELECT * FROM task_commands"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY priority DESC, created_at, id LIMIT ?"
        params.append(max(1, int(limit)))
        with self._connection() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [self._serialize_command(_row_dict(row) or {}) for row in rows]

    def claim_command(
        self,
        worker_id: str,
        *,
        task_id: str | None = None,
        run_id: str | None = None,
        command_types: Iterable[str] | None = None,
        now: str | datetime | None = None,
    ) -> dict[str, Any] | None:
        if not worker_id.strip():
            raise ValueError("worker_id cannot be empty")
        claim_time = _normalise_time(now, self._now())
        clauses = ["status = 'queued'", "available_at <= ?"]
        params: list[Any] = [claim_time]
        if task_id is not None:
            clauses.append("task_id = ?")
            params.append(task_id)
        if run_id is not None:
            clauses.append("(run_id IS NULL OR run_id = ?)")
            params.append(run_id)
        types = [item.strip().lower() for item in command_types or [] if item.strip()]
        if types:
            clauses.append("command_type IN (" + ",".join("?" for _ in types) + ")")
            params.extend(types)
        sql = (
            "SELECT * FROM task_commands WHERE "
            + " AND ".join(clauses)
            + " ORDER BY priority DESC, created_at, id LIMIT 1"
        )
        with self._connection(write=True) as conn:
            row = conn.execute(sql, params).fetchone()
            if row is None:
                return None
            cursor = conn.execute(
                """
                UPDATE task_commands
                SET status = 'claimed', worker_id = ?, claimed_at = ?, updated_at = ?
                WHERE id = ? AND status = 'queued'
                """,
                (worker_id, claim_time, claim_time, row["id"]),
            )
            if cursor.rowcount != 1:
                return None
            claimed = conn.execute(
                "SELECT * FROM task_commands WHERE id = ?", (row["id"],)
            ).fetchone()
        return self._serialize_command(_row_dict(claimed) or {})

    def complete_command(
        self, command_id: str, *, result: Mapping[str, Any] | None = None
    ) -> dict[str, Any]:
        return self.transition_command(command_id, "completed", result=result)

    def complete_runtime_commands(
        self,
        run_id: str,
        completions: Mapping[str, Mapping[str, Any]],
    ) -> dict[str, Any]:
        """Complete one contiguous message batch and advance its durable fence.

        A replacement GoalSpec, plan and checkpoint are persisted before this
        method is called.  Completing the commands and advancing
        ``applied_generation`` in one transaction prevents a restored worker
        from observing only half of that acknowledgement.
        """

        values = {
            str(command_id): _json_object(result, field="result")
            for command_id, result in completions.items()
            if str(command_id).strip()
        }
        if not values:
            return {"run": self.get_run(run_id), "completed_command_ids": []}
        if any(result.get("applied") is not True for result in values.values()):
            raise TaskStateError(
                "Runtime message completion must contain an applied proof"
            )
        now = self._now()
        with self._connection(write=True) as conn:
            run = self._require_row(conn, "task_runs", run_id, "run")
            rows: list[sqlite3.Row] = []
            for command_id in values:
                command = self._require_row(
                    conn, "task_commands", command_id, "command"
                )
                if command["run_id"] != run_id or command["command_type"] != "message":
                    raise TaskStateError(
                        "Only message commands for the current run can advance intake"
                    )
                if command["status"] not in {"claimed", "completed"}:
                    raise TaskStateError(
                        f"Runtime message {command_id} is not claimed or completed"
                    )
                rows.append(command)

            applied = int(run["applied_generation"] or 0)
            generations: list[int] = []
            for row in rows:
                generation = int(row["intake_generation"] or 0)
                if generation <= applied:
                    continue
                if row["status"] == "completed":
                    stored_result = (
                        deserialize_checkpoint_state(row["result_json"]) or {}
                    )
                    if (
                        not isinstance(stored_result, Mapping)
                        or stored_result.get("applied") is not True
                    ):
                        raise TaskStateError(
                            "Completed runtime message lacks an applied proof"
                        )
                generations.append(generation)
            generations = sorted(set(generations))
            if generations:
                expected = list(range(applied + 1, generations[-1] + 1))
                if generations != expected:
                    raise TaskStateError(
                        "Runtime message generations must be applied contiguously"
                    )
                if generations[-1] > int(run["accepted_generation"] or 0):
                    raise TaskStateError(
                        "Applied generation cannot exceed accepted generation"
                    )

            completed_ids: list[str] = []
            for row in rows:
                command_id = str(row["id"])
                if row["status"] == "completed":
                    continue
                conn.execute(
                    """
                    UPDATE task_commands
                    SET status = 'completed', result_json = ?, completed_at = ?,
                        updated_at = ?
                    WHERE id = ? AND status = 'claimed'
                    """,
                    (
                        serialize_checkpoint_state(values[command_id]),
                        now,
                        now,
                        command_id,
                    ),
                )
                completed_ids.append(command_id)
            target_generation = generations[-1] if generations else applied
            run_update = conn.execute(
                """
                UPDATE task_runs
                SET applied_generation = ?, updated_at = ?
                WHERE id = ? AND applied_generation = ?
                """,
                (target_generation, now, run_id, applied),
            )
            if run_update.rowcount != 1:
                raise PublicationConflict(
                    "Runtime message completion lost its generation CAS"
                )
            updated_run = conn.execute(
                "SELECT * FROM task_runs WHERE id = ?", (run_id,)
            ).fetchone()
        return {
            "run": self._serialize_run(_row_dict(updated_run) or {}),
            "completed_command_ids": completed_ids,
        }

    def fail_command(
        self, command_id: str, error: Mapping[str, Any]
    ) -> dict[str, Any]:
        return self.transition_command(command_id, "failed", error=error)

    def release_command(
        self, command_id: str, *, delay_seconds: float = 0
    ) -> dict[str, Any]:
        base_time = datetime.fromisoformat(_normalise_time(None, self._now()))
        available = base_time + timedelta(seconds=max(0, delay_seconds))
        return self.transition_command(command_id, "queued", available_at=available)

    def cancel_command(self, command_id: str) -> dict[str, Any]:
        return self.transition_command(command_id, "cancelled")

    def transition_command(
        self,
        command_id: str,
        status: str,
        *,
        result: Mapping[str, Any] | None = None,
        error: Mapping[str, Any] | None = None,
        available_at: str | datetime | None = None,
    ) -> dict[str, Any]:
        if status not in COMMAND_STATUSES:
            raise ValueError(f"Unknown command status: {status}")
        now = self._now()
        with self._connection(write=True) as conn:
            row = self._require_row(conn, "task_commands", command_id, "command")
            self._assert_transition(
                "command", command_id, row["status"], status, COMMAND_TRANSITIONS
            )
            values: dict[str, Any] = {"status": status, "updated_at": now}
            if result is not None:
                values["result_json"] = serialize_checkpoint_state(
                    _json_object(result, field="result")
                )
            if error is not None:
                values["error_json"] = serialize_checkpoint_state(
                    _json_object(error, field="error")
                )
            if status == "queued":
                values.update(
                    {
                        "available_at": _normalise_time(available_at, now),
                        "worker_id": "",
                        "claimed_at": "",
                        "completed_at": "",
                    }
                )
            elif status in {"completed", "failed", "cancelled"}:
                values["completed_at"] = now
            assignments = ", ".join(f"{key} = ?" for key in values)
            conn.execute(
                f"UPDATE task_commands SET {assignments} WHERE id = ?",  # noqa: S608 - fixed column names
                [*values.values(), command_id],
            )
            updated = conn.execute(
                "SELECT * FROM task_commands WHERE id = ?", (command_id,)
            ).fetchone()
        return self._serialize_command(_row_dict(updated) or {})

    def delete_command(self, command_id: str) -> bool:
        with self._connection(write=True) as conn:
            row = conn.execute(
                "SELECT status FROM task_commands WHERE id = ?", (command_id,)
            ).fetchone()
            if row is None:
                return False
            if row["status"] == "claimed":
                raise TaskStateError("A claimed command cannot be deleted")
            conn.execute("DELETE FROM task_commands WHERE id = ?", (command_id,))
        return True

    def is_cancel_requested(self, task_id: str, *, run_id: str | None = None) -> bool:
        clauses = [
            "task_id = ?",
            "command_type = 'cancel'",
            "status IN ('queued', 'claimed')",
        ]
        params: list[Any] = [task_id]
        if run_id is not None:
            clauses.append("(run_id IS NULL OR run_id = ?)")
            params.append(run_id)
        with self._connection() as conn:
            row = conn.execute(
                "SELECT 1 FROM task_commands WHERE " + " AND ".join(clauses) + " LIMIT 1",
                params,
            ).fetchone()
        return row is not None

    def raise_if_cancel_requested(self, task_id: str, *, run_id: str | None = None) -> None:
        if self.is_cancel_requested(task_id, run_id=run_id):
            raise TaskCancellationRequested(task_id, run_id)

    # -- Internal helpers -------------------------------------------------

    @staticmethod
    def _require_row(
        conn: sqlite3.Connection, table: str, entity_id: str, entity: str
    ) -> sqlite3.Row:
        if table not in {
            "task_runs",
            "task_nodes",
            "task_checkpoints",
            "task_commands",
            "task_goal_specs",
            "task_verifications",
        }:
            raise ValueError("Unknown task-state table")
        row = conn.execute(
            f"SELECT * FROM {table} WHERE id = ?",  # noqa: S608 - table allowlisted above
            (entity_id,),
        ).fetchone()
        if row is None:
            raise StateNotFoundError(f"{entity} {entity_id} was not found")
        return row

    @staticmethod
    def _assert_transition(
        entity: str,
        entity_id: str,
        old_status: str,
        new_status: str,
        transitions: Mapping[str, frozenset[str]],
    ) -> None:
        if new_status not in transitions.get(old_status, frozenset()):
            raise InvalidStateTransition(entity, entity_id, old_status, new_status)

    @staticmethod
    def _validate_node_run(conn: sqlite3.Connection, node_id: str, run_id: str) -> None:
        row = conn.execute("SELECT run_id FROM task_nodes WHERE id = ?", (node_id,)).fetchone()
        if row is None:
            raise StateNotFoundError(f"node {node_id} was not found")
        if row["run_id"] != run_id:
            raise TaskStateError(f"Node {node_id} does not belong to run {run_id}")

    @staticmethod
    def _validate_resume_checkpoint(
        conn: sqlite3.Connection, task_id: str, checkpoint_id: str | None
    ) -> str:
        if not checkpoint_id:
            return ""
        row = conn.execute(
            "SELECT task_id FROM task_checkpoints WHERE id = ?", (checkpoint_id,)
        ).fetchone()
        if row is None:
            raise StateNotFoundError(f"checkpoint {checkpoint_id} was not found")
        if row["task_id"] != task_id:
            raise TaskStateError(
                f"Checkpoint {checkpoint_id} belongs to task {row['task_id']}, not {task_id}"
            )
        return checkpoint_id

    @staticmethod
    def _serialize_run(row: dict[str, Any] | None) -> dict[str, Any]:
        if not row:
            return {}
        return {
            **row,
            "attempt": int(row["attempt"]),
            "accepted_generation": int(row.get("accepted_generation") or 0),
            "applied_generation": int(row.get("applied_generation") or 0),
            "result": deserialize_checkpoint_state(row.get("result_json")) or {},
            "error": deserialize_checkpoint_state(row.get("error_json")) or {},
            "metadata": deserialize_checkpoint_state(row.get("metadata_json")) or {},
        }

    @staticmethod
    def _serialize_node(row: dict[str, Any] | None) -> dict[str, Any]:
        if not row:
            return {}
        return {
            **row,
            "sequence": int(row["sequence"]),
            "input": deserialize_checkpoint_state(row.get("input_json")) or {},
            "output": deserialize_checkpoint_state(row.get("output_json")) or {},
            "error": deserialize_checkpoint_state(row.get("error_json")) or {},
            "metadata": deserialize_checkpoint_state(row.get("metadata_json")) or {},
        }

    @staticmethod
    def _serialize_checkpoint(
        row: dict[str, Any] | None, *, include_state: bool
    ) -> dict[str, Any]:
        if not row:
            return {}
        result = {
            **row,
            "sequence": int(row["sequence"]),
            "restore_count": int(row["restore_count"]),
            "metadata": deserialize_checkpoint_state(row.get("metadata_json")) or {},
            "last_restore_metadata": deserialize_checkpoint_state(
                row.get("last_restore_metadata_json")
            )
            or {},
        }
        if include_state:
            result["state"] = deserialize_checkpoint_state(row.get("state_json"))
        else:
            result.pop("state_json", None)
        return result

    @staticmethod
    def _serialize_command(row: dict[str, Any] | None) -> dict[str, Any]:
        if not row:
            return {}
        return {
            **row,
            "type": row["command_type"],
            "priority": int(row["priority"]),
            "intake_generation": int(row.get("intake_generation") or 0),
            "payload": deserialize_checkpoint_state(row.get("payload_json")) or {},
            "result": deserialize_checkpoint_state(row.get("result_json")) or {},
            "error": deserialize_checkpoint_state(row.get("error_json")) or {},
        }

    @staticmethod
    def _serialize_goal_spec(row: dict[str, Any] | None) -> dict[str, Any]:
        if not row:
            return {}
        return {
            **row,
            "version": int(row["version"]),
            "spec": deserialize_checkpoint_state(row.get("spec_json")) or {},
            "public_summary": deserialize_checkpoint_state(
                row.get("public_summary_json")
            )
            or {},
        }

    @staticmethod
    def _serialize_verification(row: dict[str, Any] | None) -> dict[str, Any]:
        if not row:
            return {}
        return {
            **row,
            "attempt": int(row["attempt"]),
            "intake_generation": int(row.get("intake_generation") or 0),
            "report": deserialize_checkpoint_state(row.get("report_json")) or {},
            "public_report": deserialize_checkpoint_state(
                row.get("public_report_json")
            )
            or {},
        }


__all__ = [
    "ACTIVE_RUN_STATUSES",
    "ActiveRunConflict",
    "COMMAND_STATUSES",
    "InvalidStateTransition",
    "NODE_STATUSES",
    "PublicationConflict",
    "RUN_STATUSES",
    "RunIntakeClosed",
    "StateNotFoundError",
    "TASK_STATE_SCHEMA_SQL",
    "TERMINAL_RUN_STATUSES",
    "TaskCancellationRequested",
    "TaskStateError",
    "TaskStateService",
    "deserialize_checkpoint_state",
    "init_schema",
    "serialize_checkpoint_state",
]
