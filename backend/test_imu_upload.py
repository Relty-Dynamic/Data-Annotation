"""Independent uploads against isolated byte stores; never contacts S3/NAS."""
import hashlib
import tempfile
import unittest
from urllib.parse import unquote, urlsplit
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
import httpx

from fastapi import HTTPException

from backend.imu_upload import scan_imu
from backend.r2_folder import FolderUploadManager
from backend.r2_service import R2ProjectService
from backend.r2_upload import RemoteCaptureTarget
from backend.r2_uploads import R2Uploads
from backend.test_r2_integration import MultipartObjects, USER, OTHER, PREFIX, NAME
from backend.service import document_bytes


class ImuPublicationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1] / '.tmp')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.provider = MultipartObjects()
        self.service = R2ProjectService(self.root, self.provider)
        self.addCleanup(self.service.sessions.close)
        self.addCleanup(self.service.previews.close)
        self.service.probe = Mock(return_value={'codec': 'h264', 'duration_ms': 1000})
        self.uploads = R2Uploads(self.root, self.provider, self.service)
        self.raw = b'\x00\xfforiginal-IMU-wplay\r\n'
        self.video = b'explicit-compressed-video-test-fixture'
        self.imu = dict(prefix=PREFIX, capture_date='20261015', collector_name='test', uploader_name='Worker',
                        upload_kind='imu', files=[], attachments=[self.item('IMU/device.wplay', self.raw)])
        self.fpv = dict(prefix=PREFIX, capture_date='20261015', collector_name='test', uploader_name='Worker',
                        files=[dict(self.item('FPV/' + NAME, self.video), duration_ms=1000)])

    @staticmethod
    def item(key, data):
        return dict(key=key, size=len(data), sha256=hashlib.sha256(data).hexdigest())

    def complete(self, manifest, payloads):
        job = self.uploads.start(USER, manifest)
        for index, data in enumerate(payloads):
            if self.uploads.plan(job['id'], index, USER)['complete']:
                continue
            record = self.uploads.load(job['id'], USER)['files'][index]
            self.provider.uploads[record['upload_id']]['parts'][1] = data
            self.uploads.complete_file(job['id'], index, USER)
        return job

    def publish(self, manifest, payloads):
        job = self.complete(manifest, payloads)
        self.assertEqual(self.uploads.finish(job['id'], USER)['state'], 'ready')
        return job

    def test_imu_first_then_video_share_folder_and_preserve_original_bytes(self):
        self.publish(self.imu, [self.raw])
        self.service.probe.assert_not_called()
        browsing = self.service.browse(PREFIX)
        self.assertFalse(browsing['can_open'])
        self.assertEqual(browsing['entries'][0]['name'], 'IMU/device.wplay')
        with self.assertRaises(HTTPException):
            self.service.collection(PREFIX, download=False)
        imu_before = self.provider.get(PREFIX + '/imu.manifest.json')
        self.publish(self.fpv, [self.video])
        self.assertTrue(self.service.browse(PREFIX)['can_open'])
        self.assertEqual(self.provider.get(PREFIX + '/imu.manifest.json'), imu_before)
        published, _ = self.provider.optional_json(PREFIX + '/imu.manifest.json')
        self.assertEqual(self.provider.get(PREFIX + '/' + published['attachments'][0]['key']), self.raw)

    def test_video_first_then_imu_preserves_project_identity_and_timeline(self):
        self.publish(self.fpv, [self.video])
        original = self.service.collection(PREFIX, download=False)
        timeline = PREFIX + '/timeline/finally/posture.timeline.json'
        self.provider.put_if_absent(timeline, b'explicit-human-annotation-fixture')
        self.publish(self.imu, [self.raw])
        self.assertEqual(self.service.collection(PREFIX, download=False), original)
        self.assertEqual(self.provider.get(timeline), b'explicit-human-annotation-fixture')

    def test_complete_capture_and_matching_imu_can_coexist(self):
        full = {**self.fpv, 'attachments': self.imu['attachments']}
        self.publish(full, [self.video, self.raw])
        before = self.provider.get(PREFIX + '/manifest.json')
        self.publish(self.imu, [self.raw])
        self.assertEqual(self.provider.get(PREFIX + '/manifest.json'), before)
        self.assertEqual(len(self.service.browse(PREFIX)['entries']), 2)

    def test_conflicting_imu_and_same_kind_do_not_replace_existing(self):
        self.publish(self.imu, [self.raw])
        snapshot = dict(self.provider.objects)
        with self.assertRaises(HTTPException):
            self.uploads.start(OTHER, self.imu)
        full = {**self.fpv, 'attachments': [self.item('IMU/device.wplay', b'different')]}
        with self.assertRaises(HTTPException):
            self.uploads.start(USER, full)
        self.assertEqual(self.provider.objects, snapshot)
        self.assertEqual(self.uploads.start(USER, self.imu)['state'], 'ready')

    def test_racing_uploads_recheck_shared_contract_on_publication(self):
        full = {**self.fpv, 'attachments': [self.item('IMU/device.wplay', b'different')]}
        pending = self.complete(full, [self.video, b'different'])
        self.publish(self.imu, [self.raw])
        with self.assertRaises(HTTPException):
            self.uploads.finish(pending['id'], USER)
        self.assertIsNone(self.provider.optional_json(PREFIX + '/manifest.json')[0])

    def test_conditional_contract_catches_process_race_after_compatibility_read(self):
        # Both processes can see no other manifest, so conditional objects must
        # arbitrate even when the read-before-publish check sees stale state.
        from backend.capture_publication import publish
        first = {**self.fpv, 'attachments': self.imu['attachments']}
        second = {**self.imu, 'attachments': [self.item('IMU/device.wplay', b'different')]}
        with patch('backend.capture_publication.check_compatible'):
            publish(self.provider, first)
            with self.assertRaises(HTTPException):
                publish(self.provider, second)
        self.assertIsNone(self.provider.optional_json(PREFIX + '/imu.manifest.json')[0])

    def test_invalid_date_kind_paths_and_video_in_imu_rejected(self):
        variants = [dict(capture_date='20260230'), dict(capture_date='20261016'), dict(upload_kind=[]),
                    dict(files=self.fpv['files']), dict(attachments=[]), dict(cache_profile='invalid'),
                    dict(attachments=[self.item('prediction/posture.json', self.raw)]),
                    dict(attachments=[self.item('IMU/../bad.wplay', self.raw)])]
        for changes in variants:
            with self.subTest(changes=changes), self.assertRaises(HTTPException):
                self.uploads.start(USER, {**self.imu, **changes})
        self.assertEqual(self.provider.objects, {})

    def test_corruption_and_revoked_permission_cannot_publish(self):
        job = self.complete(self.imu, [self.raw])
        for user in (OTHER, {**USER, 'can_upload': False}):
            with self.assertRaises(HTTPException):
                self.uploads.finish(job['id'], user)
        saved = self.uploads.load(job['id'], USER)
        key = PREFIX + '/' + saved['manifest']['attachments'][0]['key']
        _, etag = self.provider.objects[key]
        self.provider.put(key, b'x' * len(self.raw), etag=etag)
        with self.assertRaises(HTTPException):
            self.uploads.finish(job['id'], USER)
        self.assertIsNone(self.provider.optional_json(PREFIX + '/imu.manifest.json')[0])
        self.assertIsNone(self.provider.optional_json(PREFIX + '/capture.identity.json')[0])

    def test_existing_video_collector_conflict_is_explicit(self):
        self.publish(self.fpv, [self.video])
        manifest, etag = self.provider.optional_json(PREFIX + '/manifest.json')
        manifest.pop('collector_name')
        self.provider.put(PREFIX + '/manifest.json', document_bytes(manifest), etag=etag)
        with self.assertRaises(HTTPException) as failure:
            self.uploads.start(USER, self.imu)
        self.assertEqual(failure.exception.status_code, 409)


class ImuLocalTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1] / '.tmp')
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        (self.root / 'IMU/session').mkdir(parents=True)
        self.source = self.root / 'IMU/session/sensor.wplay'
        self.raw = b'\x00\xffsensor-original-fixture'
        self.source.write_bytes(self.raw)
        self.target = RemoteCaptureTarget(None, None)
        self.manager = FolderUploadManager(self.target, self.root / '.staging')
        self.options = dict(upload_kind='imu', capture_date='20261015', prediction='keep')

    def select(self):
        return self.manager.select_folder(USER['id'], self.root, upload_kind='imu')

    def start(self, selection, options=None, user=USER):
        return self.manager.start(user, 'test', None, None, selection['selection_id'], prediction=options or self.options)

    def test_root_and_direct_imu_folder_are_equivalent_without_video(self):
        (self.root / 'identity.txt').write_text('not for upload')
        (self.root / 'FPV').mkdir()
        (self.root / 'FPV/video.avi').write_bytes(b'not read')
        self.assertEqual(scan_imu(self.root)['attachments'], scan_imu(self.root / 'IMU')['attachments'])
        self.assertEqual(scan_imu(self.root)['attachments'][0]['output'], 'IMU/session/sensor.wplay')
        self.assertEqual(self.select()['videos'], [])

    def test_raw_upload_uses_no_compression_or_video_pipeline(self):
        job = self.start(self.select())
        captured = {}
        def publish(prefix, prepared, manifest, service):
            captured.update(manifest)
            self.assertEqual(self.target.attachments[0]['output'], 'IMU/session/sensor.wplay')
            self.assertEqual(self.target.source_digest(self.target.attachments[0]), hashlib.sha256(self.raw).hexdigest())
        self.target.pipeline_enabled = True
        self.target.publish_ready = publish
        with patch('backend.upload_pipeline.run', side_effect=AssertionError('video pipeline')), \
                patch.object(self.manager, '_compress_video', side_effect=AssertionError('compression')):
            done = self.manager.finish(job['id'], USER['id'], SimpleNamespace(), lambda _: True)
        self.assertEqual(done['state'], 'ready')
        self.assertEqual(done['progress'], 100)
        self.assertEqual(captured['files'], [])
        self.assertEqual(captured['prefix'], PREFIX)
        self.assertEqual(self.source.read_bytes(), self.raw)

    def test_invalid_metadata_mode_owner_and_changed_source_rejected(self):
        selected = self.select()
        for changes in (dict(capture_date='20260230'), dict(prediction='generate'), dict(upload_kind='video')):
            with self.subTest(changes=changes), self.assertRaises(HTTPException):
                self.start(selected, {**self.options, **changes})
        with self.assertRaises(HTTPException):
            self.start(selected, user=OTHER)
        self.source.write_bytes(b'changed')
        with self.assertRaises(HTTPException):
            self.start(selected)

    def test_revocation_and_upload_error_leave_retryable_error(self):
        selected = self.select()
        job = self.start(selected)
        self.target.publish_ready = Mock()
        with self.assertRaises(HTTPException):
            self.manager.finish(job['id'], USER['id'], None, lambda _: False)
        self.target.publish_ready.assert_not_called()
        self.assertEqual(self.manager.status(job['id'], USER['id'])['state'], 'error')
        retried = self.start(selected)
        done = self.manager.finish(retried['id'], USER['id'], None, lambda _: True)
        self.assertEqual(done['state'], 'ready')

    def test_links_and_empty_folder_rejected(self):
        (self.root / 'IMU/link.wplay').symlink_to(self.source)
        with self.assertRaises(HTTPException):
            scan_imu(self.root)
        (self.root / 'empty').mkdir()
        with self.assertRaises(HTTPException):
            scan_imu(self.root / 'empty')

    def test_agent_selection_transfer_validation_and_restart_reuse(self):
        from backend.upload_agent import AgentEngine
        provider = MultipartObjects()
        local = self.root / '.local'
        local.mkdir()
        service = SimpleNamespace(local=local, probe=Mock(side_effect=AssertionError('no video probe')))
        uploads = R2Uploads(local, provider, service)
        sent = []
        def network(request):
            data = request.read()
            sent.append(data)
            key = unquote(urlsplit(str(request.url)).path).split('/test-bucket/', 1)[1]
            provider.put_if_absent(key, data)
            return httpx.Response(200)
        def api(method, path, **kwargs):
            if path == '/api/auth/me':
                return USER
            if path == '/api/r2/uploads':
                return uploads.start(USER, kwargs['json'])
            parts = urlsplit(path).path.split('/')
            if parts[-1] == 'finish':
                return uploads.finish(parts[-2], USER)
            if parts[-2] == 'files':
                return uploads.plan(parts[-3], int(parts[-1]), USER, single=True)
            raise AssertionError((method, path))
        for attempt in range(2):
            engine = AgentEngine(self.root, None, service=service, picker=lambda *_: [self.root])
            engine.target.request = api
            engine.target.pipeline_enabled = True
            engine.execute(dict(id='select', action='select', fields={'upload_kind': 'imu'}))
            self.assertEqual(engine.snapshot()['selection']['upload_kind'], 'imu')
            direct = httpx.Client(transport=httpx.MockTransport(network))
            with patch('backend.upload_transfer.httpx.Client', return_value=direct):
                engine.execute(dict(id='start', action='start', fields={**self.options, 'collector': 'test'}))
            report = engine.snapshot()
            self.assertFalse(report.get('error'), report)
            self.assertEqual(report['job']['state'], 'ready')
            self.assertEqual(report['job']['upload_kind'], 'imu')
        self.assertEqual(sent, [self.raw])
        service.probe.assert_not_called()
        self.assertEqual(self.source.read_bytes(), self.raw)
