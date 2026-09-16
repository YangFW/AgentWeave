"""API 与 Worker 真进程集成测试；必须指定独立临时 Redis。"""
import os
import json
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from pathlib import Path

import httpx
import redis


@unittest.skipUnless(os.getenv('APP_TEST_REDIS_URL'), '需要独立测试 Redis')
class WorkerProcessTests(unittest.TestCase):
    def test_api_queues_offline_tasks_for_two_workers(self):
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            with socket.socket() as sock:
                sock.bind(('127.0.0.1', 0))
                port = sock.getsockname()[1]
            environment = {
                **os.environ,
                'APP_DB_PATH': str(directory / 'platform.db'),
                'APP_UPLOAD_DIR': str(directory / 'uploads'),
                'APP_ARTIFACT_DIR': str(directory / 'artifacts'),
                'APP_SECRET_KEY_FILE': str(directory / '.secret_key'),
                'APP_SECRET_KEY': '', 'APP_AUTH_ENABLED': 'false',
                'APP_ADMIN_PASSWORD': '', 'APP_USER_PASSWORD': '',
                'APP_ALLOW_OUTBOUND_NETWORK': 'false',
                'APP_ALLOW_STDIO_MCP': 'false',
                'REDIS_URL': os.environ['APP_TEST_REDIS_URL'],
                'APP_TASK_QUEUE': 'agentnexus-process-test-' + uuid.uuid4().hex,
                # 两秒租约会在同步初始化/SQLite 竞争时失效，干扰正常团队验收。
                # 十秒仍能在本测试时限内验证真实进程终止后的租约回收。
                'APP_WORKER_LEASE_MS': '10000',
                'APP_MAX_CONCURRENT_TASKS_PER_USER': '1',
                'APP_DETERMINISTIC_STREAM_CHARS': '16',
                'APP_DETERMINISTIC_STREAM_DELAY_MS': '300',
                'AGENTNEXUS_ENV_FILE': str(directory / 'nonexistent.env'),
            }
            processes = []
            with (directory / 'processes.log').open('w+') as log, httpx.Client(base_url=f'http://127.0.0.1:{port}', timeout=3) as client:
                def start(arguments):
                    process = subprocess.Popen([sys.executable, *arguments], cwd=root, env=environment, stdout=log, stderr=log)
                    processes.append(process)
                    return process

                def wait_for(predicate, seconds=40):
                    deadline = time.monotonic() + seconds
                    while time.monotonic() < deadline:
                        try:
                            value = predicate()
                            if value:
                                return value
                        except httpx.TransportError:
                            pass
                        for process in processes:
                            if process.poll() is not None:
                                log.flush(); log.seek(0)
                                self.fail('测试进程提前退出：' + log.read()[-6000:])
                        time.sleep(0.1)
                    log.flush(); log.seek(0)
                    with sqlite3.connect(directory / 'platform.db') as connection:
                        states = connection.execute('SELECT id,status,metadata_json,error_json FROM task_runs ORDER BY created_at DESC LIMIT 3').fetchall()
                        commands = connection.execute('SELECT command_type,status,result_json FROM task_commands ORDER BY created_at DESC LIMIT 3').fetchall()
                    self.fail(f'测试超时：runs={states}\ncommands={commands}\n' + log.read()[-2000:])

                try:
                    api_arguments = ['-m', 'uvicorn', 'app.main:app', '--host', '127.0.0.1', '--port', str(port), '--no-access-log']
                    api = start(api_arguments)
                    wait_for(lambda: client.get('/api/health').status_code == 200)
                    task_ids = []
                    self.assertEqual(client.get('/api/readiness').status_code, 503)
                    for index in range(2):
                        response = client.post('/api/tasks', json={'message': '你好', 'model_id': 'deterministic', 'user_id': f'parallel-user-{index}'})
                        self.assertEqual(response.status_code, 200, response.text)
                        task_ids.append(response.json()['id'])
                    self.assertTrue(all(client.get(f'/api/tasks/{task_id}').json()['status'] == 'queued' for task_id in task_ids))
                    api.terminate()
                    api.wait(timeout=10)
                    processes.remove(api)
                    api = start(api_arguments)
                    wait_for(lambda: client.get('/api/health').status_code == 200)
                    start(['-m', 'app.worker'])
                    start(['-m', 'app.worker'])
                    wait_for(lambda: client.get('/api/readiness').status_code == 200)
                    def finished():
                        rows = [client.get(f'/api/tasks/{task_id}').json() for task_id in task_ids]
                        return rows if all(row['status'] in {'completed', 'failed', 'cancelled', 'waiting_approval'} for row in rows) else None
                    results = wait_for(finished)
                    self.assertEqual([row['status'] for row in results], ['completed', 'completed'], str(results)[:3000])
                    with sqlite3.connect(directory / 'platform.db') as connection:
                        self.assertEqual(connection.execute('SELECT count(*) FROM task_runs').fetchone()[0], 2)
                        self.assertEqual(connection.execute("SELECT count(*) FROM task_events WHERE type='answer'").fetchone()[0], 2)
                        self.assertEqual(connection.execute('SELECT count(*) FROM dispatch_outbox WHERE delivered=1').fetchone()[0], 2)
                        timings = connection.execute('SELECT started_at,finished_at FROM task_runs').fetchall()
                        self.assertTrue(all(started and finished for started, finished in timings))
                        self.assertLess(max(started for started, _ in timings), min(finished for _, finished in timings), '两个用户的普通任务必须有重叠的执行时间')
                    response = client.post('/api/tasks', json={'message': '请给 WorkBuddy 写一份 PRD，包含产品目标、用户故事和验收标准，直接回答即可。', 'model_id': 'deterministic'})
                    self.assertEqual(response.status_code, 200, response.text)
                    approval_task = response.json()['id']
                    def waiting():
                        row = client.get(f'/api/tasks/{approval_task}').json()
                        return row if row['status'] == 'waiting_approval' else None
                    wait_for(waiting)
                    response = client.post(f'/api/tasks/{approval_task}/approve', json={'approved': False, 'note': '继续原任务，不安装技能'})
                    self.assertEqual(response.status_code, 200, response.text)
                    def approved_done():
                        row = client.get(f'/api/tasks/{approval_task}').json()
                        return row if row['status'] in {'completed','failed','cancelled'} else None
                    final = wait_for(approved_done)
                    self.assertEqual(final['status'], 'completed', str(final)[:2000])
                    with sqlite3.connect(directory / 'platform.db') as connection:
                        self.assertEqual(connection.execute('SELECT count(*) FROM approval_dispatch WHERE delivered=1').fetchone()[0], 1)
                        self.assertEqual(connection.execute('SELECT count(*) FROM task_runs WHERE task_id=?', (approval_task,)).fetchone()[0], 1)
                    for name in ('test-supervisor', 'test-member-a', 'test-member-b'):
                        response = client.post('/api/agents', json={'id': name, 'name': name})
                        self.assertEqual(response.status_code, 200, response.text)
                    response = client.post('/api/expert-teams', json={
                        'id': 'test-team', 'name': 'Test Team', 'supervisor_agent_id': 'test-supervisor',
                        'members': [{'agent_id': 'test-member-a'}, {'agent_id': 'test-member-b'}],
                    })
                    self.assertEqual(response.status_code, 201, response.text)
                    response = client.post('/api/tasks', json={'message': '分析团队协作的注意事项', 'executor_type': 'team', 'executor_id': 'test-team', 'model_id': 'deterministic'})
                    self.assertEqual(response.status_code, 200, response.text)
                    team_task = response.json()['id']
                    def team_done():
                        row = client.get(f'/api/tasks/{team_task}').json()
                        return row if row['status'] in {'completed','failed','cancelled'} else None
                    final = wait_for(team_done)
                    if final['status'] != 'completed':
                        with sqlite3.connect(directory / 'platform.db') as connection:
                            errors = connection.execute('SELECT error_json FROM team_runs WHERE parent_task_id=?', (team_task,)).fetchall()
                        log.flush()
                        log.seek(0)
                        self.fail(f'专家团失败：{errors}\n' + log.read()[-6000:])
                    self.assertEqual(final['status'], 'completed', str(final)[:3000])
                    with sqlite3.connect(directory / 'platform.db') as connection:
                        self.assertEqual(connection.execute('SELECT count(*) FROM task_runs WHERE task_id=?', (team_task,)).fetchone()[0], 1)
                        self.assertEqual(connection.execute("SELECT count(*) FROM team_runs WHERE parent_task_id=? AND status='completed'", (team_task,)).fetchone()[0], 1)
                    response = client.post('/api/loops', json={'id': 'test-loop', 'name': 'Test Loop', 'prompt': '你好', 'agent_id': 'general-agent', 'model_id': 'deterministic', 'max_runs': 1, 'auto_start': False})
                    self.assertEqual(response.status_code, 200, response.text)
                    self.assertEqual(client.post('/api/loops/test-loop/run').status_code, 202)
                    def loop_done():
                        row = client.get('/api/loops/test-loop').json()
                        return row if row.get('status') in {'completed','failed'} else None
                    final = wait_for(loop_done)
                    self.assertEqual(final['status'], 'completed', str(final)[:2000])
                    self.assertEqual(final['run_count'], 1)
                    response = client.post('/api/loops', json={
                        'id': 'approval-loop', 'name': 'Approval Loop',
                        'prompt': '请给 WorkBuddy 写一份 PRD，包含产品目标、用户故事和验收标准，直接回答即可。',
                        'agent_id': 'general-agent', 'model_id': 'deterministic',
                        'max_runs': 1, 'auto_start': False,
                    })
                    self.assertEqual(response.status_code, 200, response.text)
                    self.assertEqual(client.post('/api/loops/approval-loop/run').status_code, 202)

                    def loop_waiting():
                        row = client.get('/api/loops/approval-loop').json()
                        with sqlite3.connect(directory / 'platform.db') as connection:
                            dispatched = connection.execute("SELECT status FROM automation_dispatch WHERE loop_id='approval-loop'").fetchone()
                        return row if row.get('status') == 'waiting_approval' and dispatched and dispatched[0] == 'completed' else None

                    waiting_loop = wait_for(loop_waiting)
                    loop_task = waiting_loop['last_task_id']
                    # 等待审批时重建全部执行进程，验证恢复不依赖内存协程。
                    for process in list(processes):
                        if process is not api:
                            process.kill()
                            process.wait(timeout=5)
                            processes.remove(process)
                    api.kill()
                    api.wait(timeout=5)
                    processes.remove(api)
                    api = start(api_arguments)
                    wait_for(lambda: client.get('/api/health').status_code == 200)
                    self.assertEqual(client.get('/api/loops/approval-loop').json()['status'], 'waiting_approval')
                    response = client.post(f'/api/tasks/{loop_task}/approve', json={'approved': False, 'note': '不安装技能，继续完成本轮'})
                    self.assertEqual(response.status_code, 200, response.text)
                    start(['-m', 'app.worker'])
                    start(['-m', 'app.worker'])

                    def approved_loop_done():
                        row = client.get('/api/loops/approval-loop').json()
                        return row if row.get('status') in {'completed', 'failed'} else None

                    final = wait_for(approved_loop_done)
                    self.assertEqual(final['status'], 'completed', str(final)[:2000])
                    self.assertEqual(final['run_count'], 1)
                    with sqlite3.connect(directory / 'platform.db') as connection:
                        self.assertEqual(connection.execute("SELECT count(*) FROM loop_runs WHERE loop_id='approval-loop' AND status='completed'").fetchone()[0], 1)
                        self.assertEqual(connection.execute("SELECT count(*) FROM automation_trigger_events WHERE loop_id='approval-loop' AND status='completed'").fetchone()[0], 1)
                        self.assertEqual(connection.execute("SELECT count(*) FROM task_events WHERE task_id=? AND type='answer'", (loop_task,)).fetchone()[0], 1)
                        self.assertEqual(connection.execute("SELECT count(*) FROM notifications WHERE entity_id='approval-loop' AND title='自动化审批结果已同步'").fetchone()[0], 1)
                    response = client.post('/api/tasks', json={'message': '比较集中式和分布式架构的优缺点', 'model_id': 'deterministic'})
                    self.assertEqual(response.status_code, 200, response.text)
                    crash_task = response.json()['id']
                    def running_owner():
                        with sqlite3.connect(directory / 'platform.db') as connection:
                            row = connection.execute("SELECT r.metadata_json FROM task_runs r WHERE r.task_id=? AND r.status='running' AND EXISTS(SELECT 1 FROM task_checkpoints c WHERE c.run_id=r.id)", (crash_task,)).fetchone()
                        return json.loads(row[0]).get('worker_id') if row else None
                    worker_id = wait_for(running_owner)
                    worker_pid = int(worker_id.split(':')[-2])
                    victim = next(process for process in processes if process.pid == worker_pid)
                    victim.kill()
                    victim.wait(timeout=5)
                    processes.remove(victim)
                    def recovered_done():
                        row = client.get(f'/api/tasks/{crash_task}').json()
                        return row if row['status'] in {'completed','failed','cancelled'} else None
                    final = wait_for(recovered_done)
                    self.assertEqual(final['status'], 'completed', str(final)[:2500])
                    with sqlite3.connect(directory / 'platform.db') as connection:
                        self.assertEqual(connection.execute('SELECT count(*) FROM task_runs WHERE task_id=?', (crash_task,)).fetchone()[0], 2)
                        self.assertEqual(connection.execute("SELECT count(*) FROM task_events WHERE task_id=? AND type='answer'", (crash_task,)).fetchone()[0], 1)
                        self.assertTrue(connection.execute('SELECT resumed_from_checkpoint_id FROM task_runs WHERE task_id=? ORDER BY attempt DESC LIMIT 1', (crash_task,)).fetchone()[0])
                    response = client.post('/api/policies', json={
                        'id': 'approve-output', 'name': 'Approve final output',
                        'event': 'output.before', 'scope': 'organization', 'priority': 100,
                        'match': {}, 'handler': {'type': 'builtin_rule', 'decision': 'require_approval', 'reason': '测试恢复审批'},
                    })
                    self.assertIn(response.status_code, (200, 201), response.text)
                    # 前一场景终止了一个 Worker，补回两个消费者以验证故障转移。
                    start(['-m', 'app.worker'])
                    response = client.post('/api/tasks', json={'message': '你好，请直接回答。', 'model_id': 'deterministic'})
                    self.assertEqual(response.status_code, 200, response.text)
                    policy_task = response.json()['id']

                    def policy_waiting():
                        row = client.get(f'/api/tasks/{policy_task}').json()
                        return row if row['status'] == 'waiting_approval' and row.get('result', {}).get('pending_action') == 'policy_approval' else None

                    wait_for(policy_waiting)
                    with sqlite3.connect(directory / 'platform.db') as connection:
                        metadata = json.loads(connection.execute('SELECT metadata_json FROM task_runs WHERE task_id=?', (policy_task,)).fetchone()[0])
                    owner_pid = int(metadata['worker_id'].split(':')[-2])
                    victim = next(process for process in processes if process.pid == owner_pid)
                    victim.kill()
                    victim.wait(timeout=5)
                    processes.remove(victim)
                    response = client.post(f'/api/tasks/{policy_task}/approve', json={'approved': True, 'note': '允许发布本次输出'})
                    self.assertEqual(response.status_code, 200, response.text)

                    def policy_done():
                        row = client.get(f'/api/tasks/{policy_task}').json()
                        return row if row['status'] in {'completed', 'failed', 'cancelled'} else None

                    final = wait_for(policy_done)
                    self.assertEqual(final['status'], 'completed', str(final)[:2500])
                    with sqlite3.connect(directory / 'platform.db') as connection:
                        self.assertEqual(connection.execute('SELECT count(*) FROM task_runs WHERE task_id=?', (policy_task,)).fetchone()[0], 2)
                        self.assertEqual(connection.execute("SELECT count(*) FROM task_events WHERE task_id=? AND type='answer'", (policy_task,)).fetchone()[0], 1)
                        latest = json.loads(connection.execute('SELECT metadata_json FROM task_runs WHERE task_id=? ORDER BY attempt DESC LIMIT 1', (policy_task,)).fetchone()[0])
                        self.assertTrue(latest.get('policy_approval_decisions'))
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
                    with redis.Redis.from_url(environment['REDIS_URL']) as queue:
                        keys = list(queue.scan_iter(match=environment['APP_TASK_QUEUE'] + '*'))
                        if keys:
                            queue.delete(*keys)
