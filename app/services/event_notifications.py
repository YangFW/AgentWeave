"""Redis 仅传递事件游标提示，SSE 正文始终从 SQLite 读取。"""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

from app import db
from app.services import task_queue

logger = logging.getLogger(__name__)


def channel(task_id: str) -> str:
    return f"{task_queue.QUEUE_NAME}:events:{task_id}"


async def relay_once(client, after_id: int) -> int:
    rows = db.query_all("SELECT id,task_id FROM task_events WHERE id>? ORDER BY id LIMIT 500", (after_id,))
    latest = {}
    for row in rows:
        latest[row['task_id']] = row['id']
    for task_id, cursor in latest.items():
        await client.publish(channel(task_id), str(cursor))
    return rows[-1]['id'] if rows else after_id


async def relay_forever() -> None:
    from redis.asyncio import Redis
    from redis.exceptions import RedisError
    cursor = db.query_one("SELECT COALESCE(MAX(id),0) AS cursor FROM task_events")['cursor']
    async with Redis.from_url(task_queue.redis_url(), socket_connect_timeout=1, socket_timeout=2) as client:
        while True:
            try:
                cursor = await relay_once(client, cursor)
            except RedisError:
                logger.warning('实时事件通知暂不可用，SSE 将从数据库补读')
                await asyncio.sleep(1)
            await asyncio.sleep(0.1)


@asynccontextmanager
async def notification_waiter(task_id: str):
    if not task_queue.enabled():
        async def poll():
            await asyncio.sleep(0.1)
        yield poll
        return
    from redis.asyncio import Redis
    from redis.exceptions import RedisError
    client = Redis.from_url(task_queue.redis_url(), socket_connect_timeout=1, socket_timeout=2)
    pubsub = client.pubsub()
    subscribed = False

    async def wait():
        nonlocal subscribed
        try:
            if not subscribed:
                await pubsub.subscribe(channel(task_id))
                subscribed = True
            # 超时后仍读数据库，处理通知丢失、重连窗口和权限撤销。
            await pubsub.get_message(ignore_subscribe_messages=True, timeout=1)
        except RedisError:
            subscribed = False
            await asyncio.sleep(0.1)

    try:
        yield wait
    finally:
        try:
            await pubsub.aclose()
        finally:
            await client.aclose()
