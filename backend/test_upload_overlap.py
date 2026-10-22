"""Bounded transfer/verification overlap with publication and error barriers."""
import tempfile
import threading
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from fastapi import HTTPException
from backend.r2_upload import RemoteCaptureTarget
from backend.r2_folder import FolderUploadManager
from backend.test_r2_integration import USER


class UploadOverlapTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1]/'.tmp')
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.collection = self.root/'collection'
        self.collection.mkdir()
        for index in range(3):
            (self.collection/f'A00001_20261015120{index}00_000{index+1}.avi').write_bytes(b'raw fixture '+bytes([index]))
        self.target = RemoteCaptureTarget(None, None)
        self.target.request = lambda method,path,**kw: {'capabilities':['s3-single-video-cache-v1','s3-upload-pipeline-v1']}
        self.manager = FolderUploadManager(self.target, self.root/'staging')
        self.service = SimpleNamespace(local=self.root, desktop_cache=SimpleNamespace(enabled=False), probe=lambda _:dict(duration_ms=1000))
        selection = self.manager.select_folder(USER['id'], self.collection)
        self.job = self.manager.start(USER,'fixture',None,None,selection['selection_id'])
        self.release = threading.Event()
        self.two = threading.Event()
        self.prepared = threading.Event()
        self.third = threading.Event()
        self.sent, self.verified, self.published, self.errors = [], [], [], []
        self.access = True

    def build(self, service, prepared, bundle, progress, *, profile, artifacts):
        item = bundle['files'][0]
        self.assertEqual(artifacts[item['key']].digest_for(prepared/item['key']), item['sha256'])
        bundle.update(cache_profile=profile,cache_files=[])
        if '0003.mp4' in item['key']:self.prepared.set()
        return {}

    def begin(self, remote, prefix, prepared, bundle, service, **kwargs):
        self.assertTrue(kwargs['prepare'])
        index = len(self.sent)
        self.sent.append(bundle)
        if len(self.sent)==2:self.two.set()
        if len(self.sent)==3:self.third.set()
        return index

    def verify(self, remote, index):
        self.assertTrue(self.release.wait(5))
        remote.check_cancel()
        self.verified.append(index)

    def publish(self, remote, prefix, prepared, bundle, service, **kwargs):
        self.assertEqual(sorted(self.verified), [0,1,2])
        self.assertEqual(len(kwargs['completed_keys']),3)
        self.published.append(bundle)

    def run_job(self):
        try:self.manager.finish(self.job['id'],USER['id'],self.service,lambda _:self.access)
        except Exception as error:self.errors.append(error)

    def working(self, verify=None):
        from contextlib import ExitStack
        stack = ExitStack()
        stack.enter_context(patch.object(self.manager,'_compress_video',side_effect=lambda service,source,destination,progress:(destination.write_bytes(b'compressed '+source.read_bytes()),progress(100))))
        stack.enter_context(patch('backend.upload_pipeline.build',side_effect=self.build))
        stack.enter_context(patch.object(RemoteCaptureTarget,'begin_publish',lambda remote,*a,**kw:self.begin(remote,*a,**kw)))
        stack.enter_context(patch.object(RemoteCaptureTarget,'wait_publish',lambda remote,index:(verify or self.verify)(remote,index)))
        stack.enter_context(patch.object(RemoteCaptureTarget,'publish_ready',lambda remote,*a,**kw:self.publish(remote,*a,**kw)))
        return stack

    def test_next_upload_overlaps_verification_with_two_clip_bound_and_final_barrier(self):
        with self.working():
            thread = threading.Thread(target=self.run_job);thread.start()
            try:
                self.assertTrue(self.two.wait(3),'second upload must start before first verification finishes')
                self.assertTrue(self.prepared.wait(3))
                self.assertFalse(self.third.wait(.1),'third upload must wait for a validation slot')
                self.assertFalse(self.published)
                status = self.manager.status(self.job['id'],USER['id'])
                self.assertEqual(status['clips_verified'],0)
                self.assertEqual(status['progress'],0)
                stages = {row['stage']:row for row in status['preparation']['stages']}
                self.assertEqual(stages['compress']['completed'],3)
                self.assertGreaterEqual(stages['upload']['completed'],1)
                self.assertEqual(stages['verify']['completed'],0)
            finally:self.release.set();thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertFalse(self.errors,self.errors)
        self.assertEqual(len(self.published),1)
        self.assertEqual(self.manager.status(self.job['id'],USER['id'])['state'],'ready')

    def test_failed_verification_stops_waiting_upload_and_never_publishes(self):
        def verify(remote,index):
            self.assertTrue(self.two.wait(3))
            if index==0:raise HTTPException(422,'fixture remote checksum mismatch')
            self.assertTrue(self.release.wait(5))
            remote.check_cancel()
        with self.working(verify):
            thread=threading.Thread(target=self.run_job);thread.start()
            try:
                self.assertTrue(self.two.wait(3))
                # Wait for the first failure to be visible, while the other
                # validation is still deliberately blocked.
                for _ in range(100):
                    if self.manager.status(self.job['id'],USER['id'])['state']=='error':break
                    threading.Event().wait(.01)
                status=self.manager.status(self.job['id'],USER['id'])
                self.assertEqual(status['state'],'error')
                self.assertIn('checksum mismatch',status['detail'])
                self.assertFalse(self.third.is_set())
                self.assertFalse(self.published)
            finally:self.release.set();thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertTrue(self.errors)
        self.assertEqual(self.errors[0].status_code,422)
        self.assertEqual(len(list(self.collection.glob('*.avi'))),3)

    def test_permission_revoked_during_validation_blocks_remaining_transfer_and_publication(self):
        with self.working():
            thread=threading.Thread(target=self.run_job);thread.start()
            try:
                self.assertTrue(self.two.wait(3))
                self.access=False
            finally:self.release.set();thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertTrue(self.errors)
        self.assertEqual(self.errors[0].status_code,403)
        self.assertFalse(self.published)
