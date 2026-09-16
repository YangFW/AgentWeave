import asyncio
import json
import os
import tempfile
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx

from app import db
from app.services.call_limits import call_limits, ModelCallTimeout, ToolCallTimeout
from app.services.mcp_gateway import McpGateway
from app.services.model_gateway import ModelGateway
from app.services import model_budget


class CallLimitTests(unittest.IsolatedAsyncioTestCase):
    def test_resource_override_environment_and_finite_limits(self):
        with patch.dict(os.environ, {'APP_MODEL_TIMEOUT_SECONDS': '12', 'APP_MODEL_MAX_RETRIES': '1'}):
            self.assertEqual(call_limits('model', {}).timeout, 12)
            self.assertEqual(call_limits('model', {}).max_retries, 1)
            self.assertEqual(call_limits('model', {'timeout': 3, 'max_retries': 0}).timeout, 3)
            self.assertEqual(call_limits('model', {'max_retries': 0}).max_retries, 0)
        for value in ('nan', 'inf', -1, 0, 601, True):
            with self.subTest(value=value), self.assertRaises(ValueError):
                call_limits('model', {'timeout': value})
        for value in (1.5, 6, -1, True):
            with self.subTest(value=value), self.assertRaises(ValueError):
                call_limits('model', {'max_retries': value})
        for kind in ('http_tool', 'mcp'):
            with self.subTest(kind=kind), self.assertRaises(ValueError):
                call_limits(kind, {'max_retries': 1})

    async def test_disabled_model_retry_sends_one_request_and_no_fallback(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(db, 'DB_PATH', Path(directory) / 'model.db'):
            db.execute('CREATE TABLE model_configs(id TEXT PRIMARY KEY,model TEXT,enabled INTEGER,config_json TEXT)')
            db.execute('INSERT INTO model_configs VALUES(?,?,1,?)', ('model', 'test', json.dumps({'max_retries': 0})))
            requests = []

            def respond(request):
                requests.append(request)
                return httpx.Response(503, json={'error': 'unavailable'})

            gateway = ModelGateway()
            client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
            with patch.dict(os.environ, {'APP_ALLOW_OUTBOUND_NETWORK': 'true'}), patch.object(gateway, '_validated_base_url', return_value='https://model.example.test'), patch.object(gateway, '_api_key', return_value='test'), patch('app.services.model_gateway.httpx.AsyncClient', return_value=client):
                with self.assertRaises(httpx.HTTPStatusError):
                    await gateway.solve_with_tools('hello', 'test', 'model', [], AsyncMock())
            self.assertEqual(len(requests), 1)

    async def test_summary_retry_obeys_config_and_counts_each_request(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(db, 'DB_PATH', Path(directory) / 'summary.db'):
            db.execute(model_budget.SCHEMA)
            db.execute('CREATE TABLE model_configs(id TEXT PRIMARY KEY,model TEXT,provider TEXT,enabled INTEGER,config_json TEXT)')
            db.execute('INSERT INTO model_configs VALUES(?,?,?,1,?)', ('model', 'test', 'openai', json.dumps({'max_retries': 1, 'retry_backoff': 0})))
            requests = []

            def respond(request):
                requests.append(request)
                return httpx.Response(503) if len(requests) == 1 else httpx.Response(200, json={'choices': [{'message': {'content': '完成'}}]})

            gateway = ModelGateway()
            client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
            token = model_budget.bind('summary-task')
            try:
                with patch.dict(os.environ, {'APP_ALLOW_OUTBOUND_NETWORK': 'true', 'APP_MAX_MODEL_CALLS': '2'}), patch.object(gateway, '_validated_base_url', return_value='https://model.example.test'), patch.object(gateway, '_api_key', return_value='test'), patch('app.services.model_gateway.httpx.AsyncClient', return_value=client):
                    self.assertEqual(await gateway.summarize('hello', model_config_id='model'), '完成')
            finally:
                model_budget.reset(token)
            self.assertEqual(len(requests), 2)
            self.assertEqual(db.query_one('SELECT calls FROM model_call_budget')['calls'], 2)

    async def test_model_total_timeout_cancels_waiting_response_without_retry(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(db, 'DB_PATH', Path(directory) / 'timeout.db'):
            db.execute('CREATE TABLE model_configs(id TEXT PRIMARY KEY,model TEXT,enabled INTEGER,config_json TEXT)')
            db.execute('INSERT INTO model_configs VALUES(?,?,1,?)', ('model', 'test', json.dumps({'timeout': 0.1})))
            cancelled = asyncio.Event()
            requests = []

            async def respond(request):
                requests.append(request)
                try:
                    await asyncio.Event().wait()
                finally:
                    cancelled.set()

            gateway = ModelGateway()
            client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
            with patch.dict(os.environ, {'APP_ALLOW_OUTBOUND_NETWORK': 'true'}), patch.object(gateway, '_validated_base_url', return_value='https://model.example.test'), patch.object(gateway, '_api_key', return_value='test'), patch('app.services.model_gateway.httpx.AsyncClient', return_value=client):
                with self.assertRaises(ModelCallTimeout) as caught:
                    await gateway.solve_with_tools('hello', 'test', 'model', [], AsyncMock())
            self.assertIn('0.1 秒', str(caught.exception))
            self.assertEqual(len(requests), 1)
            self.assertTrue(cancelled.is_set())

    async def test_http_write_timeout_is_not_automatically_repeated(self):
        requests = []

        async def respond(request):
            requests.append(request)
            await asyncio.Event().wait()

        client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
        gateway = McpGateway()
        server = {'config': {'base_url': 'https://tool.example.test', 'timeout': 0.1},
                  'tools': [{'name': 'write', 'path': '/write', 'method': 'POST'}]}
        with patch.dict(os.environ, {'APP_ALLOW_OUTBOUND_NETWORK': 'true', 'APP_ALLOW_HTTP_TOOLS': 'true'}), patch.object(gateway, '_validate_remote_url', side_effect=lambda url: url), patch('app.services.mcp_gateway.httpx.AsyncClient', return_value=client):
            with self.assertRaises(ToolCallTimeout) as caught:
                await gateway._invoke_http_tool(server, 'write', {'value': 'test'})
        self.assertIn('HTTP 工具', str(caught.exception))
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0].method, 'POST')

    async def test_remote_mcp_total_timeout_cancels_pending_tool(self):
        cancelled = asyncio.Event()

        async def slow_tool(*args):
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        @asynccontextmanager
        async def transport(*args, **kwargs):
            yield (None, None, None)

        @asynccontextmanager
        async def session(*args, **kwargs):
            yield SimpleNamespace(initialize=AsyncMock(), call_tool=slow_tool)

        gateway = McpGateway()
        with patch.object(gateway, '_mcp_http_config', return_value=('https://mcp.example.test', {})), patch('mcp.client.streamable_http.streamable_http_client', transport), patch('mcp.ClientSession', session):
            with self.assertRaises(ToolCallTimeout) as caught:
                await gateway._invoke_mcp_http({'config': {'timeout': 0.1}}, 'slow', {})
        self.assertIn('MCP', str(caught.exception))
        self.assertTrue(cancelled.is_set())
