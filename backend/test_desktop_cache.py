"""Desktop media placement, migration, corruption, and shared-reference cleanup."""
import copy
import ctypes
import json
import os
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from .desktop_cache import desktop_directory, safe_name, collection_name, sha256_file
from . import test_playback_session as fixtures
from .service import ProjectService

class DesktopCacheTests(unittest.TestCase):
    tearDown = fixtures.PlaybackSessionTests.tearDown
    probe = staticmethod(fixtures.PlaybackSessionTests.probe)
    encode = fixtures.PlaybackSessionTests.encode
    wait_ready = fixtures.PlaybackSessionTests.wait_ready
    prepare = fixtures.PlaybackSessionTests.prepare
    asset = fixtures.PlaybackSessionTests.asset

    def setUp(self):
        fixtures.PlaybackSessionTests.setUp(self)
        self.desktop = self.root / 'Desktop' / 'dataMarkProject'
        self.service.desktop_cache.enabled = True
        self.service.desktop_cache.override = str(self.desktop)

    def test_all_media_live_in_collection_directory_and_have_checksums(self):
        manifest = self.prepare()
        directory = self.desktop / 'card'
        self.assertEqual(manifest['collection_name'], 'card')
        self.assertEqual(manifest['cache_location']['root'], str(self.desktop))
        self.assertEqual(len(manifest['assets']), 8)
        self.assertEqual(len(list(directory.glob('*.mp4'))), 4)
        for record in manifest['assets']:
            path = directory / record['file_name']
            self.assertTrue(path.is_file())
            self.assertEqual(path.stat().st_size, record['size'])
            self.assertEqual(sha256_file(path), record['sha256'])
        self.assertFalse((self.service.storage.directory(self.ident) / 'playback').exists())
        self.assertTrue(list(directory.glob('manifest.*.json')))
        before = copy.deepcopy(self.service.load(self.ident))
        self.service.rename_project(self.ident, 'New display name', before['revision'])
        self.assertEqual(collection_name(self.service.load(self.ident)), 'card')

    def test_normal_nas_selection_downloads_to_desktop_without_nas_changes(self):
        self.service.desktop_cache.enabled = False
        with patch.dict(os.environ, {'DATAMARK_STORAGE_BACKEND': 'nas', 'DATAMARK_SOURCE_ROOT': str(self.root)}):
            self.prepare()
        nas = self.source / '.datamark-cache' / 'ubuntu-playback-v1'
        before = {str(path.relative_to(nas)): path.read_bytes() for path in nas.rglob('*') if path.is_file()}
        local = ProjectService(self.root / 'local-desktop-app')
        local.probe = self.probe
        local.desktop_cache.enabled = True
        local.desktop_cache.override = str(self.desktop)
        self.services.append(local)
        with patch.dict(os.environ, {'DATAMARK_STORAGE_BACKEND': 'local', 'DATAMARK_LOCAL_NAS_ROOT': str(self.root)}):
            opened = local.open_files([str(path) for path in sorted(self.source.glob('*.avi'))])
            with patch('backend.session_cache.run_ffmpeg', side_effect=AssertionError('must reuse NAS cache')):
                local.sessions.start(opened['id'])
                deadline = time.monotonic() + 5
                while local.sessions.status(opened['id'])['state'] == 'running' and time.monotonic() < deadline:
                    time.sleep(.01)
            self.assertEqual(local.sessions.status(opened['id'])['state'], 'ready')
            manifest = local.sessions.manifest(opened['id'])
            self.assertEqual(manifest['collection_name'], 'card')
            for item in manifest['assets']:
                target = self.desktop / 'card' / item['file_name']
                self.assertEqual(sha256_file(target), item['sha256'])
            self.assertTrue(local.sessions.asset(opened['id'], 'normal', 'v0001').is_relative_to(self.desktop / 'card'))
        self.assertEqual(before, {str(path.relative_to(nas)): path.read_bytes() for path in nas.rglob('*') if path.is_file()})

    def test_legacy_playback_migrates_without_new_encoding_or_deleting_old_files(self):
        self.service.desktop_cache.enabled = False
        self.prepare()
        old = self.service.storage.directory(self.ident) / 'playback'
        original = {path.name:path.read_bytes() for path in old.glob('*.mp4')}
        encodes = dict(self.encodes)
        self.service.sessions.invalidate(self.ident)
        self.service.desktop_cache.enabled = True
        manifest = self.prepare()
        self.assertEqual(dict(self.encodes), encodes)
        self.assertEqual({path.name:path.read_bytes() for path in old.glob('*.mp4')}, original)
        self.assertTrue(all((self.desktop / 'card' / record['file_name']).exists() for record in manifest['assets']))

    def test_upload_cache_survives_staging_cleanup_and_retry(self):
        import shutil
        staging = self.root / 'upload-staging'
        staging.mkdir()
        source = staging / 'capture.mp4'
        source.write_bytes(b'verified compressed content')
        cache = self.service.desktop_cache
        retained = cache.retain_upload('daily/20261010/1010zengl', 'capture.avi', source)
        self.assertEqual(cache.retain_upload('daily/20261010/1010zengl', 'capture.avi', source), retained)
        shutil.rmtree(staging)
        self.assertEqual(retained.parent, self.desktop / '1010zengl')
        self.assertEqual(retained.read_bytes(), b'verified compressed content')
        refs = list(retained.parent.glob('.datamark-ref.*.json'))
        self.assertEqual(len(refs), 1)
        self.assertTrue(json.loads(refs[0].read_text())['files'][0]['owned'])

    def test_clearing_one_project_preserves_other_references_and_user_files(self):
        manifest = self.prepare()
        directory = self.desktop / 'card'
        user_file = directory / 'personal.mp4'
        user_file.write_bytes(b'user-owned')
        other = copy.deepcopy(self.service.load(self.ident))
        other['id'] = 'b' * 32
        self.service.save(other)
        original_ident = self.ident
        self.ident = other['id']
        self.prepare()
        self.ident = original_ident
        self.service.clear_local_previews(self.ident, self.project['revision'])
        self.assertTrue(all((directory / record['file_name']).exists() for record in manifest['assets']))
        self.service.clear_local_previews(other['id'], other['revision'])
        self.assertFalse(any((directory / record['file_name']).exists() for record in manifest['assets']))
        self.assertEqual(user_file.read_bytes(), b'user-owned')
        self.assertEqual(self.service.load(original_ident)['annotations'], self.project['annotations'])

    def test_corrupt_managed_cache_is_repaired_and_changed_bytes_are_preserved(self):
        manifest = self.prepare()
        record = manifest['assets'][0]
        path = self.desktop / 'card' / record['file_name']
        path.write_bytes(b'changed content')
        self.service.sessions.invalidate(self.ident)
        repaired = self.prepare()
        self.assertEqual(sha256_file(path), record['sha256'])
        backups = list(path.parent.glob('.damaged.*'))
        self.assertTrue(any(file.read_bytes() == b'changed content' for file in backups))
        self.assertEqual(repaired['collection_name'], manifest['collection_name'])

    def test_collection_setting_keeps_annotations_and_reuses_previous_folder(self):
        from fastapi.testclient import TestClient
        self.prepare()
        before = copy.deepcopy(self.service.load(self.ident))
        encodes = dict(self.encodes)
        client = TestClient(self.app, base_url='http://127.0.0.1')
        response = client.put(f'/api/projects/{self.ident}/cache-collection', json={'name': '1010zengl', 'expected_revision': before['revision']})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['annotations'], before['annotations'])
        invalid = client.put(f'/api/projects/{self.ident}/cache-collection', json={'name': '../escape', 'expected_revision': response.json()['revision']})
        self.assertEqual(invalid.status_code, 422)
        client.close()
        self.prepare()
        self.assertEqual(dict(self.encodes), encodes)
        self.assertTrue((self.desktop / '1010zengl').is_dir())
        self.assertTrue((self.desktop / 'card').is_dir())

    def test_matching_personal_file_can_be_reused_but_is_not_owned_or_deleted(self):
        manifest = self.prepare()
        directory = self.desktop / 'card'
        records = manifest['assets']
        ref = directory / self.service.desktop_cache.ref_name(self.ident)
        metadata = json.loads(ref.read_text())
        for item in metadata['files']:
            item['owned'] = False
        ref.write_text(json.dumps(metadata))
        self.service.clear_local_previews(self.ident, self.project['revision'])
        self.assertTrue(all((directory / record['file_name']).exists() for record in records))

    def test_setting_root_stays_project_local_and_survives_new_service(self):
        root = self.root / 'redirected-desktop' / 'dataMarkProject'
        settings = self.service.desktop_cache.configure(str(root))
        self.assertEqual(settings['root'], str(root))
        saved = json.loads((self.app_root / '.local' / 'cache-settings.json').read_text())
        self.assertEqual(saved['root'], str(root))
        self.assertEqual(self.service.desktop_cache.directory(self.project).parent, root)

class DesktopNamingTests(unittest.TestCase):
    def test_windows_reserved_and_traversal_names_are_safe_and_distinct(self):
        for value in ('CON', 'a/b', '..', 'name.', 'name:other'):
            cleaned = safe_name(value)
            self.assertNotIn('/', cleaned)
            self.assertNotIn('\\', cleaned)
            self.assertNotEqual(cleaned, value)
        self.assertNotEqual(safe_name('a/b'), safe_name('a:b'))
        self.assertEqual(safe_name('1010zengl'), '1010zengl')
        self.assertEqual(collection_name({'id':'1','source_dir':r'\\Relty\homes\1010zengl\FPV'}), '1010zengl')

    def test_windows_desktop_uses_known_folder_and_frees_system_memory(self):
        calls=[]
        buffer=ctypes.create_unicode_buffer('C:/Users/test/OneDrive/Desktop')
        def known_folder(guid, flags, token, pointer):
            calls.append('known-folder')
            ctypes.cast(pointer, ctypes.POINTER(ctypes.c_wchar_p))[0] = ctypes.cast(buffer, ctypes.c_wchar_p)
            return 0
        fake=SimpleNamespace(shell32=SimpleNamespace(SHGetKnownFolderPath=known_folder),ole32=SimpleNamespace(CoTaskMemFree=lambda pointer:calls.append('free'),CoInitializeEx=lambda pointer,mode:0,CoUninitialize=lambda:None))
        with patch('backend.desktop_cache.sys.platform','win32'), patch.object(ctypes,'windll',fake,create=True):
            self.assertIn('OneDrive/Desktop', str(desktop_directory()))
        self.assertEqual(calls,['known-folder','free'])
