import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app import db
from app.services.agent_runtime import create_task_record
from app.services.task_state import TaskStateService
from app.worker import execute_with_deadline, recover_worker_attempt
from app import worker
from app.services.loop_scheduler import LoopScheduler


class WorkerDeadlineTests(unittest.IsolatedAsyncioTestCase):
    async def test_member_retry_deadline_closes_dispatch_and_team(self):
        from app.services.expert_team_service import ExpertTeamService
        with tempfile.TemporaryDirectory() as directory, patch.object(db, 'DB_PATH', Path(directory) / 'member.db'), patch.dict(os.environ, {'APP_AUTH_ENABLED': 'false'}):
            db.init_db()
            state = TaskStateService()
            task = create_task_record('成员重试', 'general-agent', executor_type='team', executor_id='team-config')
            run = state.begin_run(task['id'], activate_task_projection=True)
            now = db.utc_now()
            db.execute('INSERT INTO team_runs(id,team_id,parent_task_id,parent_run_id,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?)', ('team-a', 'team-config', task['id'], run['id'], 'running', now, now))
            payload = {'job_id': 'retry-timeout', 'task_id': task['id'], 'member_run_id': 'member-a', 'scope': {}}
            db.execute('INSERT INTO member_retry_dispatch(job_id,team_run_id,payload_json,created_at) VALUES(?,?,?,?)', ('retry-timeout', 'team-a', db.json_dumps(payload), now))
            cancelled = asyncio.Event()

            async def blocked(*args):
                try:
                    await asyncio.Event().wait()
                finally:
                    cancelled.set()

            service = ExpertTeamService(SimpleNamespace(task_state=state), task_state=state)
            client = AsyncMock()
            client.set.return_value = True
            with patch.object(service, 'retry_member', blocked), patch.object(worker, 'acknowledge', AsyncMock()) as acknowledge:
                await worker.execute_member_retry(client, service, payload, 0.1, 'slot', 'token')
            self.assertTrue(cancelled.is_set())
            acknowledge.assert_awaited_once()
            self.assertEqual(db.query_one('SELECT status FROM member_retry_dispatch')['status'], 'failed')
            self.assertEqual(db.query_one('SELECT status FROM team_runs')['status'], 'failed')
            self.assertEqual(state.get_run(run['id'])['error']['error_type'], 'TaskTimeout')
            state.assert_terminal_clean(task_id=task['id'], run_id=run['id'])

    async def test_automation_deadline_closes_task_round_and_trigger(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(db, 'DB_PATH', Path(directory) / 'automation.db'), patch.dict(os.environ, {'APP_AUTH_ENABLED': 'false'}):
            db.init_db()
            state = TaskStateService()
            now = db.utc_now()
            db.execute("INSERT INTO loops(id,name,prompt,agent_id,model_id,created_at,updated_at) VALUES('loop-timeout','测试','测试','general-agent','deterministic',?,?)", (now, now))
            payload = {'job_id': 'job-timeout', 'loop_id': 'loop-timeout', 'scheduled': False, 'trigger_event_id': '', 'trigger_type': 'manual'}
            db.execute('INSERT INTO automation_dispatch(job_id,loop_id,payload_json,created_at) VALUES(?,?,?,?)', ('job-timeout', 'loop-timeout', db.json_dumps(payload), now))
            cancelled = asyncio.Event()

            async def blocked(task_id, *, run_id):
                state.begin_run(task_id, run_id=run_id, activate_task_projection=True)
                try:
                    await asyncio.Event().wait()
                finally:
                    cancelled.set()

            scheduler = LoopScheduler(SimpleNamespace(run_task=blocked, task_state=state))
            client = AsyncMock()
            client.set.return_value = True
            with patch.object(worker, 'acknowledge', AsyncMock()) as acknowledge:
                await worker.execute_automation(client, scheduler, payload, 0.1, 'slot', 'token')
            self.assertTrue(cancelled.is_set())
            acknowledge.assert_awaited_once()
            self.assertEqual(db.query_one('SELECT status FROM automation_dispatch')['status'], 'failed')
            self.assertEqual(db.query_one('SELECT status FROM loops')['status'], 'paused')
            self.assertEqual(db.query_one('SELECT status FROM loop_runs')['status'], 'failed')
            self.assertEqual(db.query_one('SELECT status FROM automation_trigger_events')['status'], 'failed')
            self.assertEqual(db.json_loads(db.query_one('SELECT error_json FROM loop_runs')['error_json'], {})['error_type'], 'TaskTimeout')
            self.assertIn('超过执行时间', db.query_one('SELECT error FROM automation_trigger_events')['error'])
            self.assertIn('超过执行时间', db.query_one('SELECT content FROM notifications')['content'])
            run = state.list_runs()[0]
            self.assertEqual(run['error']['error_type'], 'TaskTimeout')
            state.assert_terminal_clean(task_id=run['task_id'], run_id=run['id'])

    async def test_repeated_worker_loss_is_bounded_and_persisted(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(db, 'DB_PATH', Path(directory) / 'recovery.db'), patch('app.worker.MAX_RETRIES',1):
            db.init_db()
            state = TaskStateService()
            task = create_task_record('worker failure','general-agent')
            first = state.begin_run(task['id'],activate_task_projection=True,metadata={'dispatch_backend':'redis'})
            runtime = SimpleNamespace(task_state=state)
            recover_worker_attempt(runtime,first['id'])
            second = state.list_runs(task_id=task['id'])[0]
            self.assertEqual(second['metadata']['worker_recovery_count'],1)
            state.begin_run(task['id'],run_id=second['id'],activate_task_projection=True)
            recover_worker_attempt(runtime,second['id'])
            self.assertEqual(state.get_run(second['id'])['status'],'failed')
            self.assertEqual(len(state.list_runs(task_id=task['id'])),2)
            state.assert_terminal_clean(task_id=task['id'],run_id=second['id'])

    async def test_timeout_commits_failure_after_coroutine_cancelled(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(db, 'DB_PATH', Path(directory) / 'deadline.db'):
            db.init_db()
            state = TaskStateService()
            task = create_task_record('测试超时', 'general-agent')
            run = state.begin_run(task['id'], activate_task_projection=True)
            cancelled = asyncio.Event()
            async def slow_operation():
                try:
                    await asyncio.Event().wait()
                finally:
                    cancelled.set()
            await execute_with_deadline(SimpleNamespace(task_state=state), task['id'], run['id'], slow_operation(), 0.02)
            self.assertTrue(cancelled.is_set())
            self.assertEqual(state.get_run(run['id'])['status'], 'failed')
            self.assertEqual(db.query_one('SELECT status FROM tasks WHERE id=?', (task['id'],))['status'], 'failed')
            self.assertEqual(state.get_run(run['id'])['error']['error_type'], 'TaskTimeout')
            self.assertEqual(len(state.list_runs(task_id=task['id'])), 1)
            state.assert_terminal_clean(task_id=task['id'], run_id=run['id'])
