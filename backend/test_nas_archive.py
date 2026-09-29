from __future__ import annotations

import hashlib
import json
import os
import tempfile
import threading
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException

from backend.nas_archive import NasArchiveCache, sha256
from backend.session_cache import PROFILE


class NasArchiveTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1] / '.tmp',
                                                 prefix='nas-archive-')
        self.root = Path(self.temp.name)
        self.source = self.root / 'FPV' / 'source.avi'
        self.source.parent.mkdir()
        self.source.write_bytes(b'original')
        self.cache = NasArchiveCache(PROFILE, 1)
        self.stop = threading.Event()
        self.stamp = {'size': 8, 'mtime_ns': 1000000000, 'sample_sha256': 'a' * 64}
        self.job = {'id': 'b' * 64, 'path': str(self.source), 'stamp': self.stamp,
                    'media': {'duration_ms': 1000, 'media_start_seconds': 0}}
        self.local = self.root / 'server.zip'
        self.manifest = {'protocol': 1, 'profile': PROFILE, 'source_stamp': self.stamp,
                         'duration_ms': 1000, 'media_start_seconds': 0}
        self.make_archive()

    def tearDown(self):
        self.temp.cleanup()

    def make_archive(self):
        with zipfile.ZipFile(self.local, 'w') as archive:
            archive.writestr('manifest.json', json.dumps(self.manifest))
            archive.writestr('normal.mp4', b'preview' * 256)
        self.job['archive'] = {'size': self.local.stat().st_size, 'sha256': sha256(self.local)}

    def publish(self, verify=None):
        return self.cache.publish(self.job, self.local, self.stop, verify or (lambda: None))

    def assert_no_partial(self):
        self.assertEqual(list(self.source.parent.rglob('*.partial')), [])

    def test_publish_is_readable_checksum_verified_and_preserves_source_and_local(self):
        observed = []
        result = self.publish(lambda: observed.append(self.source.read_bytes()))
        self.assertGreaterEqual(len(observed), 2)
        self.assertTrue(all(item == b'original' for item in observed))
        archive, metadata = self.cache.paths(self.source, self.job['id'])
        self.assertEqual(archive.read_bytes(), self.local.read_bytes())
        self.assertEqual(result['sha256'], hashlib.sha256(archive.read_bytes()).hexdigest())
        self.assertEqual(self.cache.read(self.job), result)
        self.assertEqual(json.loads(metadata.read_bytes())['source_path'], str(self.source))
        self.assertIn('Deleting a desktop project does not delete',
                      (archive.parent / 'README.md').read_text(encoding='utf-8'))
        self.assert_no_partial()

    def test_missing_metadata_corruption_and_wrong_identity_are_cache_misses(self):
        self.assertIsNone(self.cache.read(self.job))
        self.publish()
        archive, metadata = self.cache.paths(self.source, self.job['id'])
        original = metadata.read_bytes()
        for corrupt in (b'broken-json', b'[]', b'null', b'{}'):
            metadata.write_bytes(corrupt)
            self.assertIsNone(self.cache.read(self.job))
        metadata.write_bytes(original)
        record = json.loads(original)
        for field, value in (('profile', 'old'), ('protocol', 999), ('source_path', '/another/source.avi'),
                             ('duration_ms', 2000), ('media_start_seconds', 1)):
            modified = {**record, field: value}
            metadata.write_text(json.dumps(modified), encoding='utf-8')
            self.assertIsNone(self.cache.read(self.job))
        metadata.write_bytes(original)
        archive.write_bytes(archive.read_bytes()[:-1])
        self.assertIsNone(self.cache.read(self.job))

    def test_changed_stamp_and_source_timing_never_publish(self):
        for field, value in (('source_stamp', {**self.stamp, 'sample_sha256': 'c' * 64}),
                             ('duration_ms', 2000), ('media_start_seconds', 4)):
            original = self.manifest[field]
            self.manifest[field] = value
            self.make_archive()
            with self.assertRaises(HTTPException) as caught:
                self.publish()
            self.assertEqual(caught.exception.status_code, 422)
            self.manifest[field] = original
        self.assertFalse((self.source.parent / '.datamark-cache').exists())
        self.assertTrue(self.local.exists())

    def test_source_changes_before_publish_keep_previous_archive_and_remove_partials(self):
        self.publish()
        archive, metadata = self.cache.paths(self.source, self.job['id'])
        previous_archive, previous_record = archive.read_bytes(), metadata.read_bytes()

        def changed():
            raise HTTPException(409, 'source changed')

        with self.assertRaises(HTTPException) as caught:
            self.publish(changed)
        self.assertEqual(caught.exception.status_code, 409)
        self.assertEqual(archive.read_bytes(), previous_archive)
        self.assertEqual(metadata.read_bytes(), previous_record)
        self.assertTrue(self.local.exists())
        self.assert_no_partial()

    def test_failed_atomic_marker_commit_leaves_no_trusted_partial_and_can_retry(self):
        archive, metadata = self.cache.paths(self.source, self.job['id'])
        replace = os.replace

        def fail_marker(source, target):
            if Path(target) == metadata:
                raise OSError('fixture NAS write failed')
            return replace(source, target)

        with patch('backend.nas_archive.os.replace', side_effect=fail_marker), self.assertRaises(OSError):
            self.publish()
        self.assertTrue(archive.exists())
        self.assertFalse(metadata.exists())
        self.assertIsNone(self.cache.read(self.job))
        self.assertTrue(self.local.exists())
        self.assert_no_partial()
        self.publish()
        self.assertIsNotNone(self.cache.read(self.job))

    def test_shutdown_during_copy_cleans_only_its_partial_and_keeps_local(self):
        archive, _ = self.cache.paths(self.source, self.job['id'], create=True)
        other_partial = archive.with_name('other-job.partial')
        other_partial.write_bytes(b'other-active-writer')
        original = self.cache.validate_archive

        def stop_after_local_validation(path, *args):
            result = original(path, *args)
            if path == self.local:
                self.stop.set()
            return result

        with patch.object(self.cache, 'validate_archive', side_effect=stop_after_local_validation):
            with self.assertRaises(HTTPException) as caught:
                self.publish()
        self.assertEqual(caught.exception.status_code, 503)
        self.assertTrue(self.local.exists())
        self.assertEqual(other_partial.read_bytes(), b'other-active-writer')
        self.assertEqual(list(archive.parent.glob('*.partial')), [other_partial])
        self.assertFalse(archive.exists())

    def test_invalid_ids_and_redirected_files_are_refused(self):
        for ident in ('../outside', 'a' * 63, 'A' * 64):
            with self.assertRaises(HTTPException):
                self.cache.paths(self.source, ident, create=True)
        archive, metadata = self.cache.paths(self.source, self.job['id'], create=True)
        original = Path.is_symlink
        for target in (archive.parent.parent, archive.parent, archive, metadata):
            with patch.object(Path, 'is_symlink', lambda path, target=target: path == target or original(path)):
                with self.assertRaises(HTTPException) as caught:
                    self.publish()
                self.assertEqual(caught.exception.status_code, 409)
        self.assertTrue(self.local.exists())
        self.assertFalse(archive.exists())

    def test_junctions_are_refused_even_without_symlink_flag(self):
        if not hasattr(Path, 'is_junction'):
            self.skipTest('Platform does not have junctions')
        cache_root = self.source.parent / '.datamark-cache'
        original = Path.is_junction
        with patch.object(Path, 'is_junction', lambda path: path == cache_root or original(path)):
            with self.assertRaises(HTTPException) as caught:
                self.publish()
        self.assertEqual(caught.exception.status_code, 409)
        self.assertFalse(cache_root.exists())


if __name__ == '__main__':
    unittest.main()
