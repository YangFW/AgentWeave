import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from app import db
from app.services import auth_service
from scripts.repair_test_account import repair


class RepairTestAccountTests(unittest.TestCase):
    def test_deployment_refuses_known_test_admin_without_modifying_it(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(db,'DB_PATH',Path(directory)/'test.db'), patch.dict('os.environ',{'APP_ADMIN_PASSWORD':'','APP_USER_PASSWORD':'','APP_AUTH_ENABLED':'true'}):
            auth_service.init_schema()
            auth_service.create_user('admin','secret','admin')
            with self.assertRaisesRegex(RuntimeError,'测试管理员凭据'):
                auth_service.validate_deployment_admin()
            self.assertTrue(repair(db.DB_PATH)['matched_test_credential'])
            repair(db.DB_PATH,apply=True)
            auth_service.validate_deployment_admin()

    def test_default_is_read_only_and_apply_revokes_old_session(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(db,'DB_PATH',Path(directory)/'test.db'), patch.dict('os.environ',{'APP_ADMIN_PASSWORD':'','APP_USER_PASSWORD':''}):
            auth_service.init_schema()
            auth_service.create_user('admin','secret','admin')
            token,_ = auth_service.login('admin','secret')
            self.assertEqual(repair(db.DB_PATH),{'matched_test_credential':True,'changed':False})
            self.assertIsNotNone(auth_service.get_session(token))
            result = repair(db.DB_PATH,apply=True)
            self.assertTrue(Path(result['backup']).is_file())
            self.assertIsNone(auth_service.get_session(token))
            self.assertIsNone(auth_service.login('admin','secret'))
            self.assertIsNotNone(auth_service.login('admin',result['password']))
            self.assertFalse(repair(db.DB_PATH,apply=True)['changed'])
