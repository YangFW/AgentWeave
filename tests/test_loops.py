from __future__ import annotations

import asyncio
import hashlib
import hmac
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException
from starlette.requests import Request

from app import db
from app import main as main_module
from app.main import create_loop
from app.schemas import LoopCreate
from app.seed import seed_agents
from app.services.agent_runtime import create_task_record
from app.services.loop_scheduler import LoopScheduler, next_cron_time, serialize_loop
from app.services.secret_store import secret_store


class FakeRuntime:
    def __init__(
        self,
        succeed: bool = True,
        gate: asyncio.Event | None = None,
        final_status: str = "",
    ) -> None:
        self.succeed = succeed
        self.gate = gate
        self.final_status = final_status

    async def run_task(self, task_id: str, **_: object) -> None:
        if self.gate:
            await self.gate.wait()
        if self.final_status == "waiting_approval":
            db.update_task_status(
                task_id,
                "waiting_approval",
                result={"pending_action": "policy_approval"},
                artifacts=[],
            )
        elif self.succeed:
            db.update_task_status(task_id, "completed", result={"summary": "本轮完成"}, artifacts=[])
        else:
            db.update_task_status(task_id, "failed", result={"error": "模拟失败"}, artifacts=[])


class SequenceRuntime:
    def __init__(self, outcomes: list[tuple[str, dict]]) -> None:
        self.outcomes = list(outcomes)
        self.calls = 0

    async def run_task(self, task_id: str, **_: object) -> None:
        status, result = self.outcomes[min(self.calls, len(self.outcomes) - 1)]
        self.calls += 1
        db.update_task_status(task_id, status, result=result, artifacts=[])


def signed_request(body: bytes, secret: str, idempotency_key: str, timestamp: str | None = None) -> Request:
    stamp = timestamp or str(int(time.time()))
    signature = hmac.new(secret.encode(), stamp.encode() + b"." + body, hashlib.sha256).hexdigest()
    headers = [
        (b"x-automation-timestamp", stamp.encode()),
        (b"x-automation-signature", f"sha256={signature}".encode()),
        (b"idempotency-key", idempotency_key.encode()),
    ]
    sent = False

    async def receive() -> dict:
        nonlocal sent
        if sent:
            return {"type": "http.disconnect"}
        sent = True
        return {"type": "http.request", "body": body, "more_body": False}

    return Request({"type": "http", "method": "POST", "path": "/", "headers": headers}, receive)


class LoopSchedulerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.original_db_path = db.DB_PATH
        self.original_key_file = secret_store.key_file
        self.temp_dir = tempfile.TemporaryDirectory()
        db.DB_PATH = Path(self.temp_dir.name) / "loops.db"
        secret_store.key_file = Path(self.temp_dir.name) / ".secret-key"
        db.init_db()
        seed_agents()

    async def asyncTearDown(self) -> None:
        db.DB_PATH = self.original_db_path
        secret_store.key_file = self.original_key_file
        self.temp_dir.cleanup()

    def add_loop(self, *, loop_id: str = "loop-test", status: str = "paused", max_runs: int = 3, max_failures: int = 2) -> None:
        now = db.utc_now()
        db.execute(
            """INSERT INTO loops(id, name, prompt, agent_id, model_id, interval_seconds, status,
               max_runs, max_failures, next_run_at, created_at, updated_at)
               VALUES (?, '测试循环', '检查平台并输出摘要', 'general-agent', 'deterministic', 5, ?, ?, ?, '', ?, ?)""",
            (loop_id, status, max_runs, max_failures, now, now),
        )

    async def test_manual_run_creates_real_task_and_history(self) -> None:
        self.add_loop()
        run = await LoopScheduler(FakeRuntime()).run_once("loop-test")
        loop = db.query_one("SELECT * FROM loops WHERE id = 'loop-test'")
        task = db.query_one("SELECT * FROM tasks WHERE id = ?", (run["task_id"],))
        task_run = db.query_one("SELECT * FROM task_runs WHERE id = ?", (run["task_run_id"],))
        run_metadata = db.json_loads(task_run["metadata_json"], {})
        self.assertEqual(run["status"], "completed")
        self.assertEqual(loop["run_count"], 1)
        self.assertEqual(loop["status"], "paused")
        self.assertEqual(task["status"], "completed")
        self.assertEqual(task["executor_type"], "automation")
        self.assertEqual(task["executor_id"], "loop-test")
        self.assertEqual(task["workspace"], "loop:loop-test")
        self.assertEqual(task["conversation_id"], "loop_loop-test")
        self.assertEqual(task_run["task_id"], task["id"])
        self.assertEqual(run_metadata["executor_type"], "automation")
        self.assertEqual(run_metadata["executor_id"], "loop-test")
        self.assertEqual(run_metadata["automation_run_id"], run["id"])
        self.assertEqual(
            len(db.query_all("SELECT id FROM task_runs WHERE task_id = ?", (task["id"],))),
            1,
        )

    async def test_automation_attempt_creation_rolls_back_as_one_unit(self) -> None:
        main_module.task_state.init_schema()
        self.add_loop()
        db.execute(
            """
            CREATE TRIGGER fail_automation_task_run_insert
            BEFORE INSERT ON task_runs
            WHEN NEW.id LIKE 'trun_%'
            BEGIN
                SELECT RAISE(ABORT, 'injected automation task_run failure');
            END
            """
        )
        with self.assertRaises(Exception):
            await LoopScheduler(FakeRuntime()).run_once("loop-test")
        self.assertEqual(db.query_all("SELECT * FROM loop_runs"), [])
        self.assertEqual(
            db.query_all("SELECT * FROM tasks WHERE executor_type = 'automation'"),
            [],
        )
        loop = db.query_one("SELECT status FROM loops WHERE id = 'loop-test'")
        self.assertEqual(loop["status"], "failed")

    async def test_max_runs_stops_loop(self) -> None:
        self.add_loop(max_runs=1)
        scheduler = LoopScheduler(FakeRuntime())
        await scheduler.run_once("loop-test")
        loop = db.query_one("SELECT status, next_run_at FROM loops WHERE id = 'loop-test'")
        self.assertEqual(loop["status"], "completed")
        self.assertEqual(loop["next_run_at"], "")
        with self.assertRaises(RuntimeError):
            await scheduler.run_once("loop-test")

    async def test_consecutive_failure_trips_circuit_breaker(self) -> None:
        self.add_loop(status="active", max_failures=1)
        run = await LoopScheduler(FakeRuntime(succeed=False)).run_once("loop-test", scheduled=True)
        loop = db.query_one("SELECT status, consecutive_failures, next_run_at FROM loops WHERE id = 'loop-test'")
        self.assertEqual(run["status"], "failed")
        self.assertEqual(loop["status"], "failed")
        self.assertEqual(loop["consecutive_failures"], 1)
        self.assertEqual(loop["next_run_at"], "")

    async def test_same_loop_cannot_reenter(self) -> None:
        self.add_loop()
        gate = asyncio.Event()
        scheduler = LoopScheduler(FakeRuntime(gate=gate))
        first = asyncio.create_task(scheduler.run_once("loop-test"))
        await asyncio.sleep(0)
        with self.assertRaises(RuntimeError):
            await scheduler.run_once("loop-test")
        gate.set()
        await first
        self.assertEqual(len(db.query_all("SELECT id FROM loop_runs WHERE loop_id = 'loop-test'")), 1)

    async def test_waiting_approval_pauses_without_counting_as_failure(self) -> None:
        self.add_loop(status="active", max_failures=1)
        run = await LoopScheduler(
            FakeRuntime(final_status="waiting_approval")
        ).run_once("loop-test", scheduled=True)
        loop = db.query_one(
            "SELECT status, consecutive_failures, next_run_at FROM loops WHERE id = 'loop-test'"
        )
        self.assertEqual(run["status"], "waiting_approval")
        self.assertEqual(loop["status"], "waiting_approval")
        self.assertEqual(loop["consecutive_failures"], 0)
        self.assertEqual(loop["next_run_at"], "")

    async def test_dispatch_reserves_loop_before_coroutine_runs(self) -> None:
        self.add_loop()
        gate = asyncio.Event()
        scheduler = LoopScheduler(FakeRuntime(gate=gate))
        first = scheduler.dispatch_once("loop-test")
        with self.assertRaises(RuntimeError):
            scheduler.dispatch_once("loop-test")
        gate.set()
        await first

    async def test_last_failed_logical_run_is_failed_not_completed(self) -> None:
        self.add_loop(status="active", max_runs=1, max_failures=3)
        run = await LoopScheduler(FakeRuntime(succeed=False)).run_once("loop-test", scheduled=True)
        loop = db.query_one("SELECT status, run_count FROM loops WHERE id = 'loop-test'")
        self.assertEqual(run["status"], "failed")
        self.assertEqual(loop["status"], "failed")
        self.assertEqual(loop["run_count"], 1)
        self.assertIn("最后一轮", run["decision"]["reason"])

    async def test_retry_attempts_do_not_consume_logical_rounds(self) -> None:
        self.add_loop(status="active")
        db.execute(
            "UPDATE loops SET max_attempts = 2, state_json = ? WHERE id = 'loop-test'",
            (db.json_dumps({"count": 0}),),
        )
        runtime = SequenceRuntime([
            ("failed", {"error": "temporary"}),
            ("completed", {"summary": "ok", "automation_state": {"count": 1}}),
        ])
        final = await LoopScheduler(runtime).run_once("loop-test", scheduled=True)
        rows = db.query_all(
            "SELECT run_number, attempt, status FROM loop_runs WHERE loop_id = 'loop-test' ORDER BY attempt"
        )
        loop = db.query_one("SELECT run_count, state_json, last_diff_json FROM loops WHERE id = 'loop-test'")
        self.assertEqual([(row["run_number"], row["attempt"]) for row in rows], [(1, 1), (1, 2)])
        self.assertEqual([row["status"] for row in rows], ["failed", "completed"])
        self.assertEqual(loop["run_count"], 1)
        self.assertEqual(final["attempt"], 2)
        self.assertEqual(db.json_loads(loop["state_json"], {}), {"count": 1})
        self.assertTrue(db.json_loads(loop["last_diff_json"], {})["changed"])

    async def test_recovery_closes_orphaned_loop_run_and_pauses_parent(self) -> None:
        self.add_loop(status="running")
        now = db.utc_now()
        db.execute(
            """INSERT INTO loop_runs(id, loop_id, task_id, run_number, status, started_at)
               VALUES ('run-orphan', 'loop-test', 'task-missing', 1, 'running', ?)""",
            (now,),
        )
        interrupted = LoopScheduler(FakeRuntime()).recover_interrupted_runs()
        run = db.query_one("SELECT status, error_json FROM loop_runs WHERE id = 'run-orphan'")
        loop = db.query_one("SELECT status, run_count, next_run_at FROM loops WHERE id = 'loop-test'")
        self.assertEqual(interrupted, {"task-missing"})
        self.assertEqual(run["status"], "failed")
        self.assertEqual(db.json_loads(run["error_json"], {})["error_type"], "ServiceRestart")
        self.assertEqual(loop["status"], "paused")
        self.assertEqual(loop["run_count"], 1)
        self.assertEqual(loop["next_run_at"], "")
        self.assertEqual(len(db.query_all("SELECT id FROM notifications")), 1)

    async def test_startup_does_not_restart_interrupted_automation_task(self) -> None:
        main_module.task_state.init_schema()
        self.add_loop(status="running")
        task = create_task_record("automation child", "general-agent")
        task_run = main_module.task_state.create_run(task["id"])
        db.execute(
            """INSERT INTO loop_runs(id, loop_id, task_id, run_number, status, started_at)
               VALUES ('run-restart', 'loop-test', ?, 1, 'running', ?)""",
            (task["id"], db.utc_now()),
        )
        interrupted = LoopScheduler(FakeRuntime()).recover_interrupted_runs()
        recovered = main_module._recover_interrupted_runs(interrupted)
        stored_task = db.query_one("SELECT status FROM tasks WHERE id = ?", (task["id"],))
        stored_run = main_module.task_state.get_run(task_run["id"])
        self.assertEqual(recovered, [])
        self.assertEqual(stored_task["status"], "failed")
        self.assertEqual(stored_run["status"], "cancelled")

    async def test_generic_recovery_never_claims_durable_automation_owner(self) -> None:
        main_module.task_state.init_schema()
        self.add_loop(status="running")
        task = create_task_record(
            "automation child",
            "general-agent",
            workspace="loop:loop-test",
            conversation_id="loop_loop-test",
            executor_type="automation",
            executor_id="loop-test",
        )
        task_run = main_module.task_state.create_run(
            task["id"],
            metadata={
                "executor_type": "automation",
                "executor_id": "loop-test",
                "automation_run_id": "run-durable-owner",
            },
        )
        main_module.task_state.begin_run(task["id"], run_id=task_run["id"], activate_task_projection=True)
        db.execute(
            """INSERT INTO loop_runs(
                   id, loop_id, task_id, task_run_id, run_number, status, started_at
               ) VALUES ('run-durable-owner', 'loop-test', ?, ?, 1, 'running', ?)""",
            (task["id"], task_run["id"], db.utc_now()),
        )
        recovered_once = main_module._recover_interrupted_runs()
        recovered_twice = main_module._recover_interrupted_runs()
        stored_task = db.query_one("SELECT status, executor_type FROM tasks WHERE id = ?", (task["id"],))
        stored_run = main_module.task_state.get_run(task_run["id"])
        self.assertEqual(recovered_once, [])
        self.assertEqual(recovered_twice, [])
        self.assertEqual(stored_task["executor_type"], "automation")
        self.assertEqual(stored_task["status"], "running")
        self.assertEqual(stored_run["status"], "running")

    async def test_once_trigger_completes_after_single_success(self) -> None:
        self.add_loop(status="active")
        db.execute(
            "UPDATE loops SET trigger_type = 'once', once_at = ? WHERE id = 'loop-test'",
            (db.utc_now(),),
        )
        await LoopScheduler(FakeRuntime()).run_once("loop-test", scheduled=True)
        loop = db.query_one("SELECT status, run_count, next_run_at FROM loops WHERE id = 'loop-test'")
        self.assertEqual(loop["status"], "completed")
        self.assertEqual(loop["run_count"], 1)
        self.assertEqual(loop["next_run_at"], "")

    def test_cron_supports_steps_and_returns_future_utc_time(self) -> None:
        current = main_module.datetime(2026, 8, 11, 3, 7, tzinfo=main_module.timezone.utc)
        upcoming = next_cron_time("0/15 * * * *", current)
        self.assertEqual(upcoming.isoformat(), "2026-08-11T03:15:00+00:00")

    def test_loop_serialization_whitelists_encrypted_secret(self) -> None:
        self.add_loop()
        ciphertext = secret_store.encrypt("webhook-secret-value")
        db.execute(
            "UPDATE loops SET trigger_type = 'webhook', webhook_secret_ciphertext = ? WHERE id = 'loop-test'",
            (ciphertext,),
        )
        serialized = serialize_loop(db.query_one("SELECT * FROM loops WHERE id = 'loop-test'") or {})
        self.assertTrue(serialized["webhook_secret_configured"])
        self.assertNotIn("webhook_secret_ciphertext", serialized)
        self.assertNotIn(ciphertext, str(serialized))
        self.assertNotIn("webhook-secret-value", str(serialized))

    async def test_signed_webhook_is_idempotent_and_encrypts_payload(self) -> None:
        secret = "webhook-secret-value"
        created = create_loop(LoopCreate(
            id="webhook-test",
            name="Webhook test",
            prompt="process",
            trigger_type="webhook",
            webhook_secret=secret,
            auto_start=True,
        ))
        self.assertTrue(created["webhook_secret_configured"])
        body = b'{"event":"created","token":"must-not-be-public"}'
        with patch.object(main_module.loop_scheduler, "is_busy", return_value=True):
            first = await main_module.trigger_loop_webhook(
                "webhook-test", signed_request(body, secret, "event-1")
            )
            duplicate = await main_module.trigger_loop_webhook(
                "webhook-test", signed_request(body, secret, "event-1")
            )
            self.assertFalse(first["duplicate"])
            self.assertTrue(duplicate["duplicate"])
            self.assertEqual(first["event"]["id"], duplicate["event"]["id"])
            with self.assertRaises(HTTPException) as caught:
                await main_module.trigger_loop_webhook(
                    "webhook-test", signed_request(b'{"event":"different"}', secret, "event-1")
                )
        self.assertEqual(caught.exception.status_code, 409)
        events = db.query_all("SELECT * FROM automation_trigger_events WHERE loop_id = 'webhook-test'")
        self.assertEqual(len(events), 1)
        self.assertNotIn("must-not-be-public", events[0]["payload_ciphertext"])
        self.assertNotIn("payload_ciphertext", first["event"])


if __name__ == "__main__":
    unittest.main()
