import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app import db
from app.services import auth_service
from app.services.agent_runtime import create_task_record
from app.services.workspace_service import WorkspaceService
from app.worker import execution_authorized, recheck_execution_permission
from app.services.task_state import TaskStateService
from app.seed import seed_agents
from app import worker
from app.services.loop_scheduler import LoopScheduler


class WorkerPermissionTests(unittest.TestCase):
    def test_queue_does_not_preserve_revoked_account_or_membership(self):
        with tempfile.TemporaryDirectory() as temporary, patch.object(db, 'DB_PATH', Path(temporary) / 'auth.db'), patch.dict(os.environ, {'APP_AUTH_ENABLED': 'true', 'APP_ADMIN_PASSWORD': '', 'APP_USER_PASSWORD': ''}):
            db.init_db()
            seed_agents()
            auth_service.init_schema()
            WorkspaceService()
            user = auth_service.create_user('member', 'example-password')
            task = create_task_record('queued', 'general-agent', user_id=user['id'])
            self.assertTrue(execution_authorized(task['id']))
            db.execute('UPDATE users SET enabled=0 WHERE id=?', (user['id'],))
            self.assertFalse(execution_authorized(task['id']))
            db.execute('UPDATE users SET enabled=1 WHERE id=?', (user['id'],))
            db.execute("UPDATE workspace_members SET role='viewer' WHERE user_id=?", (user['id'],))
            self.assertFalse(execution_authorized(task['id']))
            db.execute("DELETE FROM workspace_members WHERE user_id=?", (user['id'],))
            self.assertFalse(execution_authorized(task['id']))
            self.assertFalse(execution_authorized('unknown-task'))
            state = TaskStateService()
            run = state.begin_run(task['id'])
            recheck_execution_permission(SimpleNamespace(task_state=state), task['id'], run['id'])
            self.assertTrue(state.is_cancel_requested(task['id'], run_id=run['id']))


class WorkerRenewalPermissionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        for replacement in (
            patch.object(db, 'DB_PATH', Path(temporary.name) / 'permissions.db'),
            patch.dict(os.environ, {'APP_AUTH_ENABLED': 'true', 'APP_ADMIN_PASSWORD': '', 'APP_USER_PASSWORD': ''}),
        ):
            replacement.start()
            self.addCleanup(replacement.stop)
        db.init_db()
        seed_agents()
        auth_service.init_schema()
        WorkspaceService()
        self.user = auth_service.create_user('renewal-member', 'example-password')
        self.state = TaskStateService()
        self.started = asyncio.Event()
        self.cancelled = asyncio.Event()
        self.client = AsyncMock()
        self.client.set.return_value = True
        self.sleep = asyncio.sleep

    async def revoke_while_running(self, *args, **kwargs):
        db.execute('UPDATE users SET enabled=0 WHERE id=?', (self.user['id'],))
        self.started.set()
        try:
            await asyncio.Event().wait()
        finally:
            self.cancelled.set()

    async def next_renewal(self, seconds):
        # 精确模拟执行已开始、权限随后撤销、下一次续租到来的顺序。
        await self.started.wait()
        await self.sleep(0)

    async def test_member_retry_revocation_cancels_operation_and_closes_dispatch(self):
        task = create_task_record('retry', 'general-agent', user_id=self.user['id'])
        run = self.state.begin_run(task['id'], activate_task_projection=True)
        db.execute('INSERT INTO team_runs(id,team_id,parent_task_id,parent_run_id,created_at,updated_at) VALUES(?,?,?,?,?,?)',
                   ('team-a', 'team-config', task['id'], run['id'], db.utc_now(), db.utc_now()))
        message = {'job_id': 'retry-a', 'task_id': task['id'], 'member_run_id': 'member-a', 'scope': {}}
        db.execute('INSERT INTO member_retry_dispatch(job_id,team_run_id,payload_json,created_at) VALUES(?,?,?,?)',
                   ('retry-a', 'team-a', db.json_dumps(message), db.utc_now()))

        def fail_team(team, reason, *, parent_run_id):
            self.state.commit_failure(task_id=task['id'], run_id=parent_run_id,
                                      error={'message': reason}, result={'summary': reason})

        service = SimpleNamespace(retry_member=self.revoke_while_running, task_state=self.state,
                                  fail_interrupted_run=lambda team_id, run_id, error: fail_team({}, error['message'], parent_run_id=run_id))
        with patch.object(worker.asyncio, 'sleep', self.next_renewal), patch.object(worker, 'renew_lock', AsyncMock(return_value=True)) as renew_lock, patch.object(worker, 'renew_user_slot', AsyncMock(return_value=True)), patch.object(worker, 'acknowledge', AsyncMock()) as acknowledge:
            await worker.execute_member_retry(self.client, service, message, 2, 'slot', 'token')
        renew_lock.assert_awaited_once()
        self.assertTrue(self.cancelled.is_set())
        self.assertEqual(db.query_one('SELECT status FROM member_retry_dispatch')['status'], 'failed')
        self.assertEqual(self.state.get_run(run['id'])['status'], 'failed')
        self.state.assert_terminal_clean(task_id=task['id'], run_id=run['id'])
        acknowledge.assert_awaited_once()

    async def test_automation_revocation_cancels_task_and_pauses_scheduler(self):
        now = db.utc_now()
        db.execute("INSERT INTO loops(id,name,prompt,agent_id,model_id,user_id,created_at,updated_at) VALUES('loop-a','测试','测试','general-agent','deterministic',?,?,?)",
                   (self.user['id'], now, now))
        message = {'job_id': 'automation-a', 'loop_id': 'loop-a', 'scheduled': False, 'trigger_event_id': '', 'trigger_type': 'manual'}
        db.execute('INSERT INTO automation_dispatch(job_id,loop_id,payload_json,created_at) VALUES(?,?,?,?)',
                   ('automation-a', 'loop-a', db.json_dumps(message), now))
        runtime = SimpleNamespace(run_task=self.revoke_while_running)
        scheduler = LoopScheduler(runtime)
        with patch.object(worker.asyncio, 'sleep', self.next_renewal), patch.object(worker, 'renew_lock', AsyncMock(return_value=True)) as renew_lock, patch.object(worker, 'renew_user_slot', AsyncMock(return_value=True)), patch.object(worker, 'acknowledge', AsyncMock()) as acknowledge:
            await worker.execute_automation(self.client, scheduler, message, 2, 'slot', 'token')
        renew_lock.assert_awaited_once()
        self.assertTrue(self.cancelled.is_set())
        self.assertEqual(db.query_one('SELECT status FROM automation_dispatch')['status'], 'failed')
        self.assertEqual(db.query_one('SELECT status FROM loops')['status'], 'paused')
        self.assertEqual(db.query_one('SELECT status FROM loop_runs')['status'], 'failed')
        self.assertEqual(db.query_one('SELECT status FROM tasks')['status'], 'failed')
        self.assertEqual(db.query_one('SELECT status FROM automation_trigger_events')['status'], 'failed')
        acknowledge.assert_awaited_once()
