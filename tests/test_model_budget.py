import os
import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx

from app import db
from app.services import model_budget
from app.services.model_gateway import ModelGateway


class ModelBudgetTests(unittest.IsolatedAsyncioTestCase):
    def test_platform_tool_limit_only_tightens_agent_permissions(self):
        from app.services.agent_runtime import AgentRuntime
        with patch.dict(os.environ, {'APP_MAX_TOOL_CALLS':'3'}):
            self.assertEqual(AgentRuntime._normalize_permissions({})['max_tool_calls'],3)
            self.assertEqual(AgentRuntime._normalize_permissions({'max_tool_calls':100})['max_tool_calls'],3)
            self.assertEqual(AgentRuntime._normalize_permissions({'max_tool_calls':2})['max_tool_calls'],2)
            self.assertEqual(AgentRuntime._normalize_permissions({'max_tool_calls':0})['max_tool_calls'],0)

    async def asyncSetUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        database = patch.object(db, 'DB_PATH', Path(temporary.name) / 'budget.db')
        database.start()
        self.addCleanup(database.stop)
        environment = patch.dict(os.environ, {'APP_MAX_MODEL_CALLS':'2'})
        environment.start()
        self.addCleanup(environment.stop)
        db.execute(model_budget.SCHEMA)

    async def test_gateway_limit_persists_across_context_reentry(self):
        gateway = ModelGateway()
        token = model_budget.bind('task-a')
        try:
            await gateway.summarize('first')
            child = model_budget.bind('team-child')
            try:
                await gateway.summarize('second')
            finally:
                model_budget.reset(child)
        finally:
            model_budget.reset(token)
        token = model_budget.bind('task-a')
        try:
            with self.assertRaises(model_budget.ModelBudgetExceeded):
                await gateway.summarize('third')
        finally:
            model_budget.reset(token)
        self.assertEqual(db.query_all('SELECT task_id,calls FROM model_call_budget'), [{'task_id':'task-a','calls':2}])

    async def test_atomic_reservation_cannot_exceed_limit(self):
        def attempt():
            token = model_budget.bind('task-a')
            try:
                model_budget.reserve_call()
                return True
            except model_budget.ModelBudgetExceeded:
                return False
            finally:
                model_budget.reset(token)
        with ThreadPoolExecutor(max_workers=8) as pool:
            self.assertEqual(sum(pool.map(lambda _: attempt(), range(8))),2)

    async def check_fallback_budget(self, limit):
        db.execute('CREATE TABLE model_configs(id TEXT PRIMARY KEY,model TEXT,enabled INTEGER,config_json TEXT)')
        db.execute("INSERT INTO model_configs VALUES('test-model','test',1,'{}')")
        requests = []

        def respond(request):
            payload = json.loads(request.content)
            requests.append(payload)
            if payload.get('stream'):
                return httpx.Response(503, json={'error': 'temporary failure'})
            return httpx.Response(200, json={'choices': [{'message': {'content': '备用回答'}}]})

        client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
        gateway = ModelGateway()
        token = model_budget.bind('fallback-task')
        try:
            with patch.dict(os.environ, {'APP_MAX_MODEL_CALLS': str(limit), 'APP_ALLOW_OUTBOUND_NETWORK': 'true'}), patch.object(gateway, '_validated_base_url', return_value='https://model.example.test'), patch.object(gateway, '_api_key', return_value='test-key'), patch('app.services.model_gateway.httpx.AsyncClient', return_value=client), patch('app.services.model_gateway.asyncio.sleep', AsyncMock()):
                operation = gateway.solve_with_tools('测试', '测试', 'test-model', [], AsyncMock())
                if limit == 3:
                    with self.assertRaises(model_budget.ModelBudgetExceeded):
                        await operation
                else:
                    self.assertEqual(await operation, '备用回答')
        finally:
            model_budget.reset(token)
            await client.aclose()
        self.assertEqual(len(requests), limit)
        self.assertTrue(all(item.get('stream') for item in requests[:3]))
        if limit == 4:
            self.assertFalse(requests[-1].get('stream'))
        self.assertEqual(db.query_one("SELECT calls FROM model_call_budget WHERE task_id='fallback-task'")['calls'], limit)

    async def test_exhausted_budget_prevents_fallback_http_request(self):
        await self.check_fallback_budget(3)

    async def test_fallback_http_request_counts_toward_persistent_budget(self):
        await self.check_fallback_budget(4)
