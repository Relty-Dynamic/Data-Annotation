from __future__ import annotations

import copy
import json
import os
import shutil
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException
from fastapi.testclient import TestClient

from backend.remote_worker import Worker, create_worker
from backend import test_remote_worker as fixtures
from backend.test_remote_worker import FakeSessions, TOKEN


class WorkerNasCacheTests(unittest.TestCase):
    setUp = fixtures.RemoteWorkerTests.setUp
    tearDown = fixtures.RemoteWorkerTests.tearDown
    payload = fixtures.RemoteWorkerTests.payload
    wait_job = fixtures.RemoteWorkerTests.wait_job

    def complete(self):
        response = self.client.post('/v1/jobs', json=self.payload())
        self.assertEqual(response.status_code, 200, response.text)
        return self.wait_job(response.json()['id'])

    def await_error(self, ident):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            result = self.worker.status(ident)
            if result['state'] == 'error':
                return result
            time.sleep(.01)
        self.fail('Job did not report its failure')

    def replace_worker(self, slots=2, root=None):
        probe = self.worker.service.probe
        self.worker.close()
        self.client.close()
        self.app = create_worker(root or self.root / 'app', token=TOKEN, allowed_roots=[self.nas], slots=slots)
        self.worker = self.app.state.worker
        self.fixtures = []
        for service in self.worker.services:
            service.probe = probe
            service.sessions.close()
            service.sessions = FakeSessions(service)
            self.fixtures.append(service.sessions)
        self.sessions = self.fixtures[0]
        self.client = TestClient(self.app, headers={'Authorization': 'Bearer ' + TOKEN})

    def test_two_slots_prepare_independently_and_deduplicate_same_job(self):
        self.replace_worker()
        for sessions in self.fixtures:
            sessions.ready.clear()
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(self.worker.submit, self.payload()) for _ in range(2)]
            responses = [future.result(timeout=3) for future in futures]
        self.assertEqual(responses[0]['id'], responses[1]['id'])
        first = responses[0]['id']
        second_source = self.nas / 'FPV' / 'A09999_20260917120002_0002.avi'
        second_source.write_bytes(b'second-original')
        second = self.worker.submit(self.payload(second_source))['id']
        deadline = time.monotonic() + 3
        while sum(len(item.calls) for item in self.fixtures) < 2 and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertEqual([len(item.calls) for item in self.fixtures], [1, 1])
        for ident in (first, second):
            self.assertEqual(self.worker.status(ident)['state'], 'running')
        self.assertNotEqual(self.worker.services[0].db_path, self.worker.services[1].db_path)
        self.fixtures[0].ready.set()
        ready_id = self.fixtures[0].calls[0]
        self.wait_job(first if first.startswith(ready_id) else second)
        other = second if first.startswith(ready_id) else first
        self.assertEqual(self.worker.status(other)['state'], 'running')
        self.fixtures[1].ready.set()
        self.wait_job(other)
        self.assertEqual(sum(len(item.calls) for item in self.fixtures), 2)

    def test_slots_accept_only_one_or_two_and_default_to_two(self):
        for slots in (0, 3, -1, True, '2'):
            with self.assertRaises(RuntimeError):
                Worker(self.root / 'bad-slot', [self.nas], slots=slots)
        for value in ('no', '0', '3'):
            with patch.dict(os.environ, {'DATAMARK_WORKER_SLOTS': value}), self.assertRaises(RuntimeError):
                create_worker(self.root / 'bad-slot', token=TOKEN, allowed_roots=[self.nas])
        with patch.dict(os.environ, {'DATAMARK_WORKER_SLOTS': '2'}):
            app = create_worker(self.root / 'default-slot', token=TOKEN, allowed_roots=[self.nas])
        try:
            self.assertEqual(len(app.state.worker.services), 2)
        finally:
            app.state.worker.close()

    def test_new_worker_uses_nas_archive_without_generating_or_project_database(self):
        first = self.complete()
        self.assertEqual(first['cache_origin'], 'generated')
        original = self.source.read_bytes()
        archive, metadata = self.worker.nas.paths(self.source, first['id'])
        original_archive = archive.read_bytes()
        self.replace_worker(root=self.root / 'clean-worker')
        with patch.object(self.worker.service, 'create', side_effect=AssertionError('must not transcode')):
            second = self.complete()
        self.assertEqual(second['cache_origin'], 'nas')
        self.assertEqual(first['archive'], second['archive'])
        self.assertEqual(archive.read_bytes(), original_archive)
        self.assertEqual(self.source.read_bytes(), original)
        self.assertTrue(metadata.exists())
        self.assertTrue(all(not item.calls for item in self.fixtures))
        self.assertFalse(self.worker._local_archive(first['id']).exists())
        with self.worker.service.connection() as connection:
            self.assertEqual(connection.execute('SELECT COUNT(*) FROM projects').fetchone()[0], 0)

    def test_failed_nas_publication_retains_local_archive_then_retry_migrates(self):
        with patch.object(self.worker.nas, 'publish', side_effect=HTTPException(503, 'NAS temporarily read-only')):
            ident = self.worker.submit(self.payload())['id']
            self.await_error(ident)
        local = self.worker._local_archive(ident)
        self.assertTrue(local.exists())
        self.assertTrue(self.worker.service.storage.directory(ident[:32]).exists())
        self.assertEqual(self.worker._read_record(ident)['state'], 'prepared')
        with patch.object(self.sessions, 'start', side_effect=AssertionError('do not regenerate completed archive')):
            retried = self.complete()
        self.assertEqual(retried['cache_origin'], 'server-local')
        self.assertFalse(local.exists())
        self.assertFalse(self.worker.service.storage.directory(ident[:32]).exists())
        self.assertEqual(len(self.sessions.calls), 1)

    def test_legacy_server_only_zip_is_published_without_repackaging_after_restart(self):
        ready = self.complete()
        ident = ready['id']
        nas_zip, nas_record = self.worker.nas.paths(self.source, ident)
        local = self.worker._local_archive(ident)
        shutil.copyfile(nas_zip, local)
        job = copy.deepcopy(self.worker.jobs[ident])
        job.pop('archive_storage')
        job['archive']['mtime_ns'] = local.stat().st_mtime_ns
        self.worker._save_record(job)
        nas_zip.unlink()
        nas_record.unlink()
        self.replace_worker()
        with patch.object(self.worker, '_generate', side_effect=AssertionError('legacy package already complete')):
            result = self.complete()
        self.assertEqual(result['cache_origin'], 'server-local')
        self.assertEqual(result['archive'], ready['archive'])
        self.assertFalse(local.exists())
        self.assertTrue(nas_zip.exists())

    def test_cleanup_preserves_source_annotations_other_cache_and_other_projects(self):
        timeline = self.nas / 'timeline'
        timeline.mkdir()
        annotation = timeline / 'scene.timeline.json'
        annotation.write_text('precious-annotation', encoding='utf-8')
        other_cache = self.source.parent / '.datamark-cache' / 'some-old-project'
        other_cache.mkdir(parents=True)
        other_asset = other_cache / 'normal.mp4'
        other_asset.write_bytes(b'precious-cache')
        other_project = self.worker.service.storage.directory('d' * 32)
        other_project.mkdir()
        other_preview = other_project / 'preview.mp4'
        other_preview.write_bytes(b'other-project')
        with patch.object(self.worker.service, 'delete_project', side_effect=AssertionError('not the user deletion path')):
            ready = self.complete()
        self.assertEqual(annotation.read_text(encoding='utf-8'), 'precious-annotation')
        self.assertEqual(other_asset.read_bytes(), b'precious-cache')
        self.assertEqual(other_preview.read_bytes(), b'other-project')
        self.assertEqual(self.source.read_bytes(), b'original-video')
        self.assertFalse(self.worker.service.storage.directory(ready['id'][:32]).exists())

    def test_bad_same_size_archive_is_rebuilt_from_source(self):
        first = self.complete()
        archive = self.worker._archive(first['id'])
        old_stat = archive.stat()
        data = bytearray(archive.read_bytes())
        data[0] ^= 1
        archive.write_bytes(data)
        os.utime(archive, ns=(old_stat.st_atime_ns, old_stat.st_mtime_ns))
        with self.worker.guard:
            self.worker.jobs.clear()
        self.assertEqual(self.client.get('/v1/jobs/' + first['id']).status_code, 404)
        second = self.complete()
        self.assertEqual(second['cache_origin'], 'generated')
        self.assertEqual(len(self.sessions.calls), 2)

    def test_slow_archive_restore_does_not_hold_global_guard(self):
        first = self.complete()
        with self.worker.guard:
            self.worker.jobs.clear()
        entered = threading.Event()
        release = threading.Event()
        original = self.worker.nas.read

        def slow_read(*args):
            entered.set()
            release.wait(4)
            return original(*args)

        with patch.object(self.worker.nas, 'read', side_effect=slow_read), ThreadPoolExecutor(max_workers=1) as executor:
            restoring = executor.submit(self.worker.status, first['id'])
            try:
                self.assertTrue(entered.wait(2))
                acquired = self.worker.guard.acquire(timeout=.2)
                self.assertTrue(acquired, 'NAS hash held the global guard')
                if acquired:
                    self.worker.guard.release()
                self.assertEqual(self.client.get('/health').status_code, 200)
            finally:
                release.set()
            self.assertEqual(restoring.result(timeout=3)['state'], 'ready')

    def test_close_stops_both_slot_sessions_and_rejects_new_jobs(self):
        self.replace_worker()
        self.worker.close()
        self.assertTrue(self.worker.stopping.is_set())
        self.assertTrue(all(session.ready.is_set() for session in self.fixtures))
        with self.assertRaises(HTTPException) as caught:
            self.worker.submit(self.payload())
        self.assertEqual(caught.exception.status_code, 503)

    def test_linked_source_or_cache_component_is_rejected_without_mutation(self):
        original = Path.is_symlink
        with patch.object(Path, 'is_symlink', lambda path: path == self.source.parent or original(path)):
            response = self.client.post('/v1/inspect', json={'path': str(self.source)})
        self.assertEqual(response.status_code, 403)
        ident = self.complete()['id']
        archive, metadata = self.worker.nas.paths(self.source, ident)
        for linked in (self.source.parent / '.datamark-cache', archive.parent, archive, metadata):
            with patch.object(Path, 'is_symlink', lambda path, linked=linked: path == linked or original(path)):
                with self.assertRaises(HTTPException) as caught:
                    self.worker.nas.read(self.worker.jobs[ident])
                self.assertEqual(caught.exception.status_code, 409)
        self.assertEqual(self.source.read_bytes(), b'original-video')

    def test_timings_are_nonnegative_and_stage_is_complete(self):
        ready = self.complete()
        self.assertEqual(ready['stage'], 'ready')
        for name in ('queue_seconds', 'prepare_seconds', 'package_seconds', 'nas_check_seconds',
                     'nas_save_seconds', 'cleanup_seconds'):
            self.assertIn(name, ready['timings'])
            self.assertGreaterEqual(ready['timings'][name], 0)

    def test_ready_and_cleanup_wait_until_nas_publication_is_confirmed(self):
        entered = threading.Event()
        release = threading.Event()
        original = self.worker.nas.publish

        def publishing(*args):
            entered.set()
            release.wait(3)
            return original(*args)

        with patch.object(self.worker.nas, 'publish', side_effect=publishing):
            ident = self.worker.submit(self.payload())['id']
            try:
                self.assertTrue(entered.wait(2))
                status = self.worker.status(ident)
                self.assertEqual(status['state'], 'running')
                self.assertEqual(status['stage'], 'publishing')
                self.assertNotIn('archive', status)
                self.assertTrue(self.worker._local_archive(ident).exists())
                self.assertTrue(self.worker.service.storage.directory(ident[:32]).exists())
                self.assertEqual(self.client.get('/v1/jobs/' + ident + '/archive').status_code, 409)
            finally:
                release.set()
            self.wait_job(ident)
        self.assertFalse(self.worker._local_archive(ident).exists())

    def test_restart_retries_leftover_cleanup_after_nas_was_already_confirmed(self):
        with patch.object(self.worker, '_cleanup', side_effect=HTTPException(409, 'fixture file locked')):
            ident = self.worker.submit(self.payload())['id']
            self.await_error(ident)
        self.assertTrue(self.worker._local_archive(ident).exists())
        self.assertEqual(self.worker._read_record(ident)['archive_storage'], 'nas')
        self.replace_worker()
        with patch.object(self.worker, '_generate', side_effect=AssertionError('NAS copy already exists')):
            status = self.worker.status(ident)
        self.assertEqual(status['state'], 'ready')
        self.assertEqual(status['cache_origin'], 'nas')
        self.assertFalse(self.worker._local_archive(ident).exists())
        self.assertFalse(self.worker.service.storage.directory(ident[:32]).exists())


if __name__ == '__main__':
    unittest.main()
