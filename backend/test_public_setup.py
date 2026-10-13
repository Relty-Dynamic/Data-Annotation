"""Public setup exposes transport diagnostics, never a simulated storage API."""
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from backend.public_setup import create_app, public_origin


class PublicSetupTests(unittest.TestCase):
    def setUp(self):
        self.environment = patch.dict('os.environ', {
            'DATAMARK_PUBLIC_ORIGIN': 'https://datamark.reltydynamic.com',
            'DATAMARK_PUBLIC_API_ORIGIN': 'https://api-annotate.reltydynamic.com'})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.client = TestClient(create_app(), base_url='https://api-annotate.reltydynamic.com')

    def test_health_distinguishes_live_transport_from_business_readiness(self):
        response = self.client.get('/api/health')
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()['ready'])
        self.assertFalse(response.json()['storage_ready'])
        self.assertEqual(response.json()['storage_backend'], 'unconfigured')

    def test_no_storage_account_or_filesystem_operations_are_available(self):
        for path in ['/api/auth/me', '/api/projects', '/api/storage/sources', '/api/media/a/b']:
            response = self.client.get(path)
            self.assertEqual(response.status_code, 503, path)
            self.assertEqual(response.json()['code'], 'public_setup_required')
        response = self.client.post('/api/projects/open', json={'path': '/mnt/nas'},
                                    headers={'Origin': 'https://datamark.reltydynamic.com'})
        self.assertEqual(response.status_code, 503)

    def test_exact_frontend_cors_and_rejected_foreign_origins(self):
        headers = {'Origin': 'https://datamark.reltydynamic.com',
                   'Access-Control-Request-Method': 'POST', 'Access-Control-Request-Headers': 'content-type'}
        self.assertEqual(self.client.options('/api/auth/login', headers=headers).status_code, 200)
        headers['Origin'] = 'https://other.example.com'
        self.assertEqual(self.client.options('/api/auth/login', headers=headers).status_code, 400)
        self.assertEqual(self.client.post('/api/auth/login', headers=headers).status_code, 403)

    def test_websocket_connects_only_for_configured_frontend(self):
        with self.client.websocket_connect('wss://api-annotate.reltydynamic.com/api/browser/connection',
                                           headers={'Origin': 'https://datamark.reltydynamic.com'}) as socket:
            socket.send_text('keepalive')
        with self.assertRaises(WebSocketDisconnect):
            with self.client.websocket_connect('wss://api-annotate.reltydynamic.com/api/browser/connection', headers={'Origin': 'https://other.example.com'}):
                self.fail('foreign origin connected')

    def test_invalid_hosts_and_unsafe_origins_are_rejected(self):
        self.assertEqual(self.client.get('/api/health', headers={'Host': 'other.example.com'}).status_code, 400)
        for value in ['http://api.example.com', 'https://api.example.com:10443',
                      'https://api.example.com/path', 'https://user:secret@api.example.com',
                      'https://api.example.com?x=1']:
            with self.assertRaises(ValueError):
                public_origin(value)

    def test_api_can_use_8443_without_expanding_frontend_permissions(self):
        origin = 'https://api-annotate.reltydynamic.com:8443'
        self.assertEqual(public_origin(origin, api=True), origin)
        with patch.dict('os.environ', {'DATAMARK_PUBLIC_API_ORIGIN': origin}):
            client = TestClient(create_app(), base_url=origin)
            self.assertEqual(client.get('/api/health').status_code, 200)
            with client.websocket_connect(origin.replace('https:', 'wss:') + '/api/browser/connection',
                                           headers={'Origin': 'https://datamark.reltydynamic.com'}) as socket:
                socket.send_text('keepalive')
            self.assertEqual(client.post('/api/auth/login', headers={'Origin': origin}).status_code, 503)
            self.assertEqual(client.post('/api/auth/login', headers={'Origin': 'https://datamark.reltydynamic.com:8443'}).status_code, 403)
        for value in ['https://api.example.com:10443', 'http://api.example.com:8443', 'https://api.example.com:8443/path']:
            with self.assertRaises(ValueError):
                public_origin(value, api=True)
