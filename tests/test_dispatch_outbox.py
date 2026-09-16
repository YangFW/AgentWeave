import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch
import sqlite3

from app import db
from app.services import task_queue
from app.services.task_state import TaskStateService


class DispatchOutboxTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        database = patch.object(db, 'DB_PATH', Path(temporary.name) / 'test.db')
        database.start()
        self.addCleanup(database.stop)
        self.state = TaskStateService()

    async def test_run_and_dispatch_intent_commit_together(self):
        run = self.state.create_run('task-a', metadata={'dispatch_backend': 'redis'})
        row = db.query_one('SELECT * FROM dispatch_outbox WHERE run_id=?', (run['id'],))
        self.assertEqual(row['task_id'], 'task-a')
        self.assertEqual(row['delivered'], 0)

    async def test_outbox_failure_rolls_back_run(self):
        db.execute("CREATE TRIGGER reject_dispatch BEFORE INSERT ON dispatch_outbox BEGIN SELECT RAISE(ABORT,'test failure'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.state.create_run('task-a', metadata={'dispatch_backend': 'redis'})
        self.assertEqual(db.query_all('SELECT * FROM task_runs'), [])

    async def test_failed_send_stays_pending_then_retries(self):
        self.state.create_run('task-a', metadata={'dispatch_backend': 'redis'})
        with patch.object(task_queue, 'enqueue', AsyncMock(side_effect=ConnectionError('offline'))):
            with self.assertRaises(ConnectionError):
                await task_queue.dispatch_pending()
        self.assertEqual(db.query_one('SELECT delivered FROM dispatch_outbox')['delivered'], 0)
        with patch.object(task_queue, 'enqueue', AsyncMock()) as send:
            await task_queue.dispatch_pending()
            await task_queue.dispatch_pending()
            send.assert_awaited_once()
        self.assertEqual(db.query_one('SELECT delivered FROM dispatch_outbox')['delivered'], 1)

    async def test_approval_and_dispatch_commit_together_and_deduplicate(self):
        run = self.state.create_run('task-a', metadata={'dispatch_backend': 'redis'})
        decision = self.state.enqueue_command('task-a', 'approval', run_id=run['id'], command_id='approval-a', payload={'approved': True}, deduplicate=True)
        repeated = self.state.enqueue_command('task-a', 'approval', run_id=run['id'], command_id='approval-a', payload={'approved': True}, deduplicate=True)
        self.assertEqual(decision['id'], repeated['id'])
        self.assertEqual(len(db.query_all('SELECT * FROM approval_dispatch')), 1)
        with patch.object(task_queue, 'enqueue', AsyncMock()) as send:
            await task_queue.dispatch_pending()
        approval = send.await_args_list[-1].args[0]
        self.assertEqual(approval['kind'], 'approval')
        self.assertEqual(approval['command_id'], 'approval-a')
        self.assertNotEqual(approval['dispatch_id'], run['id'])

    async def test_approval_dispatch_failure_rolls_back_decision(self):
        run = self.state.create_run('task-a', metadata={'dispatch_backend': 'redis'})
        db.execute("CREATE TRIGGER reject_approval BEFORE INSERT ON approval_dispatch BEGIN SELECT RAISE(ABORT,'test failure'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.state.enqueue_command('task-a', 'approval', run_id=run['id'], payload={'approved': True})
        self.assertEqual(db.query_all('SELECT * FROM task_commands'), [])

    async def test_dispatch_workspace_comes_from_database(self):
        db.execute('CREATE TABLE tasks(id TEXT PRIMARY KEY,workspace TEXT)')
        db.execute("INSERT INTO tasks VALUES('task-a','real-project')")
        message = task_queue.with_database_scope({'task_id':'task-a','run_id':'run-a','workspace_id':'forged-project'})
        self.assertEqual(message['workspace_id'],'real-project')
