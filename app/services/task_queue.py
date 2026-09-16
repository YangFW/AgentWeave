"""可选 Redis 任务队列。

Redis 未配置时由调用方继续使用进程内调度，保证本地开发兼容性。
"""
from __future__ import annotations

import json
import os
import asyncio
import logging
import hashlib
from typing import Any

QUEUE_NAME = os.getenv("APP_TASK_QUEUE", "agentnexus:tasks")
STREAM_NAME = f"{QUEUE_NAME}:stream:v1"
GROUP_NAME = "agent-workers"
MAX_RETRIES = max(0, int(os.getenv("APP_TASK_MAX_RETRIES", "2")))
OUTBOX_SCHEMA = """CREATE TABLE IF NOT EXISTS dispatch_outbox (
    run_id TEXT PRIMARY KEY, task_id TEXT NOT NULL, payload_json TEXT NOT NULL,
    delivered INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL
)"""
APPROVAL_OUTBOX_SCHEMA = """CREATE TABLE IF NOT EXISTS approval_dispatch (
    command_id TEXT PRIMARY KEY, task_id TEXT NOT NULL, run_id TEXT NOT NULL,
    delivered INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL
)"""
AUTOMATION_SCHEMA = """CREATE TABLE IF NOT EXISTS automation_dispatch (
    job_id TEXT PRIMARY KEY, loop_id TEXT NOT NULL, payload_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued', delivered INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
)"""
MEMBER_RETRY_SCHEMA = """CREATE TABLE IF NOT EXISTS member_retry_dispatch (
    job_id TEXT PRIMARY KEY, team_run_id TEXT NOT NULL, payload_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued', delivered INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
)"""


def redis_url() -> str:
    return os.getenv("REDIS_URL", "").strip()


def enabled() -> bool:
    return bool(redis_url())


def worker_key(worker_id: str) -> str:
    return f"{QUEUE_NAME}:worker:{worker_id}"


def user_slot_key(organization_id: str, user_id: str) -> str:
    scope = hashlib.sha256(json.dumps([organization_id, user_id]).encode()).hexdigest()
    return f'{QUEUE_NAME}:user-slots:{scope}'


async def acquire_user_slot(client: Any, key: str, token: str, limit: int, *, lease_ms: int = 60000) -> bool:
    return bool(await client.eval(
        "local t=redis.call('TIME'); local now=t[1]*1000+math.floor(t[2]/1000); "
        "redis.call('ZREMRANGEBYSCORE',KEYS[1],'-inf',now); "
        "if redis.call('ZCARD',KEYS[1])>=tonumber(ARGV[2]) then return 0 end; "
        "redis.call('ZADD',KEYS[1],now+tonumber(ARGV[3]),ARGV[1]); "
        "if redis.call('PTTL',KEYS[1])<tonumber(ARGV[3])*2 then redis.call('PEXPIRE',KEYS[1],tonumber(ARGV[3])*2) end; return 1",
        1, key, token, limit, lease_ms,
    ))


async def renew_user_slot(client: Any, key: str, token: str, *, lease_ms: int = 60000) -> bool:
    return bool(await client.eval(
        "local t=redis.call('TIME'); local now=t[1]*1000+math.floor(t[2]/1000); "
        "local score=redis.call('ZSCORE',KEYS[1],ARGV[1]); "
        "if not score or tonumber(score)<=now then return 0 end; "
        "redis.call('ZADD',KEYS[1],now+tonumber(ARGV[2]),ARGV[1]); "
        "if redis.call('PTTL',KEYS[1])<tonumber(ARGV[2])*2 then redis.call('PEXPIRE',KEYS[1],tonumber(ARGV[2])*2) end; return 1",
        1, key, token, lease_ms,
    ))


async def defer_message(client: Any, payload: dict[str, Any], consumer: str, *, idle_ms: int = 60000) -> None:
    # 五秒后可重领，保留 PEL；容量不足时仍继续读取其他用户的新消息。
    await client.xclaim(STREAM_NAME, GROUP_NAME, consumer, 0, [payload['_message_id']], idle=max(0, idle_ms - 5000), justid=True)


def readiness() -> dict[str, Any]:
    if not enabled():
        return {"ready": True, "mode": "local", "workers_online": 0}
    from redis import Redis
    from redis.exceptions import RedisError
    try:
        with Redis.from_url(redis_url(), socket_connect_timeout=1, socket_timeout=1) as client:
            client.ping()
            workers = sum(1 for _ in client.scan_iter(match=worker_key('*'), count=100))
            return {"ready": workers > 0, "mode": "redis", "redis": True, "workers_online": workers}
    except RedisError:
        return {"ready": False, "mode": "redis", "redis": False, "workers_online": 0}


async def enqueue(payload: dict[str, Any]) -> None:
    if not enabled():
        return
    from redis.asyncio import Redis

    client = Redis.from_url(redis_url(), decode_responses=True)
    try:
        await client.eval(
            "local old=redis.call('GET',KEYS[2]); if old then return old end; "
            "local id=redis.call('XADD',KEYS[1],'*','payload',ARGV[1]); "
            "redis.call('SET',KEYS[2],id); return id",
            2, STREAM_NAME, f"{STREAM_NAME}:sent:{payload.get('dispatch_id') or payload['run_id']}",
            json.dumps({"version": 1, "attempt": 1, "priority": 50, "workspace_id": "default", **payload}, ensure_ascii=False),
        )
    finally:
        await client.aclose()


def with_database_scope(payload: dict[str, Any]) -> dict[str, Any]:
    from app import db
    table, column, resource_id = ('loops', 'workspace_id', payload.get('loop_id')) if payload.get('kind') == 'automation' else ('tasks', 'workspace', payload.get('task_id'))
    # TaskState 可独立用于无 tasks 表的单元环境；正式平台始终有资源表。
    if db.query_one("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)):
        row = db.query_one(f'SELECT {column} AS workspace_id FROM {table} WHERE id=?', (resource_id,))
        if row:
            return {**payload, 'workspace_id': row['workspace_id']}
    return payload


async def dispatch_pending() -> None:
    from app import db
    for row in db.query_all("SELECT * FROM dispatch_outbox WHERE delivered=0 ORDER BY created_at LIMIT 100"):
        await enqueue(with_database_scope(json.loads(row["payload_json"])))
        db.execute("UPDATE dispatch_outbox SET delivered=1 WHERE run_id=?", (row["run_id"],))
    for row in db.query_all("SELECT * FROM approval_dispatch WHERE delivered=0 ORDER BY created_at LIMIT 100"):
        await enqueue(with_database_scope({"task_id": row["task_id"], "run_id": row["run_id"], "kind": "approval", "command_id": row["command_id"], "dispatch_id": f"approval:{row['command_id']}"}))
        db.execute("UPDATE approval_dispatch SET delivered=1 WHERE command_id=?", (row["command_id"],))
    for row in db.query_all("SELECT * FROM automation_dispatch WHERE delivered=0 AND status='queued' ORDER BY created_at LIMIT 100"):
        await enqueue(with_database_scope(json.loads(row['payload_json'])))
        db.execute("UPDATE automation_dispatch SET delivered=1 WHERE job_id=?", (row['job_id'],))
    for row in db.query_all("SELECT * FROM member_retry_dispatch WHERE delivered=0 AND status='queued' ORDER BY created_at LIMIT 100"):
        await enqueue(with_database_scope(json.loads(row['payload_json'])))
        db.execute("UPDATE member_retry_dispatch SET delivered=1 WHERE job_id=?", (row['job_id'],))


async def reconcile_queued_dispatches() -> None:
    """Redis 恢复后补投各类未开始的工作，不接管活动执行。"""
    from app import db
    from redis.asyncio import Redis
    rows = db.query_all("SELECT o.* FROM dispatch_outbox o JOIN task_runs r ON r.id=o.run_id WHERE o.delivered=1 AND r.status='queued' ORDER BY o.created_at LIMIT 100")
    payloads = [json.loads(row['payload_json']) for row in rows]
    approvals = db.query_all("SELECT a.* FROM approval_dispatch a JOIN task_commands c ON c.id=a.command_id JOIN task_runs r ON r.id=a.run_id WHERE a.delivered=1 AND c.status='queued' AND r.status='waiting_approval' ORDER BY a.created_at LIMIT 100")
    for row in approvals:
        payloads.append({'kind':'approval','task_id':row['task_id'],'run_id':row['run_id'],'command_id':row['command_id'],'dispatch_id':f"approval:{row['command_id']}"})
    for table in ('automation_dispatch','member_retry_dispatch'):
        payloads.extend(json.loads(row['payload_json']) for row in db.query_all(f"SELECT payload_json FROM {table} WHERE delivered=1 AND status='queued' ORDER BY created_at LIMIT 100"))
    if not payloads:
        return
    async with Redis.from_url(redis_url(), socket_connect_timeout=1, socket_timeout=2) as client:
        for payload in payloads:
            dispatch_id = payload.get('dispatch_id') or payload['run_id']
            if not await client.exists(f"{STREAM_NAME}:sent:{dispatch_id}"):
                await enqueue(with_database_scope(payload))


async def dispatch_forever() -> None:
    from redis.exceptions import RedisError
    logger = logging.getLogger(__name__)
    while True:
        try:
            await dispatch_pending()
            await reconcile_queued_dispatches()
        except RedisError:
            # 消息仍在 SQLite 中，不记录可能包含连接凭据的异常文本。
            logger.warning("Redis 投递暂不可用，将重试未投递运行记录")
        await asyncio.sleep(2)


async def dequeue(client: Any, timeout: int = 5, *, consumer: str = "worker", idle_ms: int = 60000) -> dict[str, Any] | None:
    from redis.exceptions import ResponseError
    try:
        await client.xgroup_create(STREAM_NAME, GROUP_NAME, id="0", mkstream=True)
    except ResponseError as exc:
        if "BUSYGROUP" not in str(exc):
            raise
    # 未确认消息留在 PEL，消费者退出后由其他 Worker 重新领取。
    reclaimed = await client.xautoclaim(STREAM_NAME, GROUP_NAME, consumer, idle_ms, "0-0", count=1)
    messages = reclaimed[1]
    if not messages:
        batches = await client.xreadgroup(GROUP_NAME, consumer, {STREAM_NAME: ">"}, count=1, block=max(1, timeout * 1000))
        messages = batches[0][1] if batches else []
    if not messages:
        return None
    message_id, fields = messages[0]
    try:
        payload = json.loads(fields["payload"])
        if not isinstance(payload, dict) or payload.get("version") != 1:
            raise ValueError("无效队列协议")
        valid_ids = (payload.get('job_id') and payload.get('loop_id')) if payload.get('kind') == 'automation' else (payload.get('task_id') and payload.get('run_id'))
        if not valid_ids:
            raise ValueError("无效队列协议")
    except (ValueError, KeyError, TypeError):
        async with client.pipeline(transaction=True) as pipe:
            pipe.xadd(f"{STREAM_NAME}:dead", {"message_id": message_id, "reason": "invalid_envelope"})
            pipe.xack(STREAM_NAME, GROUP_NAME, message_id)
            pipe.xdel(STREAM_NAME, message_id)
            await pipe.execute()
        return None
    return {**payload, "_message_id": message_id}


async def acknowledge(client: Any, payload: dict[str, Any]) -> None:
    async with client.pipeline(transaction=True) as pipe:
        pipe.xack(STREAM_NAME, GROUP_NAME, payload["_message_id"])
        pipe.xdel(STREAM_NAME, payload["_message_id"])
        await pipe.execute()


async def retry_or_dead_letter(client: Any, payload: dict[str, Any]) -> None:
    attempt = int(payload.get("attempt", 1))
    next_payload = {key: value for key, value in payload.items() if key != "_message_id"}
    next_payload["attempt"] = attempt + 1
    destination = STREAM_NAME if attempt <= MAX_RETRIES else f"{STREAM_NAME}:dead"
    async with client.pipeline(transaction=True) as pipe:
        pipe.xadd(destination, {"payload": json.dumps(next_payload, ensure_ascii=False)})
        pipe.xack(STREAM_NAME, GROUP_NAME, payload["_message_id"])
        pipe.xdel(STREAM_NAME, payload["_message_id"])
        await pipe.execute()


async def renew_lock(client: Any, key: str, owner: str, ttl_ms: int = 60000) -> bool:
    return bool(await client.eval("if redis.call('GET',KEYS[1]) == ARGV[1] then return redis.call('PEXPIRE',KEYS[1],ARGV[2]) else return 0 end", 1, key, owner, ttl_ms))


async def release_lock(client: Any, key: str, owner: str) -> None:
    await client.eval("if redis.call('GET',KEYS[1]) == ARGV[1] then return redis.call('DEL',KEYS[1]) else return 0 end", 1, key, owner)


def lock_name(run_id: str) -> str:
    return f"{QUEUE_NAME}:lock:{run_id}"


def cancel_name(run_id: str) -> str:
    return f"{QUEUE_NAME}:cancel:{run_id}"


async def request_cancel(run_id: str) -> None:
    if not enabled() or not run_id:
        return
    from redis.asyncio import Redis
    from redis.exceptions import RedisError
    client = Redis.from_url(redis_url(), decode_responses=True, socket_connect_timeout=1, socket_timeout=1)
    try:
        await client.set(cancel_name(run_id), "1", ex=3600)
    except RedisError:
        logging.getLogger(__name__).warning('取消通知暂不可用，已持久化的取消命令仍由运行时处理')
    finally:
        await client.aclose()
