"""NAS and public logins use one real fixture database, without media access."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import httpx

from fastapi.testclient import TestClient

from backend.auth import AuthStore
from backend.app import create_app as nas_app
from backend.public_setup import create_app as public_app
from backend.shared_accounts import AccountClient, create_app as account_app


class SharedAccountTests(unittest.TestCase):
    def test_private_account_admin_requires_admin_session_and_csrf(self):
        with TestClient(account_app(self.root), base_url='http://accounts:8766') as bridge:
            worker = bridge.post('/v1/login', json={'username': 'worker',
                'password': 'another long safe password', 'client': 'fixture-worker'}).json()
            response = bridge.post('/v1/admin/accounts', json={'token': worker['token'],
                'csrf': worker['csrf'], 'action': 'list'})
            self.assertEqual(403, response.status_code)
            admin = bridge.post('/v1/login', json={'username': 'admin',
                'password': 'correct horse battery staple', 'client': 'fixture-admin'}).json()
            request = {'token': admin['token'], 'action': 'list'}
            self.assertEqual(403, bridge.post('/v1/admin/accounts', json=request).status_code)
            response = bridge.post('/v1/admin/accounts', json={**request, 'csrf': admin['csrf']})
            self.assertEqual(200, response.status_code)
            self.assertTrue(response.json()['result'])
            self.assertNotIn('password_hash', response.json()['result'][0])
            bridge.post('/v1/logout', json={'token': admin['token']})
            self.assertEqual(401, bridge.post('/v1/admin/accounts', json={**request, 'csrf': admin['csrf']}).status_code)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='shared-accounts-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.auth = AuthStore(self.root)
        self.user = self.auth.create_user('admin', '管理员', 'correct horse battery staple', 'admin')
        self.worker = self.auth.create_user('worker', '标注员', 'another long safe password')
        self.environment = patch.dict('os.environ', {
            'DATAMARK_PUBLIC_ORIGIN': 'https://datamark.reltydynamic.com',
            'DATAMARK_PUBLIC_API_ORIGIN': 'https://api-annotate.reltydynamic.com:8443',
            'DATAMARK_SHARED_ACCOUNTS': '0'})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.bridge = TestClient(account_app(self.root), base_url='http://accounts:8766')
        self.client = TestClient(public_app(accounts=AccountClient(self.bridge)), base_url='https://api-annotate.reltydynamic.com:8443')
        self.origin = {'Origin': 'https://datamark.reltydynamic.com'}
        self.addCleanup(self.client.close)
        self.addCleanup(self.bridge.close)

    def login(self, password='another long safe password'):
        response = self.client.post('/api/auth/login', json={'username': 'worker', 'password': password}, headers=self.origin)
        self.assertEqual(response.status_code, 200, response.text)
        return response

    def test_public_browser_encrypted_login_uses_same_account_and_secure_cookie(self):
        from backend.test_login_encryption import envelope
        challenge = self.client.get('/api/auth/login-challenge')
        self.assertEqual(200, challenge.status_code)
        self.assertEqual('no-store', challenge.headers['cache-control'])
        body = envelope(challenge.json())
        denied = self.client.post('/api/auth/login-encrypted', json=body, headers={'Origin': 'https://other.example.com'})
        self.assertEqual(403, denied.status_code)
        response = self.client.post('/api/auth/login-encrypted', json=body, headers=self.origin)
        self.assertEqual(200, response.status_code, response.text)
        self.assertEqual(self.worker['id'], response.json()['user']['id'])
        self.assertIn('Secure', response.headers['set-cookie'])
        self.assertIn('HttpOnly', response.headers['set-cookie'])
        self.assertEqual(400, self.client.post('/api/auth/login-encrypted', json=body, headers=self.origin).status_code)

    def test_nas_and_public_login_return_same_identity_in_one_database(self):
        # NAS app retains its original account root and login behavior.
        with patch.dict('os.environ', {'DATAMARK_PUBLIC_ORIGIN': '', 'DATAMARK_PUBLIC_API_ORIGIN': ''}):
            app = nas_app(self.root)
        with TestClient(app, base_url='http://127.0.0.1') as nas:
            logged = nas.post('/api/auth/login', json={'username': 'worker', 'password': 'another long safe password'})
            self.assertEqual(logged.status_code, 200, logged.text)
            response = self.login()
            self.assertEqual(logged.json()['user'], response.json()['user'])
            self.assertEqual(response.json()['user']['id'], self.worker['id'])
            self.assertNotIn('token', response.json())
            self.assertEqual(len(list(self.root.rglob('auth.sqlite3'))), 1)
            self.assertEqual(self.client.get('/api/auth/me').json()['id'], self.worker['id'])
            self.assertIn('HttpOnly', response.headers.get_list('set-cookie')[0])
            self.assertIn('Secure', response.headers.get_list('set-cookie')[0])

    def test_nas_password_reset_revokes_public_login_and_accepts_new_password(self):
        self.login()
        self.auth.reset_password(self.worker['id'], 'changed shared password')
        self.assertEqual(self.client.get('/api/auth/me').status_code, 401)
        old = self.client.post('/api/auth/login', json={'username': 'worker', 'password': 'another long safe password'}, headers=self.origin)
        self.assertEqual(old.status_code, 401)
        self.login('changed shared password')

    def test_nas_deactivation_revokes_public_session(self):
        self.login()
        self.auth.set_active(self.worker['id'], False)
        self.assertEqual(self.client.get('/api/auth/me').status_code, 401)

    def test_logout_requires_origin_and_matching_csrf(self):
        csrf = self.login().json()['csrf']
        self.assertEqual(self.client.post('/api/auth/logout', headers=self.origin).status_code, 403)
        self.assertEqual(self.client.post('/api/auth/logout', headers={'Origin': 'https://other.example.com', 'X-CSRF-Token': csrf}).status_code, 403)
        response = self.client.post('/api/auth/logout', headers={**self.origin, 'X-CSRF-Token': csrf})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.client.get('/api/auth/me').status_code, 401)

    def test_account_ready_does_not_enable_nas_or_s3_business_routes(self):
        health = self.client.get('/api/health').json()
        self.assertTrue(health['accounts_ready'])
        self.assertFalse(health['storage_ready'])
        self.assertFalse(health['ready'])
        self.login()
        for path in ['/api/projects', '/api/storage/sources', '/api/media/a/b', '/api/users']:
            self.assertEqual(self.client.get(path).status_code, 503)
            self.assertEqual(self.bridge.get(path).status_code, 404)

    def test_missing_existing_database_fails_without_creating_a_new_one(self):
        root = self.root / 'missing'
        with self.assertRaises(RuntimeError):
            account_app(root)
        self.assertFalse(root.exists())

    def test_unavailable_account_service_fails_closed_and_reports_not_ready(self):
        def offline(request):
            raise httpx.ConnectError('offline', request=request)
        transport = httpx.MockTransport(offline)
        bridge = AccountClient(httpx.Client(base_url='http://accounts:8766', transport=transport))
        with TestClient(public_app(accounts=bridge), base_url='https://api-annotate.reltydynamic.com:8443') as client:
            self.assertFalse(client.get('/api/health').json()['accounts_ready'])
            self.assertEqual(client.get('/api/auth/me').status_code, 503)
            response = client.post('/api/auth/login', json={'username': 'worker', 'password': 'another long safe password'}, headers=self.origin)
            self.assertEqual(response.status_code, 503)
            self.assertNotIn('set-cookie', response.headers)

    def test_login_throttling_is_persistent_in_shared_database(self):
        for _ in range(5):
            response = self.client.post('/api/auth/login', json={'username': 'worker', 'password': 'wrong password'}, headers=self.origin)
            self.assertEqual(response.status_code, 401)
        self.assertEqual(self.client.post('/api/auth/login', json={'username': 'worker', 'password': 'another long safe password'}, headers=self.origin).status_code, 429)
        with self.auth.connection() as con:
            self.assertGreater(con.execute('SELECT COUNT(*) FROM login_attempts').fetchone()[0], 0)
