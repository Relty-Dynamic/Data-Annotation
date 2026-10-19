"""Cache manifest validation uses isolated metadata, never production objects."""
import copy
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from fastapi import HTTPException

from backend.session_cache import PROFILE
from backend.upload_playback import reference, validate, direct_ready, direct_manifest


class PlaybackManifestTests(unittest.TestCase):
    def test_status_and_confirmation_validate_without_signing_downloads(self):
        from backend.r2_service import R2ProjectService
        from backend.r2_routes import R2Media
        from backend.tasks.workspace import Workspaces
        manifest = self.manifest()
        normal = manifest['files'][0]
        normal.update(size=100, sha256='c' * 64)
        video = {'id': 'v0001', 'name': normal['name'], 'duration_ms': normal['duration_ms']}
        project = {'id': 'a' * 32, 'videos': [video], 'collection_folder_name': 'test',
                   '_sources': {'v0001': {'r2_playback': reference('daily/test', manifest, normal['name'])}}}
        provider = SimpleNamespace(signed_url=Mock(side_effect=lambda key: 'https://objects.example/' + key))
        service = object.__new__(R2ProjectService)
        service.load = lambda ident: project
        service.provider = provider
        service.allowed = lambda *args: True
        workspace = object.__new__(Workspaces)
        workspace.media = R2Media(service)
        self.assertEqual(service.session_status(project['id'])['state'], 'ready')
        self.assertEqual(service.browser_ready(project['id']), {'released': False})
        self.assertEqual(workspace.session_status(project)['state'], 'ready')
        self.assertTrue(workspace.direct_ready(project))
        provider.signed_url.assert_not_called()
        self.assertEqual(len(direct_manifest(provider, project)['assets']), 5)
        self.assertEqual(provider.signed_url.call_count, 5)
        capture = project['_sources']['v0001']['r2_playback']['manifest']
        capture['cache_files'].pop()
        for operation in (lambda: direct_ready(project), lambda: service.session_status(project['id']),
                          lambda: service.browser_ready(project['id']), lambda: workspace.session_status(project)):
            with self.assertRaises(HTTPException):
                operation()
        self.assertEqual(provider.signed_url.call_count, 5)

    def manifest(self):
        name = 'A00001_20261015120000_0001.mp4'
        base = {'size': 100, 'sha256': 'a' * 64, 'video_name': name}
        return {'cache_profile': PROFILE, 'files': [{'name': name, 'key': 'FPV/' + name, 'duration_ms': 101000}],
                'cache_files': [{**base, 'kind': kind, 'index': index,
                    'key': 'FPV/cache/' + 'b' * 64 + '/' + filename} for kind,index,filename in
                    [('fast',None,'fast.mp4'),('thumbnail',None,'thumbnail.jpg'),
                     ('sheet',0,'sheet-00000.jpg'),('sheet',1,'sheet-00001.jpg')]]}

    def test_complete_profile_and_each_second_sheet_count(self):
        manifest = self.manifest()
        self.assertEqual(len(validate(manifest)),4)
        self.assertEqual(validate({'files':manifest['files']}),[])
        manifest['cache_files'].pop()
        with self.assertRaises(HTTPException):validate(manifest)

    def test_unsafe_paths_duplicates_unknown_clip_and_invalid_indices_rejected(self):
        for field,value in [('key','FPV/cache/../secret.jpg'),('key','/tmp/secret.jpg'),
                            ('key','FPV/cache/'+'b'*64+'/thumbnail.mp4'),
                            ('video_name','other.mp4'),('index',True),('size',0),('sha256','bad')]:
            manifest=self.manifest()
            manifest['cache_files'][-1][field]=value
            with self.subTest(field=field,value=value),self.assertRaises(HTTPException):validate(manifest)
        manifest=self.manifest()
        manifest['cache_files'].append(copy.deepcopy(manifest['cache_files'][0]))
        with self.assertRaises(HTTPException):validate(manifest)

    def test_reference_keeps_only_selected_clip_and_no_host_paths(self):
        manifest=self.manifest()
        other='A00001_20261015121000_0002.mp4'
        manifest['files'].append({'name':other,'key':'FPV/'+other,'duration_ms':1000})
        manifest['cache_files'].append({**manifest['cache_files'][0],'video_name':other})
        record=reference('daily/20261015/1015test',manifest,manifest['files'][0]['name'])
        self.assertEqual(len(record['manifest']['files']),1)
        self.assertEqual(len(record['manifest']['cache_files']),4)
        self.assertEqual(len(validate(record['manifest'])),4)
