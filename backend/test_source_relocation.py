"""Reopen legacy root-level recordings moved into their collection's FPV folder."""
import json
import os
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException

from backend import test_service
from backend.service import FILENAMES, ProjectService


class SourceRelocationTests(unittest.TestCase):
    tearDown = test_service.PersistenceTests.tearDown
    annotated_project = test_service.PersistenceTests.annotated_project

    def setUp(self):
        test_service.PersistenceTests.setUp(self)
        for path in self.fpv.iterdir():
            path.rename(self.source / path.name)
        self.fpv.rmdir()

    def move_to_fpv(self):
        self.fpv.mkdir()
        for path in self.source.glob('*.mp4'):
            path.rename(self.fpv / path.name)

    def test_reopen_preserves_draft_and_cache_identity_and_backs_up_old_paths(self):
        project = self.annotated_project()
        project = self.service.add_custom_track(project['id'], '环境', 'state', ['安静'], project['revision'])
        old = self.service.load(project['id'])
        cache_keys = {video['id']: self.service.preview_spec(old, video['id'], check_source=False).key
                      for video in old['videos']}
        self.move_to_fpv()
        opened = self.service.open_path(str(self.source), claim_mode='keep')
        stored = self.service.load(project['id'])
        self.assertEqual(opened['id'], old['id'])
        for key in ('annotations', 'custom_tracks', 'fixed_tracks', 'track_labels', 'draft_dirty',
                    'source_fingerprint', 'name', 'last_writeback', '_external_hashes'):
            self.assertEqual(stored.get(key), old.get(key), key)
        self.assertEqual(stored['revision'], old['revision'] + 1)
        for video in stored['videos']:
            source = stored['_sources'][video['id']]
            self.assertEqual(Path(source['path']), self.fpv / video['name'])
            self.assertEqual(video['relative_path'], 'FPV/' + video['name'])
            self.assertNotIn('cache_directory', source)
            self.assertEqual(self.service.preview_spec(stored, video['id'], check_source=False).key, cache_keys[video['id']])
            self.assertEqual(self.service.media_source(stored, video['id'])[0], self.fpv / video['name'])
        recovery = self.service.storage.directory(old['id']) / 'recoveries' / (opened['recovery_id'] + '.json')
        self.assertEqual(json.loads(recovery.read_bytes()), old)
        with self.assertRaises(HTTPException):
            self.service.update_draft(old['id'], old['annotations'], old['revision'])
        reopened = self.service.open_path(str(self.fpv), claim_mode='keep')
        self.assertEqual(reopened['revision'], stored['revision'])
        self.assertEqual(reopened['annotations'], old['annotations'])
        self.service.writeback(old['id'])
        fresh = ProjectService(self.root / 'fresh-app')
        try:
            imported = fresh.open_path(str(self.source))
            self.assertEqual(imported['annotations'], old['annotations'])
        finally:
            fresh.sessions.close()
            fresh.previews.close()

    def test_continue_partial_old_json_preserves_original_fingerprint_and_annotations(self):
        project = self.annotated_project()
        self.service.writeback(project['id'])
        old = self.service.load(project['id'])
        for axis in ('category', 'habit'):
            (self.source / 'timeline' / FILENAMES[axis]).unlink()
        originals = {axis: (self.source / 'timeline' / FILENAMES[axis]).read_bytes() for axis in ('scene', 'posture')}
        self.move_to_fpv()
        inspected = self.service.inspect_source_claim(str(self.source))
        opened = self.service.open_path(str(self.fpv), claim_mode='continue', claim_hashes=inspected['hashes'])
        self.assertEqual(opened['source_fingerprint'], old['source_fingerprint'])
        self.assertEqual(opened['annotations']['scene'], old['annotations']['scene'])
        self.assertEqual(opened['annotations']['posture'], old['annotations']['posture'])
        self.assertEqual(opened['annotations']['category'], [])
        self.assertEqual(opened['annotations']['habit'], [])
        self.assertEqual({axis: (self.source / 'timeline' / FILENAMES[axis]).read_bytes() for axis in originals}, originals)

    def test_changed_content_with_same_size_and_mtime_is_rejected_without_saving(self):
        project = self.annotated_project()
        old = self.service.load(project['id'])
        self.move_to_fpv()
        path = self.fpv / old['videos'][0]['name']
        stamp = path.stat()
        raw = path.read_bytes()
        path.write_bytes(bytes([raw[0] ^ 1]) + raw[1:])
        os.utime(path, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
        with self.assertRaises(HTTPException) as error:
            self.service.open_path(str(self.source))
        self.assertEqual(error.exception.status_code, 409)
        self.assertEqual(self.service.load(project['id']), old)
        self.assertFalse((self.service.storage.directory(project['id']) / 'recoveries').exists())

    def test_changed_duration_is_rejected_without_saving(self):
        project = self.annotated_project()
        old = self.service.load(project['id'])
        self.move_to_fpv()
        probe = self.service.probe
        def changed(path):
            result = probe(path)
            if path.name == old['videos'][-1]['name']:
                result['duration_ms'] += 1
            return result
        with patch.object(self.service, 'probe', side_effect=changed), self.assertRaises(HTTPException) as error:
            self.service.open_path(str(self.source))
        self.assertEqual(error.exception.status_code, 409)
        self.assertEqual(self.service.load(project['id']), old)

    def test_missing_or_added_video_is_not_a_path_relocation(self):
        project = self.annotated_project()
        old = self.service.load(project['id'])
        self.move_to_fpv()
        missing = self.fpv / old['videos'][-1]['name']
        raw = missing.read_bytes()
        missing.unlink()
        with self.assertRaises(HTTPException) as error:
            self.service.open_path(str(self.source))
        self.assertEqual(error.exception.status_code, 409)
        self.assertEqual(self.service.load(project['id']), old)
        missing.write_bytes(raw)
        (self.fpv / 'clip11_20260914000003.mp4').write_bytes(b'new video')
        with self.assertRaises(HTTPException) as error:
            self.service.open_path(str(self.source))
        self.assertEqual(error.exception.status_code, 409)
        self.assertEqual(self.service.load(project['id']), old)

    def test_relocation_allows_changed_mtime_and_leaves_old_copy_untouched(self):
        project = self.annotated_project()
        old = self.service.load(project['id'])
        self.move_to_fpv()
        video = old['videos'][0]
        copy_path = self.source / video['name']
        raw = (self.fpv / video['name']).read_bytes()
        copy_path.write_bytes(raw)
        for path in self.fpv.glob('*.mp4'):
            stamp = path.stat()
            os.utime(path, ns=(stamp.st_atime_ns, stamp.st_mtime_ns + 5_000_000_000))
        opened = self.service.open_path(str(self.source), claim_mode='keep')
        stored = self.service.load(project['id'])
        self.assertEqual(opened['annotations'], old['annotations'])
        self.assertEqual(copy_path.read_bytes(), raw)
        for video in stored['videos']:
            path, source = self.service.media_source(stored, video['id'])
            self.assertEqual(source['stamp']['mtime_ns'], path.stat().st_mtime_ns)
        self.assertEqual(self.service.open_path(str(self.source))['revision'], stored['revision'])

    def test_failed_json_validation_does_not_commit_path_changes(self):
        project = self.annotated_project()
        self.service.writeback(project['id'])
        old = self.service.load(project['id'])
        self.move_to_fpv()
        path = self.source / 'timeline' / FILENAMES['posture']
        doc = json.loads(path.read_bytes())
        doc['save_id'] = 'another-batch'
        path.write_text(json.dumps(doc), encoding='utf-8')
        with self.assertRaises(HTTPException) as error:
            self.service.open_path(str(self.source), claim_mode='continue')
        self.assertEqual(error.exception.status_code, 409)
        self.assertEqual(self.service.load(project['id']), old)
        self.assertFalse((self.service.storage.directory(project['id']) / 'recoveries').exists())
