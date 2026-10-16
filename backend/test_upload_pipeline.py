"""Pipeline concurrency, final-publication barrier and metadata-only registration."""
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from fastapi import HTTPException

from backend.r2_folder import FolderUploadManager, scan_collection, attachment_manifest
from backend.r2_upload import RemoteCaptureTarget
from backend.r2_uploads import R2Uploads
from backend.r2_service import R2ProjectService
from backend.r2_routes import R2Media
from backend.service import ProjectService
from backend.upload_playback import PROFILE
from backend.test_r2_integration import MemoryObjects, USER, PREFIX, NAME
import hashlib


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name).resolve()
        collection=self.root/'collection';(collection/'FPV').mkdir(parents=True)
        self.names=['A00001_20261015120000_0001.avi','A00001_20261015121000_0002.avi']
        for name in self.names:(collection/'FPV'/name).write_bytes(b'original-'+name.encode())
        self.started=threading.Event();self.first=threading.Event();self.second=threading.Event()
        self.stages=[];self.final=[]
        outer=self
        class Target(RemoteCaptureTarget):
            def capture_exists(inner,prefix):return False
            def publish_ready(inner,prefix,prepared,manifest,service,*,prepare=False,completed_keys=None):
                if inner.attachments:manifest['attachments']=attachment_manifest(inner.attachments)
                if prepare:
                    outer.stages.append(manifest)
                    outer.first.set()
                else:
                    outer.final.append(manifest)
                    outer.assertTrue(outer.second.is_set())
                    outer.assertEqual(len(outer.stages),2)
                    outer.assertTrue(any(a['key']=='prediction/posture.json' for a in manifest.get('attachments',[])))
                    outer.assertEqual(len(completed_keys),2)
        self.target=Target(SimpleNamespace(),SimpleNamespace())
        self.target.pipeline_enabled=True
        self.manager=FolderUploadManager(self.target,self.root/'staging')
        self.service=SimpleNamespace(local=self.root/'local',desktop_cache=SimpleNamespace(enabled=False),probe=lambda p:{'duration_ms':1000})
        self.service.local.mkdir()
        self.selection=self.manager.select_folder(USER['id'],collection)
        self.target.attachments=self.selection['attachments']

    def start(self):
        return self.manager.start(USER,'test',None,None,self.selection['selection_id'],prediction=dict(
            prediction='generate',chest_device='chest',thigh_device='thigh'))

    def prediction(self,target,service,root,selection,roles,**kwargs):
        self.started.set()
        self.assertTrue(self.second.wait(3),'compression must proceed without waiting for prediction')
        kwargs['cancel']()
        directory=Path(root)/'prediction';directory.mkdir()
        (directory/'posture.json').write_text('{"segments":[]}')
        return scan_collection(root),dict(sha256='a'*64,segments=0,needs_alignment_review=False)

    def compress(self,service,source,destination,progress):
        self.assertTrue(self.started.wait(3))
        if source.name==self.names[1]:
            self.assertTrue(self.first.wait(3),'first clip must upload before all clips are compressed')
            self.second.set()
        destination.write_bytes(b'compressed-'+source.name.encode());progress(100)

    def build(self,service,prepared,manifest,progress):
        manifest.update(cache_profile=PROFILE,cache_files=[])
        return {}

    def test_prediction_compression_upload_overlap_and_publish_once(self):
        job=self.start()
        with patch('backend.prediction_prepare.prepare_prediction',side_effect=self.prediction),patch.object(self.manager,'_compress_video',side_effect=self.compress),patch('backend.upload_pipeline.build',side_effect=self.build):
            result=self.manager.finish(job['id'],USER['id'],self.service,lambda user:True)
        self.assertEqual(result['state'],'ready')
        self.assertEqual(len(self.final),1)
        self.assertFalse((self.root/'staging'/job['id']).exists())

    def test_stage_failure_never_publishes_complete_capture(self):
        job=self.manager.start(USER,'test',None,None,self.selection['selection_id'])
        self.started.set()
        def fail(*args,**kwargs):
            self.first.set()
            raise HTTPException(503,'fixture upload interrupted')
        with patch.object(self.manager,'_compress_video',side_effect=self.compress),patch('backend.upload_pipeline.build',side_effect=self.build),patch.object(type(self.target),'publish_ready',side_effect=fail):
            with self.assertRaises(HTTPException):self.manager.finish(job['id'],USER['id'],self.service,lambda user:True)
        self.assertFalse(self.final)
        self.assertEqual(self.manager.status(job['id'],USER['id'])['state'],'error')


class MetadataIntakeTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name).resolve();self.provider=MemoryObjects()
        self.service=R2ProjectService(self.root/'origin',self.provider)
        self.addCleanup(self.service.sessions.close);self.addCleanup(self.service.previews.close)
        self.uploads=R2Uploads(self.service.local,self.provider,self.service)
        self.data=b'isolated-compressed-video-fixture'
        self.manifest=dict(prefix=PREFIX,collector_name='Collector',uploader_name='Worker',files=[dict(
            key='FPV/'+NAME,size=len(self.data),sha256=hashlib.sha256(self.data).hexdigest(),duration_ms=1000)])

    def uploaded(self,manifest=None):
        job=self.uploads.start(USER,manifest or self.manifest)
        current=self.uploads.load(job['id'],USER)
        for item in current['manifest']['files']:
            key=PREFIX+'/'+item['key']
            if key not in self.provider.objects:self.provider.put(key,self.data,create=True)
        return job

    def test_staging_not_discoverable_final_reuses_proof_and_publishes_real_metadata(self):
        job=self.uploaded()
        info=dict(codec='h264',duration_ms=1000,media_start_seconds=0,pixel_format='yuv420p',audio_codecs=[])
        with patch.object(self.service,'probe',return_value=info):
            self.assertEqual(self.uploads.finish(job['id'],USER,prepare=True)['state'],'verified')
        self.assertIsNone(self.provider.optional_json(PREFIX+'/manifest.json')[0])
        final=self.uploaded({**self.manifest,'note':'final collection'})
        with patch.object(self.provider,'download',side_effect=AssertionError('already verified')),patch.object(self.service,'probe',side_effect=AssertionError('already probed')):
            self.assertEqual(self.uploads.finish(final['id'],USER)['state'],'ready')
        published=self.provider.optional_json(PREFIX+'/manifest.json')[0]
        self.assertEqual(published['media_metadata'][NAME],info)

    def test_register_and_freeze_tasks_on_fresh_server_without_video_download_or_probe(self):
        manifest={**self.manifest,'cache_profile':PROFILE,'cache_files':[]}
        for kind,index,suffix in [('fast',None,'fast.mp4'),('thumbnail',None,'thumbnail.jpg'),('sheet',0,'sheet-00000.jpg')]:
            manifest['cache_files'].append(dict(key='FPV/cache/'+'a'*64+'/'+suffix,video_name=NAME,kind=kind,index=index,
                size=len(self.data),sha256=hashlib.sha256(self.data).hexdigest()))
        job=self.uploaded(manifest)
        stored=self.uploads.load(job['id'],USER)
        for item in stored['manifest']['cache_files']:self.provider.put(PREFIX+'/'+item['key'],self.data,create=True)
        with patch.object(self.service,'probe',side_effect=lambda path:dict(codec='h264',duration_ms=50 if path.name.endswith('fast.mp4') else 1000)),patch.object(self.service,'valid_thumbnail',return_value=True):
            self.uploads.finish(job['id'],USER)
        fresh=R2ProjectService(self.root/'fresh',self.provider)
        self.addCleanup(fresh.sessions.close);self.addCleanup(fresh.previews.close)
        with patch.object(self.provider,'download',side_effect=AssertionError('registration must not download media')),patch.object(self.provider,'head',side_effect=AssertionError('published metadata is enough')),patch.object(fresh,'probe',side_effect=AssertionError('registration must not probe media')):
            project=fresh.open_capture(PREFIX,actor=USER)
            media=R2Media(fresh)
            catalog=media.catalog(USER)
            record=media.reference(USER,dict(id='video-fixture',source=catalog[0]['source'],duration_ms=1000))
            collection=media.task_collection([record])
            self.assertEqual(collection['duration_ms'],1000)
            self.assertEqual(fresh.start_session(project['id'])['state'],'ready')
            self.assertTrue(fresh.session_manifest(project['id'])['assets'])
            reopened=fresh.open_capture(PREFIX,actor=USER)
            self.assertEqual(reopened['id'],project['id'])
        self.assertFalse(list(fresh.local.rglob('*.mp4')))
        self.assertTrue(all(s.get('storage')=='s3' for s in fresh.load(project['id'])['_sources'].values()))

    def test_changed_validation_contract_cannot_reuse_staged_proof(self):
        job=self.uploaded()
        with patch.object(self.service,'probe',return_value=dict(codec='h264',duration_ms=1000)):
            self.uploads.finish(job['id'],USER,prepare=True)
        manifest={**self.manifest,'files':[{**self.manifest['files'][0],'duration_ms':1500}]}
        final=self.uploaded(manifest)
        with patch.object(self.service,'probe',return_value=dict(codec='h264',duration_ms=1000)) as probe:
            with self.assertRaises(HTTPException):self.uploads.finish(final['id'],USER)
        probe.assert_called()
        self.assertIsNone(self.provider.optional_json(PREFIX+'/manifest.json')[0])

    def test_client_cannot_publish_forged_media_metadata(self):
        job=self.uploaded({**self.manifest,'media_metadata':{NAME:dict(codec='h264',duration_ms=999)}})
        with patch.object(self.service,'probe',return_value=dict(codec='h264',duration_ms=1000)):
            self.uploads.finish(job['id'],USER)
        result=self.provider.optional_json(PREFIX+'/manifest.json')[0]
        self.assertEqual(result['media_metadata'][NAME]['duration_ms'],1000)
