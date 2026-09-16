"""使用已有 Chromium/CDP 验证界面，不下载浏览器或安装前端依赖。"""
import base64
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

import httpx
from websockets.sync.client import connect


@unittest.skipUnless(os.getenv('APP_TEST_CHROMIUM'), '需要指定已有 Chromium 可执行文件')
class BrowserReleaseTests(unittest.TestCase):
    def test_login_user_management_and_members(self):
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            with socket.socket() as sock:
                sock.bind(('127.0.0.1', 0))
                port = sock.getsockname()[1]
            environment = {**os.environ, 'APP_DB_PATH': str(directory / 'platform.db'), 'APP_AUTH_ENABLED': 'true',
                'APP_ADMIN_USERNAME': 'test-admin', 'APP_ADMIN_PASSWORD': 'browser-test-password', 'APP_USER_PASSWORD': '',
                'REDIS_URL': '', 'APP_SECRET_KEY': '', 'APP_SECRET_KEY_FILE': str(directory / '.secret_key'),
                'APP_UPLOAD_DIR': str(directory / 'uploads'), 'APP_ARTIFACT_DIR': str(directory / 'artifacts'),
                'AGENTNEXUS_ENV_FILE': str(directory / 'absent.env'), 'APP_ALLOW_OUTBOUND_NETWORK': 'false', 'APP_ALLOW_STDIO_MCP': 'false'}
            processes = []
            with (directory / 'process.log').open('w+') as log:
                def start(args, env=None):
                    process = subprocess.Popen(args, cwd=root, env=env, stdout=log, stderr=log)
                    processes.append(process)
                    return process

                def wait(predicate):
                    deadline = time.monotonic() + 20
                    while time.monotonic() < deadline:
                        try:
                            result = predicate()
                            if result:
                                return result
                        except httpx.TransportError:
                            pass
                        time.sleep(0.1)
                    self.fail('浏览器验收等待超时')

                try:
                    start([sys.executable, '-m', 'uvicorn', 'app.main:app', '--host', '127.0.0.1', '--port', str(port)], environment)
                    wait(lambda: httpx.get(f'http://127.0.0.1:{port}/api/health').status_code == 200)
                    profile = directory / 'browser'
                    start([os.environ['APP_TEST_CHROMIUM'], '--headless', '--no-sandbox', '--disable-gpu', '--disable-background-networking', '--no-first-run', '--remote-debugging-port=0', f'--user-data-dir={profile}', 'about:blank'])
                    wait(lambda: (profile / 'DevToolsActivePort').is_file())
                    debug_port = (profile / 'DevToolsActivePort').read_text().splitlines()[0]
                    targets = wait(lambda: [target for target in httpx.get(f'http://127.0.0.1:{debug_port}/json/list').json() if target['type'] == 'page'])
                    address = targets[0]['webSocketDebuggerUrl']
                    with connect(address, max_size=20 * 1024 * 1024) as ws:
                        sequence = 0
                        errors = []
                        denied_task_requests = []
                        def command(method, params=None):
                            nonlocal sequence
                            sequence += 1
                            ws.send(json.dumps({'id': sequence, 'method': method, 'params': params or {}}))
                            while True:
                                response = json.loads(ws.recv(timeout=15))
                                if response.get('method') == 'Runtime.exceptionThrown':
                                    errors.append(response['params'])
                                if response.get('method') == 'Network.responseReceived':
                                    network_response = response['params']['response']
                                    if network_response['url'].endswith('/api/tasks') and network_response['status'] == 403:
                                        denied_task_requests.append(network_response['url'])
                                if response.get('id') == sequence:
                                    self.assertNotIn('error', response)
                                    return response.get('result', {})
                        def evaluate(expression):
                            result = command('Runtime.evaluate', {'expression': expression, 'awaitPromise': True, 'returnByValue': True})
                            self.assertNotIn('exceptionDetails', result, str(result))
                            return result.get('result', {}).get('value')

                        command('Runtime.enable')
                        command('Network.enable')
                        command('Page.enable')
                        command('Emulation.setDeviceMetricsOverride', {'width':1280, 'height':1000, 'deviceScaleFactor':1, 'mobile':False})
                        command('Page.navigate', {'url': f'http://127.0.0.1:{port}/'})
                        wait(lambda: evaluate("document.querySelector('#loginPanel') && !document.querySelector('#loginPanel').classList.contains('hidden')"))
                        evaluate("document.querySelector('#loginUsername').value='test-admin';document.querySelector('#loginPassword').value='browser-test-password';document.querySelector('#loginForm').requestSubmit()")
                        wait(lambda: evaluate("document.querySelector('#usersNav') && !document.querySelector('#usersNav').classList.contains('hidden') && typeof loadAdminUsers==='function'"))
                        wait(lambda: evaluate("document.querySelector('#serviceStatus b')?.textContent==='服务已连接'"))
                        evaluate("document.querySelector('#usersNav').click()")
                        wait(lambda: evaluate("document.querySelector('#adminUserList').textContent.includes('test-admin')"))
                        evaluate("document.querySelector('#newUserBtn').click();document.querySelector('#adminUsername').value='browser-member';document.querySelector('#adminUserPassword').value='member-test-password';document.querySelector('#adminUserForm').requestSubmit()")
                        wait(lambda: evaluate("document.querySelector('#adminUserList').textContent.includes('browser-member')"))
                        self.assertEqual(evaluate("document.querySelector('#adminUserPassword').value"), '')
                        evaluate("document.querySelector('[data-tab=workspaces]').click()")
                        wait(lambda: evaluate("state.workspaces.some(w=>w.id==='default')"))
                        evaluate("selectWorkspaceEditor('default', {activate:false})")
                        wait(lambda: evaluate("!document.querySelector('#workspaceMembersPanel').classList.contains('hidden')"))
                        evaluate("document.querySelector('#workspaceMemberUsername').value='browser-member';document.querySelector('#workspaceMemberRole').value='viewer';document.querySelector('#addWorkspaceMemberBtn').click()")
                        wait(lambda: evaluate("document.querySelector('#workspaceMembersList').textContent.includes('browser-member · 只读')"))
                        output = os.getenv('APP_TEST_BROWSER_OUTPUT')
                        if output:
                            Path(output).mkdir(parents=True, exist_ok=True)
                            screenshot = command('Page.captureScreenshot', {'format': 'png'})['data']
                            (Path(output) / 'workspace-members.png').write_bytes(base64.b64decode(screenshot))
                        evaluate("document.querySelector('#logoutButton').click()")
                        wait(lambda: evaluate("document.querySelector('#loginPanel') && !document.querySelector('#loginPanel').classList.contains('hidden')"))
                        evaluate("document.querySelector('#loginUsername').value='browser-member';document.querySelector('#loginPassword').value='member-test-password';document.querySelector('#loginForm').requestSubmit()")
                        wait(lambda: evaluate("document.querySelector('#serviceStatus b')?.textContent==='服务已连接' && !document.body.classList.contains('auth-pending')"))
                        self.assertTrue(evaluate("document.querySelector('#usersNav').classList.contains('hidden')"))
                        self.assertEqual(evaluate("fetch('/api/users').then(r=>r.status)"),403)
                        evaluate("document.querySelector('#messageInput').value='你好';document.querySelector('#sendBtn').click()")
                        wait(lambda: evaluate("document.querySelector('#messageInput').value==='你好' && !state.taskUiRunning"))
                        self.assertEqual(evaluate("fetch('/api/tasks').then(r=>r.json()).then(rows=>rows.length)"),0)
                        self.assertTrue(denied_task_requests)
                        self.assertEqual(errors, [])
                finally:
                    for process in reversed(processes):
                        if process.poll() is None:
                            process.terminate()
                    for process in processes:
                        try:
                            process.wait(timeout=10)
                        except subprocess.TimeoutExpired:
                            process.kill()
                            process.wait(timeout=5)
