import unittest
from unittest.mock import AsyncMock, patch
from redis.exceptions import ConnectionError
from app import worker


class WorkerSupervisorTests(unittest.IsolatedAsyncioTestCase):
    async def test_reconnects_after_redis_failure(self):
        with patch.object(worker, 'main', AsyncMock(side_effect=[ConnectionError('test'), None])) as main, patch.object(worker.asyncio, 'sleep', AsyncMock()) as sleep:
            await worker.supervise()
        self.assertEqual(main.await_count, 2)
        sleep.assert_awaited_once_with(2)

    async def test_programming_error_is_not_hidden_by_retry(self):
        with patch.object(worker, 'main', AsyncMock(side_effect=ValueError('test'))):
            with self.assertRaises(ValueError):
                await worker.supervise()
