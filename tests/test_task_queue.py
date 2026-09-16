"""队列协议回归；集成测试仅连接显式指定的独立测试 Redis。"""
import json
import os
import unittest
import uuid
import asyncio
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.services import task_queue


class TaskQueueTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancel_notification_failure_does_not_reject_durable_decision(self):
        from redis.exceptions import ConnectionError
        client = AsyncMock()
        client.set.side_effect = ConnectionError('test failure')
        with patch.dict(os.environ, {'REDIS_URL':'redis://unused'}), patch('redis.asyncio.Redis.from_url', return_value=client):
            await task_queue.request_cancel('run-a')
        client.aclose.assert_awaited_once()

    async def test_no_redis_keeps_local_mode(self):
        with patch.dict(os.environ, {"REDIS_URL": ""}):
            self.assertFalse(task_queue.enabled())
            await task_queue.enqueue({"task_id": "task-1"})

    async def test_dequeue_returns_payload(self):
        client = AsyncMock()
        client.xautoclaim.return_value = ["0-0", []]
        client.xreadgroup.return_value = [(task_queue.STREAM_NAME, [("1-0", {"payload": '{"version":1,"task_id":"task-1","run_id":"run-1"}'})])]
        self.assertEqual(await task_queue.dequeue(client), {"version": 1, "task_id": "task-1", "run_id": "run-1", "_message_id": "1-0"})

    async def test_idle_queue_returns_none(self):
        client = AsyncMock()
        client.xautoclaim.return_value = ["0-0", []]
        client.xreadgroup.return_value = []
        self.assertIsNone(await task_queue.dequeue(client))


@unittest.skipUnless(os.getenv("APP_TEST_REDIS_URL"), "需要独立测试 Redis")
class RedisQueueIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from redis.asyncio import Redis
        self.namespace = 'agentnexus-test-' + uuid.uuid4().hex
        self.client = Redis.from_url(os.environ["APP_TEST_REDIS_URL"], decode_responses=True)
        self.addAsyncCleanup(self.client.aclose)
        for name, value in (("QUEUE_NAME", self.namespace), ("STREAM_NAME", self.namespace + ':stream')):
            replacement = patch.object(task_queue, name, value)
            replacement.start()
            self.addCleanup(replacement.stop)
        environment = patch.dict(os.environ, {"REDIS_URL": os.environ["APP_TEST_REDIS_URL"]})
        environment.start()
        self.addCleanup(environment.stop)

    async def asyncTearDown(self):
        keys = [key async for key in self.client.scan_iter(match=self.namespace + '*')]
        if keys:
            await self.client.delete(*keys)

    async def test_unacknowledged_message_survives_consumer_exit(self):
        await task_queue.enqueue({"task_id": "task-a", "run_id": "run-a"})
        first = await task_queue.dequeue(self.client, consumer='worker-a')
        self.assertEqual(first['priority'],50)
        self.assertEqual(first['attempt'],1)
        self.assertEqual(first['workspace_id'],'default')
        self.assertEqual((await self.client.xpending(task_queue.STREAM_NAME, task_queue.GROUP_NAME))["pending"], 1)
        recovered = await task_queue.dequeue(self.client, consumer='worker-b', idle_ms=0)
        self.assertEqual(first, recovered)
        await task_queue.acknowledge(self.client, recovered)
        self.assertEqual((await self.client.xpending(task_queue.STREAM_NAME, task_queue.GROUP_NAME))["pending"], 0)
        self.assertEqual(await self.client.xlen(task_queue.STREAM_NAME), 0)

    async def test_repeated_outbox_delivery_does_not_duplicate_message(self):
        payload = {"task_id": "task-a", "run_id": "run-a"}
        await task_queue.enqueue(payload)
        await task_queue.enqueue(payload)
        self.assertEqual(await self.client.xlen(task_queue.STREAM_NAME), 1)
        claimed = await task_queue.dequeue(self.client)
        await task_queue.acknowledge(self.client, claimed)
        await task_queue.enqueue(payload)
        self.assertEqual(await self.client.xlen(task_queue.STREAM_NAME), 0)

    async def test_retry_is_atomic_and_exhausted_job_is_retained(self):
        await task_queue.enqueue({"task_id": "task-a", "run_id": "run-a", "attempt": task_queue.MAX_RETRIES + 1})
        message = await task_queue.dequeue(self.client)
        await task_queue.retry_or_dead_letter(self.client, message)
        self.assertEqual(await self.client.xlen(task_queue.STREAM_NAME), 0)
        self.assertEqual(await self.client.xlen(task_queue.STREAM_NAME + ':dead'), 1)

    async def test_old_owner_cannot_renew_or_delete_new_lease(self):
        key = task_queue.lock_name('run-a')
        await self.client.set(key, 'owner-b', px=60000)
        self.assertFalse(await task_queue.renew_lock(self.client, key, 'owner-a'))
        await task_queue.release_lock(self.client, key, 'owner-a')
        self.assertEqual(await self.client.get(key), 'owner-b')
        self.assertTrue(await task_queue.renew_lock(self.client, key, 'owner-b'))
        await task_queue.release_lock(self.client, key, 'owner-b')
        self.assertIsNone(await self.client.get(key))

    async def test_readiness_requires_own_queue_worker_heartbeat(self):
        self.assertFalse(task_queue.readiness()['ready'])

        key = task_queue.worker_key('test-worker')
        await self.client.set(key, 'online', ex=30)
        state = task_queue.readiness()
        self.assertTrue(state['ready'])
        self.assertEqual(state['workers_online'], 1)
        with patch.object(task_queue, 'QUEUE_NAME', self.namespace + '-other'):
            self.assertFalse(task_queue.readiness()['ready'])
        await self.client.pexpire(key, 0)
        self.assertFalse(task_queue.readiness()['ready'])

    async def test_user_slots_are_atomic_and_isolated(self):
        key = task_queue.user_slot_key('org', 'alice')
        results = await asyncio.gather(*(task_queue.acquire_user_slot(self.client, key, str(index), 2) for index in range(8)))
        self.assertEqual(sum(results), 2)
        admitted = [str(index) for index, result in enumerate(results) if result]
        self.assertTrue(await task_queue.renew_user_slot(self.client, key, admitted[0]))
        other = task_queue.user_slot_key('org', 'bob')
        self.assertTrue(await task_queue.acquire_user_slot(self.client, other, 'bob-run', 2))
        await self.client.zrem(key, admitted[0])
        self.assertTrue(await task_queue.acquire_user_slot(self.client, key, 'new-run', 2))
        self.assertFalse(await task_queue.renew_user_slot(self.client, key, admitted[0]))

    async def test_crashed_user_slot_expires_with_execution_lease(self):
        key = task_queue.user_slot_key('org', 'crashed-user')
        self.assertTrue(await task_queue.acquire_user_slot(self.client, key, 'old', 1, lease_ms=100))
        self.assertFalse(await task_queue.acquire_user_slot(self.client, key, 'new', 1, lease_ms=100))
        self.assertTrue(await task_queue.renew_user_slot(self.client, key, 'old', lease_ms=100))
        await asyncio.sleep(0.15)
        self.assertTrue(await task_queue.acquire_user_slot(self.client, key, 'new', 1, lease_ms=1000))
        self.assertFalse(await task_queue.renew_user_slot(self.client, key, 'old', lease_ms=100))
        self.assertFalse(await task_queue.acquire_user_slot(self.client, key, 'third', 1, lease_ms=100))

    async def test_short_lease_does_not_expire_other_automation_slot(self):
        key = task_queue.user_slot_key('org', 'mixed-user')
        self.assertTrue(await task_queue.acquire_user_slot(self.client, key, 'automation', 2))
        self.assertTrue(await task_queue.acquire_user_slot(self.client, key, 'task', 2, lease_ms=100))
        self.assertTrue(await task_queue.renew_user_slot(self.client, key, 'task', lease_ms=100))
        self.assertGreater(await self.client.pttl(key), 60000)
        await asyncio.sleep(0.25)
        self.assertIsNotNone(await self.client.zscore(key, 'automation'))
        self.assertTrue(await task_queue.acquire_user_slot(self.client, key, 'new-task', 2, lease_ms=1000))
        self.assertFalse(await task_queue.acquire_user_slot(self.client, key, 'third-task', 2))

    async def test_redis_state_loss_republishes_only_unstarted_run(self):
        from app import db
        from app.services.task_state import TaskStateService
        with tempfile.TemporaryDirectory() as directory, patch.object(db, 'DB_PATH', Path(directory) / 'outbox.db'):
            state = TaskStateService()
            run = state.create_run('task-a', metadata={'dispatch_backend':'redis'})
            await task_queue.dispatch_pending()
            marker = f"{task_queue.STREAM_NAME}:sent:{run['id']}"
            await self.client.delete(task_queue.STREAM_NAME, marker)
            await task_queue.reconcile_queued_dispatches()
            await task_queue.reconcile_queued_dispatches()
            self.assertEqual(await self.client.xlen(task_queue.STREAM_NAME), 1)
            state.begin_run('task-a', run_id=run['id'])
            await self.client.delete(task_queue.STREAM_NAME, marker)
            await task_queue.reconcile_queued_dispatches()
            self.assertFalse(await self.client.exists(task_queue.STREAM_NAME))

    async def test_queued_cancel_finishes_before_execution(self):
        from app import db
        from app.services.task_state import TaskStateService
        from app.services.agent_runtime import create_task_record
        from app.worker import consume_queued_cancellation
        with tempfile.TemporaryDirectory() as directory, patch.object(db, 'DB_PATH', Path(directory) / 'cancel.db'):
            db.init_db()
            state = TaskStateService()
            task = create_task_record('queued cancellation', 'general-agent')
            run = state.create_run(task['id'])
            await task_queue.enqueue({'task_id':task['id'],'run_id':run['id']})
            message = await task_queue.dequeue(self.client)
            await task_queue.request_cancel(run['id'])
            self.assertFalse(await consume_queued_cancellation(self.client, SimpleNamespace(task_state=state), message))
            state.request_cancel(task['id'], run_id=run['id'])
            self.assertTrue(await consume_queued_cancellation(self.client, SimpleNamespace(task_state=state), message))
            self.assertEqual(state.get_run(run['id'])['status'], 'cancelled')
            state.assert_terminal_clean(task_id=task['id'],run_id=run['id'])
            self.assertFalse(await self.client.exists(task_queue.cancel_name(run['id'])))

    async def test_state_loss_restores_only_pending_approval(self):
        from app import db
        from app.services.task_state import TaskStateService
        with tempfile.TemporaryDirectory() as directory, patch.object(db, 'DB_PATH', Path(directory) / 'approval.db'):
            state = TaskStateService()
            run = state.create_run('task-a', metadata={'dispatch_backend': 'redis'})
            # 定位在审批已提交、Worker 尚未消费的故障时刻。
            db.execute("UPDATE task_runs SET status='waiting_approval' WHERE id=?", (run['id'],))
            state.enqueue_command('task-a', 'approval', run_id=run['id'], command_id='decision-a', payload={'approved': True})
            await task_queue.dispatch_pending()
            marker = f'{task_queue.STREAM_NAME}:sent:approval:decision-a'
            await self.client.delete(task_queue.STREAM_NAME, marker)
            await task_queue.reconcile_queued_dispatches()
            await task_queue.reconcile_queued_dispatches()
            messages = await self.client.xrange(task_queue.STREAM_NAME)
            self.assertEqual(len(messages), 1)
            payload = json.loads(messages[0][1]['payload'])
            self.assertEqual(payload['kind'], 'approval')
            self.assertEqual(payload['command_id'], 'decision-a')
            self.assertEqual(payload['run_id'], run['id'])
            db.execute("UPDATE task_runs SET status='running' WHERE id=?", (run['id'],))
            await self.client.delete(task_queue.STREAM_NAME, marker)
            await task_queue.reconcile_queued_dispatches()
            self.assertFalse(await self.client.exists(task_queue.STREAM_NAME))

    async def test_state_loss_restores_only_unstarted_automation_and_member_retry(self):
        from app import db
        from app.services.task_state import TaskStateService
        with tempfile.TemporaryDirectory() as directory, patch.object(db, 'DB_PATH', Path(directory) / 'jobs.db'):
            TaskStateService()
            for table, column, kind in (
                ('automation_dispatch', 'loop_id', 'automation'),
                ('member_retry_dispatch', 'team_run_id', 'member_retry'),
            ):
                for status in ('queued', 'running', 'completed', 'failed'):
                    job_id = f'{kind}-{status}'
                    payload = {'kind': kind, 'task_id': 'task-a', 'run_id': job_id, 'dispatch_id': job_id}
                    db.execute(
                        f'INSERT INTO {table}(job_id,{column},payload_json,status,delivered,created_at) VALUES(?,?,?,?,1,?)',
                        (job_id, f'resource-{status}', json.dumps(payload), status, db.utc_now()),
                    )
                    await task_queue.enqueue(payload)
            # 只清除本用例的随机命名空间，模拟 Redis 持久状态全部丢失。
            keys = [key async for key in self.client.scan_iter(match=self.namespace + '*')]
            await self.client.delete(*keys)
            await task_queue.reconcile_queued_dispatches()
            await task_queue.reconcile_queued_dispatches()
            messages = await self.client.xrange(task_queue.STREAM_NAME)
            self.assertEqual(len(messages), 2)
            self.assertEqual(
                {json.loads(fields['payload'])['dispatch_id'] for _, fields in messages},
                {'automation-queued', 'member_retry-queued'},
            )



if __name__ == "__main__":
    unittest.main()
