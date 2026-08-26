from __future__ import annotations

import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from app.services.tool_effect_journal import (
    DISPATCH,
    RECONCILE,
    REUSE,
    WAIT,
    ToolEffectConflict,
    ToolEffectJournal,
    classify_tool_effect,
    idempotency_key_for_effect,
    inject_http_idempotency_key,
    inject_mcp_idempotency_argument,
    stable_tool_effect_key,
)


class MutableClock:
    def __init__(self) -> None:
        self.value = datetime(2026, 8, 15, 4, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += timedelta(seconds=seconds)


class IdempotentHttpSink:
    """A remote write endpoint that honours Idempotency-Key."""

    def __init__(self) -> None:
        self.external_effect_count = 0
        self.responses: dict[str, dict[str, str | int]] = {}

    def post(self, headers: dict[str, str], body: dict[str, str]) -> dict[str, str | int]:
        key = headers["Idempotency-Key"]
        if key in self.responses:
            return self.responses[key]
        self.external_effect_count += 1
        response: dict[str, str | int] = {
            "delivery_id": f"delivery-{self.external_effect_count}",
            "message": body["message"],
        }
        self.responses[key] = response
        return response


class DocumentGenerator:
    """A deliberately non-idempotent Artifact generator."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.invocation_count = 0

    def generate(self, content: str) -> dict[str, str]:
        self.invocation_count += 1
        artifact_id = f"artifact-{self.invocation_count}"
        target = self.directory / f"{artifact_id}.docx"
        target.write_text(content, encoding="utf-8")
        return {
            "id": artifact_id,
            "path": str(target),
            "sha256": f"sha-{self.invocation_count}",
        }


class ToolEffectJournalTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "effects.db"
        self.clock = MutableClock()
        self.journal = ToolEffectJournal(self._connect, clock=self.clock)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.db_path, check_same_thread=False)

    def _prepare(
        self,
        *,
        run_id: str,
        effect_kind: str,
        operation_key: str = "plan-v1:deliver",
        server_id: str = "delivery",
        tool_name: str = "send",
        arguments: dict | None = None,
    ) -> dict:
        return self.journal.prepare_effect(
            task_id="task-1",
            run_id=run_id,
            goal_spec_hash="goal-hash-1",
            operation_key=operation_key,
            server_id=server_id,
            tool_name=tool_name,
            arguments=arguments or {"message": "hello"},
            effect_kind=effect_kind,
            safe_arguments=arguments or {"message": "hello"},
        )

    def test_stable_effect_key_crosses_runs_but_preserves_logical_call_slots(self) -> None:
        first = self._prepare(run_id="run-1", effect_kind="idempotent_write")
        second = self._prepare(run_id="run-2", effect_kind="idempotent_write")
        separate = self._prepare(
            run_id="run-2",
            effect_kind="idempotent_write",
            operation_key="plan-v1:deliver-again",
        )

        self.assertEqual(first["effect_key"], second["effect_key"])
        self.assertEqual(first["idempotency_key"], second["idempotency_key"])
        self.assertEqual(second["first_run_id"], "run-1")
        self.assertEqual(second["last_run_id"], "run-2")
        self.assertNotEqual(first["effect_key"], separate["effect_key"])
        self.assertNotIn("run-1", first["idempotency_key"])

        derived = stable_tool_effect_key(
            task_id="task-1",
            goal_spec_hash="goal-hash-1",
            operation_key="plan-v1:deliver",
            server_id="delivery",
            tool_name="send",
            arguments={"message": "hello"},
        )
        self.assertEqual(derived, first["effect_key"])
        self.assertEqual(idempotency_key_for_effect(derived), first["idempotency_key"])

    def test_http_and_mcp_propagation_are_stable_and_schema_safe(self) -> None:
        key = "agentnexus-test-key"
        headers = inject_http_idempotency_key(
            {"Authorization": "secret", "idempotency-key": "unstable"}, key
        )
        self.assertEqual(headers["Idempotency-Key"], key)
        self.assertEqual(headers["Authorization"], "secret")
        self.assertEqual(
            [name for name in headers if name.lower() == "idempotency-key"],
            ["Idempotency-Key"],
        )

        forwarded, used, argument_name = inject_mcp_idempotency_argument(
            {"document": "content"},
            {
                "type": "object",
                "properties": {
                    "document": {"type": "string"},
                    "idempotency_key": {"type": "string"},
                },
            },
            key,
        )
        self.assertTrue(used)
        self.assertEqual(argument_name, "idempotency_key")
        self.assertEqual(forwarded["idempotency_key"], key)

        unchanged, used, argument_name = inject_mcp_idempotency_argument(
            {"document": "content"},
            {"type": "object", "properties": {"document": {"type": "string"}}},
            key,
        )
        self.assertFalse(used)
        self.assertEqual(argument_name, "")
        self.assertEqual(unchanged, {"document": "content"})

        with self.assertRaises(ToolEffectConflict):
            inject_mcp_idempotency_argument(
                {"idempotencyKey": "caller-owned-key"},
                {"properties": {"idempotencyKey": {"type": "string"}}},
                key,
            )

    def test_effect_classification_fails_closed(self) -> None:
        self.assertEqual(classify_tool_effect({"effect": "read"}), "read")
        self.assertEqual(
            classify_tool_effect({"annotations": {"readOnlyHint": True}}), "read"
        )
        self.assertEqual(
            classify_tool_effect(
                {
                    "annotations": {
                        "idempotentHint": True,
                        "destructiveHint": False,
                    }
                }
            ),
            "idempotent_write",
        )
        self.assertEqual(
            classify_tool_effect(
                {
                    "annotations": {
                        "idempotentHint": True,
                        "destructiveHint": True,
                    }
                }
            ),
            "non_idempotent_write",
        )
        self.assertEqual(classify_tool_effect({}, artifact=True), "artifact_write")

    def test_active_lease_waits_and_success_is_replayed(self) -> None:
        prepared = self._prepare(run_id="run-1", effect_kind="idempotent_write")
        first = self.journal.acquire_effect(
            prepared["effect_key"], run_id="run-1", worker_id="worker-1"
        )
        self.assertEqual(first.action, DISPATCH)
        concurrent = self.journal.acquire_effect(
            prepared["effect_key"], run_id="run-2", worker_id="worker-2"
        )
        self.assertEqual(concurrent.action, WAIT)

        succeeded = self.journal.mark_succeeded(
            prepared["effect_key"],
            lease_token=first.lease_token,
            result={"delivery_id": "delivery-1"},
            external_ref="delivery-1",
        )
        self.assertEqual(succeeded["state"], "succeeded")
        replay = self.journal.acquire_effect(
            prepared["effect_key"], run_id="run-2", worker_id="worker-2"
        )
        self.assertEqual(replay.action, REUSE)
        self.assertEqual(replay.effect["result"], {"delivery_id": "delivery-1"})

    def test_state_and_transition_write_roll_back_together(self) -> None:
        prepared = self._prepare(run_id="run-1", effect_kind="read")
        with patch.object(
            self.journal,
            "_record_transition",
            side_effect=RuntimeError("injected transition failure"),
        ):
            with self.assertRaisesRegex(RuntimeError, "injected transition failure"):
                self.journal.acquire_effect(
                    prepared["effect_key"], run_id="run-1", worker_id="worker-1"
                )

        after = self.journal.get_effect(prepared["effect_key"])
        self.assertIsNotNone(after)
        self.assertEqual(after["state"], "prepared")
        self.assertEqual(after["attempt_count"], 0)
        self.assertEqual(
            [item["to_state"] for item in self.journal.list_transitions(prepared["effect_key"])],
            ["prepared"],
        )

    def test_crash_restart_replays_idempotent_http_without_duplicate_external_effect(self) -> None:
        sink = IdempotentHttpSink()
        prepared = self._prepare(run_id="run-1", effect_kind="idempotent_write")
        first = self.journal.acquire_effect(
            prepared["effect_key"],
            run_id="run-1",
            worker_id="worker-before-crash",
            lease_seconds=5,
        )
        self.assertEqual(first.action, DISPATCH)
        headers = inject_http_idempotency_key({}, first.effect["idempotency_key"])
        first_response = sink.post(headers, {"message": "hello"})
        self.assertEqual(sink.external_effect_count, 1)

        # Process loss occurs after the remote system commits but before the
        # local journal can call mark_succeeded().  A new service instance
        # represents the restarted platform process.
        restarted = ToolEffectJournal(self._connect, clock=self.clock)
        recovered = restarted.recover_interrupted_executions()
        self.assertEqual([item["state"] for item in recovered], ["unknown"])

        same = restarted.prepare_effect(
            task_id="task-1",
            run_id="run-2",
            goal_spec_hash="goal-hash-1",
            operation_key="plan-v1:deliver",
            server_id="delivery",
            tool_name="send",
            arguments={"message": "hello"},
            effect_kind="idempotent_write",
            safe_arguments={"message": "hello"},
        )
        self.assertEqual(same["effect_key"], prepared["effect_key"])
        retry = restarted.acquire_effect(
            same["effect_key"], run_id="run-2", worker_id="worker-after-crash"
        )
        self.assertEqual(retry.action, DISPATCH)
        retry_headers = inject_http_idempotency_key({}, retry.effect["idempotency_key"])
        second_response = sink.post(retry_headers, {"message": "hello"})
        self.assertEqual(second_response, first_response)
        self.assertEqual(sink.external_effect_count, 1)

        restarted.mark_succeeded(
            same["effect_key"],
            lease_token=retry.lease_token,
            result=second_response,
            external_ref=str(second_response["delivery_id"]),
        )
        replay = restarted.acquire_effect(
            same["effect_key"], run_id="run-3", worker_id="worker-later"
        )
        self.assertEqual(replay.action, REUSE)
        self.assertEqual(sink.external_effect_count, 1)
        self.assertEqual(
            [item["to_state"] for item in restarted.list_transitions(same["effect_key"])],
            ["prepared", "executing", "unknown", "prepared", "executing", "succeeded"],
        )

    def test_crash_restart_holds_unknown_artifact_for_manual_reconciliation(self) -> None:
        generator = DocumentGenerator(Path(self.temp_dir.name))
        prepared = self._prepare(
            run_id="run-1",
            effect_kind="artifact_write",
            operation_key="plan-v1:document",
            server_id="report",
            tool_name="generate_document",
            arguments={"title": "Report", "content": "Only once", "format": "docx"},
        )
        first = self.journal.acquire_effect(
            prepared["effect_key"], run_id="run-1", worker_id="artifact-worker"
        )
        self.assertEqual(first.action, DISPATCH)
        artifact = generator.generate("Only once")
        self.assertEqual(generator.invocation_count, 1)

        # Crash before mark_succeeded.  Unlike the idempotent HTTP case, the
        # document generator has no external idempotency contract, so startup
        # must not invoke it again automatically.
        restarted = ToolEffectJournal(self._connect, clock=self.clock)
        restarted.recover_interrupted_executions()
        same = restarted.prepare_effect(
            task_id="task-1",
            run_id="run-2",
            goal_spec_hash="goal-hash-1",
            operation_key="plan-v1:document",
            server_id="report",
            tool_name="generate_document",
            arguments={"title": "Report", "content": "Only once", "format": "docx"},
            effect_kind="artifact_write",
            safe_arguments={"title": "Report", "format": "docx"},
        )
        decision = restarted.acquire_effect(
            same["effect_key"], run_id="run-2", worker_id="artifact-worker-2"
        )
        self.assertEqual(decision.action, RECONCILE)
        self.assertTrue(decision.requires_reconciliation)
        self.assertEqual(generator.invocation_count, 1)
        self.assertEqual(len(list(Path(self.temp_dir.name).glob("artifact-*.docx"))), 1)

        reconciled = restarted.reconcile_unknown(
            same["effect_key"],
            outcome="succeeded",
            note="operator verified the existing file and artifact record",
            run_id="run-2",
            result={"artifact": artifact},
            artifact_id=artifact["id"],
            artifact_sha256=artifact["sha256"],
            external_ref=artifact["path"],
        )
        self.assertEqual(reconciled["state"], "succeeded")
        replay = restarted.acquire_effect(
            same["effect_key"], run_id="run-3", worker_id="artifact-worker-3"
        )
        self.assertEqual(replay.action, REUSE)
        self.assertEqual(replay.effect["artifact_id"], artifact["id"])
        self.assertEqual(generator.invocation_count, 1)


if __name__ == "__main__":
    unittest.main()
