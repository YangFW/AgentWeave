import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException
from app import db, main, worker
from app.services.context_service import ExecutionScope
from app.services.task_state import TaskStateService


class MemberRetryDispatchTests(unittest.IsolatedAsyncioTestCase):
    async def test_durable_retry_is_exclusive_and_consumed_by_worker(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(db, 'DB_PATH', Path(directory) / 'retry.db'), patch.dict(os.environ, {'REDIS_URL':'redis://unused', 'APP_AUTH_ENABLED':'false'}):
            TaskStateService()
            db.execute('CREATE TABLE team_runs(id TEXT PRIMARY KEY,parent_task_id TEXT,parent_run_id TEXT)')
            db.execute("INSERT INTO team_runs VALUES('team-run','parent-task','parent-run')")
            scope = ExecutionScope()
            await main._schedule_member_retry('team-run','member-run',scope)
            with self.assertRaises(HTTPException) as conflict:
                main._schedule_member_retry('team-run','member-run',scope)
            self.assertEqual(conflict.exception.status_code,409)
            rows = db.query_all('SELECT * FROM member_retry_dispatch')
            self.assertEqual(len(rows),1)
            message = db.json_loads(rows[0]['payload_json'],{})
            service = SimpleNamespace(retry_member=AsyncMock())
            client = AsyncMock()
            client.set.return_value = True
            with patch.object(worker,'acknowledge',AsyncMock()) as acknowledge:
                await worker.execute_member_retry(client,service,message,5,'slot-key','slot-token')
            service.retry_member.assert_awaited_once_with('team-run','member-run',scope)
            acknowledge.assert_awaited_once()
            self.assertEqual(db.query_one('SELECT status FROM member_retry_dispatch')['status'],'completed')
