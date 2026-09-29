from __future__ import annotations

import copy
import json
import math
import os
import tempfile
import threading
import time
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException

from backend.service import FILENAMES, ProjectService, empty_annotations
from backend.source_cache import CACHE_FOLDER, MARKER
from backend.testing_annotations import complete_annotations

ROOT = Path(__file__).resolve().parents[1]


class SourceCacheTests(unittest.TestCase):
    """Source-adjacent caches must be reusable without owning the original videos."""

    def setUp(self):
        (ROOT / '.tmp').mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix='source-cache-test-', dir=ROOT / '.tmp')
        self.root = Path(self.temp.name)
        self.service = ProjectService(self.root / 'app')
        self.service.probe = lambda path: {
            'duration_ms': 1000, 'codec': 'mjpeg', 'pixel_format': 'yuvj420p',
            'audio_codecs': [], 'media_start_seconds': 0, 'format_start_seconds': 0,
        }
        self.services = [self.service]

    def tearDown(self):
        for service in self.services:
            service.sessions.close()
            service.previews.close()
        self.assertEqual(self.root.resolve().parent, (ROOT / '.tmp').resolve())
        self.temp.cleanup()

    def video(self, index=0, folder='card', payload=None):
        directory = self.root / folder
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f'A09999_202609171200{index:02d}_{index:04d}.avi'
        path.write_bytes(payload if payload is not None else b'original-video-' + path.name.encode())
        return path

    def local_playback(self, project):
        directory = self.service.storage.directory(project['id']) / 'playback'
        directory.mkdir(parents=True, exist_ok=True)
        (directory / 'compact.mp4').write_bytes(b'local compact cache')
        return directory

    def fresh_service(self, name):
        service = ProjectService(self.root / name)
        service.probe = self.service.probe
        self.services.append(service)
        return service

    def complete_cache(self, project, video_id='v0001'):
        spec = self.service.preview_spec(project, video_id)
        spec.target.write_bytes(b'completed-normal-preview')
        spec.thumbnail_target.write_bytes(b'\xff\xd8thumbnail\xff\xd9')
        self.service.fast_preview_path(spec).write_bytes(b'0000ftyp' + b'fast-preview' * 8)
        directory = self.service.storyboards.directory(spec.key)
        directory.mkdir(parents=True, exist_ok=True)
        count = math.ceil(spec.source['duration_ms'] / 1000)
        sheets = [f'sheet-{index:05d}.jpg' for index in range(math.ceil(count / 100))]
        for name in sheets:
            (directory / name).write_bytes(b'\xff\xd8storyboard-sheet\xff\xd9')
        (directory / 'manifest.json').write_text(json.dumps({
            'version': 1, 'interval_ms': 1000, 'tile_width': 160, 'tile_height': 90,
            'columns': 10, 'rows': 10, 'frame_count': count, 'sheets': sheets,
        }), encoding='utf-8')
        return spec

    def legacy_project(self, source, uploaded=False):
        ident = uuid.uuid4().hex
        if uploaded:
            copied = self.service.storage.directory(ident) / 'imports' / source.name
            copied.parent.mkdir(parents=True)
            copied.write_bytes(source.read_bytes())
            source = copied
        project = self.service.create([source], 'old-project', None, ident=ident)
        project['annotations'] = empty_annotations()
        project['annotations']['habit'] = [
            {'id': 'water', 'label': '喝水', 'kind': 'point', 'start_ms': 250, 'end_ms': 250},
        ]
        project['revision'] = 7
        project['draft_dirty'] = True
        self.service.save(project)
        return project, source, self.complete_cache(project)

    def assert_reusable(self, service, project):
        with patch.object(service.previews, 'render') as render:
            self.assertEqual(service.preview_status(project['id'], 'v0001')['state'], 'ready')
            self.assertEqual(service.preview_status(project['id'], 'v0001', fast=True)['state'], 'ready')
            self.assertEqual(service.storyboard_status(project['id'], 'v0001')['state'], 'ready')
            service.prepare_preview(project['id'], 'v0001', prefetch=False)
            render.assert_not_called()
        self.assertEqual(service.media(project['id'], 'v0001').read_bytes(), b'completed-normal-preview')

    def test_multisource_import_keeps_originals_and_all_preview_types_beside_each_source(self):
        first = self.video(0, 'card-a')
        second = self.video(1, 'card-b')
        before = {p: p.read_bytes() for p in (first, second)}
        opened = self.service.open_files([str(second), str(first)])
        project = self.service.load(opened['id'])
        self.assertEqual(opened['cache_mode'], 'source')
        self.assertFalse(opened['needs_source_relink'])
        self.assertEqual([v['name'] for v in opened['videos']], [first.name, second.name])
        expected = set()
        for video, path in zip(project['videos'], (first, second)):
            self.assertEqual(Path(project['_sources'][video['id']]['path']), path)
            preview = path.parent / CACHE_FOLDER / project['id'] / 'preview'
            spec = self.complete_cache(project, video['id'])
            expected.add(str(preview))
            self.assertEqual(spec.target.parent, preview)
            self.assertEqual(spec.thumbnail_target.parent, preview)
            self.assertEqual(self.service.fast_preview_path(spec).parent, preview)
            self.assertTrue(self.service.storyboards.directory(spec.key).is_relative_to(preview))
            self.assertEqual(path.read_bytes(), before[path])
        self.assertEqual(set(opened['cache_directories']), expected)
        self.assertEqual(list(self.service.local.rglob('*.avi')), [])
        self.assertEqual(list(self.service.local.rglob('*.mp4')), [])
        self.assertEqual(list(self.service.local.rglob('sheet-*.jpg')), [])

    def test_readme_explains_originals_and_restart_reuses_completed_caches(self):
        source = self.video()
        opened = self.service.open_files([str(source)])
        project = self.service.load(opened['id'])
        self.complete_cache(project)
        directory = source.parent / CACHE_FOLDER / project['id']
        for path in (directory.parent / 'README.md', directory / 'README.md'):
            content = path.read_text(encoding='utf-8')
            self.assertIn('DataMark', content)
            self.assertIn('原视频', content)
            self.assertIn('缓存', content)
            self.assertIn('删除', content)
        restarted = ProjectService(self.root / 'app')
        self.services.append(restarted)
        restarted.migrate_legacy_projects()
        self.assert_reusable(restarted, restarted.load(project['id']))
        self.assertEqual(restarted.public(restarted.load(project['id']))['cache_mode'], 'source')

    def test_readonly_source_cache_fails_import_without_local_copy_or_saved_project(self):
        source = self.video()
        original_open = Path.open
        def readonly_write(path, *args, **kwargs):
            if path.name.startswith('.write-check-'):
                raise PermissionError('simulated read-only card')
            return original_open(path, *args, **kwargs)
        with patch.object(Path, 'open', readonly_write):
            with self.assertRaises(HTTPException) as caught:
                self.service.open_files([str(source)])
        self.assertEqual(caught.exception.status_code, 422)
        self.assertEqual(self.service.projects(), [])
        self.assertTrue(source.is_file())
        self.assertEqual(list(self.service.local.rglob('*.avi')), [])
        self.assertEqual(list(self.service.local.rglob('*.mp4')), [])

    def test_offline_original_directory_does_not_silently_fall_back_to_platform_cache(self):
        source = self.video()
        opened = self.service.open_files([str(source)])
        project = self.service.load(opened['id'])
        self.complete_cache(project)
        original_is_dir = Path.is_dir
        with patch.object(Path, 'is_dir', lambda path: False if path == source.parent else original_is_dir(path)):
            with self.assertRaises(HTTPException) as caught:
                self.service.preview_status(project['id'], 'v0001')
        self.assertEqual(caught.exception.status_code, 404)
        self.assertEqual(list(self.service.local.rglob('*.mp4')), [])
        self.assertEqual(self.service.load(project['id'])['annotations'], project['annotations'])

    def wait_for_preparation(self, ident, timeout=4):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            status = self.service.preparation_status(ident)
            if status['state'] == 'ready':
                return status
            time.sleep(0.01)
        self.fail('Preparation status did not finish after the simulated network was released')

    def test_slow_project_status_does_not_block_single_video_or_draft_save(self):
        paths = [self.video(index) for index in (0, 1)]
        opened = self.service.open_files([str(path) for path in paths])
        project = self.service.load(opened['id'])
        for video in project['videos']:
            self.complete_cache(project, video['id'])
        entered, release = threading.Event(), threading.Event()
        original_spec = self.service.preview_spec
        def slow_remote_spec(snapshot, video_id, *args, **kwargs):
            if video_id == 'v0002':
                entered.set()
                if not release.wait(5):
                    raise AssertionError('Network fixture was not released')
            return original_spec(snapshot, video_id, *args, **kwargs)
        annotations = empty_annotations()
        annotations['habit'] = [{'id': 'during-scan', 'label': '喝水', 'kind': 'point',
                                 'start_ms': 250, 'end_ms': 250}]
        with patch.object(self.service, 'preview_spec', side_effect=slow_remote_spec):
            with ThreadPoolExecutor(max_workers=4) as pool:
                scan = pool.submit(self.service.preparation_status, project['id'])
                try:
                    self.assertTrue(entered.wait(2), 'The slow remote check never started')
                    preview = pool.submit(self.service.preview_status, project['id'], 'v0001')
                    media = pool.submit(self.service.media, project['id'], 'v0001')
                    draft = pool.submit(self.service.update_draft, project['id'], annotations, project['revision'])
                    self.assertEqual(preview.result(timeout=1)['state'], 'ready')
                    self.assertEqual(media.result(timeout=1).read_bytes(), b'completed-normal-preview')
                    saved = draft.result(timeout=1)
                    self.assertEqual(saved['revision'], project['revision'] + 1)
                    self.assertEqual(saved['annotations']['habit'], annotations['habit'])
                    self.assertFalse(release.is_set(), 'Interactive work only completed after the slow scan')
                finally:
                    release.set()
                    scan.result(timeout=4)
                    self.wait_for_preparation(project['id'])
        self.assertEqual(self.service.load(project['id'])['annotations']['habit'], annotations['habit'])

    def test_slow_single_preview_status_does_not_block_other_video_or_draft(self):
        paths = [self.video(index) for index in (0, 1)]
        opened = self.service.open_files([str(path) for path in paths])
        project = self.service.load(opened['id'])
        for video in project['videos']:
            self.complete_cache(project, video['id'])
        entered, release = threading.Event(), threading.Event()
        original_spec = self.service.preview_spec
        def slow_remote_spec(snapshot, video_id, *args, **kwargs):
            if video_id == 'v0002':
                entered.set()
                if not release.wait(5):
                    raise AssertionError('Network fixture was not released')
            return original_spec(snapshot, video_id, *args, **kwargs)
        annotations = empty_annotations()
        annotations['habit'] = [{'id': 'while-status-loads', 'label': '喝水', 'kind': 'point',
                                 'start_ms': 250, 'end_ms': 250}]
        with patch.object(self.service, 'preview_spec', side_effect=slow_remote_spec):
            with ThreadPoolExecutor(max_workers=4) as pool:
                slow = pool.submit(self.service.preview_status, project['id'], 'v0002')
                try:
                    self.assertTrue(entered.wait(2))
                    preview = pool.submit(self.service.preview_status, project['id'], 'v0001')
                    media = pool.submit(self.service.media, project['id'], 'v0001')
                    draft = pool.submit(self.service.update_draft, project['id'], annotations, project['revision'])
                    self.assertEqual(preview.result(timeout=1)['state'], 'ready')
                    self.assertEqual(media.result(timeout=1).read_bytes(), b'completed-normal-preview')
                    self.assertEqual(draft.result(timeout=1)['annotations']['habit'], annotations['habit'])
                    self.assertFalse(release.is_set())
                finally:
                    release.set()
                    self.assertEqual(slow.result(timeout=4)['state'], 'ready')

    def test_local_cleanup_during_single_nas_preview_status_preserves_source_cache(self):
        source = self.video()
        opened = self.service.open_files([str(source)])
        project = self.service.load(opened['id'])
        spec = self.complete_cache(project)
        cache_directory = spec.target.parent.parent
        local_directory = self.local_playback(project)
        entered, release = threading.Event(), threading.Event()
        original_spec = self.service.preview_spec
        def delayed_spec(snapshot, video_id, *args, **kwargs):
            entered.set()
            if not release.wait(5):
                raise AssertionError('Network fixture was not released')
            return original_spec(snapshot, video_id, *args, **kwargs)
        with patch.object(self.service, 'preview_spec', side_effect=delayed_spec):
            with ThreadPoolExecutor(max_workers=2) as pool:
                reading = pool.submit(self.service.preview_status, project['id'], 'v0001')
                try:
                    self.assertTrue(entered.wait(2))
                    deletion = pool.submit(self.service.clear_local_previews, project['id'], project['revision'])
                    self.assertEqual(deletion.result(timeout=1)['cleared'], project['id'])
                finally:
                    release.set()
                self.assertEqual(reading.result(timeout=4)['state'], 'ready')
        self.assertTrue(cache_directory.exists())
        self.assertFalse(local_directory.exists())
        self.assertEqual(self.service.load(project['id']), project)
        self.assertTrue(source.is_file())

    def test_concurrent_slow_status_requests_share_one_scan_and_return_without_waiting_for_nas(self):
        paths = [self.video(index) for index in (0, 1)]
        opened = self.service.open_files([str(path) for path in paths])
        project = self.service.load(opened['id'])
        for video in project['videos']:
            self.complete_cache(project, video['id'])
        entered, release = threading.Event(), threading.Event()
        original_spec = self.service.preview_spec
        checks = []
        # Map a saved UNC cache location to local media at the filesystem boundary.
        # No actual network files are opened, and the project's database stays unchanged.
        network_snapshot = copy.deepcopy(project)
        for source in network_snapshot['_sources'].values():
            source['cache_directory'] = chr(92) * 2 + 'fixture-nas' + chr(92) + project['id']
        def slow_remote_spec(snapshot, video_id, *args, **kwargs):
            if video_id == 'v0002':
                checks.append(video_id)
                entered.set()
                if not release.wait(5):
                    raise AssertionError('Network fixture was not released')
            return original_spec(project, video_id, *args, **kwargs)
        with patch.object(self.service, 'load', return_value=network_snapshot), \
             patch.object(self.service, 'preview_spec', side_effect=slow_remote_spec):
            with ThreadPoolExecutor(max_workers=6) as pool:
                initial = pool.submit(self.service.preparation_status, project['id'])
                try:
                    self.assertTrue(entered.wait(2))
                    initial_status = initial.result(timeout=1)
                    self.assertEqual(initial_status['project_id'], project['id'])
                    polls = [pool.submit(self.service.preparation_status, project['id']) for _ in range(5)]
                    for poll in polls:
                        status = poll.result(timeout=1)
                        self.assertEqual(status['project_id'], project['id'])
                        self.assertEqual(status['total'], 2)
                        self.assertNotEqual(status['state'], 'ready')
                    self.assertEqual(checks, ['v0002'])
                    self.assertFalse(release.is_set())
                finally:
                    release.set()
                    initial.result(timeout=4)
                    self.wait_for_preparation(project['id'])
            ready = self.service.preparation_status(project['id'])
            self.assertEqual((ready['state'], ready['ready']), ('ready', 2))
            self.assertEqual(checks, ['v0002'], 'Polling needlessly restarted a completed remote scan')

    def test_local_cleanup_during_slow_nas_scan_does_not_recreate_local_playback(self):
        source = self.video()
        opened = self.service.open_files([str(source)])
        project = self.service.load(opened['id'])
        spec = self.complete_cache(project)
        cache_directory = spec.target.parent.parent
        local_directory = self.local_playback(project)
        entered, release, finished = threading.Event(), threading.Event(), threading.Event()
        original_spec = self.service.preview_spec
        original_scan = self.service._scan_preparation_status
        def paused_spec(snapshot, video_id, *args, **kwargs):
            entered.set()
            if not release.wait(5):
                raise AssertionError('Network fixture was not released')
            return original_spec(snapshot, video_id, *args, **kwargs)
        def tracked_scan(snapshot):
            try:
                return original_scan(snapshot)
            finally:
                finished.set()
        with patch.object(self.service, 'preview_spec', side_effect=paused_spec), \
             patch.object(self.service, '_scan_preparation_status', side_effect=tracked_scan):
            with ThreadPoolExecutor(max_workers=2) as pool:
                scan = pool.submit(self.service.preparation_status, project['id'])
                try:
                    self.assertTrue(entered.wait(2))
                    deletion = pool.submit(self.service.clear_local_previews, project['id'], project['revision'])
                    self.assertEqual(deletion.result(timeout=1)['cleared'], project['id'])
                    self.assertTrue(cache_directory.exists())
                    self.assertFalse(local_directory.exists())
                finally:
                    release.set()
                    scan.result(timeout=4)
                    self.assertTrue(finished.wait(4))
        self.assertTrue(cache_directory.exists(), 'Local cleanup removed source-adjacent caches')
        self.assertFalse(local_directory.exists(), 'A NAS status scan recreated cleared local playback')
        self.assertTrue(source.is_file())
        self.assertEqual(self.service.load(project['id']), project)

    def test_slow_cached_thumbnail_read_does_not_serialize_preview_status_or_draft_save(self):
        paths = [self.video(index) for index in (0, 1)]
        opened = self.service.open_files([str(path) for path in paths])
        project = self.service.load(opened['id'])
        for video in project['videos']:
            self.complete_cache(project, video['id'])
        entered, release = threading.Event(), threading.Event()
        original_cached = self.service.cached_thumbnail
        def slow_thumbnail(spec):
            if spec.path == paths[1]:
                entered.set()
                if not release.wait(5):
                    raise AssertionError('Network fixture was not released')
            return original_cached(spec)
        annotations = empty_annotations()
        annotations['habit'] = [{'id': 'while-thumbnail-loads', 'label': '喝水', 'kind': 'point',
                                 'start_ms': 500, 'end_ms': 500}]
        with patch.object(self.service, 'cached_thumbnail', side_effect=slow_thumbnail):
            with ThreadPoolExecutor(max_workers=4) as pool:
                thumbnail = pool.submit(self.service.media, project['id'], 'v0002', thumbnail=True)
                try:
                    self.assertTrue(entered.wait(2))
                    preview = pool.submit(self.service.preview_status, project['id'], 'v0001')
                    preparation = pool.submit(self.service.preparation_status, project['id'])
                    draft = pool.submit(self.service.update_draft, project['id'], annotations, project['revision'])
                    self.assertEqual(preview.result(timeout=1)['state'], 'ready')
                    self.assertEqual(preparation.result(timeout=1)['project_id'], project['id'])
                    self.assertEqual(draft.result(timeout=1)['annotations']['habit'], annotations['habit'])
                    self.assertFalse(release.is_set())
                finally:
                    release.set()
                    thumbnail.result(timeout=4)
                    self.wait_for_preparation(project['id'])

    def test_local_cleanup_during_nas_thumbnail_lookup_preserves_completed_source_assets(self):
        for stale_cached_path in (False, True):
            with self.subTest(stale_cached_path=stale_cached_path):
                source = self.video(folder='cached-hit' if stale_cached_path else 'cached-miss')
                opened = self.service.open_files([str(source)])
                project = self.service.load(opened['id'])
                spec = self.complete_cache(project)
                cache_directory = spec.target.parent.parent
                local_directory = self.local_playback(project)
                entered, release = threading.Event(), threading.Event()
                original_cached = self.service.cached_thumbnail
                def delayed_lookup(current):
                    cached = original_cached(current) if stale_cached_path else None
                    entered.set()
                    if not release.wait(5):
                        raise AssertionError('Network fixture was not released')
                    return cached if stale_cached_path else original_cached(current)
                with patch.object(self.service, 'cached_thumbnail', side_effect=delayed_lookup), \
                     patch.object(self.service, 'render_thumbnail') as render:
                    with ThreadPoolExecutor(max_workers=2) as pool:
                        reading = pool.submit(self.service.media, project['id'], 'v0001', thumbnail=True)
                        try:
                            self.assertTrue(entered.wait(2))
                            deletion = pool.submit(self.service.clear_local_previews, project['id'], project['revision'])
                            self.assertEqual(deletion.result(timeout=1)['cleared'], project['id'])
                        finally:
                            release.set()
                        self.assertEqual(reading.result(timeout=4), spec.thumbnail_target)
                        self.assertTrue(spec.thumbnail_target.exists())
                    render.assert_not_called()
                self.assertTrue(cache_directory.exists())
                self.assertFalse(local_directory.exists())
                self.assertEqual(self.service.load(project['id']), project)
                self.assertTrue(source.is_file())

    def test_public_source_project_skips_remote_path_resolution_but_legacy_still_needs_relink(self):
        source = self.video()
        opened = self.service.open_files([str(source)])
        project = self.service.load(opened['id'])
        with patch.object(self.service, 'is_managed_copy', side_effect=AssertionError('Unexpected NAS path resolution')):
            public = self.service.public(project)
        self.assertFalse(public['needs_source_relink'])
        self.assertEqual(public['cache_mode'], 'source')
        legacy, _, _ = self.legacy_project(source, uploaded=True)
        original_check = self.service.is_managed_copy
        with patch.object(self.service, 'is_managed_copy', wraps=original_check) as ownership:
            self.assertTrue(self.service.public(legacy)['needs_source_relink'])
            ownership.assert_called_once()

    def test_local_cleanup_preserves_nas_caches_project_drafts_results_and_other_projects(self):
        source = self.video()
        a = self.service.open_files([str(source)])
        b = self.service.open_files([str(source)])
        self.assertNotEqual(a['id'], b['id'])
        pa, pb = self.service.load(a['id']), self.service.load(b['id'])
        sa, sb = self.complete_cache(pa), self.complete_cache(pb)
        local_a, local_b = [self.service.storage.directory(p['id']) for p in (a, b)]
        for directory in (local_a, local_b):
            for relative in ('playback/compact.mp4', 'playback/fast20.mp4', 'playback/cover.jpg',
                             'playback/sheet-00000.jpg', 'playback/manifest.json',
                             'playback/video.assets.json', 'results/scene.json', 'preparation.json'):
                target = directory / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(b'project-local-cache-or-result')
        exported = source.parent / 'scene.json'
        exported.write_bytes(b'separately-saved-annotations')
        original_bytes = source.read_bytes()
        self.service.clear_local_previews(a['id'], a['revision'])
        self.assertEqual(sa.target.read_bytes(), b'completed-normal-preview')
        self.assertFalse((local_a / 'playback').exists())
        self.assertEqual((local_a / 'results/scene.json').read_bytes(), b'project-local-cache-or-result')
        self.assertEqual((local_a / 'preparation.json').read_bytes(), b'project-local-cache-or-result')
        self.assertTrue((local_b / 'playback/compact.mp4').exists())
        self.assertEqual(sb.target.read_bytes(), b'completed-normal-preview')
        self.assertEqual(source.read_bytes(), original_bytes)
        self.assertEqual(exported.read_bytes(), b'separately-saved-annotations')
        self.assertEqual(self.service.load(a['id']), pa)
        self.assertEqual(self.service.load(b['id'])['id'], b['id'])

    def test_old_permanent_delete_is_disabled_without_touching_any_cache_or_project(self):
        source = self.video()
        opened = self.service.open_files([str(source)])
        project = self.service.load(opened['id'])
        spec = self.complete_cache(project)
        owned = spec.target.parent.parent
        local = self.local_playback(project)
        with patch.object(self.service.source_cache, 'remove') as remove, \
             patch.object(self.service.sessions, 'invalidate') as cancel:
            with self.assertRaises(HTTPException) as failed:
                self.service.delete_project(project['id'], project['revision'])
            remove.assert_not_called()
            cancel.assert_not_called()
        self.assertEqual(failed.exception.status_code, 409)
        self.assertIn('永久删除已停用', failed.exception.detail)
        self.assertEqual(json.loads((owned / MARKER).read_text(encoding='utf-8')), self.service.source_cache.owner(project['id']))
        self.assertEqual(self.service.load(project['id']), project)
        self.assertTrue(local.exists())
        self.assertTrue(spec.target.exists())
        self.assertTrue(source.exists())

    def test_busy_local_playback_can_be_retried_without_touching_source_cache(self):
        source = self.video()
        opened = self.service.open_files([str(source)])
        project = self.service.load(opened['id'])
        spec = self.complete_cache(project)
        owned = spec.target.parent.parent
        local = self.local_playback(project)
        from backend import service as service_module
        original_rmtree = service_module.shutil.rmtree
        def fail_owned_removal(path, *args, **kwargs):
            if Path(path) == local:
                raise PermissionError('fixture: directory open')
            return original_rmtree(path, *args, **kwargs)
        with patch.object(service_module.shutil, 'rmtree', fail_owned_removal):
            with self.assertRaises(HTTPException) as failed:
                self.service.clear_local_previews(project['id'], project['revision'])
        self.assertEqual(failed.exception.status_code, 409)
        self.assertEqual(json.loads((owned / MARKER).read_text(encoding='utf-8')), self.service.source_cache.owner(project['id']))
        self.assertEqual(self.service.load(project['id']), project)
        self.service.clear_local_previews(project['id'], project['revision'])
        self.assertFalse(local.exists())
        self.assertTrue(spec.target.exists())
        self.assertTrue(source.exists())

    def test_offline_nas_does_not_block_local_cleanup_or_touch_source_caches(self):
        sources = [self.video(0, 'card-a'), self.video(1, 'card-b')]
        opened = self.service.open_files([str(path) for path in sources])
        project = self.service.load(opened['id'])
        specs = [self.complete_cache(project, video['id']) for video in project['videos']]
        local = self.service.storage.directory(project['id']) / 'playback'
        local.mkdir(parents=True, exist_ok=True)
        (local / 'compact.mp4').write_bytes(b'local compact cache')
        original_stat = Path.stat
        def offline(path, *args, **kwargs):
            if any(path == source.parent or path.is_relative_to(source.parent) for source in sources):
                raise AssertionError('Local cleanup must not inspect the offline NAS')
            return original_stat(path, *args, **kwargs)
        with patch.object(Path, 'stat', offline), \
             patch.object(self.service.sessions, 'invalidate', wraps=self.service.sessions.invalidate) as cancel:
            self.service.clear_local_previews(project['id'], project['revision'])
            cancel.assert_called_once_with(project['id'])
        self.assertEqual(self.service.load(project['id']), project)
        for spec in specs:
            self.assertTrue(spec.target.exists())
        self.assertFalse(local.exists())
        self.assertTrue(all(path.exists() for path in sources))

    def test_local_writer_cancel_timeout_keeps_both_caches_and_draft_for_cleanup_retry(self):
        source = self.video()
        opened = self.service.open_files([str(source)])
        project = self.service.load(opened['id'])
        spec = self.complete_cache(project)
        local = self.service.storage.directory(project['id']) / 'playback'
        local.mkdir(parents=True, exist_ok=True)
        (local / 'compact.mp4').write_bytes(b'local compact cache')
        with patch.object(self.service.sessions, 'invalidate', side_effect=HTTPException(409, 'still stopping')):
            with self.assertRaises(HTTPException):
                self.service.clear_local_previews(project['id'], project['revision'])
        self.assertEqual(self.service.load(project['id']), project)
        self.assertTrue(spec.target.exists())
        self.assertTrue(local.exists())
        self.service.clear_local_previews(project['id'], project['revision'])
        self.assertTrue(spec.target.parent.parent.exists())
        self.assertFalse(local.exists())
        self.assertTrue(source.exists())

    def test_tampered_nas_owner_marker_is_preserved_and_does_not_block_local_cleanup(self):
        source = self.video()
        opened = self.service.open_files([str(source)])
        project = self.service.load(opened['id'])
        spec = self.complete_cache(project)
        marker = spec.target.parent.parent / MARKER
        marker.write_text(json.dumps({'project_id': uuid.uuid4().hex}), encoding='utf-8')
        saved = marker.read_bytes()
        local = self.local_playback(project)
        self.service.clear_local_previews(project['id'], project['revision'])
        self.assertEqual(self.service.load(project['id']), project)
        self.assertEqual(marker.read_bytes(), saved)
        self.assertFalse(local.exists())
        self.assertTrue(spec.target.exists())
        self.assertTrue(source.exists())

    def test_linked_nas_cache_directory_is_not_followed_or_removed_by_local_cleanup(self):
        source = self.video()
        opened = self.service.open_files([str(source)])
        project = self.service.load(opened['id'])
        spec = self.complete_cache(project)
        local = self.local_playback(project)
        from backend import source_cache
        check = source_cache.is_reparse
        with patch.object(source_cache, 'is_reparse', lambda path: path == spec.target.parent.parent or check(path)), \
             patch.object(self.service.source_cache, 'validate', side_effect=AssertionError('Unexpected NAS cache access')):
            self.service.clear_local_previews(project['id'], project['revision'])
        self.assertFalse(local.exists())
        self.assertTrue(spec.target.is_file())
        self.assertTrue(source.is_file())
        self.assertFalse(self.service.load(project['id']).get('_deleting', False))

    def test_stored_nas_cache_path_cannot_redirect_local_cleanup_to_an_unmanaged_directory(self):
        source = self.video()
        opened = self.service.open_files([str(source)])
        project = self.service.load(opened['id'])
        victim = self.root / 'personal-files'
        victim.mkdir()
        personal = victim / 'keep.txt'
        personal.write_text('keep me', encoding='utf-8')
        project['_sources']['v0001']['cache_directory'] = str(victim)
        self.service.save(project)
        local = self.local_playback(project)
        self.service.clear_local_previews(project['id'], project['revision'])
        self.assertFalse(local.exists())
        self.assertEqual(self.service.load(project['id']), project)
        self.assertEqual(personal.read_text(encoding='utf-8'), 'keep me')
        self.assertTrue(source.is_file())

    def test_local_cleanup_preserves_unlinked_legacy_upload_and_annotations(self):
        source = self.video()
        project, uploaded, preview = self.legacy_project(source, uploaded=True)
        local = self.local_playback(project)
        contents = uploaded.read_bytes()
        with patch.object(self.service.previews, 'cancel_project', side_effect=AssertionError('Unexpected NAS cancellation')):
            self.service.clear_local_previews(project['id'], project['revision'])
        self.assertFalse(local.exists())
        self.assertEqual(uploaded.read_bytes(), contents)
        self.assertTrue(preview.target.exists())
        self.assertEqual(self.service.load(project['id']), project)
        self.assertTrue(source.exists())

    def test_restart_does_not_resolve_other_sources_without_pending_duplicates(self):
        first = self.service.open_files([str(self.video(0, 'first'))])
        other = self.service.open_files([str(self.video(1, 'second'))])
        before = [self.service.load(item['id']) for item in (first, other)]
        watched = {item['path'] for project in before for item in project['_sources'].values()}
        resolve = Path.resolve

        def reject_source_resolution(path, *args, **kwargs):
            if str(path) in watched:
                raise AssertionError('Startup must not resolve remote video paths without pending duplicates')
            return resolve(path, *args, **kwargs)

        with patch.object(Path, 'resolve', reject_source_resolution):
            self.service.migrate_legacy_projects()
        for previous in before:
            current = self.service.load(previous['id'])
            for field in ('annotations', 'revision', 'videos', '_sources'):
                self.assertEqual(current[field], previous[field])

    def test_existing_path_project_migrates_caches_without_retiming_or_rerendering(self):
        source = self.video()
        before, _, old = self.legacy_project(source)
        self.service.migrate_legacy_projects()
        after = self.service.load(before['id'])
        for field in ('id', 'annotations', 'videos', 'revision', 'source_fingerprint', 'draft_dirty'):
            self.assertEqual(after[field], before[field])
        current = self.service.preview_spec(after, 'v0001')
        self.assertEqual(current.key, old.key)
        self.assertEqual(current.target.parent, source.parent / CACHE_FOLDER / before['id'] / 'preview')
        self.assertFalse(old.target.exists())
        self.assert_reusable(self.service, after)
        self.service.migrate_legacy_projects()
        self.assertEqual(self.service.load(before['id']), after)

    def test_old_upload_stays_playable_until_verified_relink_then_reuses_cache(self):
        source = self.video()
        before, copy_path, old = self.legacy_project(source, uploaded=True)
        self.service.migrate_legacy_projects()
        unlinked = self.service.public(self.service.load(before['id']))
        self.assertEqual(unlinked['cache_mode'], 'legacy')
        self.assertTrue(unlinked['needs_source_relink'])
        self.assertTrue(copy_path.exists())
        self.assert_reusable(self.service, before)
        result = self.service.relink_sources(before['id'], str(source.parent), before['revision'])
        after = self.service.load(before['id'])
        self.assertEqual(result['relinked_count'], 1)
        self.assertEqual(result['project']['cache_mode'], 'source')
        self.assertFalse(result['project']['needs_source_relink'])
        for field in ('id', 'annotations', 'videos', 'source_fingerprint', 'draft_dirty'):
            self.assertEqual(after[field], before[field])
        self.assertEqual(Path(after['_sources']['v0001']['path']), source)
        self.assertFalse(copy_path.exists())
        self.assertFalse(old.target.exists())
        self.assertEqual(self.service.preview_spec(after, 'v0001').key, old.key)
        self.assert_reusable(self.service, after)

    def test_relink_keeps_uploaded_copy_while_another_saved_project_still_references_it(self):
        source = self.video()
        before, copy_path, old = self.legacy_project(source, uploaded=True)
        other = self.service.create([copy_path], 'shared-legacy-source', None)
        self.service.save(other)
        result = self.service.relink_sources(before['id'], str(source.parent), before['revision'])
        self.assertEqual(result['removed_copies'], 0)
        self.assertTrue(result['warnings'])
        self.assertTrue(copy_path.exists())
        self.assertTrue(old.target.exists())
        other_path, _ = self.service.media_source(self.service.load(other['id']), 'v0001')
        self.assertEqual(other_path.read_bytes(), source.read_bytes())
        self.service.relink_sources(other['id'], str(source.parent), other['revision'])
        retried = self.service.relink_sources(before['id'], str(source.parent), before['revision'])
        self.assertEqual(retried['removed_copies'], 1)
        self.assertFalse(copy_path.exists())
        self.assert_reusable(self.service, self.service.load(before['id']))

    def test_relinked_fpv_exports_restore_fresh_import_with_new_mtimes_and_reordered_video_ids(self):
        for supplemented in (False, True):
            with self.subTest(supplemented=supplemented):
                folder = ('supplemented' if supplemented else 'single') + '/FPV'
                last = self.video(2 if supplemented else 0, folder)
                os.utime(last, ns=(1_600_000_000_000_000_000, 1_600_000_000_000_000_000))
                before, copy_path, _ = self.legacy_project(last, uploaded=True)
                self.assertNotEqual(last.stat().st_mtime_ns, copy_path.stat().st_mtime_ns)
                if supplemented:
                    earlier = [self.video(index, folder) for index in (0, 1)]
                    self.service.supplement(before['id'], earlier, before['revision'])
                    before = self.service.load(before['id'])
                    self.assertEqual([video['id'] for video in before['videos']],
                                     ['v0002', 'v0003', 'v0001'])
                    self.assertEqual(before['annotations']['habit'][0]['start_ms'], 2250)
                linked = self.service.relink_sources(before['id'], str(last.parent), before['revision'])['project']
                stored = self.service.load(before['id'])
                self.assertEqual(stored['source_fingerprint'], before['source_fingerprint'])
                self.assertTrue(all(not video['relative_path'].startswith('FPV/') for video in stored['videos']))
                linked = self.service.update_draft(before['id'], complete_annotations(linked), linked['revision'])
                self.service.writeback(before['id'])
                export = last.parent.parent / 'timeline' / FILENAMES['habit']
                document = json.loads(export.read_bytes())
                self.assertEqual(document['timebase']['source_fingerprint'], before['source_fingerprint'])
                self.assertEqual(len(document['timebase']['source_fingerprint_aliases']), 1)
                saved_bytes = export.read_bytes()
                self.assertTrue(last.is_file())
                self.assertEqual(export.read_bytes(), saved_bytes)
                restorer = self.fresh_service('restored-' + str(supplemented))
                restored = restorer.open_path(str(last.parent))
                self.assertEqual(restored['annotations'], linked['annotations'])
                self.assertEqual(restored['recording_start'], linked['recording_start'])
                self.assertEqual([(video['name'], video['start_ms'], video['end_ms']) for video in restored['videos']],
                                 [(video['name'], video['start_ms'], video['end_ms']) for video in linked['videos']])
                self.assertEqual(restored['source_fingerprint'], document['timebase']['source_fingerprint_aliases'][0])
                self.assertTrue(all(video['relative_path'].startswith('FPV/') for video in restored['videos']))
                if supplemented:
                    self.assertEqual([video['id'] for video in restored['videos']], ['v0001', 'v0002', 'v0003'])
                changed_reader = self.fresh_service('changed-source-' + str(supplemented))
                stamp = last.stat()
                os.utime(last, ns=(stamp.st_atime_ns, stamp.st_mtime_ns + 1_000_000_000))
                with self.assertRaises(HTTPException) as changed_mtime:
                    changed_reader.open_path(str(last.parent))
                self.assertEqual(changed_mtime.exception.status_code, 409)
                os.utime(last, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
                contents = last.read_bytes()
                last.write_bytes(b'X' + contents[1:])
                os.utime(last, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
                with self.assertRaises(HTTPException) as changed_content:
                    changed_reader.open_path(str(last.parent))
                self.assertEqual(changed_content.exception.status_code, 409)
                self.assertEqual(changed_reader.projects(), [])
                self.assertEqual(restorer.load(restored['id'])['annotations'], linked['annotations'])

    def test_relink_checks_full_content_even_when_name_size_and_sample_hash_match(self):
        payload = b'A' * 210000
        source = self.video(payload=payload)
        before, copy_path, old = self.legacy_project(source, uploaded=True)
        wrong = bytearray(payload)
        wrong[100000] = ord('B')
        source.write_bytes(wrong)
        self.assertEqual(self.service.source_stamp(source)['sample_sha256'],
                         self.service.source_stamp(copy_path)['sample_sha256'])
        with self.assertRaises(HTTPException):
            self.service.relink_sources(before['id'], str(source.parent), before['revision'])
        self.assertEqual(self.service.load(before['id']), before)
        self.assertEqual(copy_path.read_bytes(), payload)
        self.assertTrue(old.target.exists())

    def test_relink_conflicts_and_commit_failure_never_remove_the_only_local_copy(self):
        source = self.video()
        before, copy_path, old = self.legacy_project(source, uploaded=True)
        with self.assertRaises(HTTPException) as caught:
            self.service.relink_sources(before['id'], str(source.parent), before['revision'] - 1)
        self.assertEqual(caught.exception.status_code, 409)
        self.assertEqual(self.service.load(before['id']), before)
        with patch.object(self.service, 'save', side_effect=OSError('simulated database commit failure')):
            with self.assertRaises((OSError, HTTPException)):
                self.service.relink_sources(before['id'], str(source.parent), before['revision'])
        self.assertEqual(self.service.load(before['id']), before)
        self.assertEqual(copy_path.read_bytes(), source.read_bytes())
        self.assertTrue(old.target.exists())
        self.assert_reusable(self.service, before)
        original_unlink = Path.unlink
        def locked_duplicate(path, *args, **kwargs):
            if path == copy_path:
                raise PermissionError('simulated file held open after commit')
            return original_unlink(path, *args, **kwargs)
        with patch.object(Path, 'unlink', locked_duplicate):
            pending = self.service.relink_sources(before['id'], str(source.parent), before['revision'])
        self.assertEqual(pending['removed_copies'], 0)
        self.assertTrue(pending['warnings'])
        self.assertTrue(copy_path.exists())
        self.assertTrue(old.target.exists())
        committed = self.service.load(before['id'])
        self.assertEqual(Path(committed['_sources']['v0001']['path']), source)
        self.assertEqual(committed['annotations'], before['annotations'])
        self.assert_reusable(self.service, committed)
        retried = self.service.relink_sources(before['id'], str(source.parent), committed['revision'])
        self.assertEqual(retried['removed_copies'], 1)
        self.assertFalse(copy_path.exists())
        self.assertFalse(old.target.exists())
        self.assertEqual(self.service.load(before['id'])['_relink_cleanup'], [])
        self.assert_reusable(self.service, self.service.load(before['id']))


if __name__ == '__main__':
    unittest.main()
