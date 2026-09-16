"""只展开配置，不创建或修改部署资源。"""
import json
import shutil
import subprocess
import unittest
from pathlib import Path


@unittest.skipUnless(shutil.which('docker'), '需要 Docker Compose 配置解析器')
class ReleaseComposeTests(unittest.TestCase):
    def test_api_worker_share_environment_and_worker_can_scale(self):
        root = Path(__file__).resolve().parents[1]
        result = subprocess.run(['docker', 'compose', '--env-file', '.env.release.example', 'config', '--format', 'json'], cwd=root, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        services = json.loads(result.stdout)['services']
        api, worker = services['agentnexus'], services['worker']
        self.assertEqual(set(api['environment']), set(worker['environment']))
        # 避免失败诊断输出环境中的凭据。
        self.assertTrue(api['environment'] == worker['environment'])
        self.assertNotIn('container_name', worker)
        self.assertEqual(worker['deploy']['replicas'], 2)
        self.assertEqual(worker['environment']['APP_MAX_CONCURRENT_TASKS_PER_USER'], '1')
        self.assertEqual(worker['depends_on']['agentnexus']['condition'], 'service_healthy')
        self.assertIn('healthcheck', api)
        self.assertEqual(api['volumes'], worker['volumes'])
        self.assertNotIn('ports', services['redis'])
        for service in (api, worker):
            self.assertEqual(service['user'], '1000:1000')
            self.assertTrue(service['read_only'])
            self.assertIn('ALL', service['cap_drop'])
            self.assertEqual(service['pids_limit'], 128)
