"""AgentNexus 独立 Worker 入口。

Worker 通过 Redis 接收 task_id/run_id，并复用现有 AgentRuntime 执行任务。
"""
from __future__ import annotations

import asyncio
import logging
import os
import socket
import uuid

from redis.asyncio import Redis

from app.services.task_queue import (
    acknowledge, dequeue, enabled, lock_name, redis_url,
    release_lock, renew_lock, retry_or_dead_letter,
    worker_key, user_slot_key, acquire_user_slot, renew_user_slot, defer_message,
    cancel_name,
    MAX_RETRIES,
)

logger = logging.getLogger(__name__)


class WorkerLeaseLost(RuntimeError):
    pass


def recover_worker_attempt(runtime, run_id: str) -> None:
    run = runtime.task_state.get_run(run_id)
    if not run:
        raise RuntimeError('待恢复运行不存在')
    count = int((run.get('metadata') or {}).get('worker_recovery_count') or 0)
    if count >= MAX_RETRIES:
        runtime.task_state.commit_failure(
            task_id=run['task_id'],run_id=run_id,
            error={'error_type':'WorkerRecoveryExhausted','message':'Worker 自动恢复次数已达到上限'},
            result={'summary':'执行进程反复中断，已停止自动恢复，请检查后手动重试。'},
        )
        return
    runtime.task_state.recover_interrupted_attempt(run_id,reason='Worker 中断，已安排检查点恢复',metadata={'dispatch_backend':'redis','worker_recovery_count':count+1})


async def consume_queued_cancellation(client, runtime, payload) -> bool:
    if payload.get('kind') not in {None, 'approval'}:
        return False
    run_id = payload['run_id']
    if not await client.exists(cancel_name(run_id)):
        return False
    key, owner = lock_name(run_id), uuid.uuid4().hex
    if not await client.set(key, owner, nx=True, px=60000):
        return False
    try:
        run = runtime.task_state.get_run(run_id)
        if not run or run['task_id'] != payload['task_id'] or run['status'] != 'queued':
            return False
        if not runtime.task_state.is_cancel_requested(payload['task_id'], run_id=run_id):
            return False
        runtime.task_state.begin_run(payload['task_id'], run_id=run_id, activate_task_projection=True)
        runtime.task_state.commit_cancellation(task_id=payload['task_id'], run_id=run_id, result={'cancelled': True})
        await acknowledge(client, payload)
        await client.delete(cancel_name(run_id))
        return True
    finally:
        await release_lock(client, key, owner)


async def execute_member_retry(client, service, payload, seconds, slot_key, slot_token):
    from app import db
    from app.services.context_service import ExecutionScope
    job = db.query_one('SELECT * FROM member_retry_dispatch WHERE job_id=?', (payload.get('job_id'),))
    if not job:
        raise RuntimeError('成员重试投递记录不存在')
    if job['status'] not in {'queued','running'}:
        await acknowledge(client, payload)
        return
    message = db.json_loads(job['payload_json'], {})
    key, owner = lock_name('member-retry:' + job['team_run_id']), uuid.uuid4().hex
    if not await client.set(key, owner, nx=True, px=60000):
        return
    operation = renewal = None
    deadline = asyncio.timeout(seconds)
    try:
        try:
            if job['status'] == 'running':
                raise RuntimeError('成员重试进程已中断')
            if not execution_authorized(message['task_id']):
                raise PermissionError('成员重试执行权限已撤销')
            db.execute("UPDATE member_retry_dispatch SET status='running' WHERE job_id=?", (job['job_id'],))
            async def keep_lease():
                while True:
                    await asyncio.sleep(10)
                    if not await renew_lock(client,key,owner) or not await renew_user_slot(client,slot_key,slot_token):
                        raise WorkerLeaseLost('成员重试租约已丢失')
                    if not execution_authorized(message['task_id']):
                        raise PermissionError('成员重试执行权限已撤销')
            operation = asyncio.create_task(service.retry_member(job['team_run_id'], message['member_run_id'], ExecutionScope(**message['scope'])))
            renewal = asyncio.create_task(keep_lease())
            async with deadline:
                done, _ = await asyncio.wait({operation,renewal}, return_when=asyncio.FIRST_COMPLETED)
                if renewal in done:
                    await renewal
                await operation
        except Exception as exc:
            if operation:
                operation.cancel()
                await asyncio.gather(operation, return_exceptions=True)
            team = db.query_one('SELECT * FROM team_runs WHERE id=?', (job['team_run_id'],))
            if team:
                run = service.task_state.get_run(team['parent_run_id'])
                if run and run['status'] in {'queued','running','paused','waiting_approval'}:
                    error = {'error_type': 'MemberRetryInterrupted', 'message': '成员重试执行中断，请检查后重新重试'}
                    if deadline.expired():
                        error = {'error_type': 'TaskTimeout', 'message': '成员重试超过执行时间上限'}
                    elif isinstance(exc, PermissionError):
                        error = {'error_type': 'PermissionRevoked', 'message': '成员重试执行权限已撤销'}
                    service.fail_interrupted_run(team['id'], run['id'], error)
            db.execute("UPDATE member_retry_dispatch SET status='failed' WHERE job_id=?", (job['job_id'],))
            logger.warning('成员重试失败：job_id=%s', job['job_id'])
        else:
            db.execute("UPDATE member_retry_dispatch SET status='completed' WHERE job_id=?", (job['job_id'],))
        await acknowledge(client,payload)
    finally:
        children = [item for item in (operation,renewal) if item is not None]
        for child in children:
            child.cancel()
        await asyncio.gather(*children, return_exceptions=True)
        await release_lock(client,key,owner)


async def execute_automation(client, scheduler, payload, seconds: float, slot_key: str, slot_token: str) -> None:
    from app import db
    job = db.query_one('SELECT * FROM automation_dispatch WHERE job_id=?', (payload['job_id'],))
    if not job or job['loop_id'] != payload['loop_id']:
        raise RuntimeError('自动化投递记录不匹配')
    if job['status'] not in {'queued', 'running'}:
        await acknowledge(client, payload)
        return
    key, owner = lock_name('automation:' + job['loop_id']), uuid.uuid4().hex
    if not await client.set(key, owner, nx=True, px=60000):
        return
    operation = renewal = None

    def fail_interrupted(error=None):
        failure = error or {'error_type': 'AutomationInterrupted', 'message': '自动化执行中断，请确认后重试。'}
        others = {row['id'] for row in db.query_all('SELECT id FROM loops WHERE id!=?', (job['loop_id'],))}
        interrupted = scheduler.recover_interrupted_runs(exclude_loop_ids=others, error=failure)
        for task_id in interrupted:
            for run in scheduler.task_state.list_runs(task_id=task_id):
                if run['status'] in {'queued', 'running', 'paused', 'waiting_approval'}:
                    scheduler.task_state.commit_failure(task_id=task_id, run_id=run['id'], error=failure, result={'summary': failure['message']})
        db.execute("UPDATE automation_dispatch SET status='failed' WHERE job_id=?", (job['job_id'],))

    try:
        if job['status'] == 'running':
            fail_interrupted()
            await acknowledge(client, payload)
            return
        loop = db.query_one('SELECT * FROM loops WHERE id=?', (job['loop_id'],))
        if not loop:
            db.execute("UPDATE automation_dispatch SET status='failed' WHERE job_id=?", (job['job_id'],))
            await acknowledge(client, payload)
            return
        if not automation_authorized(loop['id']):
            db.execute("UPDATE loops SET status='paused',next_run_at='' WHERE id=?", (loop['id'],))
            db.execute("UPDATE automation_dispatch SET status='failed' WHERE job_id=?", (job['job_id'],))
            await acknowledge(client, payload)
            return
        options = db.json_loads(job['payload_json'], {})
        db.execute("UPDATE automation_dispatch SET status='running' WHERE job_id=?", (job['job_id'],))
        async def keep_lease():
            while True:
                await asyncio.sleep(10)
                if not await renew_user_slot(client, slot_key, slot_token):
                    raise RuntimeError('用户并发配额租约已丢失')
                if not await renew_lock(client, key, owner):
                    raise WorkerLeaseLost('自动化执行租约已丢失')
                if not automation_authorized(loop['id']):
                    raise PermissionError('自动化执行权限已撤销')
        operation = asyncio.create_task(scheduler.run_once(loop['id'], scheduled=options['scheduled'], trigger_event_id=options['trigger_event_id'], trigger_type=options['trigger_type']))
        renewal = asyncio.create_task(keep_lease())
        deadline = asyncio.timeout(seconds)
        try:
            async with deadline:
                done, _ = await asyncio.wait({operation,renewal}, return_when=asyncio.FIRST_COMPLETED)
                if renewal in done:
                    await renewal
                await operation
        except Exception as exc:
            operation.cancel()
            await asyncio.gather(operation, return_exceptions=True)
            failure = None
            if deadline.expired():
                failure = {'error_type': 'TaskTimeout', 'message': '自动化超过执行时间上限，已暂停后续调度。'}
            elif isinstance(exc, PermissionError):
                failure = {'error_type': 'PermissionRevoked', 'message': '自动化执行权限已撤销。'}
            fail_interrupted(failure)
            logger.warning('自动化运行已失败：job_id=%s', job['job_id'])
        else:
            db.execute("UPDATE automation_dispatch SET status='completed' WHERE job_id=?", (job['job_id'],))
        await acknowledge(client, payload)
    finally:
        children = [item for item in (operation,renewal) if item is not None]
        for child in children:
            child.cancel()
        await asyncio.gather(*children, return_exceptions=True)
        await release_lock(client, key, owner)


def automation_authorized(loop_id: str) -> bool:
    """开始和续租时均读取当前账号及资源权限，不缓存提交时的身份。"""
    from app import db
    from app.services import auth_service
    if not auth_service.enabled():
        return True
    loop = db.query_one('SELECT * FROM loops WHERE id=?', (loop_id,))
    if not loop or loop['organization_id'] != 'local-org':
        return False
    user = db.query_one('SELECT id AS user_id,role FROM users WHERE id=? AND enabled=1', (loop['user_id'],))
    return bool(user and auth_service.workspace_access(loop['workspace_id'], user) in {'owner', 'member'} and auth_service.agent_access(loop['agent_id'], user, loop['workspace_id']))


def execution_authorized(task_id: str) -> bool:
    from app import db
    from app.services import auth_service
    if not auth_service.enabled():
        return True
    task = db.query_one("SELECT user_id,organization_id,workspace,agent_id FROM tasks WHERE id=?", (task_id,))
    if not task or task['organization_id'] != 'local-org':
        return False
    user = db.query_one("SELECT id AS user_id,role FROM users WHERE id=? AND enabled=1", (task['user_id'],))
    if not user:
        return False
    return auth_service.workspace_access(task['workspace'], user) in {'owner', 'member'} and auth_service.agent_access(task['agent_id'], user, task['workspace'])


def recheck_execution_permission(runtime, task_id: str, run_id: str) -> None:
    from app.services.task_state import RunIntakeClosed
    if execution_authorized(task_id):
        return
    try:
        runtime.task_state.request_cancel(task_id, run_id=run_id, reason="账号或工作区执行权限已撤销", requested_by="worker")
    except RunIntakeClosed:
        return


async def execute_with_deadline(runtime, task_id: str, run_id: str, operation, seconds: float, *, on_timeout=None) -> None:
    from app.services.task_state import PublicationConflict
    from app.services import model_budget
    budget_token = model_budget.bind(task_id)
    deadline = asyncio.timeout(seconds)
    try:
        async with deadline:
            await operation
    except TimeoutError:
        if not deadline.expired():
            raise
        # 区分超时和进程退出：超时是终态，不应被重启恢复反复重试。
        try:
            error = {"error_type": "TaskTimeout", "message": "任务超过执行时间上限"}
            if on_timeout:
                on_timeout(error)
            else:
                runtime.task_state.commit_failure(
                    task_id=task_id, run_id=run_id, error=error,
                    result={"error_type": "TaskTimeout", "summary": "任务超过执行时间上限，请缩小任务范围或调整配置。"},
                )
        except PublicationConflict:
            runtime.task_state.assert_terminal_clean(task_id=task_id, run_id=run_id)
    finally:
        model_budget.reset(budget_token)


async def main() -> None:
    if not enabled():
        raise SystemExit("REDIS_URL 未配置，无法启动 Worker")
    user_limit = int(os.getenv('APP_MAX_CONCURRENT_TASKS_PER_USER', '2'))
    if not 1 <= user_limit <= 100:
        raise SystemExit('APP_MAX_CONCURRENT_TASKS_PER_USER 必须在 1 到 100 之间')
    lease_ms = int(os.getenv('APP_WORKER_LEASE_MS', '60000'))
    if not 1000 <= lease_ms <= 600000:
        raise SystemExit('APP_WORKER_LEASE_MS 必须在 1000 到 600000 之间')
    task_timeout = float(os.getenv("APP_TASK_TIMEOUT_SECONDS", "1800"))
    if not 0 < task_timeout <= 86400:
        raise SystemExit("APP_TASK_TIMEOUT_SECONDS 必须大于 0 且不超过 86400")

    # 延迟导入，避免 API 仅使用本地模式时初始化 Worker 依赖。
    from app.main import runtime
    from app.main import expert_team_service
    from app.main import loop_scheduler
    from app.main import _reload_policy_rules
    loop_scheduler.worker_execution = True
    from app import db
    db.init_db()
    runtime.task_state.init_schema()
    runtime.tool_effect_journal.init_schema()

    client = Redis.from_url(redis_url(), decode_responses=True, socket_connect_timeout=2, socket_timeout=10)
    worker_id = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"
    heartbeat_key = worker_key(worker_id)

    async def heartbeat() -> None:
        while True:
            await client.set(heartbeat_key, "online", ex=30)
            await asyncio.sleep(10)

    heartbeat_task = asyncio.create_task(heartbeat())
    try:
        while True:
            if heartbeat_task.done():
                await heartbeat_task
            payload = await dequeue(client, consumer=worker_id, idle_ms=lease_ms)
            if not payload:
                continue
            if await consume_queued_cancellation(client, runtime, payload):
                continue
            if payload.get('kind') == 'automation':
                scope = db.query_one('SELECT organization_id,user_id FROM loops WHERE id=?', (payload['loop_id'],))
            else:
                scope = db.query_one('SELECT organization_id,user_id FROM tasks WHERE id=?', (payload['task_id'],))
            if not scope:
                await acknowledge(client, payload)
                continue
            slot_key = user_slot_key(scope['organization_id'], scope['user_id'])
            slot_token = uuid.uuid4().hex
            # 自动化/成员重试使用各自固定 60 秒锁；普通运行按配置续租。
            slot_lease_ms = 60000 if payload.get('kind') in {'automation', 'member_retry'} else lease_ms
            if not await acquire_user_slot(client, slot_key, slot_token, user_limit, lease_ms=slot_lease_ms):
                await defer_message(client, payload, worker_id, idle_ms=lease_ms)
                continue
            try:
                # API 的内存规则刷新不会传播到独立进程；每次执行读取持久配置。
                _reload_policy_rules()
                if payload.get('kind') == 'member_retry':
                    await execute_member_retry(client, expert_team_service, payload, task_timeout, slot_key, slot_token)
                    continue
                if payload.get('kind') == 'automation':
                    await execute_automation(client, loop_scheduler, payload, task_timeout, slot_key, slot_token)
                    continue
                run_id = str(payload["run_id"])
                lock_key = lock_name(run_id)
                owner = uuid.uuid4().hex
                if not await client.set(lock_key, owner, nx=True, px=lease_ms):
                    # 保留 pending，持锁者完成后再检查，不丢弃重复投递。
                    continue
                execution = None
                renewal = None
                try:
                    run = db.query_one("SELECT task_id,status,metadata_json FROM task_runs WHERE id=?", (run_id,))
                    if not run or run["task_id"] != payload["task_id"] or run["status"] in {"completed", "failed", "cancelled"}:
                        await acknowledge(client, payload)
                        continue
                    if not execution_authorized(payload["task_id"]):
                        error = {"error_type": "PermissionRevoked", "message": "执行权限已撤销"}
                        if payload.get('kind') == 'team':
                            expert_team_service.fail_interrupted_run(payload['team_run_id'], run_id, error)
                        else:
                            runtime.task_state.commit_failure(
                                task_id=payload["task_id"], run_id=run_id, error=error,
                                result={"error_type": "PermissionRevoked", "summary": "账号或工作区权限已变化，任务未继续执行。"},
                            )
                        await acknowledge(client, payload)
                        continue
                    if run["status"] in {"running", "paused"}:
                        if db.json_loads(run["metadata_json"], {}).get("dispatch_backend") != "redis":
                            raise RuntimeError("拒绝接管非 Worker 的运行")
                        runtime.tool_effect_journal.recover_interrupted_executions(reason="worker_lease_expired", only_run_ids={run_id})
                        if payload.get('kind') == 'team':
                            team_run = db.query_one('SELECT * FROM team_runs WHERE id=? AND parent_run_id=?', (payload.get('team_run_id'), run_id))
                            if not team_run:
                                raise RuntimeError('专家团运行绑定不匹配')
                            expert_team_service.fail_interrupted_run(team_run['id'], run_id, {'error_type': 'WorkerInterrupted', 'message': '专家团执行中断，请重新提交或重试成员'})
                            await acknowledge(client, payload)
                            continue
                        recover_worker_attempt(runtime, run_id)
                        await acknowledge(client, payload)
                        continue
                    approval = payload.get("kind") == "approval"
                    pending_policy = db.json_loads(run["metadata_json"], {}).get("pending_policy_approval")
                    if approval and run["status"] == "waiting_approval" and pending_policy:
                        command = runtime.task_state.get_command(str(payload.get("command_id") or ""))
                        if not command or command.get("run_id") != run_id or command.get("task_id") != payload["task_id"] or command.get("type") != "approval":
                            raise RuntimeError("Policy 审批投递与持久化命令不匹配")
                        decision = runtime.task_state.commit_policy_approval_decision(task_id=payload["task_id"], run_id=run_id, approval_id=pending_policy["approval_id"], worker_id=worker_id)
                        if decision is None:
                            raise RuntimeError("Policy 审批决定尚不可消费")
                        runtime.task_state.recover_interrupted_attempt(run_id, reason="Policy 审批已处理，从检查点继续", metadata={"dispatch_backend": "redis"})
                        await acknowledge(client, payload)
                        continue
                    if run["status"] == "waiting_approval" and not approval:
                        await acknowledge(client, payload)
                        continue
    
                    async def renew() -> None:
                        while True:
                            await asyncio.sleep(lease_ms / 3000)
                            if not await renew_user_slot(client, slot_key, slot_token, lease_ms=slot_lease_ms):
                                raise WorkerLeaseLost('用户并发配额租约已丢失')
                            if not await renew_lock(client, lock_key, owner, lease_ms):
                                raise WorkerLeaseLost("Worker 运行租约已丢失")
                            recheck_execution_permission(runtime, payload["task_id"], run_id)
    
                    if payload.get('kind') == 'team':
                        team_run = db.query_one('SELECT id FROM team_runs WHERE id=? AND parent_run_id=? AND parent_task_id=?', (payload.get('team_run_id'), run_id, payload['task_id']))
                        if not team_run:
                            raise RuntimeError('专家团运行绑定不匹配')
                        operation = expert_team_service.run_team(team_run['id'])
                    elif approval:
                        command = runtime.task_state.get_command(str(payload.get("command_id") or ""))
                        if not command or command.get("run_id") != run_id or command.get("task_id") != payload["task_id"] or command.get("type") != "approval":
                            raise RuntimeError("审批投递与持久化命令不匹配")
                        decision = command.get("payload") or {}
                        if not isinstance(decision.get("approved"), bool):
                            raise RuntimeError("审批决定缺失")
                        from app.main import _resume_after_approval_safely
                        operation = _resume_after_approval_safely(payload["task_id"], decision["approved"], str(decision.get("note") or ""), command["id"])
                    else:
                        activation = db.json_loads(run["metadata_json"], {}).get("recovery_activation_result")
                        operation = runtime.run_task(str(payload["task_id"]), run_id=run_id, activation_result=activation)
                    runtime.task_state.update_run_metadata(run_id, {'worker_id': worker_id})
                    timeout_handler = None
                    if payload.get('kind') == 'team':
                        timeout_handler = lambda error: expert_team_service.fail_interrupted_run(payload['team_run_id'], run_id, error)
                    execution = asyncio.create_task(execute_with_deadline(runtime, payload["task_id"], run_id, operation, task_timeout, on_timeout=timeout_handler))
                    renewal = asyncio.create_task(renew())
                    done, _ = await asyncio.wait({execution, renewal}, return_when=asyncio.FIRST_COMPLETED)
                    if renewal in done:
                        # 不确认消息，取消本地协程并退出，留给恢复流程处理。
                        await renewal
                    try:
                        await execution
                    except Exception:
                        logger.exception("任务执行失败：task_id=%s run_id=%s", payload["task_id"], run_id)
                        await retry_or_dead_letter(client, payload)
                    else:
                        await acknowledge(client, payload)
                finally:
                    children = [item for item in (execution, renewal) if item is not None]
                    for child in children:
                        child.cancel()
                    await asyncio.gather(*children, return_exceptions=True)
                    await release_lock(client, lock_key, owner)
            finally:
                await client.zrem(slot_key, slot_token)
    finally:
        heartbeat_task.cancel()
        await asyncio.gather(heartbeat_task, return_exceptions=True)
        try:
            await client.delete(heartbeat_key)
        finally:
            await client.aclose()


async def supervise() -> None:
    from redis.exceptions import RedisError
    while True:
        try:
            await main()
            return
        except (RedisError, WorkerLeaseLost):
            logger.warning('Worker 连接或租约中断，等待重连；未确认消息保留供恢复')
            await asyncio.sleep(2)


if __name__ == "__main__":
    asyncio.run(supervise())
