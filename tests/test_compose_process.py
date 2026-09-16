"""使用已构建镜像创建独立 Compose 项目，不修改现有服务和数据卷。"""
import json
import os
import subprocess
import tempfile
import time
import unittest
import uuid
from pathlib import Path

import httpx
from scripts.data_snapshot import backup, restore


@unittest.skipUnless(os.getenv('APP_TEST_IMAGE'), '需要指定已构建测试镜像')
class ComposeProcessTests(unittest.TestCase):
    def test_non_root_read_only_api_and_workers(self):
        root = Path(__file__).resolve().parents[1]
        project = 'agentnexus-proof-' + uuid.uuid4().hex[:10]
        with tempfile.TemporaryDirectory() as temporary:
            data = Path(temporary) / 'data'
            data.mkdir()
            rendered = subprocess.run(['docker','compose','--env-file','.env.release.example','config','--format','json'], cwd=root, capture_output=True,text=True,check=True)
            config = json.loads(rendered.stdout)
            config.pop('name',None)
            config['networks'] = {'default':{'name':project+'-network'}}
            config['volumes'] = {'agentnexus-redis':{'name':project+'-redis'}}
            for name,service in config['services'].items():
                service.pop('container_name',None)
                if name != 'redis':
                    service.pop('build',None)
                    service['image'] = os.environ['APP_TEST_IMAGE']
                    service['user'] = f'{os.getuid()}:{os.getgid()}'
                    service['volumes'] = [{'type':'bind','source':str(data),'target':'/app/data'}]
                    environment = service['environment']
                    for key in list(environment):
                        if key.startswith('APP_ALLOW_'):
                            environment[key] = 'false'
                    for key in ('OPENAI_API_KEY','TAVILY_API_KEY','BRAVE_SEARCH_API_KEY','APP_SECRET_KEY','APP_USER_PASSWORD'):
                        environment[key] = ''
                    environment.update(APP_AUTH_ENABLED='true', APP_ADMIN_USERNAME='test-admin', APP_ADMIN_PASSWORD='compose-test-password', APP_DB_PATH='/app/data/platform.db', APP_UPLOAD_DIR='/app/data/uploads',APP_ARTIFACT_DIR='/app/data/artifacts',APP_SECRET_KEY_FILE='/app/data/.secret_key', APP_POLICY_RULES_JSON='[]')
            config['services']['agentnexus']['ports'] = [{'target':8000,'published':'0','host_ip':'127.0.0.1','protocol':'tcp'}]
            file = Path(temporary) / 'compose.json'
            file.write_text(json.dumps(config))
            command = ['docker','compose','-p',project,'-f',str(file)]
            def compose(*args):
                result = subprocess.run([*command,*args],capture_output=True,text=True)
                details = result.stderr
                if result.returncode and args[0] == 'up':
                    logs = subprocess.run([*command,'logs','--no-color','--tail','60'],capture_output=True,text=True)
                    details += logs.stdout
                self.assertEqual(result.returncode,0,details)
                return result
            try:
                compose('up','-d','--no-build','--pull','never','--scale','worker=2')
                address = compose('port','agentnexus','8000').stdout.strip()
                with httpx.Client(base_url='http://'+address, timeout=5) as client:
                    deadline = time.monotonic()+30
                    while time.monotonic()<deadline:
                        if client.get('/api/readiness').status_code == 200:
                            break
                        time.sleep(0.2)
                    self.assertEqual(client.get('/api/readiness').status_code,200)
                    self.assertEqual(client.post('/api/auth/login',json={'username':'test-admin','password':'compose-test-password'}).status_code,200)
                    uploaded = client.post('/api/uploads',files={'file':('source.txt',b'local test content','text/plain')})
                    self.assertEqual(uploaded.status_code,200,uploaded.text)
                    response = client.post('/api/tasks',json={'message':'你好','model_id':'deterministic'})
                    self.assertEqual(response.status_code,200,response.text)
                    task_id = response.json()['id']
                    deadline = time.monotonic()+30
                    while time.monotonic()<deadline:
                        result = client.get('/api/tasks/'+task_id).json()
                        if result['status'] in {'completed','failed','cancelled'}:
                            break
                        time.sleep(0.2)
                    self.assertEqual(result['status'],'completed',str(result)[:1000])
                    response = client.post('/api/tasks',json={'message':'生成一个 Markdown 文档，标题为部署验收，正文为本次测试已完成。','model_id':'deterministic'})
                    self.assertEqual(response.status_code,200,response.text)
                    document_task = response.json()['id']
                    deadline = time.monotonic()+30
                    while time.monotonic()<deadline:
                        result = client.get('/api/tasks/'+document_task).json()
                        if result['status'] in {'completed','failed','cancelled'}:
                            break
                        time.sleep(0.2)
                    self.assertEqual(result['status'],'completed',str(result)[:1000])
                    artifacts = client.get(f'/api/tasks/{document_task}/artifacts').json()
                    self.assertTrue(artifacts, str(result)[:1000])
                    downloaded = client.get('/api/artifacts/'+artifacts[0]['id']+'/download')
                    self.assertEqual(downloaded.status_code,200)
                    self.assertTrue(downloaded.content)
                    cookies = client.cookies
                    expected_document = downloaded.content
                    artifact_id = artifacts[0]['id']
                compose('stop','worker','agentnexus')
                snapshot = Path(temporary) / 'snapshot'
                recovered = Path(temporary) / 'recovered'
                backup(data,snapshot,quiesced=True)
                restore(snapshot,recovered)
                for name in ('agentnexus','worker'):
                    config['services'][name]['volumes'] = [{'type':'bind','source':str(recovered),'target':'/app/data'}]
                file.write_text(json.dumps(config))
                compose('up','-d','--no-build','--pull','never','--force-recreate','--scale','worker=2','agentnexus','worker')
                restored_address = compose('port','agentnexus','8000').stdout.strip()
                with httpx.Client(base_url='http://'+restored_address,cookies=cookies,timeout=5) as restored_client:
                    self.assertTrue(restored_client.get('/api/auth/me').json()['authenticated'])
                    self.assertEqual(restored_client.get('/api/tasks/'+document_task).json()['status'],'completed')
                    restored_document = restored_client.get('/api/artifacts/'+artifact_id+'/download')
                    self.assertEqual(restored_document.status_code,200)
                    self.assertEqual(restored_document.content,expected_document)
                check = compose('exec','-T','agentnexus','python','-c',"import os; assert os.getuid()!=0; print(os.getuid())")
                self.assertNotEqual(check.stdout.strip(),'0')
                container = compose('ps','-q','agentnexus').stdout.strip()
                inspect = json.loads(subprocess.run(['docker','inspect',container],capture_output=True,text=True,check=True).stdout)[0]
                self.assertTrue(inspect['HostConfig']['ReadonlyRootfs'])
                self.assertIn('ALL',inspect['HostConfig']['CapDrop'])
                self.assertTrue(list((data/'uploads').iterdir()))
            finally:
                compose('down','--volumes','--remove-orphans')
