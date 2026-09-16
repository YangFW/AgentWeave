import os
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, patch

from app import db
from app.services import event_notifications, task_queue


class NotificationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        database = patch.object(db, 'DB_PATH', Path(temporary.name) / 'events.db')
        database.start()
        self.addCleanup(database.stop)
        db.execute('CREATE TABLE task_events(id INTEGER PRIMARY KEY,task_id TEXT,content TEXT)')
        db.execute("INSERT INTO task_events VALUES(1,'task-a','private answer')")
        db.execute("INSERT INTO task_events VALUES(2,'task-a','private answer delta')")

    async def test_relay_only_publishes_latest_cursor(self):
        client = AsyncMock()
        self.assertEqual(await event_notifications.relay_once(client, 0), 2)
        client.publish.assert_awaited_once_with(event_notifications.channel('task-a'), '2')
        client.publish.reset_mock()
        self.assertEqual(await event_notifications.relay_once(client, 2), 2)
        client.publish.assert_not_awaited()

    @unittest.skipUnless(os.getenv('APP_TEST_REDIS_URL'), '需要独立测试 Redis')
    async def test_real_pubsub_transports_only_cursor(self):
        from redis.asyncio import Redis
        namespace = 'event-test-' + uuid.uuid4().hex
        with patch.object(task_queue, 'QUEUE_NAME', namespace):
            async with Redis.from_url(os.environ['APP_TEST_REDIS_URL'], decode_responses=True) as client:
                async with client.pubsub() as subscriber:
                    await subscriber.subscribe(event_notifications.channel('task-a'))
                    await subscriber.get_message(timeout=1)
                    await event_notifications.relay_once(client, 0)
                    message = await subscriber.get_message(timeout=1)
                    self.assertEqual(message['type'], 'message')
                    self.assertEqual(message['data'], '2')
