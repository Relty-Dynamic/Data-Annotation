"""Range reads keep every byte checked; isolated S3 fixture, no network."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import io
import json
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from fastapi import HTTPException
from botocore.exceptions import ClientError

from backend.r2_store import R2Store
from backend.r2_uploads import R2Uploads
from backend.test_r2_integration import USER, PREFIX, NAME


class ValidationDownloadTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        config=self.root/'config.json'
        config.write_text(json.dumps(dict(endpoint='https://'+'a'*32+'.r2.cloudflarestorage.com',
            bucket='test-bucket',access_key_id='fixture',secret_access_key='fixture')))
        config.chmod(0o600)
        self.data=bytes(range(256))*4
        self.etag='"version-one"'
        self.calls=[];self.active=0;self.peak=0;self.lock=threading.Lock()
        self.response_hook=lambda response:response
        self.failed_offsets=set()
        self.store=R2Store(config,client=SimpleNamespace(get_object=self.get_object))
        self.small=patch('backend.validation_download.CHUNK',128)
        self.threshold=patch('backend.validation_download.THRESHOLD',256)
        self.small.start();self.threshold.start()
        self.addCleanup(self.small.stop);self.addCleanup(self.threshold.stop)

    def get_object(self,**kwargs):
        self.assertEqual(kwargs['IfMatch'],self.etag)
        start,end=map(int,kwargs['Range'].removeprefix('bytes=').split('-'))
        with self.lock:
            self.calls.append(start)
            self.active+=1;self.peak=max(self.peak,self.active)
        try:
            time.sleep(.01)
            if start in self.failed_offsets:
                raise ClientError({'Error':{'Code':'PreconditionFailed'}},'GetObject')
            body=self.data[start:end+1]
            return self.response_hook(dict(Body=io.BytesIO(body),ContentLength=len(body),
                ContentRange=f'bytes {start}-{end}/{len(self.data)}',ETag=self.etag))
        finally:
            with self.lock:self.active-=1

    def download(self,path=None,checksum=None,progress=lambda n:None):
        path=path or self.root/'result.bin'
        self.store.download_for_validation('daily/fixture',path,checksum or hashlib.sha256(self.data).hexdigest(),len(self.data),self.etag,progress)
        return path

    def test_parallel_ranges_preserve_order_checksum_and_clean_chunks(self):
        progress=[]
        path=self.download(progress=progress.append)
        self.assertEqual(path.read_bytes(),self.data)
        self.assertEqual(set(self.calls),set(range(0,len(self.data),128)))
        self.assertGreater(self.peak,1);self.assertLessEqual(self.peak,4)
        self.assertEqual(sum(progress),len(self.data))
        self.assertFalse(list(self.root.glob('.ranges-*')))
        self.assertFalse(list(self.root.glob('*.tmp')))

    def test_failure_preserves_original_and_resumes_only_missing_range(self):
        path=self.root/'result.bin';path.write_bytes(b'original')
        self.failed_offsets={256}
        with self.assertRaises(HTTPException):self.download(path)
        self.assertEqual(path.read_bytes(),b'original')
        first=list(self.calls);self.failed_offsets=set();self.calls=[]
        self.download(path)
        self.assertEqual(path.read_bytes(),self.data)
        self.assertEqual(set(self.calls),set(range(0,len(self.data),128))-(set(first)-{256}))
        self.assertEqual(self.calls.count(256),1)

    def test_bad_range_etag_and_truncated_body_never_replace_original(self):
        for kind in ('range','etag','truncated','oversized'):
            with self.subTest(kind=kind),tempfile.TemporaryDirectory(dir=self.root) as temporary:
                path=Path(temporary)/'result';path.write_bytes(b'original')
                def alter(response):
                    if kind=='range':response['ContentRange']='bytes 0-127/999'
                    if kind=='etag':response['ETag']='"other-version"'
                    if kind=='truncated':response['Body']=io.BytesIO(b'x')
                    if kind=='oversized':response['Body']=io.BytesIO(b'x'*129)
                    return response
                self.response_hook=alter
                with self.assertRaises(HTTPException):self.download(path)
                self.assertEqual(path.read_bytes(),b'original')

    def test_wrong_whole_file_hash_rejects_and_discards_bad_chunk_cache(self):
        path=self.root/'result';path.write_bytes(b'original')
        with self.assertRaises(HTTPException):self.download(path,checksum='a'*64)
        self.assertEqual(path.read_bytes(),b'original')
        self.assertFalse(list(self.root.glob('.ranges-*')))

    def test_two_files_share_global_network_limit_and_same_file_avoids_duplicate_reads(self):
        with ThreadPoolExecutor(max_workers=2) as pool:
            results=list(pool.map(lambda n:self.download(self.root/f'result{n}'),range(2)))
        self.assertTrue(all(p.read_bytes()==self.data for p in results))
        self.assertLessEqual(self.peak,6)
        self.calls=[]
        with ThreadPoolExecutor(max_workers=2) as pool:
            results=list(pool.map(lambda n:self.download(self.root/'same'),range(2)))
        self.assertEqual(len(self.calls),8)

    def test_corrupted_retained_chunk_is_redownloaded(self):
        self.failed_offsets={256}
        with self.assertRaises(HTTPException):self.download()
        completed={int(p.stem)*128 for p in self.root.glob('.ranges-*/*.bin')}
        chunk=next(self.root.glob('.ranges-*/000000.bin'));chunk.write_bytes(b'x'*128)
        next(self.root.glob('.ranges-*/000001.sha256')).write_bytes(b'\xff\xfe')
        self.failed_offsets=set();self.calls=[]
        self.download()
        self.assertEqual(set(self.calls),(set(range(0,len(self.data),128))-completed)|{0,128})

    def test_full_upload_publication_uses_ranges_and_still_checks_video_format(self):
        provider=self.store
        provider.optional_json=lambda key:(None,None)
        provider.head=lambda key:dict(ContentLength=len(self.data),ETag=self.etag,Metadata={'sha256':hashlib.sha256(self.data).hexdigest()})
        published=[]
        provider.put_if_absent=lambda key,payload:published.append((key,payload))
        service=SimpleNamespace(local=self.root,probe=lambda path:dict(codec='mpeg4',duration_ms=1000))
        uploads=R2Uploads(self.root,provider,service)
        job=uploads.start(USER,dict(prefix=PREFIX,collector_name='Collector',uploader_name='Worker',files=[dict(
            key='FPV/'+NAME,size=len(self.data),sha256=hashlib.sha256(self.data).hexdigest(),duration_ms=1000)]))
        with self.assertRaises(HTTPException):uploads.finish(job['id'],USER)
        self.assertFalse(published)
        self.assertTrue(self.calls)
        self.assertEqual(uploads.status(job['id'],USER)['validation_bytes'],len(self.data))
        service.probe=lambda path:dict(codec='h264',duration_ms=1000)
        self.assertEqual(uploads.finish(job['id'],USER)['state'],'ready')
        self.assertEqual([key for key,_ in published],[
            PREFIX+'/capture.identity.json', PREFIX+'/manifest.json'])
        identity=json.loads(published[0][1])
        self.assertEqual(identity,dict(prefix=PREFIX,collector_name='Collector',capture_date='20261015'))
        manifest=json.loads(published[1][1])
        self.assertEqual(manifest['prefix'],PREFIX)
        self.assertEqual(len(manifest['files']),1)
        self.assertEqual(manifest['files'][0]['name'],NAME)
        self.assertEqual(manifest['files'][0]['size'],len(self.data))
        self.assertEqual(manifest['files'][0]['sha256'],hashlib.sha256(self.data).hexdigest())
        self.assertEqual(manifest['media_metadata'][NAME],dict(codec='h264',duration_ms=1000))
        before=list(published)
        self.assertEqual(uploads.finish(job['id'],USER)['state'],'ready')
        self.assertEqual(published,before)

    def test_client_reports_read_bytes_while_no_whole_file_has_finished(self):
        from backend.r2_upload import RemoteCaptureTarget
        reports=[];messages=[]
        target=RemoteCaptureTarget(SimpleNamespace(),SimpleNamespace(),progress=messages.append)
        target.transfer_progress=reports.append
        def request(method,path,**kwargs):
            if path=='/api/health':return {'capabilities':['s3-single-video-cache-v1']}
            if path=='/api/r2/uploads':return dict(id='fixture')
            if method=='POST':return dict(state='validating',verified_files=0,file_count=1,
                validation_bytes=4*1024**2,validation_total_bytes=8*1024**2)
            return dict(state='ready')
        target.request=request
        with patch('backend.upload_playback.build'),patch('backend.upload_transfer.transfer_files'),patch('backend.r2_upload.time.sleep'):
            target.publish_capture(PREFIX,self.root,{'files':[]},SimpleNamespace())
        self.assertEqual(reports[0]['progress'],50)
        self.assertTrue(any('4.0/8.0 MiB' in m for m in messages))
