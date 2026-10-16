"""Real protocol checks with isolated disk and object fixtures; no production I/O."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import httpx
from fastapi import HTTPException

from backend.r2_uploads import R2Uploads
from backend.r2_upload import RemoteCaptureTarget
from backend.upload_transfer import transfer_files
from backend.test_r2_integration import MultipartObjects, USER, OTHER, PREFIX, NAME


class VersionedObjects(MultipartObjects):
    def head(self, key):
        return {**super().head(key), 'ETag': self.objects[key][1]}


class UploadPerformanceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.provider = VersionedObjects()
        self.service = SimpleNamespace(local=self.root, probe=lambda p: {'codec': 'h264', 'duration_ms': 1000})
        self.uploads = R2Uploads(self.root, self.provider, self.service)
        self.data = [b'video-fixture', b'imu-fixture', b'heart-fixture']
        def record(key, data):
            return dict(key=key, size=len(data), sha256=hashlib.sha256(data).hexdigest())
        self.manifest = dict(prefix=PREFIX, collector_name='Collector', uploader_name='Worker',
            files=[dict(record('FPV/'+NAME,self.data[0]),duration_ms=1000)],
            attachments=[record('IMU/123/data.txt',self.data[1]),record('HEART/data.txt',self.data[2])])
        self.job = self.uploads.start(USER, self.manifest)

    def uploaded(self):
        job = self.uploads.load(self.job['id'],USER)
        for item, data in zip(self.uploads.items(job),self.data):
            self.provider.put(PREFIX+'/'+item['key'],data,create=True)
        return job

    def wait_idle(self):
        deadline = time.monotonic()+5
        while self.uploads.status(self.job['id'],USER)['validation_active']:
            if time.monotonic()>deadline:
                self.fail('background validation did not terminate')
            time.sleep(.01)
        return self.uploads.status(self.job['id'],USER)

    def test_small_files_use_one_put_and_no_multipart_creation(self):
        plan = self.uploads.plan(self.job['id'],1,USER,single=True)
        self.assertEqual(plan['mode'],'single')
        self.assertEqual(plan['headers'],{'x-amz-meta-sha256':hashlib.sha256(self.data[1]).hexdigest(),'Content-Type':'application/octet-stream'})
        self.assertEqual(self.uploads.plan(self.job['id'],0,USER,single=True)['headers']['Content-Type'],'video/mp4')
        self.assertFalse(self.provider.uploads)
        # Legacy callers retain resumable multipart plans.
        legacy = self.uploads.plan(self.job['id'],2,USER)
        self.assertIn('parts',legacy)
        for actor in [OTHER,{**USER,'can_upload':False}]:
            with self.assertRaises(HTTPException):self.uploads.plan(self.job['id'],1,actor,single=True)

    def test_planning_files_concurrently_preserves_all_upload_ids(self):
        with ThreadPoolExecutor(max_workers=3) as pool:
            list(pool.map(lambda index:self.uploads.plan(self.job['id'],index,USER), range(3)))
        job=self.uploads.load(self.job['id'],USER)
        self.assertEqual(len({r['upload_id'] for r in job['files']}),3)

    def test_background_verification_is_nonblocking_and_parallel(self):
        self.uploaded()
        started=threading.Barrier(3)
        release=threading.Event()
        original=self.provider.download
        def download(*args):
            started.wait(timeout=3)
            release.wait(timeout=3)
            original(*args)
        try:
            with patch.object(self.provider,'download',side_effect=download):
                response=self.uploads.begin_finish(self.job['id'],USER,lambda:USER)
                self.assertEqual(response['state'],'validating')
                # The other two validation workers must reach the barrier concurrently.
                deadline=time.monotonic()+3
                while started.n_waiting and time.monotonic()<deadline:time.sleep(.005)
                # A second job can still be planned while validation is blocked.
                manifest={**self.manifest,'prefix':PREFIX+'2'}
                second=self.uploads.start(USER,manifest)
                self.assertIn('parts',self.uploads.plan(second['id'],0,USER))
                self.assertIsNone(self.provider.optional_json(PREFIX+'/manifest.json')[0])
                release.set()
                self.assertEqual(self.wait_idle()['state'],'ready')
        finally:release.set()

    def test_permission_revocation_during_validation_prevents_publication(self):
        self.uploaded()
        access=dict(USER)
        original=self.provider.download
        def revoke(*args):
            result=original(*args)
            access['can_upload']=False
            return result
        with patch.object(self.provider,'download',side_effect=revoke):
            self.uploads.begin_finish(self.job['id'],USER,lambda:dict(access))
            state=self.wait_idle()
        self.assertEqual(state['state'],'validation_error')
        self.assertIn('权限',state['error'])
        self.assertIsNone(self.provider.optional_json(PREFIX+'/manifest.json')[0])

    def test_checkpoint_resume_after_failure_and_process_restart(self):
        job=self.uploaded()
        failed=threading.Event()
        original=self.provider.download
        def download(key,*args):
            if 'HEART/' in key:
                failed.set()
                raise HTTPException(503,'fixture connection failed')
            return original(key,*args)
        with patch.object(self.provider,'download',side_effect=download):
            self.uploads.begin_finish(self.job['id'],USER,lambda:USER)
            state=self.wait_idle()
        self.assertTrue(failed.is_set())
        self.assertEqual(state['state'],'validation_error')
        self.assertEqual(state['verified_files'],2)
        restarted=R2Uploads(self.root,self.provider,self.service)
        visited=[]
        def resumed(key,*args):
            visited.append(key)
            return original(key,*args)
        with patch.object(self.provider,'download',side_effect=resumed):
            self.assertEqual(restarted.finish(job['id'],USER)['state'],'ready')
        self.assertEqual(len(visited),1)
        self.assertIn('HEART/',visited[0])

    def test_changed_object_version_cannot_reuse_checkpoint_or_old_local_bytes(self):
        job=self.uploaded()
        original=self.provider.put_if_absent
        # Stop immediately before publication, retaining successful per-file proofs.
        with patch.object(self.provider,'put_if_absent',side_effect=HTTPException(503,'fixture')):
            with self.assertRaises(HTTPException):self.uploads.finish(job['id'],USER)
        item=self.uploads.items(job)[0]
        key=PREFIX+'/'+item['key']
        old=self.provider.objects[key]
        self.provider.objects[key]=(b'x'*len(self.data[0]),old[1]+'changed')
        # Keep the declared checksum to exercise byte validation, not just metadata validation.
        head=self.provider.head
        def declared(k):
            value=head(k)
            if k==key:value['Metadata']['sha256']=item['sha256']
            return value
        with patch.object(self.provider,'head',side_effect=declared):
            with self.assertRaises(HTTPException):self.uploads.finish(job['id'],USER)
        self.assertIsNone(self.provider.optional_json(PREFIX+'/manifest.json')[0])

    def test_crash_state_requires_fresh_authenticated_resume_and_is_idempotent(self):
        self.uploaded()
        job=self.uploads.load(self.job['id'],USER);job['state']='validating';self.uploads.save(job)
        restarted=R2Uploads(self.root,self.provider,self.service)
        self.assertFalse(restarted.status(job['id'],USER)['validation_active'])
        with self.assertRaises(HTTPException):restarted.begin_finish(job['id'],OTHER,lambda:OTHER)
        self.uploads=restarted
        self.uploads.begin_finish(job['id'],USER,lambda:USER)
        self.assertEqual(self.wait_idle()['state'],'ready')
        with patch.object(self.provider,'download',side_effect=AssertionError('already published')):
            self.assertEqual(self.uploads.begin_finish(job['id'],USER,lambda:USER)['state'],'ready')

    def test_small_file_real_transfer_and_bounded_concurrency(self):
        active=peak=0
        guard=threading.Lock()
        barrier=threading.Barrier(3)
        received={}
        def put(request):
            nonlocal active,peak
            with guard:active+=1;peak=max(peak,active)
            try:
                barrier.wait(timeout=3)
                received[request.url.path]=request.content
                self.assertEqual(request.headers['content-length'],str(len(request.content)))
                return httpx.Response(200)
            finally:
                with guard:active-=1
        root=self.root/'prepared';root.mkdir()
        for index,data in enumerate(self.data):
            path=root/str(index);path.write_bytes(data)
        class Target:
            attachments=[]
            reports=[]
            def request(inner,method,path):
                index=int(urlsplit(path).path.rsplit('/',1)[-1])
                return dict(complete=False,mode='single',url=f'https://objects.invalid/{index}',headers={})
            def progress(inner,value):pass
            def transfer_progress(inner,value):inner.reports.append(value)
        target=Target()
        manifest={'files':[dict(key=str(i),size=len(data)) for i,data in enumerate(self.data)]}
        real_client=httpx.Client
        with patch('backend.upload_transfer.httpx.Client',side_effect=lambda **kwargs:real_client(transport=httpx.MockTransport(put),**kwargs)):
            transfer_files(target,{'id':'fixture'},root,manifest)
        self.assertEqual(peak,3)
        self.assertEqual(received,{'/'+str(i):data for i,data in enumerate(self.data)})
        final=target.reports[-1]
        self.assertEqual(final['bytes_uploaded'],sum(map(len,self.data)))
        self.assertEqual(final['files_done'],3)
        self.assertEqual(final['progress'],100)
        self.assertGreater(final['speed_bps'],0)

    def test_multipart_resume_retry_does_not_double_count_or_resend_good_parts(self):
        data=b'abcdefghijklmno'
        (self.root/'video.mp4').write_bytes(data)
        uploaded={1:data[:5]};calls=[];failed=False;reports=[]
        class Target:
            attachments=[]
            def request(inner,method,path):
                if method=='POST':
                    self.assertEqual(set(uploaded),{1,2,3})
                    return {'ok':True}
                return dict(complete=False,part_size=5,parts=[dict(number=i,uploaded=i in uploaded,url=f'https://parts.invalid/{i}') for i in (1,2,3)])
            def progress(inner,value):pass
            def transfer_progress(inner,value):reports.append(value)
        def put(request):
            nonlocal failed
            number=int(request.url.path[1:]);calls.append(number)
            if number==2 and not failed:
                failed=True
                return httpx.Response(503)
            uploaded[number]=request.content
            return httpx.Response(200)
        real_client=httpx.Client
        with patch('backend.upload_transfer.httpx.Client',side_effect=lambda **kwargs:real_client(transport=httpx.MockTransport(put),**kwargs)),patch('backend.upload_transfer.time.sleep'):
            transfer_files(Target(),{'id':'fixture'},self.root,{'files':[dict(key='video.mp4',size=len(data))]})
        self.assertEqual(calls.count(1),0)
        self.assertEqual(calls.count(2),2)
        self.assertEqual(calls.count(3),1)
        self.assertEqual(b''.join(uploaded[i] for i in (1,2,3)),data)
        self.assertTrue(all(r['bytes_uploaded']<=len(data) for r in reports))
        self.assertEqual(reports[-1]['bytes_uploaded'],len(data))

    def test_background_validation_error_stops_client_polling_instead_of_retry_loop(self):
        target=RemoteCaptureTarget(SimpleNamespace(),self.service)
        requests=[]
        def request(method,path,**kwargs):
            requests.append((method,path))
            if path=='/api/r2/uploads':return {'id':'fixture'}
            if '/finish' in path:return dict(state='validating',verified_files=0,file_count=3)
            return dict(state='validation_error',error='fixture checksum failure')
        target.request=request
        with patch('backend.upload_playback.build'),patch('backend.upload_transfer.transfer_files'),patch('backend.r2_upload.time.sleep'):
            with self.assertRaises(HTTPException) as error:
                target.publish_capture(PREFIX,self.root,{'files':[]},self.service)
        self.assertEqual(error.exception.detail,'fixture checksum failure')
        self.assertEqual(sum('/finish' in p for _,p in requests),1)

    def test_background_validation_restarts_after_api_crash_without_false_success(self):
        target=RemoteCaptureTarget(SimpleNamespace(),self.service)
        posts=[]
        def request(method,path,**kwargs):
            if path=='/api/r2/uploads':return {'id':'fixture'}
            if method=='POST':
                posts.append(path)
                return dict(state='validating',file_count=1) if len(posts)==1 else dict(state='ready')
            return dict(state='validating',validation_active=False,file_count=1)
        target.request=request
        with patch('backend.upload_playback.build'),patch('backend.upload_transfer.transfer_files'),patch('backend.r2_upload.time.sleep'):
            target.publish_capture(PREFIX,self.root,{'files':[]},self.service)
        self.assertEqual(len(posts),2)

    def test_sync_and_background_jobs_share_the_same_validation_resource_limit(self):
        jobs=[self.job]
        for n in (2,3):
            jobs.append(self.uploads.start(USER,{**self.manifest,'prefix':PREFIX+str(n)}))
        for summary in jobs:
            job=self.uploads.load(summary['id'],USER)
            for item,data in zip(self.uploads.items(job),self.data):
                self.provider.put(job['manifest']['prefix']+'/'+item['key'],data,create=True)
        entered=set();guard=threading.Lock();two=threading.Event();release=threading.Event()
        original=self.provider.download
        def download(key,*args):
            with guard:
                entered.add(key.split('/')[2])
                if len(entered)==2:two.set()
            release.wait(timeout=4)
            return original(key,*args)
        with patch.object(self.provider,'download',side_effect=download),ThreadPoolExecutor(max_workers=3) as pool:
            futures=[pool.submit(self.uploads.finish,j['id'],USER) for j in jobs]
            try:
                self.assertTrue(two.wait(timeout=3))
                # The third job must stay out of S3 while the other two hold verification slots.
                with guard:self.assertEqual(len(entered),2)
            finally:release.set()
            self.assertTrue(all(f.result(timeout=5)['state']=='ready' for f in futures))
        self.assertEqual(len(entered),3)

    def test_prediction_attachment_cannot_masquerade_as_a_thumbnail(self):
        manifest=json.loads(json.dumps(self.manifest))
        data=b'not valid prediction JSON'
        manifest['attachments'][0].update(key='prediction/posture.json',kind='thumbnail',size=len(data),sha256=hashlib.sha256(data).hexdigest())
        summary=self.uploads.start(USER,{**manifest,'prefix':PREFIX+'prediction'})
        job=self.uploads.load(summary['id'],USER)
        for item,payload in zip(self.uploads.items(job),[self.data[0],data,self.data[2]]):
            self.provider.put(job['manifest']['prefix']+'/'+item['key'],payload,create=True)
        self.service.valid_thumbnail=lambda p:True
        with self.assertRaises(HTTPException):self.uploads.finish(job['id'],USER)
        self.assertIsNone(self.provider.optional_json(job['manifest']['prefix']+'/manifest.json')[0])
