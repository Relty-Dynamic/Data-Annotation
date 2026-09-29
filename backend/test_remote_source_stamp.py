from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import HTTPException

from backend.service import FILENAMES, ProjectService
from backend.session_cache import SessionCache
from backend.testing_annotations import complete_project

ROOT = Path(__file__).resolve().parents[1]
MEDIA = {'duration_ms': 1000, 'media_start_seconds': 0, 'format_start_seconds': 0,
         'codec': 'mjpeg', 'pixel_format': 'yuv420p', 'audio_codecs': []}


class RemoteSourceStampTests(unittest.TestCase):
    def setUp(self):
        (ROOT / '.tmp').mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix='remote-stamp-', dir=ROOT / '.tmp')
        self.root = Path(self.temp.name)
        self.source_dir = self.root / 'collection'
        self.source_dir.mkdir()
        self.source = self.source_dir / 'A09999_20260917120000_0001.avi'
        self.source.write_bytes(b'original-video-for-stamp')
        self.service = ProjectService(self.root / 'app', enable_remote=False)
        self.local_stamp = self.service.source_stamp(self.source)
        self.remote_stamp = {**self.local_stamp, 'mtime_ns': self.local_stamp['mtime_ns'] + 100}
        self.inspections = []

    def tearDown(self):
        self.service.sessions.close()
        self.service.previews.close()
        self.temp.cleanup()

    def enable_remote(self):
        def inspect(path):
            self.inspections.append(path)
            return {'stamp': copy.deepcopy(self.remote_stamp), 'media': copy.deepcopy(MEDIA)}
        self.service.remote = SimpleNamespace(map_path=lambda path: '/mnt/nas/homes/' + path.name,
                                              inspect=inspect)

    def forbid_original_open(self):
        original = Path.open
        source = self.source
        def open_file(path, *args, **kwargs):
            if path == source:
                raise AssertionError('The desktop must not read original video bytes')
            return original(path, *args, **kwargs)
        return patch.object(Path, 'open', open_file)

    def test_remote_hash_keeps_windows_stamp_without_local_content_read(self):
        self.enable_remote()
        before = copy.deepcopy(self.remote_stamp)
        with self.forbid_original_open():
            actual = self.service.source_stamp(self.source)
        self.assertEqual(actual, self.local_stamp)
        self.assertEqual(self.remote_stamp, before)
        self.assertEqual(self.inspections, [self.source])

    def test_rejects_size_or_at_least_one_millisecond_mismatch(self):
        self.enable_remote()
        for difference in ({'size': self.local_stamp['size'] + 1},
                           {'mtime_ns': self.local_stamp['mtime_ns'] + 1_000_000},
                           {'mtime_ns': self.local_stamp['mtime_ns'] - 1_000_000}):
            self.remote_stamp = {**self.local_stamp, **difference}
            with self.forbid_original_open(), self.assertRaises(HTTPException) as raised:
                self.service.source_stamp(self.source)
            self.assertEqual(raised.exception.status_code, 409)

    def test_accepts_both_submillisecond_rounding_directions(self):
        self.enable_remote()
        for delta in (-999_999, 0, 999_999):
            self.remote_stamp = {**self.local_stamp, 'mtime_ns': self.local_stamp['mtime_ns'] + delta}
            with self.forbid_original_open():
                actual = self.service.source_stamp(self.source)
            self.assertEqual(actual, self.local_stamp)

    def test_inaccessible_windows_share_uses_server_verified_stamp_only(self):
        self.enable_remote()
        original_stat = Path.stat
        source = self.source
        def stat(path, *args, **kwargs):
            if path == source:
                raise OSError('Windows SMB unavailable')
            return original_stat(path, *args, **kwargs)
        with patch.object(Path, 'stat', stat), self.forbid_original_open():
            actual = self.service.source_stamp(self.source)
        self.assertEqual(actual, self.remote_stamp)
        self.assertNotEqual(actual, self.local_stamp)

    def test_unmapped_sources_keep_existing_local_hash_behavior(self):
        self.service.remote = SimpleNamespace(map_path=lambda path: None,
            inspect=lambda path: self.fail('An unmapped source must not use the worker'))
        self.assertEqual(self.service.source_stamp(self.source), self.local_stamp)

    def test_existing_fingerprint_cache_identity_and_json_remain_compatible(self):
        with patch.object(self.service, 'probe', return_value=MEDIA):
            existing = complete_project(self.service.create([self.source], 'fixture', self.source_dir))
        save_id, documents = self.service.export_documents(existing)
        timeline = self.source_dir / 'timeline'
        timeline.mkdir()
        for axis, document in documents.items():
            (timeline / FILENAMES[axis]).write_bytes(document)
        self.enable_remote()
        with self.forbid_original_open():
            reopened = self.service.create([self.source], 'fixture', self.source_dir)
            external, hashes = self.service.read_external(reopened)
            self.service.verify_sources(existing)
        self.assertEqual(reopened['source_fingerprint'], existing['source_fingerprint'])
        self.assertEqual(reopened['_sources']['v0001']['stamp'], existing['_sources']['v0001']['stamp'])
        self.assertEqual(SessionCache._source_key(reopened['_sources']['v0001']),
                         SessionCache._source_key(existing['_sources']['v0001']))
        self.assertEqual(external, existing['annotations'])
        self.assertTrue(all(hashes.values()))
        for axis in FILENAMES:
            document = json.loads((timeline / FILENAMES[axis]).read_bytes())
            self.assertEqual(document['save_id'], save_id)


if __name__ == '__main__':
    unittest.main()
