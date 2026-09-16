import os
import re
import hashlib
import hmac
import time
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import db
from app.services import auth_service


class AuthServiceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        database = patch.object(db, "DB_PATH", Path(temporary.name) / "auth.db")
        database.start()
        self.addCleanup(database.stop)
        environment = patch.dict(os.environ, {
            "APP_ADMIN_USERNAME": "admin", "APP_ADMIN_PASSWORD": "secret",
            "APP_USER_PASSWORD": "", "APP_AUTH_ENABLED": "true",
        })
        environment.start()
        self.addCleanup(environment.stop)
        auth_service.init_schema()

    def test_login_uses_constant_time_password_check_and_session(self):
        with patch.dict(os.environ, {
            "APP_ADMIN_USERNAME": "admin",
            "APP_ADMIN_PASSWORD": "secret",
        }, clear=False):
            result = auth_service.login("admin", "secret")
            self.assertIsNotNone(result)
            token, user = result
            self.assertEqual(user["role"], "admin")
            self.assertEqual(auth_service.get_session(token)["username"], "admin")
            auth_service.logout(token)
            self.assertIsNone(auth_service.get_session(token))

    def test_wrong_password_does_not_create_session(self):
        with patch.dict(os.environ, {
            "APP_ADMIN_USERNAME": "admin",
            "APP_ADMIN_PASSWORD": "secret",
        }, clear=False):
            self.assertIsNone(auth_service.login("admin", "wrong"))

    def test_disabled_user_cannot_reuse_existing_cookie(self):
        token, user = auth_service.login("admin", "secret")
        self.assertIsNotNone(auth_service.get_session(token))
        db.execute("UPDATE users SET enabled=0 WHERE id=?", (user["user_id"],))
        self.assertIsNone(auth_service.get_session(token))

    def test_revocation_is_read_from_database(self):
        token, _ = auth_service.login("admin", "secret")
        row = db.query_one("SELECT token_hash FROM user_sessions")
        self.assertEqual(row["token_hash"], hashlib.sha256(token.encode()).hexdigest())
        db.execute("DELETE FROM user_sessions WHERE token_hash=?", (row["token_hash"],))
        self.assertIsNone(auth_service.get_session(token))

    def test_expired_session_is_rejected(self):
        token, _ = auth_service.login("admin", "secret")
        db.execute("UPDATE user_sessions SET expires_at=0")
        self.assertIsNone(auth_service.get_session(token))

    def test_http_login_logout_and_unauthenticated_access(self):
        from fastapi.testclient import TestClient
        from app.main import app

        # 无 lifespan：只使用上面创建的临时认证表，不启动调度器或写入运行数据。
        client = TestClient(app)
        self.addCleanup(client.close)
        self.assertEqual(client.get("/api/tasks").status_code, 401)
        bad = client.post("/api/auth/login", json={"username": "admin", "password": "wrong"})
        self.assertEqual(bad.status_code, 401)
        result = client.post("/api/auth/login", json={"username": "admin", "password": "secret"})
        self.assertEqual(result.status_code, 200, result.text)
        self.assertIn("HttpOnly", result.headers["set-cookie"])
        self.assertTrue(client.get("/api/auth/me").json()["authenticated"])
        self.assertEqual(client.post("/api/auth/logout").status_code, 200)
        self.assertEqual(client.get("/api/tasks").status_code, 401)

    def test_every_management_write_route_rejects_normal_member(self):
        from fastapi.testclient import TestClient
        from fastapi.routing import APIRoute
        from app.main import app

        auth_service.create_user('route-member', 'example-test-password')
        client = TestClient(app)
        self.addCleanup(client.close)
        self.assertEqual(client.post('/api/auth/login', json={'username': 'route-member', 'password': 'example-test-password'}).status_code, 200)
        management = {'users', 'models', 'agents', 'skills', 'mcp', 'policies', 'marketplace', 'presentation', 'execution-engines'}
        checked = set()
        for route in app.routes:
            if not isinstance(route, APIRoute) or not route.path.startswith('/api/'):
                continue
            if route.path.split('/')[2] not in management:
                continue
            path = re.sub(r'\{[^}]+\}', 'test-resource', route.path)
            for method in route.methods - {'GET', 'HEAD', 'OPTIONS'}:
                with self.subTest(method=method, path=route.path):
                    # 权限拒绝必须早于资源查询、正文验证及任何副作用。
                    self.assertEqual(client.request(method, path, json={}).status_code, 403)
                    checked.add((method, route.path))
        self.assertGreaterEqual(len(checked), 20, '路由枚举必须实际覆盖管理写接口')
        self.assertEqual(client.get('/api/users').status_code, 403)
        self.assertEqual(client.get('/api/audit-events').status_code, 403)

    def test_admin_can_provision_ten_distinct_users_and_revoke_one(self):
        from fastapi.testclient import TestClient
        from app.main import app

        admin = TestClient(app)
        member = TestClient(app)
        self.addCleanup(admin.close)
        self.addCleanup(member.close)
        result = admin.post("/api/auth/login", json={"username": "admin", "password": "secret"})
        self.assertEqual(result.status_code, 200)
        admin_id = result.json()["user"]["user_id"]
        self.assertEqual(admin.put(f"/api/users/{admin_id}", json={"enabled": False}).status_code, 409)
        users = []
        for index in range(10):
            result = admin.post("/api/users", json={"username": f"member-{index}", "password": "example-test-password"})
            self.assertEqual(result.status_code, 201, result.text)
            self.assertNotIn("password", result.text)
            users.append(result.json())
        self.assertEqual(len({user["id"] for user in users}), 10)
        self.assertEqual(member.post("/api/auth/login", json={"username": "member-0", "password": "example-test-password"}).status_code, 200)
        self.assertEqual(member.get("/api/users").status_code, 403)
        self.assertEqual(member.post("/api/users", json={"username": "intruder", "password": "example-test-password", "role": "admin"}).status_code, 403)
        self.assertEqual(admin.put(f"/api/users/{users[0]['id']}", json={"enabled": False}).status_code, 200)
        self.assertFalse(member.get("/api/auth/me").json()["authenticated"])
        self.assertEqual(member.get("/api/tasks").status_code, 401)

    def test_task_events_artifacts_and_commands_reject_other_owner(self):
        from fastapi.testclient import TestClient
        from app import main
        from app.services.agent_runtime import create_task_record
        from app.services.task_state import TaskStateService

        db.init_db()
        main.workspace_service.init_schema()
        TaskStateService().init_schema()
        alice = auth_service.create_user("alice", "example-test-password")
        bob = auth_service.create_user("bob", "example-test-password")
        owned = create_task_record("own", "general-agent", "default", user_id=alice["id"], conversation_id="shared-id")
        other = create_task_record("private", "general-agent", "default", user_id=bob["id"], conversation_id="shared-id")
        db.execute("INSERT INTO artifacts(id,task_id,name,kind,path,created_at,delivery_status) VALUES(?,?,?,?,?,?,?)",
                   ("private-file", other["id"], "private.md", "markdown", "unused.md", db.utc_now(), "published"))
        client = TestClient(main.app)
        self.addCleanup(client.close)
        self.assertEqual(client.post("/api/auth/login", json={"username": "alice", "password": "example-test-password"}).status_code, 200)
        listing = client.get("/api/tasks", params={"user_id": bob["id"]})
        self.assertEqual([row["id"] for row in listing.json()], [owned["id"]])
        self.assertNotIn("private", client.get("/api/conversations/shared-id/messages").text)
        for suffix in ("", "/runtime", "/events", "/events/stream", "/artifacts"):
            self.assertEqual(client.get(f"/api/tasks/{other['id']}{suffix}").status_code, 403, suffix)
        for operation in ("cancel", "retry", "resume"):
            self.assertEqual(client.post(f"/api/tasks/{other['id']}/{operation}", json={}).status_code, 403, operation)
        self.assertEqual(client.get("/api/artifacts").json(), [])
        for suffix in ("", "/preview", "/download"):
            self.assertEqual(client.get(f"/api/artifacts/private-file{suffix}").status_code, 403)
        access_audit = db.query_all("SELECT route,status_code FROM audit_events WHERE method='GET'")
        self.assertIn({'route':'/api/artifacts/{artifact_id}/preview','status_code':403},access_audit)
        self.assertIn({'route':'/api/artifacts/{artifact_id}/download','status_code':403},access_audit)
        self.assertEqual(client.post("/api/skills", json={}).status_code, 403)
        self.assertIsNone(auth_service.current_identity.get())

    def test_workspace_membership_grant_viewer_and_revoke(self):
        from fastapi.testclient import TestClient
        from app import main
        from app.services.agent_runtime import create_task_record
        from app.services.context_service import ExecutionScope
        from app.services.task_state import TaskStateService

        db.init_db()
        main.workspace_service.init_schema()
        TaskStateService().init_schema()
        alice = auth_service.create_user("alice", "example-test-password")
        admin_id = db.query_one("SELECT id FROM users WHERE username='admin'")["id"]
        main.workspace_service.create_workspace(ExecutionScope(user_id=admin_id), workspace_id="project-a", name="Project A")
        task = create_task_record("private project", "general-agent", "project-a", user_id=alice["id"])
        admin, member = TestClient(main.app), TestClient(main.app)
        self.addCleanup(admin.close)
        self.addCleanup(member.close)
        admin.post("/api/auth/login", json={"username": "admin", "password": "secret"})
        member.post("/api/auth/login", json={"username": "alice", "password": "example-test-password"})
        self.assertEqual(member.get("/api/workspaces/project-a").status_code, 403)
        self.assertEqual(member.get("/api/tasks").json(), [])
        grant = f"/api/workspaces/project-a/members/{alice['id']}"
        self.assertEqual(admin.put('/api/workspaces/project-a/members/by-username/alice', json={"role": "viewer"}).status_code, 200)
        self.assertEqual(member.get("/api/workspaces/project-a").status_code, 200)
        self.assertEqual(member.get(f"/api/tasks/{task['id']}").status_code, 200)
        self.assertEqual(member.post("/api/tasks", json={"message": "test", "workspace": "project-a"}).status_code, 403)
        for operation in ("cancel", "retry", "resume"):
            self.assertEqual(member.post(f"/api/tasks/{task['id']}/{operation}", json={}).status_code, 403)
        self.assertEqual(member.put(grant, json={"role": "member"}).status_code, 403)
        self.assertEqual(member.put('/api/workspaces/project-a/members/by-username/alice', json={"role": "member"}).status_code, 403)
        self.assertEqual(admin.delete(grant).status_code, 200)
        self.assertEqual(member.get(f"/api/tasks/{task['id']}").status_code, 403)
        self.assertEqual(member.get("/api/tasks").json(), [])
        for endpoint in ("knowledge-bases", "memories", "context/effective", "expert-teams", "conversation-summaries", "loops", "notifications"):
            self.assertEqual(member.get(f"/api/{endpoint}", params={"workspace_id": "project-a", "user_id": admin_id}).status_code, 403, endpoint)

    def test_member_retry_response_matches_authenticated_dispatch_scope(self):
        from fastapi.testclient import TestClient
        from app import main

        db.init_db()
        main.workspace_service.init_schema()
        user = auth_service.create_user('retry-member', 'example-test-password')
        client = TestClient(main.app)
        self.addCleanup(client.close)
        self.assertEqual(client.post('/api/auth/login', json={'username': 'retry-member', 'password': 'example-test-password'}).status_code, 200)
        with patch.object(main.expert_team_service, 'validate_member_retry') as validate, patch.object(main, '_schedule_member_retry') as schedule:
            response = client.post('/api/expert-team-runs/team-a/members/member-a/retry',
                                   params={'user_id': 'forged-user', 'organization_id': 'forged-org'}, json={})
        self.assertEqual(response.status_code, 202, response.text)
        expected = {'organization_id': 'local-org', 'workspace_id': 'default', 'user_id': user['id']}
        self.assertEqual(response.json()['scope'], expected)
        scope = schedule.call_args.args[2]
        self.assertEqual(scope, validate.call_args.args[2])
        self.assertEqual(scope.user_id, user['id'])
        self.assertEqual(scope.organization_id, 'local-org')

    def test_automation_and_notifications_cannot_use_another_identity(self):
        from fastapi.testclient import TestClient
        from app import main

        db.init_db()
        main.workspace_service.init_schema()
        alice = auth_service.create_user("alice", "example-test-password")
        bob = auth_service.create_user("bob", "example-test-password")
        now = db.utc_now()
        db.execute("INSERT INTO loops(id,name,prompt,user_id,created_at,updated_at) VALUES(?,?,?,?,?,?)", ("bob-loop", "private", "private", bob["id"], now, now))
        db.execute("INSERT INTO notifications(id,title,user_id,created_at) VALUES(?,?,?,?)", ("bob-note", "private", bob["id"], now))
        client = TestClient(main.app)
        self.addCleanup(client.close)
        client.post("/api/auth/login", json={"username": "alice", "password": "example-test-password"})
        for endpoint in ("loops", "notifications"):
            self.assertEqual(client.get(f"/api/{endpoint}", params={"user_id": bob["id"]}).json(), [])
        for suffix in ("", "/runs", "/trigger-events"):
            self.assertEqual(client.get(f"/api/loops/bob-loop{suffix}").status_code, 403)
        for suffix in ("start", "pause", "run"):
            self.assertEqual(client.post(f"/api/loops/bob-loop/{suffix}").status_code, 403)
        self.assertEqual(client.delete("/api/loops/bob-loop").status_code, 403)
        self.assertEqual(client.post("/api/notifications/bob-note/read").status_code, 403)
        self.assertEqual(db.query_one("SELECT status FROM notifications WHERE id='bob-note'")["status"], "unread")

    def test_private_knowledge_memory_and_upload_reject_forged_user(self):
        from fastapi.testclient import TestClient
        from app import main
        from app.services.context_service import ExecutionScope

        db.init_db()
        main.workspace_service.init_schema()
        main.knowledge_service.init_schema()
        main.context_service.init_schema()
        auth_service.create_user("alice", "example-test-password")
        bob = auth_service.create_user("bob", "example-test-password")
        scope = ExecutionScope(user_id=bob["id"])
        base = main.knowledge_service.create_base(scope, base_id="bob-base", name="Private", visibility="private")
        memory = main.context_service.create_memory(scope, scope_type="user", content="Private preference")
        db.execute("INSERT INTO uploads(id,name,path,size,created_at) VALUES('bob-upload','private.txt','unused',1,?)", (db.utc_now(),))
        db.execute("INSERT INTO upload_owners VALUES('bob-upload',?)", (bob["id"],))
        client = TestClient(main.app)
        self.addCleanup(client.close)
        client.post("/api/auth/login", json={"username": "alice", "password": "example-test-password"})
        for endpoint in (f"knowledge-bases/{base['id']}", f"memories/{memory['id']}"):
            self.assertEqual(client.get(f"/api/{endpoint}", params={"user_id": bob["id"]}).status_code, 404)
        result = client.post("/api/knowledge-bases/bob-base/documents/upload", json={"upload_id": "bob-upload"})
        self.assertEqual(result.status_code, 403)

    def test_cross_site_writes_rejected_and_audit_never_contains_credentials(self):
        from fastapi.testclient import TestClient
        from app.main import app

        client = TestClient(app)
        self.addCleanup(client.close)
        credentials = {"username": "admin", "password": "secret"}
        self.assertEqual(client.post('/api/auth/login', json=credentials, headers={'Origin': 'https://other.example'}).status_code, 403)
        login = client.post('/api/auth/login', json=credentials, headers={'Origin': 'http://testserver'})
        self.assertEqual(login.status_code, 200)
        token = client.cookies.get(auth_service.SESSION_COOKIE)
        response = client.post('/api/users', json={'username': 'new-user', 'password': 'never-record-this-password'}, headers={'Origin': 'https://other.example'})
        self.assertEqual(response.status_code, 403)
        self.assertIsNone(db.query_one("SELECT id FROM users WHERE username='new-user'"))
        events = client.get('/api/audit-events')
        self.assertEqual(events.status_code, 200)
        self.assertNotIn('secret', events.text)
        self.assertNotIn(token, events.text)
        self.assertNotIn('never-record-this-password', events.text)
        login_audit = next(row for row in events.json() if row['route'] == '/api/auth/login')
        self.assertEqual(login_audit['user_id'], login.json()['user']['user_id'])
        self.assertEqual(login_audit['status_code'], 200)

    def test_login_limits_persist_and_expire(self):
        for _ in range(10):
            self.assertTrue(auth_service.reserve_login_attempt('target', 'source', now=100))
        self.assertFalse(auth_service.reserve_login_attempt('target', 'other-source', now=101))
        auth_service.init_schema()
        self.assertFalse(auth_service.reserve_login_attempt('target', 'source', now=110))
        self.assertTrue(auth_service.reserve_login_attempt('target', 'source', now=161))
        for index in range(59):
            self.assertTrue(auth_service.reserve_login_attempt(f'user-{index}', 'source', now=161))
        self.assertFalse(auth_service.reserve_login_attempt('another-user', 'source', now=161))

    def test_http_login_returns_retry_after_and_https_secure_cookie(self):
        from fastapi.testclient import TestClient
        from app.main import app

        client = TestClient(app, base_url='https://testserver')
        self.addCleanup(client.close)
        result = client.post('/api/auth/login', json={'username': 'admin', 'password': 'secret'})
        self.assertEqual(result.status_code, 200)
        self.assertIn('Secure', result.headers['set-cookie'])
        with patch.object(auth_service, 'reserve_login_attempt', return_value=False):
            result = client.post('/api/auth/login', json={'username': 'admin', 'password': 'secret'})
        self.assertEqual(result.status_code, 429)
        self.assertEqual(result.headers['retry-after'], '60')

    def test_signed_webhook_without_cookie_preserves_permissions_and_deduplication(self):
        from fastapi.testclient import TestClient
        from app import main

        db.init_db()
        main.workspace_service.init_schema()
        owner = auth_service.create_user('owner', 'example-password')
        now = db.utc_now()
        db.execute("INSERT INTO loops(id,name,prompt,user_id,created_at,updated_at,status,trigger_type,webhook_secret_ciphertext) VALUES(?,?,?,?,?,?,'active','webhook','test-cipher')", ('signed-loop', 'Test', 'Test', owner['id'], now, now))
        client = TestClient(main.app)
        self.addCleanup(client.close)
        body = b'{"value":1}'
        timestamp = str(time.time())
        signature = hmac.new(b'test-webhook-secret', timestamp.encode() + b'.' + body, hashlib.sha256).hexdigest()
        headers = {'X-Automation-Timestamp': timestamp, 'X-Automation-Signature': signature, 'Idempotency-Key': 'test-event'}
        with patch.object(main.secret_store, 'decrypt', return_value='test-webhook-secret'), patch.object(main.secret_store, 'encrypt', return_value='test-encrypted'), patch.object(main.loop_scheduler, 'is_busy', return_value=True):
            self.assertEqual(client.post('/api/loops/signed-loop/webhook', content=body).status_code, 401)
            bad = {**headers, 'X-Automation-Signature': 'invalid'}
            self.assertEqual(client.post('/api/loops/signed-loop/webhook', content=body, headers=bad).status_code, 401)
            first = client.post('/api/loops/signed-loop/webhook', content=body, headers=headers)
            self.assertEqual(first.status_code, 202, first.text)
            repeated = client.post('/api/loops/signed-loop/webhook', content=body, headers=headers)
            self.assertTrue(repeated.json()['duplicate'])
            self.assertEqual(first.json()['event']['id'], repeated.json()['event']['id'])
            db.execute('UPDATE users SET enabled=0 WHERE id=?', (owner['id'],))
            self.assertEqual(client.post('/api/loops/signed-loop/webhook', content=body, headers={**headers, 'Idempotency-Key': 'next-event'}).status_code, 403)
        self.assertEqual(client.get('/api/loops/signed-loop').status_code, 401)
        self.assertEqual(len(db.query_all('SELECT id FROM automation_trigger_events')), 1)

    def test_members_cannot_mutate_organization_context_or_forge_actor(self):
        from fastapi.testclient import TestClient
        from app import main
        from app.services.context_service import ExecutionScope

        db.init_db()
        main.workspace_service.init_schema()
        main.context_service.init_schema()
        main.knowledge_service.init_schema()
        member = auth_service.create_user('member', 'example-password')
        admin_id = db.query_one("SELECT id FROM users WHERE username='admin'")['id']
        scope = ExecutionScope(user_id=admin_id)
        memory = main.context_service.create_memory(scope, scope_type='organization', content='组织约束')
        base = main.knowledge_service.create_base(scope, base_id='org-base', name='Organization', visibility='organization')
        client = TestClient(main.app)
        self.addCleanup(client.close)
        client.post('/api/auth/login', json={'username': 'member', 'password': 'example-password'})
        self.assertEqual(client.get(f"/api/memories/{memory['id']}").status_code, 200)
        self.assertEqual(client.delete(f"/api/memories/{memory['id']}").status_code, 403)
        self.assertEqual(client.post(f"/api/memories/{memory['id']}/disable").status_code, 403)
        self.assertEqual(client.delete(f"/api/knowledge-bases/{base['id']}").status_code, 403)
        self.assertEqual(client.post('/api/memories', json={'scope_type': 'organization', 'content': 'override'}).status_code, 403)
        self.assertEqual(client.post('/api/knowledge-bases', json={'id': 'new-org', 'name': 'Organization', 'visibility': 'organization'}).status_code, 403)
        result = client.post('/api/memories', json={'scope_type': 'user', 'content': 'my preference', 'user_id': admin_id})
        self.assertEqual(result.status_code, 201, result.text)
        self.assertEqual(result.json()['created_by'], member['id'])

    def test_expert_template_creation_uses_authenticated_owner(self):
        from fastapi.testclient import TestClient
        from app import main
        db.init_db()
        main.workspace_service.init_schema()
        member = auth_service.create_user('member', 'example-password')
        client = TestClient(main.app)
        self.addCleanup(client.close)
        client.post('/api/auth/login',json={'username':'member','password':'example-password'})
        for visibility in ('organization','public'):
            response = client.post('/api/expert-templates',json={'id':'shared-template','name':'Shared','visibility':visibility})
            self.assertEqual(response.status_code,403)
        response = client.post('/api/expert-templates',json={'id':'private-template','name':'Private','visibility':'private','owner_user_id':'forged-user','organization_id':'forged-org'})
        self.assertEqual(response.status_code,201,response.text)
        self.assertEqual(response.json()['owner_user_id'],member['id'])
        self.assertEqual(response.json()['organization_id'],'local-org')
        self.assertEqual(client.put('/api/expert-templates/private-template',json={'visibility':'public'}).status_code,403)

    def test_private_agent_is_hidden_and_cannot_be_selected(self):
        from fastapi.testclient import TestClient
        from app import main
        from app.services.agent_runtime import create_task_record
        from app.worker import execution_authorized
        db.init_db()
        main.workspace_service.init_schema()
        alice = auth_service.create_user('alice','example-password')
        bob = auth_service.create_user('bob','example-password')
        now = db.utc_now()
        db.execute("INSERT INTO agents(id,name,description,created_at,updated_at,owner_user_id,visibility) VALUES('bob-agent','Private','private',?,?,?,'private')", (now,now,bob['id']))
        client = TestClient(main.app)
        self.addCleanup(client.close)
        client.post('/api/auth/login',json={'username':'alice','password':'example-password'})
        self.assertNotIn('bob-agent',client.get('/api/agents').text)
        self.assertEqual(client.post('/api/tasks',json={'message':'test','agent_id':'bob-agent'}).status_code,403)
        self.assertEqual(client.post('/api/loops',json={'name':'Test','prompt':'Test','agent_id':'bob-agent','model_id':'deterministic'}).status_code,403)
        task = create_task_record('queued','bob-agent',user_id=alice['id'])
        self.assertFalse(execution_authorized(task['id']))
        db.execute("UPDATE agents SET visibility='organization' WHERE id='bob-agent'")
        self.assertTrue(execution_authorized(task['id']))


if __name__ == "__main__":
    unittest.main()
