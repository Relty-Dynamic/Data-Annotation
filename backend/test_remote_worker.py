from __future__ import annotations

import copy
import hashlib
import io
import json
import os
import tempfile
import threading
import time
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException
from fastapi.testclient import TestClient

from backend.remote_worker import MAX_BODY, create_worker
from backend.remote import RemoteClient
from backend.service import AXES, FILENAMES, ProjectService
from backend.session_cache import PROFILE, SessionCache
from backend.testing_annotations import complete_project

ROOT = Path(__file__).resolve().parents[1]
TOKEN = 'unit-test-worker-token-with-32-characters'
NORMAL = b'0000ftyp' + b'normal-mp4' * 8
FAST = b'0000ftyp' + b'fast-mp4' * 8


class FakeSessions:
    def __init__(self, service):
        self.service = service
        self.calls = []
        self.ready = threading.Event()
        self.ready.set()
        self.files = {}

    def start(self, ident, retry_failed=False):
        self.calls.append(ident)
        directory = self.service.storage.directory(ident) / 'playback'
        directory.mkdir(parents=True, exist_ok=True)
        for kind, data in (('normal', NORMAL), ('fast', FAST),
                           ('thumbnail', b'\xff\xd8thumb\xff\xd9'), ('sheet', b'\xff\xd8sheet\xff\xd9')):
            path = directory / kind
            path.write_bytes(data)
            self.files[(ident, kind)] = path
        return self.status(ident)

    def status(self, ident):
        return {'state': 'ready' if self.ready.is_set() else 'running', 'progress': 100 if self.ready.is_set() else 20,
                'detail': 'fixture cache'}

    def manifest(self, ident):
        return {'videos': [{'id': 'v0001', 'storyboard': {'version': 1, 'frame_count': 1,
                'interval_ms': 1000, 'tile_width': 160, 'tile_height': 90, 'columns': 10,
                'rows': 10, 'sheets': ['sheet']}}]}

    def asset(self, ident, kind, video_id, index=None):
        return self.files.get((ident, kind))

    def close(self):
        self.ready.set()

    def invalidate(self, ident):
        for key in list(self.files):
            if key[0] == ident:
                self.files.pop(key)


class RemoteWorkerTests(unittest.TestCase):
    def setUp(self):
        (ROOT / '.tmp').mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix='remote-worker-', dir=ROOT / '.tmp')
        self.root = Path(self.temp.name)
        self.nas = self.root / 'nas'
        self.nas.mkdir()
        (self.nas / 'FPV').mkdir()
        self.source = self.nas / 'FPV' / 'A09999_20260917120000_0001.avi'
        self.source.write_bytes(b'original-video')
        self.app = create_worker(self.root / 'app', token=TOKEN, allowed_roots=[self.nas], slots=1)
        self.worker = self.app.state.worker
        self.worker.service.probe = lambda path: {'duration_ms': 1000, 'media_start_seconds': 0,
                                                'format_start_seconds': 0, 'codec': 'mjpeg',
                                                'pixel_format': 'yuv420p', 'audio_codecs': []}
        self.worker.service.sessions.close()
        self.sessions = FakeSessions(self.worker.service)
        self.worker.service.sessions = self.sessions
        self.client = TestClient(self.app)
        self.client.headers['Authorization'] = 'Bearer ' + TOKEN

    def tearDown(self):
        self.worker.close()
        self.client.close()
        self.temp.cleanup()

    def payload(self, source=None):
        source = source or self.source
        return {'path': str(source), 'stamp': self.worker.service.source_stamp(source),
                'duration_ms': 1000, 'media_start_seconds': 0, 'profile': PROFILE}

    def wait_job(self, ident):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            response = self.client.get('/v1/jobs/' + ident)
            self.assertEqual(response.status_code, 200, response.text)
            status = response.json()
            if status['state'] == 'ready':
                return status
            if status['state'] == 'error':
                self.fail(status['detail'])
            time.sleep(.02)
        self.fail('Cache job timed out')

    def project(self):
        project = self.worker.service.create([self.source], 'fixture', self.nas, ident='a' * 32)
        return complete_project(project)

    def test_requires_token_for_health_and_all_routes(self):
        for path in ('/health', '/v1/jobs/' + 'a' * 64, '/v1/jobs/' + 'a' * 64 + '/archive', '/docs'):
            response = self.client.get(path, headers={'Authorization': 'Bearer wrong'})
            self.assertEqual(response.status_code, 401)
        response = self.client.get('/health')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['protocol'], 1)
        self.assertEqual(response.json()['profile'], PROFILE)
        self.assertNotIn('access-control-allow-origin', response.headers)

    def test_empty_or_missing_token_fails_startup(self):
        with self.assertRaises(RuntimeError):
            create_worker(self.root / 'bad', token='', allowed_roots=[self.nas])
        with patch.dict(os.environ, {}, clear=True), self.assertRaises(RuntimeError):
            create_worker(self.root / 'bad', allowed_roots=[self.nas])
        for token in ('too-short', 'a' * 32 + ' ', 'a' * 32 + '\t'):
            with self.assertRaises(RuntimeError):
                create_worker(self.root / 'bad', token=token, allowed_roots=[self.nas])

    def test_request_size_is_enforced_before_json_parsing(self):
        response = self.client.post('/v1/inspect', content=b'x' * (MAX_BODY + 1))
        self.assertEqual(response.status_code, 413)
        response = self.client.post('/v1/inspect', content=iter([b'x' * MAX_BODY, b'x']))
        self.assertEqual(response.status_code, 413)

    def test_inspection_rejects_escape_traversal_and_nonvideo(self):
        outside = self.root / 'outside.avi'
        outside.write_bytes(b'not-authorized')
        text = self.nas / 'notes.txt'
        text.write_text('not-video', encoding='utf-8')
        for path, expected in ((outside, 403), (self.nas / '..' / 'outside.avi', 403), (text, 422)):
            response = self.client.post('/v1/inspect', json={'path': str(path)})
            self.assertEqual(response.status_code, expected, response.text)
        response = self.client.post('/v1/inspect', json={'path': str(self.source)})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['stamp'], self.worker.service.source_stamp(self.source))

    def test_symlink_cannot_escape_allowed_root(self):
        outside = self.root / 'outside.avi'
        outside.write_bytes(b'outside')
        link = self.nas / 'linked.avi'
        try:
            link.symlink_to(outside)
        except OSError:
            self.skipTest('Windows account cannot create symlinks')
        response = self.client.post('/v1/inspect', json={'path': str(link)})
        self.assertEqual(response.status_code, 403)

    def test_job_validates_identity_profile_and_timing(self):
        for key, value, expected in (('profile', 'wrong', 409), ('duration_ms', 2000, 409),
                                     ('media_start_seconds', 1, 409), ('stamp', {}, 409)):
            payload = self.payload()
            payload[key] = value
            response = self.client.post('/v1/jobs', json=payload)
            self.assertEqual(response.status_code, expected, response.text)
        payload = self.payload()
        payload['stamp']['mtime_ns'] -= 500_000
        response = self.client.post('/v1/jobs', json=payload)
        self.assertEqual(response.status_code, 200, response.text)
        self.wait_job(response.json()['id'])

    def test_archive_contract_reuse_and_restart_recovery(self):
        response = self.client.post('/v1/jobs', json=self.payload())
        self.assertEqual(response.status_code, 200, response.text)
        ident = response.json()['id']
        ready = self.wait_job(ident)
        data = self.client.get('/v1/jobs/' + ident + '/archive').content
        self.assertEqual(len(data), ready['archive']['size'])
        self.assertEqual(hashlib.sha256(data).hexdigest(), ready['archive']['sha256'])
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            self.assertEqual(set(archive.namelist()), {'manifest.json', 'normal.mp4', 'fast.mp4', 'thumbnail.jpg', 'sheet-00000.jpg'})
            self.assertTrue(all(info.compress_type == zipfile.ZIP_STORED for info in archive.infolist()))
            manifest = json.loads(archive.read('manifest.json'))
            self.assertEqual(manifest['protocol'], 1)
            self.assertEqual(manifest['profile'], PROFILE)
            self.assertEqual(manifest['source_stamp'], self.payload()['stamp'])
            self.assertEqual(manifest['duration_ms'], 1000)
            self.assertEqual(manifest['storyboard']['frame_count'], 1)
            for record in [*manifest['files'].values(), *manifest['sheets']]:
                content = archive.read(record['name'])
                self.assertEqual(record['size'], len(content))
                self.assertEqual(record['sha256'], hashlib.sha256(content).hexdigest())
        self.assertEqual(self.client.post('/v1/jobs', json=self.payload()).json()['id'], ident)
        self.assertEqual(len(self.sessions.calls), 1)
        with self.worker.guard:
            self.worker.jobs.clear()
        self.assertEqual(self.client.get('/v1/jobs/' + ident).json()['state'], 'ready')
        self.assertEqual(len(self.sessions.calls), 1)
        self.assertFalse((self.nas / '.datamark-cache').exists())
        self.assertTrue((self.source.parent / '.datamark-cache' / 'remote-v1' / (ident + '.zip')).is_file())
        self.assertFalse(self.worker._local_archive(ident).exists())
        self.assertFalse(self.worker.service.storage.directory(ident[:32]).exists())

    def test_worker_archive_installs_as_existing_local_playback_contract(self):
        response = self.client.post('/v1/jobs', json=self.payload())
        ident = response.json()['id']
        self.wait_job(ident)
        source = {**self.payload(), 'media_start_seconds': 0}
        key = SessionCache._source_key(source)
        directory = self.root / 'local-playback'
        directory.mkdir()
        client = RemoteClient.__new__(RemoteClient)
        client._closing = threading.Event()
        entry = client._install(self.worker._archive(ident), directory, key, source, threading.Event())
        self.assertEqual(entry['profile'], PROFILE)
        cache = SessionCache(self.worker.service)
        try:
            self.assertEqual(cache._load_entry(directory, key, source), entry)
        finally:
            cache.close()
        self.assertEqual((directory / (key + '.compact-v1.mp4')).read_bytes(), NORMAL)

    def test_changed_archive_is_rebuilt_on_resubmit(self):
        ident = self.client.post('/v1/jobs', json=self.payload()).json()['id']
        self.wait_job(ident)
        self.worker._archive(ident).write_bytes(b'corrupt-cache')
        self.assertEqual(self.client.get('/v1/jobs/' + ident).json()['state'], 'error')
        response = self.client.post('/v1/jobs', json=self.payload())
        self.assertEqual(response.status_code, 200)
        self.wait_job(ident)
        self.assertEqual(len(self.sessions.calls), 2)

    def test_two_jobs_are_serial_and_disconnect_does_not_cancel(self):
        self.sessions.ready.clear()
        first = self.client.post('/v1/jobs', json=self.payload()).json()['id']
        second_source = self.nas / 'A09999_20260917120002_0002.avi'
        second_source.write_bytes(b'second-original')
        second = self.client.post('/v1/jobs', json=self.payload(second_source)).json()['id']
        deadline = time.monotonic() + 3
        while not self.sessions.calls and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertEqual(len(self.sessions.calls), 1)
        self.assertEqual(self.client.get('/v1/jobs/' + second).json()['state'], 'queued')
        self.client.close()
        self.sessions.ready.set()
        self.client = TestClient(self.app, headers={'Authorization': 'Bearer ' + TOKEN})
        self.wait_job(first)
        self.wait_job(second)
        self.assertEqual(len(self.sessions.calls), 2)

    def test_queue_capacity_rejects_excess_work(self):
        self.worker.pending.maxsize = 1
        self.sessions.ready.clear()
        first = self.client.post('/v1/jobs', json=self.payload()).json()['id']
        deadline = time.monotonic() + 3
        while not self.sessions.calls and time.monotonic() < deadline:
            time.sleep(.01)
        others = []
        for index in (2, 3):
            path = self.nas / f'A09999_2026091712000{index}_000{index}.avi'
            path.write_bytes(f'original-{index}'.encode())
            others.append(self.client.post('/v1/jobs', json=self.payload(path)))
        self.assertEqual(others[0].status_code, 200)
        self.assertEqual(others[1].status_code, 429)
        self.sessions.ready.set()
        self.wait_job(first)
        self.wait_job(others[0].json()['id'])

    def test_writeback_preserves_paths_and_checks_external_conflicts(self):
        project = self.project()
        project['videos'][0]['relative_path'] = 'FPV/' + self.source.name
        project['_sources']['v0001']['cache_directory'] = str(self.root / 'outside')
        project['_sources']['v0001']['cache_identity_path'] = 'private-client-path'
        response = self.client.post('/v1/writeback', json={'project': project})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertFalse(response.json()['project']['draft_dirty'])
        for axis in AXES:
            document = json.loads((self.nas / 'timeline' / FILENAMES[axis]).read_bytes())
            self.assertIn('FPV/' + self.source.name, json.dumps(document))
        saved = self.worker.writeback_service.load(project['id'])
        self.assertNotIn('cache_directory', saved['_sources']['v0001'])
        self.assertNotIn('cache_identity_path', saved['_sources']['v0001'])
        replay = self.client.post('/v1/writeback', json={'project': project})
        self.assertEqual(replay.status_code, 200, replay.text)
        self.assertEqual(replay.json(), response.json())
        project.update(response.json()['project'])
        response = self.client.post('/v1/writeback', json={'project': project})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(any((self.nas / '.annotation-backups').iterdir()))

    def test_writeback_replays_committed_save_after_lost_response_and_restart(self):
        project = self.project()
        with patch.object(self.worker, '_writeback_response', side_effect=ConnectionResetError('response lost')):
            with self.assertRaises(ConnectionResetError):
                self.client.post('/v1/writeback', json={'project': project})
        saved = self.worker.writeback_service.load(project['id'])
        snapshots = {axis: (self.nas / 'timeline' / FILENAMES[axis]).read_bytes() for axis in AXES}
        self.worker.close()
        self.app = create_worker(self.root / 'app', token=TOKEN, allowed_roots=[self.nas])
        self.worker = self.app.state.worker
        self.client.close()
        self.client = TestClient(self.app, headers={'Authorization': 'Bearer ' + TOKEN})
        # Canonical JSON identity ignores dictionary insertion order.
        reordered = {key: project[key] for key in reversed(project)}
        with patch.object(self.worker.writeback_service, 'writeback', side_effect=AssertionError('duplicate write')):
            response = self.client.post('/v1/writeback', json={'project': reordered})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['result']['save_id'], saved['last_writeback']['save_id'])
        self.assertEqual(response.json()['project']['last_writeback'], saved['last_writeback'])
        self.assertFalse((self.nas / '.annotation-backups').exists())
        for axis in AXES:
            self.assertEqual((self.nas / 'timeline' / FILENAMES[axis]).read_bytes(), snapshots[axis])

    def test_replayed_writeback_still_rejects_a_real_external_edit(self):
        project = self.project()
        first = self.client.post('/v1/writeback', json={'project': project})
        self.assertEqual(first.status_code, 200, first.text)
        external = self.nas / 'timeline' / FILENAMES['scene']
        external.write_bytes(external.read_bytes() + b'\n')
        changed = external.read_bytes()
        response = self.client.post('/v1/writeback', json={'project': project})
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(external.read_bytes(), changed)
        saved = self.worker.writeback_service.load(project['id'])
        self.assertEqual(saved['last_writeback'], first.json()['project']['last_writeback'])

    def test_failed_writeback_cannot_replay_a_client_supplied_success_marker(self):
        project = self.project()
        project['last_writeback'] = {'save_id': 'b' * 32, 'saved_at': 'previous-local-save'}
        project['draft_dirty'] = False
        with patch.object(self.worker.writeback_service, 'writeback', side_effect=HTTPException(503, 'fixture unavailable')):
            first = self.client.post('/v1/writeback', json={'project': project})
        self.assertEqual(first.status_code, 503)
        failed = self.worker.writeback_service.load(project['id'])
        self.assertIsNone(failed['last_writeback'])
        self.assertTrue(failed['draft_dirty'])
        self.assertFalse((self.nas / 'timeline').exists())
        response = self.client.post('/v1/writeback', json={'project': project})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertNotEqual(response.json()['result']['save_id'], 'b' * 32)

    def test_writeback_rejects_changed_sources_and_invalid_metadata(self):
        project = self.project()
        for mutate in (lambda p: p.update(id='../outside'),
                       lambda p: p.update(source_dir=str(self.root)),
                       lambda p: p['videos'][0].update(start_ms=-1),
                       lambda p: p['videos'][0].update(relative_path='../escape.avi'),
                       lambda p: p['videos'][0].update(relative_path='FPV\\' + self.source.name),
                       lambda p: p['videos'][0].update(relative_path='C:/FPV/' + self.source.name),
                       lambda p: p['videos'][0].update(relative_path='not-the-source.avi'),
                       lambda p: p.update(annotations={})):
            bad = copy.deepcopy(project)
            mutate(bad)
            response = self.client.post('/v1/writeback', json={'project': bad})
            self.assertIn(response.status_code, (403, 422), response.text)
        self.source.write_bytes(b'changed-source')
        response = self.client.post('/v1/writeback', json={'project': project})
        self.assertEqual(response.status_code, 409)
        self.assertFalse((self.nas / 'timeline').exists())

    def test_writeback_rejects_mismatched_relative_path_for_another_collection(self):
        project = self.project()
        another = self.nas / 'unrelated-collection'
        another.mkdir()
        project['source_dir'] = str(another)
        response = self.client.post('/v1/writeback', json={'project': project})
        self.assertEqual(response.status_code, 422, response.text)
        self.assertEqual(list(another.iterdir()), [])

    def test_writeback_preserves_windows_fresh_import_alias_for_historical_project(self):
        project = self.project()
        project['source_fingerprint'] = 'f' * 64
        project['_sources']['v0001']['stamp']['mtime_ns'] += 100
        expected = ProjectService.current_source_fingerprint(project)
        first = self.client.post('/v1/writeback', json={'project': project})
        self.assertEqual(first.status_code, 200, first.text)
        for axis in AXES:
            document = json.loads((self.nas / 'timeline' / FILENAMES[axis]).read_bytes())
            self.assertEqual(document['timebase']['source_fingerprint'], 'f' * 64)
            self.assertEqual(document['timebase']['source_fingerprint_aliases'], [expected])
        fresh = copy.deepcopy(project)
        fresh['source_fingerprint'] = expected
        restored, hashes = self.worker.service.read_external(fresh)
        self.assertEqual(restored, project['annotations'])
        self.assertTrue(all(hashes.values()))
        replay = self.client.post('/v1/writeback', json={'project': project})
        self.assertEqual(replay.status_code, 200, replay.text)
        self.assertEqual(replay.json(), first.json())
        saved = self.worker.writeback_service.load(project['id'])
        self.assertEqual(saved['_sources']['v0001']['stamp'], self.worker.service.source_stamp(self.source))

    def test_writeback_recomputes_and_never_trusts_client_supplied_alias(self):
        project = self.project()
        project['source_fingerprint'] = 'f' * 64
        project['_remote_verified_client_fingerprint'] = 'e' * 64
        project['source_fingerprint_aliases'] = ['d' * 64]
        expected = ProjectService.current_source_fingerprint(project)
        response = self.client.post('/v1/writeback', json={'project': project})
        self.assertEqual(response.status_code, 200, response.text)
        document = json.loads((self.nas / 'timeline' / FILENAMES['scene']).read_bytes())
        self.assertEqual(document['timebase']['source_fingerprint_aliases'], [expected])
        self.assertNotEqual(expected, 'e' * 64)
        self.assertNotEqual(expected, 'd' * 64)

    def test_writeback_keeps_legacy_relinked_basename_and_adds_fresh_import_alias(self):
        legacy = complete_project(self.worker.service.create([self.source], 'legacy upload', None,
                                                             ident='c' * 32))
        historical = legacy['source_fingerprint']
        legacy['source_dir'] = str(self.nas)
        legacy['_sources']['v0001']['stamp']['mtime_ns'] += 100
        self.assertEqual(legacy['videos'][0]['relative_path'], self.source.name)
        expected = ProjectService.current_source_fingerprint(legacy)
        response = self.client.post('/v1/writeback', json={'project': legacy})
        self.assertEqual(response.status_code, 200, response.text)
        document = json.loads((self.nas / 'timeline' / FILENAMES['scene']).read_bytes())
        self.assertEqual(document['timebase']['source_fingerprint'], historical)
        self.assertEqual(document['timebase']['videos'][0]['relative_path'], self.source.name)
        self.assertEqual(document['timebase']['source_fingerprint_aliases'], [expected])
        with patch.object(self.worker.service, 'source_stamp', return_value=legacy['_sources']['v0001']['stamp']):
            fresh = self.worker.service.create([self.source], 'fresh import', self.nas)
        self.assertEqual(fresh['videos'][0]['relative_path'], 'FPV/' + self.source.name)
        self.assertEqual(fresh['source_fingerprint'], expected)
        restored, _ = self.worker.service.read_external(fresh)
        self.assertEqual(restored, legacy['annotations'])
        replay = self.client.post('/v1/writeback', json={'project': legacy})
        self.assertEqual(replay.json(), response.json())

    def test_writeback_supports_supplemented_sources_outside_collection_directory(self):
        supplement = self.nas / 'supplement'
        supplement.mkdir()
        extra = supplement / 'A09999_20260917120002_0002.avi'
        extra.write_bytes(b'supplemented-original')
        project = complete_project(self.worker.service.create([self.source, extra], 'fixture', self.source.parent,
                                                               ident='b' * 32))
        self.assertEqual([video['relative_path'] for video in project['videos']], [self.source.name, extra.name])
        response = self.client.post('/v1/writeback', json={'project': project})
        self.assertEqual(response.status_code, 200, response.text)
        destination = self.source.parent / 'timeline'
        self.assertTrue(all((destination / FILENAMES[axis]).is_file() for axis in AXES))
        self.assertFalse((supplement / 'timeline').exists())
        self.assertEqual(extra.read_bytes(), b'supplemented-original')
        project.update(response.json()['project'])
        forged = copy.deepcopy(project)
        forged['videos'][1]['relative_path'] = 'another-video.avi'
        rejected = self.client.post('/v1/writeback', json={'project': forged})
        self.assertEqual(rejected.status_code, 422, rejected.text)
        outside = self.root / extra.name
        outside.write_bytes(extra.read_bytes())
        forged = copy.deepcopy(project)
        forged['_sources']['v0002']['path'] = str(outside)
        forged['_sources']['v0002']['stamp'] = self.worker.service.source_stamp(outside)
        rejected = self.client.post('/v1/writeback', json={'project': forged})
        self.assertEqual(rejected.status_code, 403, rejected.text)

    def test_writeback_rejects_symlink_outputs(self):
        outside = self.root / 'outside-directory'
        outside.mkdir()
        try:
            (self.nas / 'timeline').symlink_to(outside, target_is_directory=True)
        except OSError:
            self.skipTest('Windows account cannot create symlinks')
        response = self.client.post('/v1/writeback', json={'project': self.project()})
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(list(outside.iterdir()), [])

    def test_writeback_checks_backup_folder_and_each_output_link(self):
        original = Path.is_symlink
        for target in (self.nas / '.annotation-backups', self.nas / 'timeline' / FILENAMES['scene']):
            with patch.object(Path, 'is_symlink', lambda path: path == target or original(path)):
                response = self.client.post('/v1/writeback', json={'project': self.project()})
            self.assertEqual(response.status_code, 409, response.text)
        self.assertFalse((self.nas / 'timeline').exists())


if __name__ == '__main__':
    unittest.main()
