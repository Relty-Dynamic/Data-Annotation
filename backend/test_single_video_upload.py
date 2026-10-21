"""Version negotiation and complete retry-cache isolation for single-video uploads."""
import copy
import hashlib
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from fastapi import HTTPException

from backend.playback_contract import LEGACY_PROFILE, SINGLE_VIDEO_PROFILE
from backend.r2_folder import FolderUploadManager
from backend.r2_upload import RemoteCaptureTarget
from backend.test_r2_integration import USER, PREFIX, NAME
from backend import upload_pipeline_cache as recovery


class SingleVideoUploadTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1] / '.tmp')
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.desktop = self.root / 'desktop'
        self.desktop.mkdir()
        self.service = SimpleNamespace(local=self.root / 'local',
            desktop_cache=SimpleNamespace(enabled=True, root=self.desktop))
        self.record = dict(name=NAME.replace('.mp4', '.avi'), output='FPV/' + NAME)
        self.digest = hashlib.sha256(b'raw fixture').hexdigest()

    def bundle(self, profile):
        files, caches, retained = [], [], {}
        kinds = ['normal', 'thumbnail', 'sheet']
        if profile == LEGACY_PROFILE:
            kinds.append('fast')
        for kind in kinds:
            key = 'FPV/' + NAME if kind == 'normal' else 'FPV/cache/' + 'a'*64 + '/' + kind + ('.mp4' if kind == 'fast' else '.jpg')
            path = self.desktop / (profile + '-' + kind)
            path.write_bytes(('isolated retry fixture ' + profile + kind).encode())
            item = dict(key=key, size=path.stat().st_size, sha256=hashlib.sha256(path.read_bytes()).hexdigest())
            retained[key] = path
            if kind == 'normal':
                files.append(dict(item, duration_ms=1000))
            else:
                caches.append(dict(item, video_name=NAME, kind=kind, index=0 if kind == 'sheet' else None))
        return dict(files=files, cache_files=caches, cache_profile=profile), retained

    def test_old_backend_stops_before_compression_prediction_or_transfer(self):
        source = self.root / self.record['name']
        source.write_bytes(b'raw fixture')
        target = RemoteCaptureTarget(None, self.service)
        target.request = Mock(return_value={'capabilities':['s3-upload-pipeline-v1']})
        manager = FolderUploadManager(target, self.root/'staging')
        selection = manager.select_folder(USER['id'], self.root)
        job = manager.start(USER, 'test', None, None, selection['selection_id'],
            prediction=dict(prediction='generate', chest_device='chest', thigh_device='thigh'))
        with patch.object(manager, '_compress_video') as compress, \
             patch('backend.prediction_prepare.prepare_prediction') as prediction, \
             patch.object(target, 'publish_ready') as publish:
            with self.assertRaises(HTTPException) as error:
                manager.finish(job['id'], USER['id'], self.service, lambda _: True)
            self.assertEqual(error.exception.status_code, 409)
            self.assertIn('先更新平台', error.exception.detail)
            compress.assert_not_called()
            prediction.assert_not_called()
            publish.assert_not_called()
        target.request.assert_called_once_with('GET', '/api/health')
        self.assertEqual(source.read_bytes(), b'raw fixture')

    def test_old_and_new_retry_indexes_remain_separate_and_reusable(self):
        old, old_retained = self.bundle(LEGACY_PROFILE)
        new, new_retained = self.bundle(SINGLE_VIDEO_PROFILE)
        for bundle, retained in ((old, old_retained), (new, new_retained)):
            recovery.save(self.service, PREFIX, self.record, self.digest, bundle, retained)
        for profile, bundle in ((LEGACY_PROFILE, old), (SINGLE_VIDEO_PROFILE, new)):
            prepared = self.root / profile
            loaded = recovery.load(self.service, PREFIX, self.record, self.digest, prepared, profile=profile)
            self.assertEqual(loaded, bundle)
            for item in bundle['files'] + bundle['cache_files']:
                self.assertEqual(hashlib.sha256((prepared/item['key']).read_bytes()).hexdigest(), item['sha256'])
            self.assertEqual(len(list(prepared.rglob('*.mp4'))), 2 if profile == LEGACY_PROFILE else 1)
        self.assertTrue(all(path.is_file() for path in old_retained.values()))

    def test_new_retry_never_uses_old_cache_or_incomplete_or_corrupt_new_cache(self):
        old, retained = self.bundle(LEGACY_PROFILE)
        recovery.save(self.service, PREFIX, self.record, self.digest, old, retained)
        prepared = self.root / 'publish'
        self.assertIsNone(recovery.load(self.service, PREFIX, self.record, self.digest, prepared, profile=SINGLE_VIDEO_PROFILE))
        new, retained = self.bundle(SINGLE_VIDEO_PROFILE)
        for invalid in ('missing-sheet', 'extra-fast', 'corrupt-content'):
            with self.subTest(invalid=invalid):
                bundle = copy.deepcopy(new)
                if invalid == 'missing-sheet':
                    bundle['cache_files'].pop()
                elif invalid == 'extra-fast':
                    bundle['cache_files'].append(old['cache_files'][-1])
                all_retained = {**retained, **{item['key']:self.desktop/(LEGACY_PROFILE+'-fast') for item in old['cache_files'] if item['kind']=='fast'}}
                recovery.save(self.service, PREFIX, self.record, self.digest, bundle, all_retained)
                if invalid == 'corrupt-content':
                    retained[new['cache_files'][0]['key']].write_bytes(b'damaged')
                self.assertIsNone(recovery.load(self.service, PREFIX, self.record, self.digest, prepared, profile=SINGLE_VIDEO_PROFILE))
                self.assertFalse(prepared.exists())
