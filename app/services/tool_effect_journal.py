from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterator, Mapping

from app import db
from app.services.runtime_contract_service import canonical_json_hash


EFFECT_STATES = frozenset({"prepared", "executing", "succeeded", "unknown"})
EFFECT_KINDS = frozenset(
    {"read", "idempotent_write", "non_idempotent_write", "artifact_write"}
)
SAFE_RETRY_EFFECT_KINDS = frozenset({"read", "idempotent_write"})

DISPATCH = "dispatch"
REUSE = "reuse"
WAIT = "wait"
RECONCILE = "reconcile"


TOOL_EFFECT_JOURNAL_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS tool_effects (
    effect_key TEXT PRIMARY KEY,
    idempotency_key TEXT NOT NULL UNIQUE,
    task_id TEXT NOT NULL,
    goal_spec_hash TEXT NOT NULL,
    operation_key TEXT NOT NULL,
    server_id TEXT NOT NULL,
    tool_name TEXT NOT NULL,
    effect_kind TEXT NOT NULL
        CHECK (effect_kind IN ('read', 'idempotent_write', 'non_idempotent_write', 'artifact_write')),
    request_hash TEXT NOT NULL,
    safe_arguments_json TEXT NOT NULL DEFAULT '{}',
    state TEXT NOT NULL DEFAULT 'prepared'
        CHECK (state IN ('prepared', 'executing', 'succeeded', 'unknown')),
    first_run_id TEXT NOT NULL,
    last_run_id TEXT NOT NULL,
    owner_run_id TEXT NOT NULL DEFAULT '',
    worker_id TEXT NOT NULL DEFAULT '',
    lease_token TEXT NOT NULL DEFAULT '',
    lease_expires_at TEXT NOT NULL DEFAULT '',
    attempt_count INTEGER NOT NULL DEFAULT 0,
    revision INTEGER NOT NULL DEFAULT 1,
    result_json TEXT NOT NULL DEFAULT '{}',
    artifact_id TEXT NOT NULL DEFAULT '',
    artifact_sha256 TEXT NOT NULL DEFAULT '',
    external_ref TEXT NOT NULL DEFAULT '',
    unknown_reason TEXT NOT NULL DEFAULT '',
    resolution_note TEXT NOT NULL DEFAULT '',
    last_error_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    succeeded_at TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS tool_effect_transitions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    effect_key TEXT NOT NULL,
    from_state TEXT NOT NULL DEFAULT '',
    to_state TEXT NOT NULL,
    run_id TEXT NOT NULL DEFAULT '',
    worker_id TEXT NOT NULL DEFAULT '',
    lease_token TEXT NOT NULL DEFAULT '',
    reason TEXT NOT NULL DEFAULT '',
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    FOREIGN KEY (effect_key) REFERENCES tool_effects(effect_key) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_tool_effects_task
    ON tool_effects(task_id, created_at);
CREATE INDEX IF NOT EXISTS idx_tool_effects_state_lease
    ON tool_effects(state, lease_expires_at, updated_at);
CREATE INDEX IF NOT EXISTS idx_tool_effect_transitions_effect
    ON tool_effect_transitions(effect_key, id);
"""


class ToolEffectJournalError(RuntimeError):
    """Base error raised by the durable tool-effect journal."""


class ToolEffectConflict(ToolEffectJournalError):
    """Raised when one logical operation is reused with different semantics."""


class ToolEffectStateError(ToolEffectJournalError):
    """Raised when a state transition loses its CAS or lease fence."""


@dataclass(frozen=True)
class EffectDecision:
    action: str
    effect: dict[str, Any]
    lease_token: str = ""
    reason: str = ""

    @property
    def should_dispatch(self) -> bool:
        return self.action == DISPATCH

    @property
    def should_reuse(self) -> bool:
        return self.action == REUSE

    @property
    def requires_reconciliation(self) -> bool:
        return self.action == RECONCILE


def init_schema(conn: sqlite3.Connection | None = None) -> None:
    """Create the standalone schema.

    Production integration should register the same SQL as a numbered
    ``app.db`` migration.  Keeping initialization here lets the journal remain
    independently testable while the runtime integration is developed in a
    separate change.  A shared caller transaction is preserved through a
    savepoint; schema initialization never uses ``executescript`` and therefore
    cannot commit unrelated caller state.
    """

    owns_connection = conn is None
    connection = conn or db.get_conn()
    outer_transaction = connection.in_transaction
    savepoint = "tool_effect_journal_schema"
    try:
        if outer_transaction:
            connection.execute(f"SAVEPOINT {savepoint}")
        else:
            connection.execute("BEGIN IMMEDIATE")
        try:
            db._execute_schema_script(connection, TOOL_EFFECT_JOURNAL_SCHEMA_SQL)
        except BaseException:
            if outer_transaction:
                connection.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
                connection.execute(f"RELEASE SAVEPOINT {savepoint}")
            else:
                connection.rollback()
            raise
        else:
            if outer_transaction:
                connection.execute(f"RELEASE SAVEPOINT {savepoint}")
            else:
                connection.commit()
    finally:
        if owns_connection:
            connection.close()


def _require_text(value: Any, field: str) -> str:
    normalised = str(value or "").strip()
    if not normalised:
        raise ValueError(f"{field} cannot be empty")
    return normalised


def _normalise_datetime(value: datetime | str) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value or "").strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _json_dumps(value: Any) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("tool effect metadata must be JSON-compatible") from exc


def stable_tool_effect_key(
    *,
    task_id: str,
    goal_spec_hash: str,
    operation_key: str,
    server_id: str,
    tool_name: str,
    arguments: Mapping[str, Any],
) -> str:
    """Return a Run-independent identity for one logical tool operation.

    ``operation_key`` must come from a persisted plan/tool-call slot.  It is
    intentionally required: two deliberate calls with identical arguments
    must not collapse into one effect merely because their payloads match.
    ``run_id`` is deliberately absent, so a restart/retry finds the same row.
    """

    material = {
        "version": 1,
        "task_id": _require_text(task_id, "task_id"),
        "goal_spec_hash": _require_text(goal_spec_hash, "goal_spec_hash"),
        "operation_key": _require_text(operation_key, "operation_key"),
        "server_id": _require_text(server_id, "server_id"),
        "tool_name": _require_text(tool_name, "tool_name"),
        "arguments": dict(arguments),
    }
    return "tef_" + canonical_json_hash(material)


def idempotency_key_for_effect(effect_key: str) -> str:
    effect_key = _require_text(effect_key, "effect_key")
    digest = hashlib.sha256(effect_key.encode("utf-8")).hexdigest()
    return "agentnexus-" + digest


def inject_http_idempotency_key(
    headers: Mapping[str, Any] | None, idempotency_key: str
) -> dict[str, str]:
    """Force the stable key into an HTTP request without duplicate casing."""

    stable = _require_text(idempotency_key, "idempotency_key")
    merged = {
        str(key): str(value)
        for key, value in dict(headers or {}).items()
        if str(key).lower() != "idempotency-key"
    }
    merged["Idempotency-Key"] = stable
    return merged


def inject_mcp_idempotency_argument(
    arguments: Mapping[str, Any],
    input_schema: Mapping[str, Any] | None,
    idempotency_key: str,
    *,
    configured_argument: str = "",
) -> tuple[dict[str, Any], bool, str]:
    """Best-effort MCP propagation without violating a tool input schema.

    MCP does not guarantee a universal per-call idempotency field.  We only
    add a key when the tool schema explicitly declares ``idempotency_key`` or
    ``idempotencyKey``, or when an administrator configured another declared
    property.  Streamable HTTP callers should additionally send the regular
    ``Idempotency-Key`` transport header.
    """

    stable = _require_text(idempotency_key, "idempotency_key")
    schema = dict(input_schema or {})
    properties = schema.get("properties")
    if not isinstance(properties, Mapping):
        return dict(arguments), False, ""
    candidates = [configured_argument] if configured_argument else []
    candidates.extend(["idempotency_key", "idempotencyKey"])
    argument_name = next(
        (name for name in candidates if name and name in properties), ""
    )
    if not argument_name:
        return dict(arguments), False, ""
    forwarded = dict(arguments)
    existing = forwarded.get(argument_name)
    if existing not in (None, "", stable):
        raise ToolEffectConflict(
            f"MCP argument {argument_name} conflicts with the durable idempotency key"
        )
    forwarded[argument_name] = stable
    return forwarded, True, argument_name


def classify_tool_effect(
    definition: Mapping[str, Any] | None,
    *,
    artifact: bool = False,
) -> str:
    """Classify retry safety conservatively from trusted tool metadata."""

    if artifact:
        return "artifact_write"
    tool = dict(definition or {})
    annotations = tool.get("annotations")
    annotations = dict(annotations) if isinstance(annotations, Mapping) else {}
    if tool.get("effect") == "read" or annotations.get("readOnlyHint") is True:
        return "read"
    if (
        annotations.get("idempotentHint") is True
        and annotations.get("destructiveHint") is not True
    ):
        return "idempotent_write"
    return "non_idempotent_write"


class ToolEffectJournal:
    """Durable state machine around external tool side effects.

    The journal cannot manufacture exactly-once semantics for an arbitrary
    remote system.  It provides three enforceable properties instead:

    * a stable idempotency key and cached success across Task Runs;
    * fenced execution leases and compare-and-swap transitions;
    * fail-closed recovery for ambiguous non-idempotent writes.
    """

    def __init__(
        self,
        connection: sqlite3.Connection | Callable[[], sqlite3.Connection] | None = None,
        *,
        clock: Callable[[], datetime | str] | None = None,
        auto_init: bool = True,
    ) -> None:
        self._shared_connection = (
            connection if isinstance(connection, sqlite3.Connection) else None
        )
        self._connection_factory = connection if callable(connection) else db.get_conn
        self._clock = clock or (lambda: datetime.now(timezone.utc))
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
            owns_transaction = bool(write and not conn.in_transaction)
            try:
                if owns_transaction:
                    conn.execute("BEGIN IMMEDIATE")
                yield conn
                if owns_transaction:
                    conn.commit()
            except Exception:
                if owns_transaction:
                    conn.rollback()
                raise
            finally:
                if owns_connection:
                    conn.close()
                else:
                    conn.row_factory = original_row_factory

    def using_connection(self, connection: sqlite3.Connection) -> ToolEffectJournal:
        return ToolEffectJournal(
            connection=connection,
            clock=self._clock,
            auto_init=False,
        )

    def init_schema(self) -> None:
        if self._shared_connection is not None:
            init_schema(self._shared_connection)
            return
        conn = self._connection_factory()
        try:
            init_schema(conn)
        finally:
            conn.close()

    def _now_datetime(self) -> datetime:
        return _normalise_datetime(self._clock())

    def _now(self) -> str:
        return self._now_datetime().isoformat()

    @staticmethod
    def _row(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        item = {key: row[key] for key in row.keys()}
        for source, target, default in (
            ("safe_arguments_json", "safe_arguments", {}),
            ("result_json", "result", {}),
            ("last_error_json", "last_error", {}),
        ):
            try:
                item[target] = json.loads(item.get(source) or "")
            except (TypeError, ValueError):
                item[target] = default
            item.pop(source, None)
        item["safe_to_retry"] = item.get("effect_kind") in SAFE_RETRY_EFFECT_KINDS
        return item

    @staticmethod
    def _get_locked(conn: sqlite3.Connection, effect_key: str) -> sqlite3.Row:
        row = conn.execute(
            "SELECT * FROM tool_effects WHERE effect_key = ?", (effect_key,)
        ).fetchone()
        if row is None:
            raise ToolEffectJournalError(f"tool effect {effect_key} was not found")
        return row

    def _record_transition(
        self,
        conn: sqlite3.Connection,
        *,
        effect_key: str,
        from_state: str,
        to_state: str,
        run_id: str = "",
        worker_id: str = "",
        lease_token: str = "",
        reason: str,
        metadata: Mapping[str, Any] | None = None,
        created_at: str,
    ) -> None:
        conn.execute(
            """
            INSERT INTO tool_effect_transitions(
                effect_key, from_state, to_state, run_id, worker_id,
                lease_token, reason, metadata_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                effect_key,
                from_state,
                to_state,
                run_id,
                worker_id,
                lease_token,
                reason,
                _json_dumps(dict(metadata or {})),
                created_at,
            ),
        )

    def prepare_effect(
        self,
        *,
        task_id: str,
        run_id: str,
        goal_spec_hash: str,
        operation_key: str,
        server_id: str,
        tool_name: str,
        arguments: Mapping[str, Any],
        effect_kind: str,
        safe_arguments: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        if effect_kind not in EFFECT_KINDS:
            raise ValueError(f"unsupported effect_kind: {effect_kind}")
        run_id = _require_text(run_id, "run_id")
        effect_key = stable_tool_effect_key(
            task_id=task_id,
            goal_spec_hash=goal_spec_hash,
            operation_key=operation_key,
            server_id=server_id,
            tool_name=tool_name,
            arguments=arguments,
        )
        request_hash = canonical_json_hash(
            {"server_id": server_id, "tool_name": tool_name, "arguments": dict(arguments)}
        )
        now = self._now()
        identity = {
            "task_id": _require_text(task_id, "task_id"),
            "goal_spec_hash": _require_text(goal_spec_hash, "goal_spec_hash"),
            "operation_key": _require_text(operation_key, "operation_key"),
            "server_id": _require_text(server_id, "server_id"),
            "tool_name": _require_text(tool_name, "tool_name"),
            "effect_kind": effect_kind,
            "request_hash": request_hash,
        }
        with self._connection(write=True) as conn:
            existing = conn.execute(
                "SELECT * FROM tool_effects WHERE effect_key = ?", (effect_key,)
            ).fetchone()
            if existing is None:
                conn.execute(
                    """
                    INSERT INTO tool_effects(
                        effect_key, idempotency_key, task_id, goal_spec_hash,
                        operation_key, server_id, tool_name, effect_kind,
                        request_hash, safe_arguments_json, state, first_run_id,
                        last_run_id, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'prepared', ?, ?, ?, ?)
                    """,
                    (
                        effect_key,
                        idempotency_key_for_effect(effect_key),
                        identity["task_id"],
                        identity["goal_spec_hash"],
                        identity["operation_key"],
                        identity["server_id"],
                        identity["tool_name"],
                        effect_kind,
                        request_hash,
                        _json_dumps(dict(safe_arguments or {})),
                        run_id,
                        run_id,
                        now,
                        now,
                    ),
                )
                self._record_transition(
                    conn,
                    effect_key=effect_key,
                    from_state="",
                    to_state="prepared",
                    run_id=run_id,
                    reason="effect_prepared",
                    created_at=now,
                )
            else:
                mismatches = [
                    field
                    for field, expected in identity.items()
                    if str(existing[field]) != str(expected)
                ]
                if mismatches:
                    raise ToolEffectConflict(
                        "logical tool effect changed immutable fields: "
                        + ", ".join(mismatches)
                    )
                conn.execute(
                    """
                    UPDATE tool_effects
                    SET last_run_id = ?, updated_at = ?, revision = revision + 1
                    WHERE effect_key = ? AND revision = ?
                    """,
                    (run_id, now, effect_key, int(existing["revision"])),
                )
            return self._row(self._get_locked(conn, effect_key)) or {}

    def get_effect(self, effect_key: str) -> dict[str, Any] | None:
        with self._connection() as conn:
            row = conn.execute(
                "SELECT * FROM tool_effects WHERE effect_key = ?", (effect_key,)
            ).fetchone()
            return self._row(row)

    def list_transitions(self, effect_key: str) -> list[dict[str, Any]]:
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT * FROM tool_effect_transitions WHERE effect_key = ? ORDER BY id",
                (effect_key,),
            ).fetchall()
            result: list[dict[str, Any]] = []
            for row in rows:
                item = {key: row[key] for key in row.keys()}
                try:
                    item["metadata"] = json.loads(item.pop("metadata_json") or "{}")
                except (TypeError, ValueError):
                    item["metadata"] = {}
                result.append(item)
            return result

    @staticmethod
    def _lease_is_active(row: sqlite3.Row, now: datetime) -> bool:
        value = str(row["lease_expires_at"] or "").strip()
        if not value:
            return False
        try:
            return _normalise_datetime(value) > now
        except (TypeError, ValueError):
            return False

    def _set_unknown_locked(
        self,
        conn: sqlite3.Connection,
        row: sqlite3.Row,
        *,
        reason: str,
        run_id: str,
        worker_id: str,
        now: str,
        error: Mapping[str, Any] | None = None,
    ) -> sqlite3.Row:
        if str(row["state"]) != "executing":
            raise ToolEffectStateError("only an executing effect can become unknown")
        cursor = conn.execute(
            """
            UPDATE tool_effects
            SET state = 'unknown', last_run_id = ?, worker_id = '',
                lease_token = '', lease_expires_at = '', unknown_reason = ?,
                last_error_json = ?, updated_at = ?, revision = revision + 1
            WHERE effect_key = ? AND state = 'executing' AND revision = ?
            """,
            (
                run_id or str(row["last_run_id"]),
                reason,
                _json_dumps(dict(error or {})),
                now,
                row["effect_key"],
                int(row["revision"]),
            ),
        )
        if cursor.rowcount != 1:
            raise ToolEffectStateError("effect state changed before unknown could be recorded")
        self._record_transition(
            conn,
            effect_key=str(row["effect_key"]),
            from_state="executing",
            to_state="unknown",
            run_id=run_id or str(row["owner_run_id"]),
            worker_id=worker_id or str(row["worker_id"]),
            lease_token=str(row["lease_token"]),
            reason=reason,
            metadata=error,
            created_at=now,
        )
        return self._get_locked(conn, str(row["effect_key"]))

    def acquire_effect(
        self,
        effect_key: str,
        *,
        run_id: str,
        worker_id: str,
        lease_seconds: float = 60.0,
    ) -> EffectDecision:
        run_id = _require_text(run_id, "run_id")
        worker_id = _require_text(worker_id, "worker_id")
        lease_seconds = max(1.0, min(float(lease_seconds), 3600.0))
        now_dt = self._now_datetime()
        now = now_dt.isoformat()
        with self._connection(write=True) as conn:
            row = self._get_locked(conn, effect_key)
            state = str(row["state"])
            if state == "succeeded":
                return EffectDecision(REUSE, self._row(row) or {}, reason="already_succeeded")
            if state == "executing":
                if self._lease_is_active(row, now_dt):
                    return EffectDecision(WAIT, self._row(row) or {}, reason="lease_active")
                row = self._set_unknown_locked(
                    conn,
                    row,
                    reason="execution_lease_expired",
                    run_id=str(row["owner_run_id"] or run_id),
                    worker_id=str(row["worker_id"]),
                    now=now,
                )
                state = "unknown"
            if state == "unknown":
                if str(row["effect_kind"]) not in SAFE_RETRY_EFFECT_KINDS:
                    return EffectDecision(
                        RECONCILE,
                        self._row(row) or {},
                        reason="ambiguous_non_idempotent_effect",
                    )
                cursor = conn.execute(
                    """
                    UPDATE tool_effects
                    SET state = 'prepared', last_run_id = ?, unknown_reason = '',
                        resolution_note = 'automatic safe retry', updated_at = ?,
                        revision = revision + 1
                    WHERE effect_key = ? AND state = 'unknown' AND revision = ?
                    """,
                    (run_id, now, effect_key, int(row["revision"])),
                )
                if cursor.rowcount != 1:
                    raise ToolEffectStateError("effect changed while preparing a safe retry")
                self._record_transition(
                    conn,
                    effect_key=effect_key,
                    from_state="unknown",
                    to_state="prepared",
                    run_id=run_id,
                    worker_id=worker_id,
                    reason="safe_retry_after_unknown",
                    created_at=now,
                )
                row = self._get_locked(conn, effect_key)
                state = "prepared"
            if state != "prepared":
                raise ToolEffectStateError(f"cannot acquire effect in state {state}")

            lease_token = "tel_" + uuid.uuid4().hex
            lease_expires_at = (now_dt + timedelta(seconds=lease_seconds)).isoformat()
            cursor = conn.execute(
                """
                UPDATE tool_effects
                SET state = 'executing', last_run_id = ?, owner_run_id = ?,
                    worker_id = ?, lease_token = ?, lease_expires_at = ?,
                    attempt_count = attempt_count + 1, updated_at = ?,
                    revision = revision + 1
                WHERE effect_key = ? AND state = 'prepared' AND revision = ?
                """,
                (
                    run_id,
                    run_id,
                    worker_id,
                    lease_token,
                    lease_expires_at,
                    now,
                    effect_key,
                    int(row["revision"]),
                ),
            )
            if cursor.rowcount != 1:
                raise ToolEffectStateError("effect claim lost its compare-and-swap fence")
            self._record_transition(
                conn,
                effect_key=effect_key,
                from_state="prepared",
                to_state="executing",
                run_id=run_id,
                worker_id=worker_id,
                lease_token=lease_token,
                reason="execution_claimed",
                metadata={"lease_expires_at": lease_expires_at},
                created_at=now,
            )
            return EffectDecision(
                DISPATCH,
                self._row(self._get_locked(conn, effect_key)) or {},
                lease_token=lease_token,
                reason="execution_claimed",
            )

    def heartbeat(
        self,
        effect_key: str,
        *,
        lease_token: str,
        lease_seconds: float = 60.0,
    ) -> dict[str, Any]:
        lease_token = _require_text(lease_token, "lease_token")
        lease_seconds = max(1.0, min(float(lease_seconds), 3600.0))
        now_dt = self._now_datetime()
        now = now_dt.isoformat()
        expires = (now_dt + timedelta(seconds=lease_seconds)).isoformat()
        with self._connection(write=True) as conn:
            cursor = conn.execute(
                """
                UPDATE tool_effects
                SET lease_expires_at = ?, updated_at = ?, revision = revision + 1
                WHERE effect_key = ? AND state = 'executing' AND lease_token = ?
                """,
                (expires, now, effect_key, lease_token),
            )
            if cursor.rowcount != 1:
                raise ToolEffectStateError("effect heartbeat lost its lease fence")
            return self._row(self._get_locked(conn, effect_key)) or {}

    def release_before_dispatch(
        self,
        effect_key: str,
        *,
        lease_token: str,
        reason: str,
        error: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Return a claimed effect to ``prepared`` before gateway dispatch.

        Local budget checks and checkpoint persistence run after the lease is
        claimed but before the gateway coroutine is created.  Their failures
        prove that no external call was handed off, so recording ``unknown``
        would incorrectly demand manual reconciliation.  The lease token is
        still required so a stale worker cannot release another worker's
        claim.
        """

        lease_token = _require_text(lease_token, "lease_token")
        reason = _require_text(reason, "reason")
        now = self._now()
        with self._connection(write=True) as conn:
            row = self._get_locked(conn, effect_key)
            if str(row["state"]) != "executing" or str(
                row["lease_token"]
            ) != lease_token:
                raise ToolEffectStateError(
                    "effect release lost its pre-dispatch lease fence"
                )
            cursor = conn.execute(
                """
                UPDATE tool_effects
                SET state = 'prepared', owner_run_id = '', worker_id = '',
                    lease_token = '', lease_expires_at = '', unknown_reason = '',
                    resolution_note = ?, last_error_json = ?, updated_at = ?,
                    revision = revision + 1
                WHERE effect_key = ? AND state = 'executing'
                    AND lease_token = ? AND revision = ?
                """,
                (
                    reason,
                    _json_dumps(dict(error or {})),
                    now,
                    effect_key,
                    lease_token,
                    int(row["revision"]),
                ),
            )
            if cursor.rowcount != 1:
                raise ToolEffectStateError(
                    "effect release lost its compare-and-swap fence"
                )
            self._record_transition(
                conn,
                effect_key=effect_key,
                from_state="executing",
                to_state="prepared",
                run_id=str(row["owner_run_id"]),
                worker_id=str(row["worker_id"]),
                lease_token=lease_token,
                reason="dispatch_not_started",
                metadata={"reason": reason, **dict(error or {})},
                created_at=now,
            )
            return self._row(self._get_locked(conn, effect_key)) or {}

    def mark_succeeded(
        self,
        effect_key: str,
        *,
        lease_token: str,
        result: Mapping[str, Any] | None = None,
        artifact_id: str = "",
        artifact_sha256: str = "",
        external_ref: str = "",
    ) -> dict[str, Any]:
        lease_token = _require_text(lease_token, "lease_token")
        now = self._now()
        with self._connection(write=True) as conn:
            row = self._get_locked(conn, effect_key)
            if str(row["state"]) == "succeeded":
                return self._row(row) or {}
            if str(row["state"]) != "executing" or str(row["lease_token"]) != lease_token:
                raise ToolEffectStateError("effect success lost its execution lease fence")
            cursor = conn.execute(
                """
                UPDATE tool_effects
                SET state = 'succeeded', result_json = ?, artifact_id = ?,
                    artifact_sha256 = ?, external_ref = ?, worker_id = '',
                    lease_token = '', lease_expires_at = '', unknown_reason = '',
                    updated_at = ?, succeeded_at = ?, revision = revision + 1
                WHERE effect_key = ? AND state = 'executing'
                    AND lease_token = ? AND revision = ?
                """,
                (
                    _json_dumps(dict(result or {})),
                    str(artifact_id or ""),
                    str(artifact_sha256 or ""),
                    str(external_ref or ""),
                    now,
                    now,
                    effect_key,
                    lease_token,
                    int(row["revision"]),
                ),
            )
            if cursor.rowcount != 1:
                raise ToolEffectStateError("effect success lost its compare-and-swap fence")
            self._record_transition(
                conn,
                effect_key=effect_key,
                from_state="executing",
                to_state="succeeded",
                run_id=str(row["owner_run_id"]),
                worker_id=str(row["worker_id"]),
                lease_token=lease_token,
                reason="execution_succeeded",
                metadata={
                    "artifact_id": str(artifact_id or ""),
                    "external_ref": str(external_ref or ""),
                },
                created_at=now,
            )
            return self._row(self._get_locked(conn, effect_key)) or {}

    def mark_unknown(
        self,
        effect_key: str,
        *,
        lease_token: str,
        reason: str,
        error: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        lease_token = _require_text(lease_token, "lease_token")
        reason = _require_text(reason, "reason")
        now = self._now()
        with self._connection(write=True) as conn:
            row = self._get_locked(conn, effect_key)
            if str(row["state"]) == "unknown":
                return self._row(row) or {}
            if str(row["state"]) != "executing" or str(row["lease_token"]) != lease_token:
                raise ToolEffectStateError("effect ambiguity lost its execution lease fence")
            updated = self._set_unknown_locked(
                conn,
                row,
                reason=reason,
                run_id=str(row["owner_run_id"]),
                worker_id=str(row["worker_id"]),
                now=now,
                error=error,
            )
            return self._row(updated) or {}

    def recover_interrupted_executions(
        self, *, reason: str = "service_restart"
    ) -> list[dict[str, Any]]:
        """Move every in-process claim to ``unknown`` during startup recovery."""

        reason = _require_text(reason, "reason")
        now = self._now()
        recovered: list[dict[str, Any]] = []
        with self._connection(write=True) as conn:
            rows = conn.execute(
                "SELECT * FROM tool_effects WHERE state = 'executing' ORDER BY created_at"
            ).fetchall()
            for row in rows:
                updated = self._set_unknown_locked(
                    conn,
                    row,
                    reason=reason,
                    run_id=str(row["owner_run_id"]),
                    worker_id=str(row["worker_id"]),
                    now=now,
                )
                recovered.append(self._row(updated) or {})
        return recovered

    def reconcile_unknown(
        self,
        effect_key: str,
        *,
        outcome: str,
        note: str,
        run_id: str,
        result: Mapping[str, Any] | None = None,
        artifact_id: str = "",
        artifact_sha256: str = "",
        external_ref: str = "",
    ) -> dict[str, Any]:
        """Apply an explicit operator decision to an ambiguous write.

        ``outcome='succeeded'`` records externally verified success and makes
        future Runs reuse it.  ``outcome='retry'`` is the explicit human fence
        required before a non-idempotent unknown operation may dispatch again.
        """

        if outcome not in {"succeeded", "retry"}:
            raise ValueError("outcome must be succeeded or retry")
        note = _require_text(note, "note")
        run_id = _require_text(run_id, "run_id")
        now = self._now()
        target = "succeeded" if outcome == "succeeded" else "prepared"
        with self._connection(write=True) as conn:
            row = self._get_locked(conn, effect_key)
            if str(row["state"]) != "unknown":
                raise ToolEffectStateError("only an unknown effect can be reconciled")
            cursor = conn.execute(
                """
                UPDATE tool_effects
                SET state = ?, last_run_id = ?, result_json = ?, artifact_id = ?,
                    artifact_sha256 = ?, external_ref = ?, unknown_reason = '',
                    resolution_note = ?, updated_at = ?, succeeded_at = ?,
                    revision = revision + 1
                WHERE effect_key = ? AND state = 'unknown' AND revision = ?
                """,
                (
                    target,
                    run_id,
                    _json_dumps(dict(result or {})),
                    str(artifact_id or ""),
                    str(artifact_sha256 or ""),
                    str(external_ref or ""),
                    note,
                    now,
                    now if target == "succeeded" else "",
                    effect_key,
                    int(row["revision"]),
                ),
            )
            if cursor.rowcount != 1:
                raise ToolEffectStateError("effect reconciliation lost its CAS fence")
            self._record_transition(
                conn,
                effect_key=effect_key,
                from_state="unknown",
                to_state=target,
                run_id=run_id,
                reason="manual_confirmed_succeeded" if target == "succeeded" else "manual_retry_authorized",
                metadata={"note": note, "artifact_id": str(artifact_id or "")},
                created_at=now,
            )
            return self._row(self._get_locked(conn, effect_key)) or {}

    def list_unknown(self, *, task_id: str = "") -> list[dict[str, Any]]:
        sql = "SELECT * FROM tool_effects WHERE state = 'unknown'"
        params: tuple[Any, ...] = ()
        if task_id:
            sql += " AND task_id = ?"
            params = (task_id,)
        sql += " ORDER BY updated_at"
        with self._connection() as conn:
            return [self._row(row) or {} for row in conn.execute(sql, params).fetchall()]
