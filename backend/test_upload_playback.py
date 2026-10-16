"""Cache manifest validation uses isolated metadata, never production objects."""
import copy
import unittest

from fastapi import HTTPException

from backend.session_cache import PROFILE
from backend.upload_playback import reference, validate


class PlaybackManifestTests(unittest.TestCase):
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
