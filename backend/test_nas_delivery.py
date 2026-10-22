"""Delivery commit, S3 batch barrier, immutable snapshots and durable NAS recovery."""
import copy
import json
import select
import sys
import unittest
from unittest.mock import patch
from fastapi import HTTPException

from backend import test_collection_outputs as fixtures
from backend.tasks.nas_delivery import NasDelivery
from backend.nas_delivery_worker import NasWorker, MountUnavailable, Changes, Snapshot
from deploy.sync_final_timelines import MARKER, sha, SyncError, find_collections, sync_capture


class NasDeliveryTests(unittest.TestCase):
    mutate=fixtures.CollectionOutputTests.mutate
    annotation=fixtures.CollectionOutputTests.annotation
    approved=fixtures.CollectionOutputTests.approved
    deliver=fixtures.CollectionOutputTests.deliver
    data=fixtures.CollectionOutputTests.data

    def setUp(self):
        fixtures.CollectionOutputTests.setUp(self)
        self.nas=self.outputs.enable_nas_sync()
        self.now=[1000.]
        self.destination=self.root/'nas';self.destination.mkdir()
        (self.destination/'1016test').mkdir()
        self.worker=NasWorker(self.nas.queue,self.destination,self.destination,clock=lambda:self.now[0])
        self.worker.check_mount=lambda:None

    def rows(self):
        with self.nas.queue.db() as db:
            return [dict(row) for row in db.execute('SELECT * FROM items ORDER BY rowid')]

    def current(self):
        with self.store.db() as db:return self.outputs.status(db,self.task['id'])['nas']

    def dispatched(self):
        task=self.approved();delivery=self.deliver(task);self.nas.dispatch_once(now=1000)
        return task,delivery

    def test_accept_does_not_trigger_delivery_copies_only_json_and_is_idempotent(self):
        task=self.approved();self.nas.dispatch_once(now=1000)
        self.assertEqual(self.rows(),[]);self.assertIsNone(self.current())
        delivery=self.deliver(task)
        self.assertEqual(self.rows(),[])
        self.nas.dispatch_once(now=1000)
        self.assertEqual(len(self.rows()),1)
        self.worker.process_once()
        target=self.destination/'1016test/timeline/final'
        self.assertEqual(len(list(target.glob('*.timeline.json'))),3)
        self.assertEqual(list(target.glob('*.csv')),[])
        self.assertEqual(self.current()['state'],'ready')
        duplicate=self.store.deliver(self.lead,{'task_ids':[task['id']],'expected_revisions':{},'request_id':'delivery-request'},output_names=self.names)
        self.assertEqual(duplicate['id'],delivery['id'])
        self.nas.dispatch_once(now=2000)
        with patch('backend.nas_delivery_worker.sync_capture',side_effect=AssertionError('must not repeat completed copy')):
            self.worker.process_once()
        self.assertEqual(len(self.rows()),1)

    def test_entire_batch_waits_for_second_collection_s3_validation(self):
        first=self.approved()
        folder=self.root/'1017other';folder.mkdir()
        (folder/'A00001_20261017120000_0001.mp4').write_bytes(b'isolated second collection fixture')
        project=self.source.load(self.source.open_path(str(folder))['id'])
        project['_r2_prefix']='daily/20261017/1017other';self.source.save(project)
        records=[]
        for item in self.media.catalog(self.admin):
            if item['collection_id']!=project['id']:continue
            records.append(self.media.reference(self.admin,{'id':item['id'].replace('/','-'),'collector_id':'采集人','date':'2026-10-17','scene':'室内','quality':'passed','duration_ms':item['duration_ms'],'source':item['source']}))
        self.store.register_collection(self.admin,records)
        body={**self.body,'video_id':records[0]['id'],'video_ids':[r['id'] for r in records]}
        # Use the current collection key expected by the real task store.
        body['collection_id']=records[0]['collection_id']
        second=self.store.create_task(self.lead,body,collection_layout=self.media.task_collection(records))
        original=self.task;self.task=second
        second=self.approved(empty=True);self.task=original
        result=self.store.deliver(self.lead,{'task_ids':[first['id'],second['id']],
            'expected_revisions':{first['id']:first['revision'],second['id']:second['revision']},'request_id':'batch'},output_names=self.names)
        self.outputs.flush(first['id'])
        self.nas.dispatch_once(now=1000)
        self.assertEqual(self.rows(),[])
        original_put=self.provider.put
        def fail(key,*args,**kwargs):
            if '1017other/timeline/final/delivery-' in key:raise HTTPException(503,'isolated S3 unavailable')
            return original_put(key,*args,**kwargs)
        with patch.object(self.provider,'put',side_effect=fail):self.outputs.flush(second['id'])
        self.nas.dispatch_once(now=1000);self.assertEqual(self.rows(),[])
        self.outputs.flush(second['id']);self.nas.dispatch_once(now=1000)
        self.assertEqual(len(self.rows()),2)
        self.assertEqual({r['batch_id'] for r in self.rows()},{result['id']})

    def test_frozen_delivery_does_not_read_later_mutable_final_version(self):
        task=self.approved()
        original=self.data('final/posture.timeline.json')
        self.deliver(task)
        # Later publication may change final/ before the NAS worker is available.
        key=self.prefix+'/timeline/final/posture.timeline.json'
        self.provider.objects[key]=b'new unrelated mutable final version'
        self.nas.dispatch_once(now=1000)
        snapshot_keys=[key for key in self.provider.objects if key.startswith('tasks/nas-deliveries/')]
        self.assertTrue(snapshot_keys)
        self.worker.process_once()
        self.assertEqual((self.destination/'1016test/timeline/final/posture.timeline.json').read_bytes(),original)

    def test_delivery_transaction_rollback_does_not_release_event(self):
        task=self.approved()
        with patch.object(self.store,'_finish',side_effect=RuntimeError('commit aborted')):
            with self.assertRaises(RuntimeError):self.deliver(task)
        with self.store.db() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM deliveries').fetchone()[0],0)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM nas_batches').fetchone()[0],0)
        self.nas.dispatch_once();self.assertEqual(self.rows(),[])

    def test_restart_releases_committed_delivery_and_replays_lost_ack_without_duplicate(self):
        task=self.approved();delivery=self.deliver(task)
        restarted=NasDelivery(self.outputs)
        restarted.dispatch_once(now=1000);self.worker.process_once()
        with self.store.db() as db:
            db.execute("UPDATE nas_batches SET state='waiting_s3' WHERE id=?",(delivery['id'],))
        restarted.dispatch_once(now=2000)
        self.assertEqual(len(self.rows()),1);self.assertEqual(self.rows()[0]['state'],'ready')

    def test_s3_snapshot_failure_is_durable_and_retries_before_release(self):
        task=self.approved();self.deliver(task)
        with patch.object(self.outputs,'_immutable',side_effect=HTTPException(503,'isolated network failure')):
            self.nas.dispatch_once(now=1000)
        self.assertEqual(self.rows(),[]);self.assertEqual(self.current()['state'],'error')
        self.nas.dispatch_once(now=1001);self.assertEqual(self.rows(),[])
        self.nas.dispatch_once(now=1006);self.assertEqual(len(self.rows()),1)

    def test_nas_offline_retries_and_preserves_delivery_success(self):
        task,delivery=self.dispatched()
        with patch.object(self.worker,'check_mount',side_effect=MountUnavailable('offline')):
            self.worker.process_once()
        self.assertEqual(self.rows()[0]['state'],'error')
        self.assertEqual(self.store.get_task(self.lead,task['id'])['status'],'delivered')
        self.assertEqual(self.store.delivery(self.lead,delivery['id'])['id'],delivery['id'])
        self.now[0]+=6;self.worker.process_once();self.assertEqual(self.rows()[0]['state'],'ready')

    def test_interrupted_partial_write_recovers_and_checks_every_json(self):
        self.dispatched()
        from deploy.sync_final_timelines import guarded_write
        writes=[]
        def fail(path,*args,**kwargs):
            if path.name.endswith('.timeline.json'):
                writes.append(path)
                if len(writes)==2:raise OSError('isolated disconnect')
            return guarded_write(path,*args,**kwargs)
        with patch('deploy.sync_final_timelines.guarded_write',side_effect=fail):self.worker.process_once()
        self.assertEqual(self.rows()[0]['state'],'error')
        self.now[0]+=6;self.worker.process_once()
        target=self.destination/'1016test/timeline/final'
        value=json.loads((target/MARKER).read_bytes())
        for name,digest in value['files'].items():self.assertEqual(sha((target/name).read_bytes()),digest)
        self.assertFalse((target/'.datamark-sync-pending.json').exists())
        self.assertEqual(self.current()['state'],'ready')

    def test_manual_conflict_needs_explicit_retry(self):
        self.dispatched();target=self.destination/'1016test/timeline/final';target.mkdir(parents=True)
        foreign=target/'posture.timeline.json';foreign.write_bytes(b'user edit')
        self.worker.process_once();self.assertEqual(self.rows()[0]['state'],'blocked')
        self.now[0]+=1000;self.worker.process_once();self.assertEqual(foreign.read_bytes(),b'user edit')
        # The operator explicitly resolves the isolated fixture conflict.
        foreign.unlink();self.nas.retry(self.task['id']);self.nas.dispatch_once(now=2000)
        self.worker.process_once();self.assertEqual(self.rows()[0]['state'],'ready')

    def test_deleted_task_cancels_pending_event_across_dispatcher_restart(self):
        task,_=self.dispatched()
        current=self.store.get_task(self.lead,task['id'])
        self.store.delete_task(self.lead,task['id'],{'expected_revision':current['revision']})
        NasDelivery(self.outputs).dispatch_once(now=1000)
        self.worker.process_once()
        self.assertEqual(self.rows()[0]['state'],'cancelled')
        self.assertFalse((self.destination/'1016test/timeline').exists())

    def test_historical_backfill_with_previously_deleted_task_cannot_release(self):
        task=self.approved()
        self.outputs.nas_sync=None
        delivery=self.deliver(task)
        current=self.store.get_task(self.lead,task['id'])
        self.store.delete_task(self.lead,task['id'],{'expected_revision':current['revision']})
        self.outputs.nas_sync=self.nas
        with self.store.db() as db:
            deleted=self.store._read(db,'tasks',task['id'])
            self.outputs.delivery(db,[deleted],delivery,self.store.artifacts.get(delivery['artifact_key']),self.names)
        self.nas.dispatch_once(now=1000)
        self.assertEqual(self.rows(),[])
        with self.store.db() as db:
            self.assertEqual(db.execute('SELECT state FROM nas_batches WHERE id=?',(delivery['id'],)).fetchone()[0],'cancelled')

    def test_running_item_is_resumed_after_worker_restart(self):
        self.dispatched()
        with self.nas.queue.db() as db:db.execute("UPDATE items SET state='running'")
        self.worker.recover();self.worker.process_once()
        self.assertEqual(self.rows()[0]['state'],'ready')

    def changed_payload(self, payload):
        result=copy.deepcopy(payload)
        result['marker']['metadata']['accepted_at']='2099-01-01T00:00:00Z'
        for name,text in result['files'].items():
            document=json.loads(text)
            document['task_result']['accepted_at']='2099-01-01T00:00:00Z'
            document['task_result']['final_version_id']='isolated-newer-version'
            result['files'][name]=json.dumps(document,ensure_ascii=False)
            result['marker']['files'][name]=sha(result['files'][name].encode())
        return result

    def apply_payload(self, payload):
        collections=find_collections(self.destination,[payload['capture']])
        return sync_capture(Snapshot(payload,self.destination,collections),self.destination,
                            payload['capture'],apply=True,collections=collections,directory='final')

    def test_update_of_existing_result_recovers_interrupted_replacement(self):
        self.dispatched();self.worker.process_once()
        newer=self.changed_payload(json.loads(self.rows()[0]['payload']))
        from deploy.sync_final_timelines import guarded_write
        calls=[]
        def disconnect(path,*args,**kwargs):
            if path.name.endswith('.timeline.json'):
                calls.append(path)
                if len(calls)==2:raise OSError('isolated update disconnected')
            return guarded_write(path,*args,**kwargs)
        with patch('deploy.sync_final_timelines.guarded_write',side_effect=disconnect):
            with self.assertRaises(OSError):self.apply_payload(newer)
        self.apply_payload(newer)
        target=self.destination/'1016test/timeline/final'
        for name,text in newer['files'].items():self.assertEqual((target/name).read_text(),text)
        self.assertFalse((target/'.datamark-sync-pending.json').exists())

    def test_marker_replacement_gap_preserves_other_axes_on_retry(self):
        self.dispatched();self.worker.process_once()
        original=json.loads(self.rows()[0]['payload'])
        newer=self.changed_payload(original)
        newer['files']={'posture.timeline.json':newer['files']['posture.timeline.json']}
        newer['marker']['files']={'posture.timeline.json':newer['marker']['files']['posture.timeline.json']}
        from deploy.sync_final_timelines import guarded_write
        def stop_after_backup(path,data,old,backup=None):
            if path.name==MARKER:
                path.rename(backup)
                raise OSError('isolated marker replacement gap')
            return guarded_write(path,data,old,backup)
        with patch('deploy.sync_final_timelines.guarded_write',side_effect=stop_after_backup):
            with self.assertRaises(OSError):self.apply_payload(newer)
        self.apply_payload(newer)
        target=self.destination/'1016test/timeline/final'
        complete=json.loads((target/MARKER).read_bytes())
        self.assertEqual(set(complete['files']),set(original['files']))
        for name,digest in complete['files'].items():self.assertEqual(sha((target/name).read_bytes()),digest)
        self.assertEqual((target/'scene.timeline.json').read_text(),original['files']['scene.timeline.json'])

    def test_other_axes_survive_and_older_delivery_cannot_overwrite_newer_axis(self):
        self.dispatched();self.worker.process_once()
        original=json.loads(self.rows()[0]['payload'])
        newer=self.changed_payload(original)
        newer['files']={name:text for name,text in newer['files'].items() if name=='posture.timeline.json'}
        newer['marker']['files']={name:digest for name,digest in newer['marker']['files'].items() if name in newer['files']}
        self.apply_payload(newer)
        target=self.destination/'1016test/timeline/final'
        self.assertEqual((target/'scene.timeline.json').read_text(),original['files']['scene.timeline.json'])
        with self.assertRaisesRegex(SyncError,'更新的交付版本'):self.apply_payload(original)
        self.assertEqual((target/'posture.timeline.json').read_text(),newer['files']['posture.timeline.json'])
        for name,digest in json.loads((target/MARKER).read_bytes())['files'].items():
            self.assertEqual(sha((target/name).read_bytes()),digest)

    def test_completed_copy_is_preserved_when_task_is_later_deleted(self):
        task,_=self.dispatched();self.worker.process_once()
        current=self.store.get_task(self.lead,task['id'])
        self.store.delete_task(self.lead,task['id'],{'expected_revision':current['revision']})
        self.nas.dispatch_once(now=1000)
        self.assertEqual(self.current()['state'],'ready')
        self.assertEqual(self.rows()[0]['state'],'ready')

    def test_queue_outage_does_not_rollback_delivery(self):
        task=self.approved()
        with patch.object(self.nas.queue,'db',side_effect=OSError('isolated queue offline')):
            delivery=self.deliver(task)
            self.assertEqual(self.current()['state'],'error')
        self.assertEqual(self.store.delivery(self.lead,delivery['id'])['id'],delivery['id'])
        self.nas.dispatch_once(now=1000);self.worker.process_once()
        self.assertEqual(self.current()['state'],'ready')

    def test_s3_snapshot_conflict_does_not_release_queue(self):
        task=self.approved();self.deliver(task)
        with patch.object(self.outputs,'_immutable',side_effect=HTTPException(409,'isolated immutable conflict')):
            self.nas.dispatch_once(now=1000)
        self.assertEqual(self.rows(),[])
        self.assertFalse(self.current()['retry_scheduled'])
        self.nas.dispatch_once(now=100000);self.assertEqual(self.rows(),[])
        self.nas.retry(task['id']);self.nas.dispatch_once(now=100000)
        self.assertEqual(len(self.rows()),1)

    def test_local_bind_mount_is_rejected_even_when_directory_exists(self):
        worker=NasWorker(self.nas.queue,self.destination,self.destination)
        with patch('os.path.ismount',return_value=True), patch('pathlib.Path.read_text',return_value=f'42 21 0:1 / {self.destination} rw - ext4 /dev/fixture rw'):
            with self.assertRaises(MountUnavailable):worker.check_mount()
        with patch('os.path.ismount',return_value=True), patch('pathlib.Path.read_text',return_value=f'42 21 0:1 / {self.destination} rw - cifs //fixture/daily rw'):
            worker.check_mount()

    @unittest.skipUnless(sys.platform=='linux','Linux inotify worker')
    def test_queue_commit_wakes_idle_worker_without_periodic_scan(self):
        watcher=Changes(self.nas.queue.root)
        try:
            watcher.drain()
            with self.nas.queue.db() as db:db.execute("INSERT INTO batches(id,state) VALUES ('notification','queued')")
            self.assertTrue(select.select([watcher.fd],[],[],2)[0])
        finally:watcher.close()
