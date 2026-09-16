import asyncio
import json
import os
import subprocess
import unittest
import uuid
from unittest.mock import patch
from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.services import task_queue


@unittest.skipUnless(os.getenv('APP_TEST_REDIS_CONTAINER') and os.getenv('APP_TEST_REDIS_URL'), '需要显式指定独立 Redis 测试容器')
class RedisRestartTests(unittest.IsolatedAsyncioTestCase):
    async def test_aof_restart_preserves_unacknowledged_delivery(self):
        container = os.environ['APP_TEST_REDIS_CONTAINER']
        metadata = json.loads(subprocess.run(['docker','inspect',container],capture_output=True,text=True,check=True).stdout)[0]
        self.assertEqual(metadata['Config']['Labels'].get('agentnexus.test'),'true')
        namespace = 'redis-restart-test-' + uuid.uuid4().hex
        with patch.dict(os.environ,{'REDIS_URL':os.environ['APP_TEST_REDIS_URL']}), patch.object(task_queue,'QUEUE_NAME',namespace), patch.object(task_queue,'STREAM_NAME',namespace+':stream'):
            async with Redis.from_url(os.environ['APP_TEST_REDIS_URL'],decode_responses=True) as client:
                message = {'task_id':'task-a','run_id':'run-a'}
                try:
                    await task_queue.enqueue(message)
                    before = await task_queue.dequeue(client,consumer='before-restart')
                    process = await asyncio.create_subprocess_exec('docker','restart',container,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
                    _,error = await process.communicate()
                    self.assertEqual(process.returncode,0,error.decode())
                    # 随机宿主机映射可能在重启后变化；容器身份和 AOF 数据保持不变。
                    address = subprocess.run(['docker','port',container,'6379'],capture_output=True,text=True,check=True).stdout.strip().splitlines()[0]
                    await client.aclose()
                    client = Redis.from_url('redis://'+address+'/0',decode_responses=True)
                    self.addAsyncCleanup(client.aclose)
                    os.environ['REDIS_URL'] = 'redis://'+address+'/0'
                    for _ in range(50):
                        try:
                            await client.ping()
                            break
                        except RedisError:
                            await asyncio.sleep(0.1)
                    recovered = await task_queue.dequeue(client,consumer='after-restart',idle_ms=0)
                    self.assertEqual(recovered,before)
                    await task_queue.enqueue(message)
                    self.assertEqual(await client.xlen(task_queue.STREAM_NAME),1)
                    await task_queue.acknowledge(client,recovered)
                    self.assertEqual((await client.xpending(task_queue.STREAM_NAME,task_queue.GROUP_NAME))['pending'],0)
                finally:
                    keys = [key async for key in client.scan_iter(match=namespace+'*')]
                    if keys:
                        await client.delete(*keys)
