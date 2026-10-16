"""Collection task geometry/persistence with explicit probe fixtures, no real storage."""
import csv
import io
import hashlib
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException

from backend.service import ProjectService
from backend.tasks.storage import ProjectMedia, FileArtifacts
from backend.tasks.store import TaskStore, TaskError
from backend.tasks.workspace import Workspaces


class CollectionTaskTests(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1]/'.tmp')
        self.addCleanup(temporary.cleanup)
        self.root=Path(temporary.name).resolve()
        self.media_service=ProjectService(self.root/'media',enable_remote=False)
        self.media_service.probe=lambda path:{'duration_ms':1000,'media_start_seconds':0,'codec':'h264'}
        self.addCleanup(self.media_service.sessions.close)
        self.addCleanup(self.media_service.previews.close)
        source=self.root/'1015test';source.mkdir()
        for second in ('00','02'):
            (source/f'A00001_202610151200{second}_000{int(second)//2}.mp4').write_bytes(b'explicit-not-real-media-fixture')
        self.project=self.media_service.open_path(str(source))
        self.admin={'id':'admin','role':'admin','can_upload':True}
        self.a={'id':'a','role':'annotator'};self.r={'id':'r','role':'annotator'}
        self.store=TaskStore(self.root/'tasks',FileArtifacts(self.root/'artifacts'))
        for actor,role in ((self.a,'annotator'),(self.r,'reviewer')):
            self.store.set_roles(self.admin,actor['id'],[role])
        self.media=ProjectMedia(self.media_service,'nas',lambda actor,ident:actor['id']=='admin')
        self.records=[]
        for source in self.media.catalog(self.admin):
            body={'id':source['id'].replace('/','-'),'collector_id':'测试采集人','date':'2026-10-15','scene':'室内',
                  'duration_ms':source['duration_ms'],'quality':'passed','source':source['source']}
            self.records.append(self.media.reference(self.admin,body))
        self.store.register_collection(self.admin,self.records)
        self.rule=self.store.publish_rule(self.admin,{'name':'完整采集','task_type':'posture',
            'axes':[{'id':'posture','name':'姿势','mode':'state','labels':['站']}]})
        self.body={'collection_id':self.project['id'],'storage_backend':'nas','rule_id':self.rule['id'],
                   'annotator_ids':['a'],'reviewer_id':'r','deadline':'2026-10-26'}
        self.workspace=Workspaces(self.root/'workspace',self.store,self.media)
        self.addCleanup(self.workspace.close)

    def create(self,body=None):
        body=body or self.body
        records,key=self.store.task_materials(self.admin,body)
        layout=self.media.task_collection(records) if key else None
        return self.store.create_task(self.admin,body,collection_layout=layout)

    def mutate(self,method,actor,task,**body):
        return getattr(self.store,method)(actor,task['id'],{'expected_revision':task['revision'],**body})

    def test_one_collection_one_task_complete_timeline_and_material_snapshot(self):
        task=self.create()
        self.assertEqual(len(self.store.list_tasks(self.admin)),1)
        self.assertEqual(len(task['video_ids']),2)
        self.assertEqual(task['video']['duration_ms'],3000)
        project=self.workspace.project(self.a,task['id'])
        self.assertEqual(project['duration_ms'],3000)
        self.assertEqual([(v['start_ms'],v['end_ms']) for v in project['videos']],[(0,1000),(2000,3000)])
        self.assertEqual(project['gaps'],[{'start_ms':1000,'end_ms':2000}])
        self.assertEqual(project['collection_folder_name'],'1015test')
        with self.assertRaises(TaskError):self.create()
        legacy_request={k:v for k,v in self.body.items() if k not in ('collection_id','storage_backend')}
        legacy_request['video_id']=self.records[1]['id']
        with self.assertRaises(TaskError):self.create(legacy_request)
        with self.assertRaises(HTTPException):self.media.task_collection(self.records[:1])

    def test_custom_rule_workspace_keeps_fixed_labels_separate_and_repairs_previous_cache(self):
        axes = [
            {'id':'posture','name':'姿势','mode':'state','labels':['站']},
            {'id':'food','name':'饮食','mode':'state','labels':['喝水']},
            {'id':'scene','name':'场景','mode':'state','labels':['室内']},
        ]
        rule=self.store.publish_rule(self.admin,{'name':'自定义规则','task_type':'custom','axes':axes})
        task=self.create({**self.body,'rule_id':rule['id']})
        project=self.workspace.public(self.a,task['id'])
        self.assertEqual(project['fixed_tracks'],['scene','posture'])
        self.assertEqual(set(project['track_labels']),{'scene','posture','category','habit'})
        self.assertEqual(project['custom_tracks'][0]['labels'],['喝水'])
        segment={'id':'drink','label':'喝水','start_ms':0,'end_ms':500,'lane':0,'kind':'interval'}
        draft={'annotations':{'food':[segment]}}
        self.mutate('save_draft',self.a,task,**draft)
        stored=self.workspace.service.load(project['id'])
        stored['track_labels']['food']=['喝水']
        stored['fixed_tracks']=['posture','scene']
        self.workspace.service.save(stored)
        reopened=self.workspace.public(self.a,task['id'])
        self.assertEqual(reopened['annotations']['food'],[segment])
        self.assertEqual(reopened['fixed_tracks'],['scene','posture'])
        self.assertNotIn('food',reopened['track_labels'])
        self.assertEqual(reopened['custom_tracks'][0]['labels'],['喝水'])
        self.assertNotIn('food',self.workspace.service.load(project['id'])['track_labels'])

    def test_all_members_quality_is_required_and_later_revocation_blocks_submission(self):
        self.store.set_quality(self.admin,self.records[1]['id'],'failed')
        with self.assertRaises(TaskError):self.create()
        self.assertEqual(self.store.list_tasks(self.admin),[])
        self.store.set_quality(self.admin,self.records[1]['id'],'passed')
        task=self.create()
        self.store.set_quality(self.admin,self.records[1]['id'],'unknown')
        with self.assertRaises(TaskError):self.mutate('submit',self.a,task,annotations={})

    def test_global_draft_submit_review_accept_and_delivery_cover_later_video(self):
        task=self.create()
        annotations={'posture':[{'id':'later','label':'站','start_ms':2000,'end_ms':3000,'lane':0}]}
        task=self.mutate('save_draft',self.a,task,annotations=annotations)
        opened=self.workspace.public(self.a,task['id'])
        self.assertEqual(opened['annotations']['posture'],annotations['posture'])
        for start,end in ((900,2100),(1100,1500)):
            with self.subTest(start=start),self.assertRaises(TaskError):
                self.mutate('submit',self.a,task,annotations={'posture':[{'id':'bad','label':'站','start_ms':start,'end_ms':end}]})
        task=self.mutate('submit',self.a,task,annotations=annotations)
        self.assertEqual(task['status'],'submitted')
        readonly=self.workspace.project(self.a,task['id'])
        self.assertTrue(readonly['task_readonly']);self.assertEqual(len(readonly['videos']),2)
        task=self.mutate('start_review',self.r,task)
        task=self.mutate('review',self.r,task,action='pass',notes=[{'axis':'posture','start_ms':2000,'end_ms':3000,'reason':'后段确认'}])
        task=self.mutate('accept',self.admin,task)
        self.assertEqual(task['status'],'accepted')
        result=self.store.deliver(self.admin,{'task_ids':[task['id']],'request_id':'collection-delivery',
                                 'expected_revisions':{task['id']:task['revision']}})
        rows=list(csv.DictReader(io.StringIO(self.store.artifacts.get(result['artifact_key']).decode('utf-8-sig'))))
        self.assertEqual(rows[0]['collection_id'],self.project['id'])
        self.assertEqual(rows[0]['start_ms'],'2000')
        self.assertEqual(len(rows[0]['video_ids'].split('|')),2)

    def test_source_timing_change_cannot_silently_shift_existing_task(self):
        task=self.create()
        project=self.media_service.load(self.project['id'])
        project['videos'][1].update(start_ms=2100,end_ms=3100)
        self.media_service.save(project)
        with self.assertRaises(HTTPException):self.workspace.project(self.a,task['id'])

    def test_real_s3_collection_downloads_two_complete_bundles_without_server_generation(self):
        from backend.mock_s3 import MockUploadManager
        from backend.r2_service import R2ProjectService
        from backend.r2_routes import R2Media
        from backend.test_r2_integration import MemoryObjects
        from backend.upload_playback import build
        from backend.service import document_bytes
        from backend.session_cache import SessionCache
        from backend.storyboards import StoryboardCache
        tools=Path(__file__).resolve().parents[1]/'.tools/ffmpeg/bin'
        ffmpeg=tools/'ffmpeg';ffprobe=tools/'ffprobe'
        if not ffmpeg.is_file():ffmpeg=Path(shutil.which('ffmpeg') or '/missing')
        if not ffprobe.is_file():ffprobe=Path(shutil.which('ffprobe') or '/missing')
        if not ffmpeg.is_file() or not ffprobe.is_file():self.skipTest('FFmpeg required')
        uploader=ProjectService(self.root/'uploader',enable_remote=False)
        self.addCleanup(uploader.sessions.close);self.addCleanup(uploader.previews.close)
        uploader.tool=lambda name:ffmpeg if name=='ffmpeg' else ffprobe
        uploader.desktop_cache.enabled=False
        prepared=self.root/'prepared';(prepared/'FPV').mkdir(parents=True)
        manifest={'prefix':'daily/20261015/1015test','uploaded_by':'admin','files':[]}
        for i,second in enumerate(('00','02')):
            name=f'A00001_202610151200{second}_000{i}'
            source=self.root/(name+'.avi')
            subprocess.run([str(ffmpeg),'-v','error','-f','lavfi','-i','color=size=64x48:rate=5','-t','1','-c:v','mpeg4',str(source)],check=True,capture_output=True)
            normal=prepared/'FPV'/(name+'.mp4')
            MockUploadManager._compress_video(uploader,source,normal,lambda value:None)
            manifest['files'].append({'name':normal.name,'key':'FPV/'+normal.name,'duration_ms':1000,
                                     'size':normal.stat().st_size,'sha256':hashlib.sha256(normal.read_bytes()).hexdigest()})
        build(uploader,prepared,manifest,lambda detail:None)
        provider=MemoryObjects()
        for item in manifest['files']+manifest['cache_files']:
            provider.put(manifest['prefix']+'/'+item['key'],(prepared/item['key']).read_bytes(),create=True)
        provider.put(manifest['prefix']+'/manifest.json',document_bytes(manifest),create=True)
        public=R2ProjectService(self.root/'s3',provider);public.tool=uploader.tool
        self.addCleanup(public.sessions.close);self.addCleanup(public.previews.close)
        opened=public.open_capture(manifest['prefix'],actor=self.admin)
        media=R2Media(public)
        store=TaskStore(self.root/'s3-tasks',FileArtifacts(self.root/'s3-artifacts'))
        for actor,role in ((self.a,'annotator'),(self.r,'reviewer')):store.set_roles(self.admin,actor['id'],[role])
        rule=store.publish_rule(self.admin,{'name':'S3 collection','task_type':'posture','axes':self.rule['axes']})
        records=[media.reference(self.admin,{'id':source['id'].replace('/','-'),'collector_id':'测试采集人','date':'2026-10-15','scene':'室内',
                    'quality':'passed','duration_ms':source['duration_ms'],'source':source['source']}) for source in media.catalog(self.admin)]
        store.register_collection(self.admin,records)
        task=store.create_task(self.admin,{**self.body,'collection_id':opened['id'],'storage_backend':'s3','rule_id':rule['id']},collection_layout=media.task_collection(records))
        workspace=Workspaces(self.root/'s3-workspace',store,media);self.addCleanup(workspace.close)
        project=workspace.project(self.a,task['id'])
        with patch.object(SessionCache,'_render_compact',side_effect=AssertionError('server encoded')), \
             patch('backend.session_cache.render_local_fast',side_effect=AssertionError('server fast')), \
             patch('backend.session_cache.render_local_thumbnail',side_effect=AssertionError('server jpg')), \
             patch.object(StoryboardCache,'render',side_effect=AssertionError('server sheets')):
            workspace.service.start_session(project['id'])
            deadline=time.monotonic()+15
            while time.monotonic()<deadline:
                status=workspace.service.sessions.status(project['id'])
                if status['state'] in ('ready','partial','error'):break
                time.sleep(.02)
            self.assertEqual(status['state'],'ready',status)
            playback=public.media_manifest(workspace.service,workspace.service.sessions.manifest(project['id']))
            self.assertEqual(len(playback['videos']),2)
            self.assertEqual(len(playback['assets']),8)
            self.assertTrue(all('/'+manifest['prefix']+'/' in asset['url'] for asset in playback['assets']))
