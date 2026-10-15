"""R2 contracts tested with conditional in-memory objects, never production keys."""
import hashlib
import io
import json
import os
import shutil
import subprocess
import time
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import HTTPException
from fastapi.testclient import TestClient
from botocore.exceptions import ClientError

from backend.public_setup import create_app
from backend.r2_service import R2ProjectService, capture_prefix
from backend.r2_store import R2Store, object_key
from backend.r2_uploads import R2Uploads
from backend.service import document_bytes


class MemoryObjects:
    def __init__(self):
        self.objects = {}
        self.generation = 0

    def health(self):
        return True

    def put(self, key, payload, *, etag=None, create=False, **kwargs):
        current = self.objects.get(key)
        if (create and current is not None) or (not create and (current is None or current[1] != etag)):
            raise HTTPException(409, 'version conflict')
        self.generation += 1
        version = str(self.generation)
        self.objects[key] = (payload, version)
        return version

    def get(self, key):
        if key not in self.objects:
            raise HTTPException(404)
        return self.objects[key][0]

    def optional_json(self, key):
        return (json.loads(self.objects[key][0]), self.objects[key][1]) if key in self.objects else (None, None)

    def put_if_absent(self, key, payload):
        if key in self.objects:
            if self.get(key) != payload:
                raise HTTPException(409)
        else:
            self.put(key, payload, create=True)

    def download(self, key, destination, checksum, size):
        data = self.get(key)
        if len(data) != size or hashlib.sha256(data).hexdigest() != checksum:
            raise HTTPException(409, 'checksum mismatch')
        if destination.is_file() and destination.read_bytes() == data:
            return
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(data)

    def put_file(self, key, path):
        self.put_if_absent(key, path.read_bytes())

    def signed_url(self, key, **kwargs):
        return ('https://' + 'a' * 32 + '.r2.cloudflarestorage.com/test-bucket/' + key +
                '?X-Amz-Algorithm=AWS4-HMAC-SHA256&X-Amz-Signature=' + 'b' * 64 + '&X-Amz-Expires=1800')

    def list(self, *args, **kwargs):
        yield {}


USER = {'id': 'worker', 'username': 'worker', 'display_name': 'Worker', 'role': 'annotator', 'can_upload': True}
OTHER = {**USER, 'id': 'other'}
PREFIX = 'daily/20261015/1015test'
NAME = 'A00001_20261015120000_0001.mp4'


class R2ProjectTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.provider = MemoryObjects()
        self.service = R2ProjectService(self.root, self.provider)
        self.addCleanup(self.service.sessions.close)
        self.addCleanup(self.service.previews.close)
        probe = patch.object(self.service, 'probe', return_value={'duration_ms': 1000, 'codec': 'h264', 'pixel_format': 'yuv420p', 'audio_codecs': [], 'media_start_seconds': 0, 'format_start_seconds': 0})
        self.probe_patch = probe
        probe.start()
        self.addCleanup(probe.stop)
        payload = b'explicit-test-fixture-not-real-video'
        self.manifest = {'prefix': PREFIX, 'files': [{'name': NAME, 'key': 'FPV/' + NAME,
            'sha256': hashlib.sha256(payload).hexdigest(), 'size': len(payload)}]}
        self.provider.put(PREFIX + '/FPV/' + NAME, payload, create=True)
        self.provider.put(PREFIX + '/manifest.json', document_bytes(self.manifest), create=True)

    def open(self):
        return self.service.open_capture(PREFIX, actor=USER)

    def test_open_defaults_and_private_paths(self):
        value = self.open()
        self.assertEqual(value['storage_mode'], 's3')
        self.assertIsNone(value['source_dir'])
        self.assertEqual(value['owner_id'], USER['id'])
        self.assertEqual(value['collection_folder_name'], '1015test')
        self.assertEqual(value['fixed_tracks'], ['scene', 'posture', 'category'])
        self.assertFalse(self.service.desktop_cache.enabled)
        self.assertTrue(self.service.allowed(USER, value['id']))
        self.assertFalse(self.service.allowed(OTHER, value['id']))

    def test_takeover_requires_inspection_and_preserves_draft_until_confirmed(self):
        value = self.open()
        before = self.service.load(value['id'])
        with self.assertRaises(HTTPException) as denied:
            self.service.open_capture(PREFIX, actor=OTHER, body={'claim_mode': 'restart'})
        self.assertEqual(denied.exception.status_code, 409)
        self.assertEqual(before, self.service.load(value['id']))
        inspection = self.service.open_capture(PREFIX, actor=OTHER, inspect=True)
        self.assertTrue(inspection['requires_takeover'])
        value = self.service.open_capture(PREFIX, actor=OTHER, body={'claim_mode': 'restart',
            'takeover_confirmations': 2, 'claim_token': inspection['claim_token']})
        self.assertEqual(value['owner_id'], OTHER['id'])
        self.assertFalse(self.service.allowed(USER, value['id']))

    def test_writeback_empty_axes_roundtrip_and_atomic_conflict(self):
        value = self.open()
        result = self.service.writeback(value['id'], expected_revision=value['revision'])
        self.assertTrue(result['paths'])
        self.assertTrue(all('/timeline/versions/' in path for path in result['paths']))
        reopened = self.service.open_capture(PREFIX, actor=USER)
        self.assertEqual(reopened['id'], value['id'])
        self.assertTrue(all(not items for items in reopened['annotations'].values()))
        pointer_key = PREFIX + '/timeline/.latest.json'
        pointer, etag = self.provider.optional_json(pointer_key)
        self.provider.put(pointer_key, document_bytes({**pointer, 'save_id': 'external'}), etag=etag)
        with self.assertRaises(HTTPException) as conflict:
            self.service.writeback(value['id'])
        self.assertEqual(conflict.exception.status_code, 409)
        self.assertEqual(self.provider.optional_json(pointer_key)[0]['save_id'], 'external')

    def test_continue_token_survives_revision_increment_and_stale_token_fails(self):
        value = self.open()
        self.service.writeback(value['id'])
        inspection = self.service.open_capture(PREFIX, actor=USER, inspect=True)
        current = self.service.open_capture(PREFIX, actor=USER, body={'claim_mode': 'continue', 'claim_token': inspection['claim_token']})
        self.assertGreater(current['revision'], value['revision'])
        with self.assertRaises(HTTPException):
            self.service.open_capture(PREFIX, actor=USER, body={'claim_mode': 'restart', 'claim_token': inspection['claim_token']})

    @unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'FFmpeg tools required')
    def test_real_h264_cache_preparation_uses_uploaded_normal_and_desktop_names(self):
        self.probe_patch.stop()
        video = self.root / NAME
        subprocess.run([shutil.which('ffmpeg'), '-v', 'error', '-f', 'lavfi', '-i', 'testsrc=size=64x48:rate=5',
                        '-t', '1', '-c:v', 'libx264', '-pix_fmt', 'yuv420p', str(video)], check=True)
        payload = video.read_bytes()
        item = self.manifest['files'][0]
        item.update(size=len(payload), sha256=hashlib.sha256(payload).hexdigest())
        self.provider.objects[PREFIX + '/' + item['key']] = (payload, 'video')
        self.provider.objects[PREFIX + '/manifest.json'] = (document_bytes(self.manifest), 'manifest')
        env = {'FFMPEG_PATH': shutil.which('ffmpeg'), 'FFPROBE_PATH': shutil.which('ffprobe')}
        with patch.dict(os.environ, env):
            value = self.open()
            self.service.start_session(value['id'])
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline:
                status = self.service.sessions.status(value['id'])
                if status['state'] in ('ready', 'partial', 'error'):
                    break
                time.sleep(.05)
            self.assertEqual(status['state'], 'ready', status)
            manifest = self.service.media_manifest(self.service, self.service.sessions.manifest(value['id']))
            self.assertEqual(manifest['collection_name'], '1015test')
            self.assertIn('/' + PREFIX + '/' + item['key'] + '?', manifest['videos'][0]['url'])
            normal = next(asset for asset in manifest['assets'] if asset['url'] == manifest['videos'][0]['url'])
            self.assertEqual(normal['sha256'], item['sha256'])
            self.assertEqual(normal['file_name'], Path(NAME).stem + '.' + item['sha256'] + '.normal.mp4')
            self.assertTrue(all(asset['url'].startswith('https://') for asset in manifest['assets']))
            self.assertTrue(any(key.startswith('media/') for key in self.provider.objects))
            self.assertEqual(self.service.sessions.browser_ready(value['id']), {'released': False})

    def test_shared_account_browser_routes_and_version_guards(self):
        from backend.auth import AuthStore
        from backend.shared_accounts import AccountClient, create_app as bridge_app
        accounts_root = self.root / 'accounts'
        auth = AuthStore(accounts_root)
        auth.create_user('admin', 'Admin', 'correct horse battery staple', 'admin')
        user = auth.create_user('worker', 'Worker', 'another long safe password')
        origin = 'https://datamark.example.com'
        env = {'DATAMARK_PUBLIC_ORIGIN': origin, 'DATAMARK_PUBLIC_API_ORIGIN': 'https://api.example.com:8443',
               'DATAMARK_R2_CONFIG': '/protected/config', 'DATAMARK_R2_STATE': str(self.root), 'DATAMARK_TASKS_ENABLED': '0'}
        with patch.dict(os.environ, env), patch('backend.r2_store.R2Store', return_value=self.provider):
            bridge = TestClient(bridge_app(accounts_root), base_url='http://accounts:8766')
            with TestClient(create_app(accounts=AccountClient(bridge)), base_url='https://api.example.com:8443') as client:
                logged = client.post('/api/auth/login', json={'username': 'worker', 'password': 'another long safe password'}, headers={'Origin': origin})
                self.assertEqual(logged.status_code, 200)
                headers = {'Origin': origin, 'X-CSRF-Token': logged.json()['csrf']}
                # Use the same probe fixture as the direct service tests.
                with patch.object(client.app.state.r2_service, 'probe', side_effect=self.service.probe):
                    opened = client.post('/api/storage/projects/open', json={'path': PREFIX}, headers=headers)
                    self.assertEqual(opened.status_code, 200, opened.text)
                    value = opened.json()
                    self.assertEqual(value['owner_id'], user['id'])
                    self.assertEqual(value['collection_folder_name'], '1015test')
                    endpoint = '/api/projects/' + value['id']
                    self.assertEqual(client.put(endpoint + '/draft', json={'annotations': value['annotations']}, headers=headers).status_code, 422)
                    self.assertEqual(client.post(endpoint + '/writeback', json={}, headers=headers).status_code, 422)
                    self.assertEqual(client.post(endpoint + '/writeback', json={'expected_revision': value['revision']}, headers={'Origin': origin}).status_code, 403)
                    saved = client.post(endpoint + '/writeback', json={'expected_revision': value['revision']}, headers=headers)
                    self.assertEqual(saved.status_code, 200, saved.text)
                    library = client.get('/api/account/track-library')
                    self.assertEqual(library.status_code, 200)
                    self.assertTrue(all(axis['is_default'] for axis in library.json()))
                    self.assertEqual(client.get('/api/users').status_code, 403)
                    own = client.post('/api/account/track-library', json={'id': 'testaxis', 'name': 'Test', 'labels': ['Label']}, headers=headers)
                    self.assertEqual(own.status_code, 200, own.text)
                    forbidden = client.post('/api/account/track-library', json={'id': 'scene', 'name': 'Scene', 'labels': ['Label'], 'is_default': True}, headers=headers)
                    self.assertEqual(forbidden.status_code, 403)
                    changed = client.post('/api/auth/password', json={'current_password': 'another long safe password', 'password': 'changed shared password'}, headers=headers)
                    self.assertEqual(changed.status_code, 200, changed.text)
                    self.assertEqual(client.get('/api/auth/me').status_code, 401)
                    self.assertIsNone(auth.session(logged.cookies.get('datamark_session')))
                    self.assertFalse((self.root / '.local/auth.sqlite3').exists())

    def test_new_host_reads_verified_committed_batch(self):
        value = self.open()
        self.service.writeback(value['id'])
        restored = R2ProjectService(self.root / 'new-host', self.provider)
        self.addCleanup(restored.sessions.close)
        self.addCleanup(restored.previews.close)
        with patch.object(restored, 'probe', side_effect=self.service.probe):
            inspected = restored.open_capture(PREFIX, actor=USER, inspect=True)
            self.assertTrue(inspected['has_json'])
            opened = restored.open_capture(PREFIX, actor=USER, body={'claim_mode': 'continue', 'claim_token': inspected['claim_token']})
            self.assertTrue(all(not items for items in opened['annotations'].values()))
            self.assertEqual(opened['collection_folder_name'], '1015test')
            restored.writeback(opened['id'], expected_revision=opened['revision'])

    def test_corrupt_object_does_not_open(self):
        self.provider.objects[PREFIX + '/FPV/' + NAME] = (b'corrupt', 'bad')
        with self.assertRaises(HTTPException) as error:
            self.open()
        self.assertEqual(error.exception.status_code, 409)
        self.assertEqual(self.service.projects(), [])

    def test_presence_uses_real_sessions_and_cannot_remove_other_user(self):
        ident = self.open()['id']
        self.assertEqual(self.service.editing(ident, USER, 'tab1'), {'others': []})
        others = self.service.editing(ident, OTHER, 'tab2')['others']
        self.assertEqual([user['id'] for user in others], [USER['id']])
        self.service.editing(ident, OTHER, 'tab1', leave=True)
        self.assertTrue(self.service.editing(ident, OTHER, 'tab2')['others'])
        self.service.editing(ident, USER, 'tab1', leave=True)
        self.assertEqual(self.service.editing(ident, OTHER, 'tab2')['others'], [])

    def test_upload_permission_and_resume_persist_without_tokens(self):
        uploads = R2Uploads(self.root / 'uploads', self.provider, self.service)
        manifest = {'prefix': PREFIX + 'new', 'collector_name': 'Collector', 'uploader_name': 'Worker',
            'files': [{**self.manifest['files'][0], 'duration_ms': 1000}]}
        first = uploads.start(USER, manifest)
        resumed = R2Uploads(self.root / 'uploads', self.provider, self.service).start(USER, manifest)
        self.assertEqual(first, resumed)
        with self.assertRaises(HTTPException) as denied:
            uploads.load(first['id'], OTHER)
        self.assertEqual(denied.exception.status_code, 404)
        with self.assertRaises(HTTPException) as revoked:
            uploads.load(first['id'], {**USER, 'can_upload': False})
        self.assertEqual(revoked.exception.status_code, 403)
        with self.assertRaises(HTTPException):
            uploads.finish(first['id'], USER)
        self.assertIsNone(self.provider.optional_json(manifest['prefix'] + '/manifest.json')[0])


class MultipartObjects(MemoryObjects):
    def __init__(self):
        super().__init__()
        self.uploads = {}

    def head(self, key):
        data = self.get(key)
        return {'ContentLength': len(data), 'Metadata': {'sha256': hashlib.sha256(data).hexdigest()}}

    def call(self, operation, **kwargs):
        key = kwargs['Key']
        if operation == 'create_multipart_upload':
            self.generation += 1
            ident = str(self.generation)
            self.uploads[ident] = {'key': key, 'parts': {}}
            return {'UploadId': ident}
        if kwargs['UploadId'] not in self.uploads:
            raise HTTPException(404, 'NoSuchUpload')
        upload = self.uploads[kwargs['UploadId']]
        if operation == 'list_parts':
            return {'Parts': [{'PartNumber': number, 'ETag': str(number)} for number in sorted(upload['parts'])]}
        if operation == 'complete_multipart_upload':
            data = b''.join(upload['parts'][part['PartNumber']] for part in kwargs['MultipartUpload']['Parts'])
            self.put(key, data, create=True)
            return {'ETag': 'completed'}
        raise AssertionError(operation)


class R2MultipartTests(unittest.TestCase):
    def test_resume_complete_and_publish_only_verified_collection(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            provider = MultipartObjects()
            service = SimpleNamespace(local=root, probe=lambda path: {'codec': 'h264', 'duration_ms': 1000})
            uploads = R2Uploads(root, provider, service)
            data = b'explicit-compressed-video-fixture'
            manifest = {'prefix': PREFIX, 'collector_name': 'Collector', 'uploader_name': 'Worker',
                'files': [{'key': 'FPV/' + NAME, 'sha256': hashlib.sha256(data).hexdigest(), 'size': len(data), 'duration_ms': 1000}]}
            job = uploads.start(USER, manifest)
            plan = uploads.plan(job['id'], 0, USER)
            self.assertFalse(plan['complete'])
            self.assertFalse(plan['parts'][0]['uploaded'])
            self.assertIn('X-Amz-Signature', plan['parts'][0]['url'])
            with self.assertRaises(HTTPException):
                uploads.complete_file(job['id'], 0, USER)
            self.assertIsNone(provider.optional_json(PREFIX + '/manifest.json')[0])
            upload_id = uploads.load(job['id'], USER)['files'][0]['upload_id']
            del provider.uploads[upload_id]
            # Recover after provider lifecycle cleanup expires a multipart upload.
            self.assertFalse(uploads.plan(job['id'], 0, USER)['complete'])
            replacement = uploads.load(job['id'], USER)['files'][0]['upload_id']
            self.assertNotEqual(replacement, upload_id)
            provider.uploads[replacement]['parts'][1] = data
            resumed = R2Uploads(root, provider, service)
            self.assertTrue(resumed.plan(job['id'], 0, USER)['parts'][0]['uploaded'])
            self.assertEqual(len(provider.uploads), 1)
            resumed.complete_file(job['id'], 0, USER)
            service.probe = lambda path: {'codec': 'mpeg4', 'duration_ms': 1000}
            with self.assertRaises(HTTPException):
                resumed.finish(job['id'], USER)
            self.assertIsNone(provider.optional_json(PREFIX + '/manifest.json')[0])
            service.probe = lambda path: {'codec': 'h264', 'duration_ms': 1000}
            self.assertEqual(resumed.finish(job['id'], USER)['state'], 'ready')
            published = provider.optional_json(PREFIX + '/manifest.json')[0]
            self.assertEqual(published['uploaded_by'], USER['id'])
            self.assertEqual(published['files'][0]['name'], NAME)
            self.assertEqual(resumed.finish(job['id'], USER)['state'], 'ready')
            self.assertEqual(resumed.start(USER, manifest)['state'], 'ready')
            with self.assertRaises(HTTPException):
                resumed.start(OTHER, manifest)


class R2StoreTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / 'config.json'
        self.path.write_text(json.dumps({'endpoint': 'https://' + 'a' * 32 + '.r2.cloudflarestorage.com', 'bucket': 'test-bucket',
            'access_key_id': 'fixture', 'secret_access_key': 'fixture'}))
        self.path.chmod(0o600)

    def test_credentials_file_permissions_and_endpoint_validation(self):
        R2Store(self.path, client=object())
        self.path.chmod(0o644)
        if os.name != 'nt':
            with self.assertRaises(ValueError):
                R2Store(self.path, client=object())
        self.path.chmod(0o600)
        value = json.loads(self.path.read_text())
        value['endpoint'] += '/test-bucket'
        self.path.write_text(json.dumps(value))
        with self.assertRaises(ValueError):
            R2Store(self.path, client=object())

    def test_paths_cannot_escape_and_writes_require_conditions(self):
        for value in ('../x', '/x', 'x//y', 'x/../y', 'C:/secret', 'x\\y'):
            with self.assertRaises(HTTPException):
                object_key(value)
        for value in ('daily', 'daily/20261015/test/manifest.json'):
            self.assertEqual(object_key(value), value)
        for value in ('/mnt/nas', 'daily/../secret', 'daily/20261015/..'):
            with self.assertRaises(HTTPException):
                capture_prefix(value)
        with self.assertRaises(ValueError):
            R2Store(self.path, client=object()).put('x', b'no condition')

    def test_s3_errors_are_sanitized_and_conflicts_are_distinct(self):
        for code, status in [('PreconditionFailed', 409), ('AccessDenied', 503), ('NoSuchKey', 404)]:
            def failure(**kwargs):
                raise ClientError({'Error': {'Code': code, 'Message': 'secret-provider-details'}}, 'GetObject')
            store = R2Store(self.path, client=SimpleNamespace(get_object=failure))
            with self.assertRaises(HTTPException) as result:
                store.get('test')
            self.assertEqual(result.exception.status_code, status)
            self.assertNotIn('secret-provider-details', result.exception.detail)

    def test_sdk_conditional_write_and_presigned_multipart_contract(self):
        from botocore.stub import Stubber
        from urllib.parse import urlsplit, parse_qs
        store = R2Store(self.path)
        payload = b'test'
        with Stubber(store.client) as stub:
            stub.add_response('put_object', {'ETag': 'v1'}, {'Bucket': 'test-bucket', 'Key': 'manifest.json',
                'Body': payload, 'ContentType': 'application/json', 'Metadata': {'sha256': hashlib.sha256(payload).hexdigest()}, 'IfNoneMatch': '*'})
            self.assertEqual(store.put('manifest.json', payload, create=True), 'v1')
            stub.assert_no_pending_responses()
        signed = store.signed_url('daily/test.mp4', operation='upload_part', expires=900, UploadId='fixture', PartNumber=1)
        query = parse_qs(urlsplit(signed).query)
        self.assertEqual(query['X-Amz-Algorithm'], ['AWS4-HMAC-SHA256'])
        self.assertEqual(query['uploadId'], ['fixture'])
        self.assertEqual(query['partNumber'], ['1'])
        self.assertEqual(len(query['X-Amz-Signature'][0]), 64)

    def test_failed_download_keeps_existing_file_and_removes_partial(self):
        destination = self.path.parent / 'media.mp4'
        destination.write_bytes(b'original')
        def response(**kwargs):
            return {'ContentLength': 3, 'Body': io.BytesIO(b'bad')}
        store = R2Store(self.path, client=SimpleNamespace(get_object=response))
        with self.assertRaises(HTTPException):
            store.download('media', destination, hashlib.sha256(b'new').hexdigest(), 3)
        self.assertEqual(destination.read_bytes(), b'original')
        self.assertEqual(list(destination.parent.glob('.*.tmp')), [])


class R2PublicTests(unittest.TestCase):
    def test_configured_routes_require_shared_session_and_expose_no_nas(self):
        with tempfile.TemporaryDirectory() as temporary:
            env = {'DATAMARK_PUBLIC_ORIGIN': 'https://datamark.example.com',
                'DATAMARK_PUBLIC_API_ORIGIN': 'https://api.example.com:8443', 'DATAMARK_R2_CONFIG': '/protected/config',
                'DATAMARK_R2_STATE': temporary, 'DATAMARK_TASKS_ENABLED': '0'}
            provider = MemoryObjects()
            class Accounts:
                def request(self, method, path, **kwargs):
                    if path == '/v1/health':
                        return {'accounts_ready': True}
                    raise HTTPException(401, 'login required')
                def close(self): pass
            with patch.dict(os.environ, env), patch('backend.r2_store.R2Store', return_value=provider):
                with TestClient(create_app(accounts=Accounts()), base_url='https://api.example.com:8443') as client:
                    health = client.get('/api/health').json()
                    self.assertTrue(health['ready'])
                    self.assertEqual(health['mode'], 's3')
                    with patch.object(provider, 'health', side_effect=HTTPException(503, 'offline')):
                        failed = client.get('/api/health').json()
                        self.assertFalse(failed['ready'])
                        self.assertFalse(failed['storage_ready'])
                        self.assertEqual(failed['status'], 'degraded')
                    for url in ['/api/projects', '/api/storage/sources', '/api/projects/private/session/manifest']:
                        self.assertEqual(client.get(url).status_code, 401)
                    self.assertNotIn('/api/projects/open', [getattr(route, 'path', '') for route in client.app.routes])
