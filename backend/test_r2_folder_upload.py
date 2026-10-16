"""Public folder upload contracts; no production accounts, credentials or network."""
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx
from fastapi import HTTPException
from fastapi.testclient import TestClient

from backend.r2_folder import scan_collection, FolderUploadManager
from backend.r2_upload import RemoteCaptureTarget
from backend.r2_upload_helper import create_app
from backend.r2_uploads import R2Uploads
from backend.r2_service import R2ProjectService
from backend.service import ProjectService, document_bytes
from backend.storyboards import StoryboardCache
from backend.test_r2_integration import MultipartObjects, USER, OTHER, NAME, PREFIX


class FolderSelectionTests(unittest.TestCase):
    def setUp(self):
        temporary = Path(__file__).resolve().parents[1] / '.tmp'
        temporary.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=temporary)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / 'video').mkdir()
        (self.root / 'video' / NAME.replace('.mp4', '.avi')).write_bytes(b'original-avi-fixture')

    def test_scan_modalities_and_preserve_source_timeline(self):
        payloads = {'IMU/part.csv': b'time,x,y,z\n0,1,2,3', 'HEART/hr.txt': b'80',
                    'prediction/posture.json': b'{"axis":"posture","segments":[]}',
                    'timeline/posture.timeline.json': b'previous-human-result', 'identity.txt': b'private'}
        for key, value in payloads.items():
            path = self.root / key
            path.parent.mkdir(exist_ok=True)
            path.write_bytes(value)
        selected = scan_collection(self.root)
        self.assertEqual(selected['videos'][0]['date'], '20261015')
        self.assertEqual({item['output'] for item in selected['attachments']}, set(payloads) - set(selected['ignored']))
        self.assertIn('identity.txt', selected['ignored'])
        self.assertIn('timeline/posture.timeline.json', selected['ignored'])
        for key, value in payloads.items():
            self.assertEqual((self.root / key).read_bytes(), value)
        self.assertEqual(len(scan_collection(self.root / 'video')['videos']), 1)

    def test_scan_numbered_imu_folder_with_txt_and_wplay(self):
        folder = self.root / 'IMU' / '134546'
        folder.mkdir(parents=True)
        payloads = {'20261014134546.txt': b'time,x,y,z\n0,1,2,3',
                    'WT901BLE67(F9_07_E6_4C_66_D3).wplay': b'\x00\xffsensor-playback',
                    'WT901BLE68(F0_46_23_95_C9_F0).WPLAY': b'other-device'}
        for name, content in payloads.items():
            (folder / name).write_bytes(content)
        selected = scan_collection(self.root)
        self.assertEqual(len(selected['videos']), 1)
        self.assertEqual({item['output'] for item in selected['attachments']},
                         {'IMU/134546/' + name for name in payloads})
        for name, content in payloads.items():
            self.assertEqual((folder / name).read_bytes(), content)

    def test_reject_link_duplicate_output_and_invalid_prediction(self):
        (self.root / 'IMU').mkdir()
        (self.root / 'IMU/link.csv').symlink_to(self.root / 'video' / NAME.replace('.mp4', '.avi'))
        with self.assertRaises(HTTPException):
            scan_collection(self.root)
        (self.root / 'IMU/link.csv').unlink()
        (self.root / 'prediction').mkdir()
        (self.root / 'prediction/posture.json').write_bytes(b'not json')
        with self.assertRaises(HTTPException):
            scan_collection(self.root)
        (self.root / 'prediction/posture.json').unlink()
        (self.root / 'FPV').mkdir()
        (self.root / 'FPV' / NAME.replace('.mp4', '.avi')).write_bytes(b'duplicate-name')
        with self.assertRaises(HTTPException) as duplicate:
            scan_collection(self.root)
        self.assertEqual(duplicate.exception.status_code, 409)

    def test_source_changes_after_selection_abort_before_compression(self):
        manager = FolderUploadManager(SimpleNamespace(capture_exists=lambda prefix: False), self.root / '.staging')
        selected = manager.select_folder(USER['id'], self.root)
        (self.root / 'video' / NAME.replace('.mp4', '.avi')).write_bytes(b'changed')
        with self.assertRaises(HTTPException) as changed:
            manager.start(USER, 'lixin', None, None, selected['selection_id'])
        self.assertEqual(changed.exception.status_code, 409)


class MultipartModalitiesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.provider = MultipartObjects()
        self.probes = []
        def probe(path):
            self.probes.append(path)
            return {'codec': 'h264', 'duration_ms': 1000}
        self.uploads = R2Uploads(Path(self.temp.name), self.provider, SimpleNamespace(local=Path(self.temp.name), probe=probe))
        self.video = b'compressed-video-fixture'
        self.prediction = b'{"axis":"posture", "segments": []}\n'
        self.payloads = [self.video, b'time,x,y,z\n0,1,2,3', b'80', self.prediction]
        self.manifest = {'prefix': PREFIX, 'collector_name': 'test', 'uploader_name': 'Worker',
            'files': [{'key': 'FPV/' + NAME, 'duration_ms': 1000, **self.record(self.video)}],
            'attachments': [{'key': key, **self.record(data)} for key, data in zip(
                ['IMU/part.csv', 'HEART/hr.txt', 'prediction/posture.json'], self.payloads[1:])]}

    @staticmethod
    def record(data):
        return {'size': len(data), 'sha256': hashlib.sha256(data).hexdigest()}

    def complete(self, job, index):
        self.uploads.plan(job['id'], index, USER)
        saved = self.uploads.load(job['id'], USER)['files'][index]
        self.provider.uploads[saved['upload_id']]['parts'][1] = self.payloads[index]
        self.uploads.complete_file(job['id'], index, USER)

    def test_all_files_verified_before_publication_prediction_preserved(self):
        job = self.uploads.start(USER, self.manifest)
        self.assertEqual(job['file_count'], 4)
        self.complete(job, 0)
        with self.assertRaises(HTTPException):
            self.uploads.finish(job['id'], USER)
        self.assertIsNone(self.provider.optional_json(PREFIX + '/manifest.json')[0])
        for index in range(1, 4):
            self.complete(job, index)
        self.uploads.finish(job['id'], USER)
        published = self.provider.optional_json(PREFIX + '/manifest.json')[0]
        self.assertEqual(len(published['files']), 1)
        prediction = published['attachments'][-1]
        self.assertEqual(self.provider.get(PREFIX + '/' + prediction['key']), self.prediction)
        self.assertEqual(prediction['name'], 'prediction/posture.json')
        self.assertTrue(all(path.name.endswith('.mp4') for path in self.probes))
        self.assertEqual(self.uploads.start(USER, self.manifest)['state'], 'ready')
        with patch.object(self.provider, 'download', side_effect=AssertionError('already verified')):
            self.assertEqual(self.uploads.finish(job['id'], USER)['state'], 'ready')
        for actor in (OTHER, {**USER, 'can_upload': False}):
            with self.assertRaises(HTTPException):
                self.uploads.finish(job['id'], actor)
        with self.assertRaises(HTTPException):
            self.uploads.start(OTHER, self.manifest)

    def test_wplay_upload_preserves_bytes_and_relative_name(self):
        key = 'IMU/134546/WT901BLE67(F9_07_E6_4C_66_D3).wplay'
        self.payloads[1] = b'\x00\xfforiginal-wplay\r\n'
        self.manifest['attachments'][0] = {'key': key, **self.record(self.payloads[1])}
        job = self.uploads.start(USER, self.manifest)
        for index in range(len(self.payloads)):
            self.complete(job, index)
        self.uploads.finish(job['id'], USER)
        published = self.provider.optional_json(PREFIX + '/manifest.json')[0]
        attachment = published['attachments'][0]
        self.assertEqual(attachment['name'], key)
        self.assertTrue(attachment['key'].startswith('IMU/134546/'))
        self.assertEqual(self.provider.get(PREFIX + '/' + attachment['key']), self.payloads[1])
        self.assertTrue(all(path.name.endswith('.mp4') for path in self.probes))

    def test_no_timeline_overwrite_no_paths_no_unsupported_files(self):
        for key in ['timeline/posture.timeline.json', 'IMU/../secret.txt', 'HEART/a.exe', 'prediction/a.csv', 'IMU/a\\b.csv', 'HEART/a.wplay', 'prediction/a.wplay', 'IMU/a.exe']:
            manifest = json.loads(json.dumps(self.manifest))
            manifest['attachments'][0]['key'] = key
            with self.subTest(key=key), self.assertRaises(HTTPException):
                self.uploads.start(USER, manifest)

    def test_revoked_and_cross_account_cannot_upload_attachments(self):
        job = self.uploads.start(USER, self.manifest)
        for actor in (OTHER, {**USER, 'can_upload': False}):
            for action in [lambda: self.uploads.plan(job['id'], 1, actor),
                           lambda: self.uploads.complete_file(job['id'], 1, actor),
                           lambda: self.uploads.finish(job['id'], actor)]:
                with self.assertRaises(HTTPException):
                    action()

    def test_corrupt_prediction_fails_publication(self):
        self.payloads[-1] = b'not JSON'
        self.manifest['attachments'][-1].update(self.record(self.payloads[-1]))
        job = self.uploads.start(USER, self.manifest)
        for index in range(4):
            self.complete(job, index)
        with self.assertRaises(HTTPException) as invalid:
            self.uploads.finish(job['id'], USER)
        self.assertEqual(invalid.exception.status_code, 422)
        self.assertIsNone(self.provider.optional_json(PREFIX + '/manifest.json')[0])


class PublicationConfirmationTests(unittest.TestCase):
    def publish(self, finish_error=None, state='ready'):
        self.requests = []
        self.progress = []
        def handle(request):
            self.requests.append(request)
            if request.url.path.endswith('/finish'):
                self.assertEqual(request.extensions['timeout']['read'], 600)
                if isinstance(finish_error, int):
                    return httpx.Response(finish_error, json={'detail': 'server response'})
                if finish_error:
                    raise finish_error
                return httpx.Response(200, json={'state': state})
            if request.method == 'POST':
                return httpx.Response(200, json={'id': 'upload'})
            if '/files/' in request.url.path:
                return httpx.Response(200, json={'complete': True})
            return httpx.Response(200, json={'state': state})
        service = SimpleNamespace(probe=lambda path: {'duration_ms': 1000})
        with httpx.Client(base_url='https://api.example.com', timeout=30,
                          transport=httpx.MockTransport(handle)) as client:
            target = RemoteCaptureTarget(client, service, progress=self.progress.append)
            with patch('backend.upload_playback.build'):
                target.publish_capture(PREFIX, Path('/unused'), {'files': [{'key': 'FPV/clip.mp4'}]}, service)

    def test_long_publication_wait_and_explicit_progress(self):
        self.publish()
        self.assertIn('校验并发布', self.progress[-1])
        self.assertEqual(len(self.requests), 3)

    def test_lost_response_and_gateway_timeout_confirm_durable_success(self):
        for error in (httpx.ReadTimeout('fixture'), httpx.RemoteProtocolError('fixture'), 504):
            with self.subTest(error=type(error).__name__):
                self.publish(error)
                self.assertEqual(self.requests[-1].url.path, '/api/r2/uploads/upload')
                self.assertEqual(self.requests[-1].method, 'GET')

    def test_unconfirmed_publication_never_reports_success(self):
        with self.assertRaises(HTTPException) as failed:
            self.publish(httpx.ReadTimeout('fixture'), 'uploading')
        self.assertEqual(failed.exception.status_code, 504)
        self.assertIn('尚未确认', failed.exception.detail)

    def test_validation_and_permission_failures_are_not_hidden_by_status(self):
        for status in (401, 403, 422):
            with self.subTest(status=status), self.assertRaises(HTTPException) as failed:
                self.publish(status)
            self.assertEqual(failed.exception.status_code, status)
            self.assertTrue(self.requests[-1].url.path.endswith('/finish'))


class HelperWindowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1] / '.tmp')
        self.addCleanup(self.temp.cleanup)
        self.service = SimpleNamespace(local=Path(self.temp.name), sessions=SimpleNamespace(close=lambda: None), previews=SimpleNamespace(close=lambda: None))
        self.remote_user = dict(USER)
        self.calls = []
        def remote(request):
            self.calls.append(request)
            if request.url.path == '/api/health':
                return httpx.Response(200, json={'mode': 's3', 'ready': True, 'capabilities': ['s3-folder-upload']})
            if request.url.path == '/api/auth/login':
                return httpx.Response(200, json={'user': self.remote_user, 'csrf': 'fixture-csrf'})
            if request.url.path == '/api/auth/me':
                return httpx.Response(200, json=self.remote_user)
            return httpx.Response(200, json={'ok': True})
        self.app = create_app(root=Path(self.temp.name), service=self.service,
            client_factory=lambda: httpx.Client(base_url='https://api-annotate.reltydynamic.com:8443', transport=httpx.MockTransport(remote)))
        self.client = TestClient(self.app, base_url='http://127.0.0.1:8767')
        self.addCleanup(self.client.close)
        page = self.client.get('/')
        self.headers = {'Origin': 'http://127.0.0.1:8767', 'X-Upload-Token': re.search(r'name="upload-token" content="([^"]+)"', page.text)[1]}

    def encrypted_login(self):
        import base64
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        challenge = self.client.get('/api/auth/login-challenge', headers=self.headers).json()
        key, iv = os.urandom(32), os.urandom(12)
        aad = ('datamark-login-v1:' + challenge['challenge']).encode()
        public = serialization.load_der_public_key(base64.b64decode(challenge['public_key']))
        wrapped = public.encrypt(key, padding.OAEP(mgf=padding.MGF1(hashes.SHA256()), algorithm=hashes.SHA256(), label=aad))
        data = AESGCM(key).encrypt(iv, document_bytes({'username': 'worker', 'password': 'fixture-password'}), aad)
        return self.client.post('/api/auth/login-encrypted', headers=self.headers,
            json={'challenge': challenge['challenge'], **{name: base64.b64encode(value).decode() for name, value in [('key', wrapped), ('iv', iv), ('ciphertext', data)]}})

    def test_loopback_session_csrf_and_host_guards(self):
        self.assertEqual(self.client.get('/api/status').status_code, 403)
        self.assertEqual(self.client.post('/api/select', headers={**self.headers, 'Origin': 'https://evil.example'}, json={}).status_code, 403)
        self.assertEqual(self.client.get('/api/status', headers={**self.headers, 'Host': 'evil.example'}).status_code, 400)
        self.assertEqual(self.client.post('/api/select', headers=self.headers, json={}).status_code, 401)

    def test_encrypted_login_and_fresh_permission_check(self):
        self.assertEqual(self.encrypted_login().status_code, 200)
        self.remote_user['can_upload'] = False
        with patch('backend.r2_upload_helper.choose_local_paths') as picker:
            denied = self.client.post('/api/select', headers=self.headers, json={})
            self.assertEqual(denied.status_code, 403)
            picker.assert_not_called()

    def test_client_initialization_failure_returns_json_and_allows_retry(self):
        with patch('backend.r2_upload_helper.httpx.Client', side_effect=ImportError('fixture proxy dependency missing')):
            response = self.encrypted_login()
        self.assertEqual(response.status_code, 503)
        self.assertTrue(response.headers['content-type'].startswith('application/json'))
        self.assertIn('requirements.lock.txt', response.json()['detail'])
        self.assertNotIn('fixture proxy', response.text)
        self.assertIsNone(self.client.get('/api/status', headers=self.headers).json()['user'])
        self.assertEqual(self.encrypted_login().status_code, 200)

    def test_installed_dependencies_support_socks_environment(self):
        # Reproduce the actual failing constructor, without making a network
        # connection, credentials or a mock transport that bypasses proxy setup.
        with patch.dict(os.environ, {'ALL_PROXY': 'socks5://127.0.0.1:1'}, clear=True):
            with httpx.Client(base_url='https://api.example.com', trust_env=True) as client:
                self.assertFalse(client.is_closed)
            self.assertTrue(client.is_closed)

    def test_native_folder_selection_keeps_paths_out_of_public_calls(self):
        self.assertEqual(self.encrypted_login().status_code, 200)
        video = Path(self.temp.name) / 'video'
        video.mkdir()
        (video / NAME.replace('.mp4', '.avi')).write_bytes(b'original')
        with patch('backend.r2_upload_helper.choose_local_paths', return_value=[str(video)]):
            response = self.client.post('/api/select', headers=self.headers, json={})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['videos'], [NAME.replace('.mp4', '.avi')])
        self.assertNotIn(self.temp.name, response.text)
        self.assertNotIn(self.temp.name, '\n'.join(request.content.decode() for request in self.calls))


@unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'FFmpeg tools required')
class FolderUploadRoundTripTests(unittest.TestCase):
    def test_real_compression_cache_modalities_and_independent_human_timeline(self):
        self.round_trip()

    def test_same_page_agent_compresses_uploads_and_keeps_prediction_separate(self):
        self.round_trip(use_agent=True)

    def round_trip(self,use_agent=False):
        base = Path(__file__).resolve().parents[1] / '.tmp'
        base.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=base) as temporary:
            root = Path(temporary)
            video = root / 'collection/video' / NAME.replace('.mp4', '.avi')
            video.parent.mkdir(parents=True)
            subprocess.run([shutil.which('ffmpeg'), '-hide_banner', '-loglevel', 'error', '-f', 'lavfi',
                '-i', 'color=size=64x48:rate=5', '-t', '1', '-c:v', 'mpeg4', str(video)], check=True)
            original = video.read_bytes()
            data = {'IMU/data.csv': b'time,x,y,z\n0,1,2,3\n', 'HEART/data.txt': b'80\n',
                    'prediction/posture.json': b'{"axis":"posture", "segments": []}\n'}
            for key, payload in data.items():
                path = root / 'collection' / key
                path.parent.mkdir(parents=True)
                path.write_bytes(payload)
            env = {'DATAMARK_DESKTOP_CACHE': '1', 'DATAMARK_DESKTOP_CACHE_ROOT': str(root / 'desktop/dataMarkProject'),
                   'DATAMARK_ORIGIN': '', 'FFMPEG_PATH': shutil.which('ffmpeg'), 'FFPROBE_PATH': shutil.which('ffprobe')}
            with patch.dict(os.environ, env):
                local = ProjectService(root / 'local', enable_remote=False)
            local.tool = lambda name: Path(shutil.which(name)) if shutil.which(name) else None
            provider = MultipartObjects()
            public = R2ProjectService(root / 'public', provider)
            uploads = R2Uploads(public.local, provider, public)
            self.addCleanup(local.sessions.close)
            self.addCleanup(local.previews.close)
            self.addCleanup(public.sessions.close)
            self.addCleanup(public.previews.close)
            public.tool = local.tool
            real_client = httpx.Client
            original_signed = provider.signed_url
            single_keys = {}
            def signed(key, **kwargs):
                if kwargs.get('operation') == 'put_object':
                    token = hashlib.sha256(key.encode()).hexdigest()
                    single_keys[token] = key
                    return f'https://small.invalid/{token}'
                if kwargs.get('operation') == 'upload_part':
                    return f"https://parts.invalid/{kwargs['UploadId']}/{kwargs['PartNumber']}"
                return original_signed(key, **kwargs)
            provider.signed_url = signed
            def direct(request):
                if request.url.host == 'small.invalid':
                    provider.put(single_keys[request.url.path[1:]], request.content, create=True)
                    return httpx.Response(200)
                ident, number = request.url.path.strip('/').split('/')
                provider.uploads[ident]['parts'][int(number)] = request.content
                return httpx.Response(200)
            class PublicClient:
                def request(self, method, path, **kwargs):
                    if path=='/api/r2/helper/identity':
                        return httpx.Response(200,json=USER)
                    path=path.replace('/api/r2/helper/transfer/uploads','/api/r2/uploads',1)
                    parts = path.split('?', 1)[0].split('/')
                    if path == '/api/r2/uploads':
                        value = uploads.start(USER, kwargs['json'])
                    elif parts[-1] == 'finish':
                        value = uploads.begin_finish(parts[4], USER, lambda: USER,prepare='prepare=1' in path) if use_agent else uploads.finish(parts[4], USER)
                    elif len(parts) == 5 and method == 'GET':
                        value = uploads.status(parts[4], USER)
                    elif parts[-1] == 'complete':
                        value = uploads.complete_file(parts[4], int(parts[6]), USER)
                    else:
                        value = uploads.plan(parts[4], int(parts[6]), USER, single=use_agent)
                    return httpx.Response(200, json=value)
            if use_agent:
                from backend.upload_agent import AgentEngine
                engine=AgentEngine(root,PublicClient(),service=local,picker=lambda root,kind:[str(root/'collection')])
                engine.target.pipeline_enabled=True
                engine.execute({'id':'select-fixture','action':'select','fields':{}})
                self.assertFalse(engine.report.get('error'),engine.report)
                self.assertEqual(len(engine.report['selection']['attachments']),3)
            else:
                remote = RemoteCaptureTarget(PublicClient(), local)
                manager = FolderUploadManager(remote, root / 'staging')
                selection = manager.select_folder(USER['id'], root / 'collection')
                remote.attachments = selection['attachments']
                job = manager.start(USER, 'lixin', None, None, selection['selection_id'])
            with patch('backend.r2_upload.httpx.Client', side_effect=lambda **kwargs: real_client(transport=httpx.MockTransport(direct), **kwargs)):
                if use_agent:
                    engine.execute({'id':'start-fixture','action':'start','fields':{'collector':'lixin'}})
                    self.assertFalse(engine.report.get('error'),engine.report)
                    self.assertEqual(engine.snapshot()['job']['state'],'ready')
                    job=engine.job
                else:
                    self.assertEqual(manager.finish(job['id'], USER['id'], local, lambda ident: True)['state'], 'ready')
            self.assertEqual(job['prefix'], 'daily/20261015/1015lixin')
            if use_agent:
                before=dict(provider.objects)
                engine.execute({'id':'select-retry','action':'select','fields':{}})
                with patch.object(engine.manager,'_compress_video',side_effect=AssertionError('retry must reuse desktop cache')),patch('backend.upload_pipeline.build',side_effect=AssertionError('retry must reuse derivatives')):
                    engine.execute({'id':'start-retry','action':'start','fields':{'collector':'lixin'}})
                self.assertFalse(engine.report.get('error'),engine.report)
                self.assertEqual(engine.manager.status(engine.job['id'],USER['id'])['state'],'ready')
                self.assertEqual(provider.objects,before)
            manifest = provider.optional_json(job['prefix'] + '/manifest.json')[0]
            compressed = provider.get(job['prefix'] + '/' + manifest['files'][0]['key'])
            self.assertEqual({item['kind'] for item in manifest['cache_files']}, {'fast', 'thumbnail', 'sheet'})
            self.assertEqual(len(manifest['cache_files']), 3)
            cache = list((root / 'desktop/dataMarkProject/1015lixin').glob('*.normal.mp4'))
            self.assertEqual(len(cache), 1)
            self.assertEqual(cache[0].read_bytes(), compressed)
            self.assertEqual(video.read_bytes(), original)
            for item in manifest['cache_files']:
                label = f"storyboard-{item['index']:03d}" if item['kind']=='sheet' else item['kind']
                local_cache = list((root / 'desktop/dataMarkProject/1015lixin').glob('*.' + label + ('.mp4' if item['kind']=='fast' else '.jpg')))
                self.assertEqual(len(local_cache), 1)
                self.assertEqual(local_cache[0].read_bytes(), provider.get(job['prefix']+'/'+item['key']))
            opened = public.open_capture(job['prefix'], actor=USER)
            from backend.session_cache import SessionCache
            from backend.r2_routes import R2Media
            from backend.tasks.workspace import Workspaces
            task = {'id':'fixture', 'video':{'id':'clip','duration_ms':1000,
                    'source':{'backend':'s3','key':opened['id']+'/v0001'}}, 'task_type':'state',
                    'rule':{'axes':[{'id':'posture','name':'姿势','mode':'state','labels':['站']}]},
                    'revision':0,'status':'annotating','annotator_ids':[USER['id']]}
            workspace = Workspaces(root/'task-workspace',SimpleNamespace(get_task=lambda actor,ident:task),R2Media(public))
            self.addCleanup(workspace.close)
            task_project = workspace.project(USER,'fixture')
            with patch.object(SessionCache, '_render_compact', side_effect=AssertionError('server must not compress')), \
                 patch('backend.session_cache.render_local_fast', side_effect=AssertionError('server must not render fast')), \
                 patch('backend.session_cache.render_local_thumbnail', side_effect=AssertionError('server must not render jpg')), \
                 patch.object(StoryboardCache, 'render', side_effect=AssertionError('server must not render sheets')):
                for svc, project in ((public, opened), (workspace.service, task_project)):
                    if svc is public:
                        state=public.start_session(project['id'])
                        download=public.session_manifest(project['id'])
                    else:
                        state=workspace.session_status(project,start=True)
                        download=workspace.session_manifest(project,task['id'])
                    self.assertEqual(state['state'],'ready',state)
                    self.assertTrue(all('/'+job['prefix']+'/' in asset['url'] for asset in download['assets']))
            self.assertFalse(any(key.startswith('media/') for key in provider.objects))
            # A fresh server must reject a missing/corrupt uploaded derivative,
            # retaining the collection instead of generating a replacement.
            broken = R2ProjectService(root/'broken-server',provider)
            broken.tool = public.tool
            self.addCleanup(broken.sessions.close)
            self.addCleanup(broken.previews.close)
            damaged = broken.open_capture(job['prefix'],actor=USER)
            cache_key=job['prefix']+'/'+manifest['cache_files'][-1]['key']
            saved_object=provider.objects[cache_key]
            provider.objects[cache_key]=(b'corrupt-fixture','corrupt')
            try:
                with patch.object(SessionCache,'_render_compact',side_effect=AssertionError('no server fallback')), \
                     patch.object(StoryboardCache,'render',side_effect=AssertionError('no server image fallback')):
                    source=broken.load(damaged['id'])['_sources'][damaged['videos'][0]['id']]
                    with self.assertRaises(HTTPException):
                        broken.uploaded_playback(source,root/'forced-legacy-hydration','fixture-key')
            finally:
                provider.objects[cache_key]=saved_object
            for item in manifest['attachments']:
                self.assertEqual(provider.get(job['prefix'] + '/' + item['key']), data[item['name']])
            project = public.open_capture(job['prefix'], actor=USER)
            stored = public.load(project['id'])
            stored['annotations']['posture'] = [{'id': 'human', 'label': '坐', 'kind': 'interval', 'lane': 0, 'start_ms': 0, 'end_ms': 1000}]
            public.save(stored)
            public.writeback(project['id'], expected_revision=stored['revision'])
            latest = provider.optional_json(job['prefix'] + '/timeline/.latest.json')[0]
            human = json.loads(provider.get(latest['axes']['posture']['key']))
            self.assertEqual(human['segments'][0]['label'], '坐')
            prediction = next(item for item in manifest['attachments'] if item['name'].startswith('prediction/'))
            self.assertEqual(provider.get(job['prefix'] + '/' + prediction['key']), data['prediction/posture.json'])


if __name__ == '__main__':
    unittest.main()
