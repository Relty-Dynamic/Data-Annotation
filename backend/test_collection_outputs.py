"""Submission revisions and approved results use isolated S3 and real JSON exports."""
import copy
import csv
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from fastapi import HTTPException

from backend.service import ProjectService
from backend.tasks.store import TaskStore
from backend.tasks.storage import ObjectArtifacts, ProjectMedia
from backend.tasks.collection_outputs import CollectionOutputs
from backend.test_r2_integration import MemoryObjects


class CollectionOutputTests(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1]/'.tmp')
        self.addCleanup(temporary.cleanup)
        self.root=Path(temporary.name)
        self.provider=MemoryObjects()
        self.source=ProjectService(self.root/'source',enable_remote=False)
        self.source.provider=self.provider
        self.source.probe=lambda path:{'duration_ms':1000,'media_start_seconds':0,'codec':'h264'}
        self.editor=ProjectService(self.root/'editor',enable_remote=False)
        for service in (self.source,self.editor):
            self.addCleanup(service.sessions.close);self.addCleanup(service.previews.close)
        folder=self.root/'1016test';folder.mkdir()
        for number,second in enumerate(('00','02')):
            (folder/f'A00001_202610161200{second}_{number:04d}.mp4').write_bytes(b'explicit-isolated-probe-fixture')
        project=self.source.load(self.source.open_path(str(folder))['id'])
        self.prefix='daily/20261016/1016test'
        project['_r2_prefix']=self.prefix
        self.source.save(project)
        self.project=project
        self.media=ProjectMedia(self.source,'s3',lambda actor,ident:True)
        self.store=TaskStore(self.root/'tasks',ObjectArtifacts(self.provider))
        self.outputs=CollectionOutputs(self.store,self.media,self.editor)
        self.store.outputs=self.outputs
        self.admin={'id':'admin','role':'admin','can_upload':True,'display_name':'管理员'}
        self.lead={'id':'lead','role':'annotator','display_name':'组长'}
        self.a={'id':'a','role':'annotator','display_name':'张三'}
        self.b={'id':'b','role':'annotator','display_name':'李四'}
        self.names={actor['id']:actor['display_name'] for actor in (self.admin,self.lead,self.a,self.b)}
        for actor,roles in ((self.lead,['leader']),(self.a,['annotator']),(self.b,['annotator'])):
            self.store.set_roles(self.admin,actor['id'],roles)
        records=[]
        for item in self.media.catalog(self.admin):
            records.append(self.media.reference(self.admin,{'id':item['id'].replace('/','-'),'collector_id':'采集人',
                'date':'2026-10-16','scene':'室内','quality':'passed','duration_ms':item['duration_ms'],'source':item['source']}))
        self.store.register_collection(self.admin,records)
        self.rule=self.store.publish_rule(self.lead,{'name':'场景姿势','task_type':'daily',
            'axes':[{'id':'scene','name':'场景','mode':'state','labels':['室内']},
                    {'id':'posture','name':'姿势','mode':'state','labels':['站','坐']},
                    {'id':'food','name':'饮食','mode':'state','labels':['喝水']}]})
        self.body={'collection_id':project['id'],'storage_backend':'s3','rule_id':self.rule['id'],
                   'annotator_ids':['a','b'],'reviewer_id':'lead','deadline':'2026-10-30'}
        self.task=self.store.create_task(self.lead,self.body,collection_layout=self.media.task_collection(records))

    def annotation(self,label='站'):
        return {'posture':[{'id':'posture-one','start_ms':2000,'end_ms':2500,'label':label,'lane':0,'kind':'interval'}],
                'scene':[],'food':[]}

    def mutate(self,method,actor,task,**body):
        result=getattr(self.store,method)(actor,task['id'],{'expected_revision':task['revision'],**body},
                **({'output_names':self.names} if method in ('submit','accept') else {}))
        self.outputs.flush(task['id'])
        return result

    def data(self,path):return self.provider.get(self.prefix+'/timeline/'+path)

    def approved(self,empty=False):
        annotations={} if empty else self.annotation()
        task=self.mutate('submit',self.a,self.task,annotations=annotations)
        task=self.mutate('submit',self.b,task,annotations=annotations)
        task=self.mutate('start_review',self.lead,task)
        task=self.mutate('review',self.lead,task,action='pass')
        return self.mutate('accept',self.lead,task)

    def deliver(self,task):
        result=self.store.deliver(self.lead,{'task_ids':[task['id']],'expected_revisions':{task['id']:task['revision']},
                  'request_id':'delivery-request'},output_names=self.names)
        self.outputs.flush(task['id'])
        return result

    def test_two_people_rework_versions_approved_final_directory_contains_json_and_csv(self):
        task=self.mutate('submit',self.a,self.task,annotations=self.annotation('坐'))
        original=self.data('张三/v1/posture.timeline.json')
        self.assertNotIn(self.prefix+'/timeline/final/posture.timeline.json',self.provider.objects)
        task=self.mutate('submit',self.b,task,annotations=self.annotation())
        self.assertEqual('李四',json.loads(self.data('李四/v1/posture.timeline.json'))['task_result']['annotator_name'])
        task=self.mutate('start_review',self.lead,task)
        task=self.mutate('review',self.lead,task,action='return',return_to=['a'],issues=[{'axis':'posture','start_ms':2000,'end_ms':2500,'reason':'确认姿势'}])
        task=self.mutate('submit',self.a,task,annotations=self.annotation())
        self.assertEqual(self.data('张三/v1/posture.timeline.json'),original)
        revised=json.loads(self.data('张三/v2/posture.timeline.json'))
        self.assertEqual(revised['task_result']['version'],2)
        self.assertEqual(revised['segments'][0]['label'],'站')
        self.assertEqual(revised['segments'][0]['start_ms'],2000)
        self.assertEqual(revised['timebase']['recording_start'],self.project['recording_start'])
        self.assertEqual(json.loads(self.data('张三/v2/scene.timeline.json'))['segments'],[])
        self.assertEqual(json.loads(self.data('张三/v2/food.timeline.json'))['axis'],'food')
        task=self.mutate('start_review',self.lead,task)
        task=self.mutate('review',self.lead,task,action='pass',resolved_issue_ids=[issue['id'] for issue in task['issues']])
        self.assertNotIn(self.prefix+'/timeline/final/posture.timeline.json',self.provider.objects)
        task=self.mutate('accept',self.lead,task)
        final=json.loads(self.data('final/posture.timeline.json'))
        self.assertEqual(final['segments'],revised['segments'])
        self.assertEqual(final['task_result']['accepted_by'],'lead')
        delivery=self.deliver(task)
        rows=list(csv.DictReader(io.StringIO(self.data('final/delivery-'+delivery['id']+'.csv').decode('utf-8-sig'))))
        self.assertEqual(len(rows),1)
        self.assertEqual(rows[0]['accepted_by_name'],'组长')
        self.assertEqual(set(rows[0]['assigned_annotator_names'].split('|')),{'张三','李四'})
        versions=json.loads(rows[0]['submission_versions'])
        self.assertEqual([(v['id'],v['version']) for v in versions],[('a',1),('b',1),('a',2)])
        with self.store.db() as db:self.assertEqual(self.outputs.status(db,task['id'])['state'],'ready')
        self.assertTrue(any(key.startswith('tasks/tasks/'+task['id']+'/final/') for key in self.provider.objects))
        self.assertIn('tasks/deliveries/'+delivery['id']+'.csv',self.provider.objects)
        self.assertFalse(any('/timeline/finally/' in key for key in self.provider.objects))
        self.assertNotIn(self.prefix+'/timeline/delivery-'+delivery['id']+'.csv',self.provider.objects)

    def legacy_output_layout(self):
        """Model an existing database/bucket written by the previous release."""
        prefix = self.prefix+'/timeline'
        for key in list(self.provider.objects):
            if key.startswith(prefix+'/final/'):
                name = key.removeprefix(prefix+'/final/')
                legacy = prefix+'/'+name if name.endswith('.csv') else prefix+'/finally/'+name
                self.provider.objects[legacy] = self.provider.objects.pop(key)
        with self.store.db() as db:
            for ident,kind,raw in db.execute(
                    "SELECT id,kind,body FROM capture_output_jobs WHERE kind IN ('final','delivery')").fetchall():
                body = json.loads(raw)
                body['directory'] = prefix+'/finally' if kind == 'final' else prefix
                db.execute('UPDATE capture_output_jobs SET body=? WHERE id=?',(json.dumps(body),ident))

    def test_legacy_ready_results_requeue_without_writes_and_retry_same_payloads(self):
        task = self.approved()
        delivery = self.deliver(task)
        self.legacy_output_layout()
        original = copy.deepcopy(self.provider.objects)
        with self.store.db() as db:
            versions = db.execute('SELECT * FROM capture_output_people').fetchall()
            audit = db.execute('SELECT * FROM audit').fetchall()
        restarted = CollectionOutputs(self.store,self.media,self.editor)
        self.assertEqual(self.provider.objects,original)
        with self.store.db() as db:
            self.assertEqual(restarted.status(db,task['id'])['state'],'pending')
        put = self.provider.put
        def interrupted(key,payload,**options):
            if '/timeline/final/' in key and key.endswith('.csv'):
                raise HTTPException(503,'CSV unavailable fixture')
            return put(key,payload,**options)
        with patch.object(self.provider,'put',side_effect=interrupted):
            self.assertEqual(restarted.flush(task['id'])['state'],'error')
        restarted = CollectionOutputs(self.store,self.media,self.editor)
        self.assertEqual(restarted.backfill(self.lead,task['id'],self.names)['state'],'ready')
        for key,record in original.items():
            self.assertEqual(self.provider.objects[key],record)
        for name in ('scene.timeline.json','posture.timeline.json','food.timeline.json'):
            self.assertEqual(self.data('final/'+name),self.data('finally/'+name))
        self.assertEqual(self.data('final/delivery-'+delivery['id']+'.csv'),
                         self.data('delivery-'+delivery['id']+'.csv'))
        with self.store.db() as db:
            self.assertEqual(db.execute('SELECT * FROM capture_output_people').fetchall(),versions)
            self.assertEqual(db.execute('SELECT * FROM audit').fetchall(),audit)
        generation = self.provider.generation
        self.assertEqual(CollectionOutputs(self.store,self.media,self.editor).flush(task['id'])['state'],'ready')
        self.assertEqual(self.provider.generation,generation)

    def test_legacy_failed_final_retargets_and_keeps_external_old_file(self):
        task = self.approved()
        self.legacy_output_layout()
        old = self.prefix+'/timeline/finally/posture.timeline.json'
        self.provider.objects[old] = (b'{"external":"keep old path"}','external-version')
        with self.store.db() as db:
            db.execute("UPDATE capture_output_jobs SET state='error',error='old conflict' WHERE kind='final'")
        restarted = CollectionOutputs(self.store,self.media,self.editor)
        self.assertEqual(restarted.flush(task['id'])['state'],'ready')
        self.assertEqual(self.provider.get(old),b'{"external":"keep old path"}')
        self.assertEqual(json.loads(self.data('final/posture.timeline.json'))['segments'][0]['label'],'站')

    def test_reserved_person_names_do_not_share_final_directory(self):
        self.names.update(a='FiNaL',b='finally')
        task = self.approved()
        self.deliver(task)
        with self.store.db() as db:
            people = db.execute('SELECT folder FROM capture_output_people').fetchall()
            self.assertEqual(self.outputs.status(db,task['id'])['state'],'ready')
        self.assertEqual(len(people),2)
        for folder, in people:
            self.assertNotIn(folder.casefold(),('final','finally'))
            self.assertIn(self.prefix+'/timeline/'+folder+'/v1/posture.timeline.json',self.provider.objects)
        self.assertIn(self.prefix+'/timeline/final/posture.timeline.json',self.provider.objects)

    def test_historical_person_bound_to_final_blocks_mixed_result_writes(self):
        with self.store.db() as db:
            db.execute('INSERT INTO capture_output_people VALUES (?,?,?,?,0)',
                       (self.prefix+'/timeline','a','final','final'))
        task = self.approved()
        self.assertEqual(task['status'],'accepted')
        self.assertIn(self.prefix+'/timeline/final/v1/posture.timeline.json',self.provider.objects)
        self.assertNotIn(self.prefix+'/timeline/final/posture.timeline.json',self.provider.objects)
        with self.store.db() as db:
            status = self.outputs.status(db,task['id'])
        self.assertEqual(status['state'],'error')
        self.assertIn('历史标注员',status['detail'])

    def test_unknown_csv_in_final_is_not_overwritten(self):
        task = self.approved()
        result = self.store.deliver(self.lead,{'task_ids':[task['id']],
            'expected_revisions':{task['id']:task['revision']},'request_id':'unknown-csv'},output_names=self.names)
        key = self.prefix+'/timeline/final/delivery-'+result['id']+'.csv'
        self.provider.put(key,b'external csv',create=True)
        self.assertEqual(self.outputs.flush(task['id'])['state'],'error')
        self.assertEqual(self.provider.get(key),b'external csv')

    def test_failed_s3_write_preserves_submission_and_retry_reuses_version(self):
        original=self.provider.put_if_absent
        def fail_once(key,data):
            if '/timeline/' in key:raise HTTPException(503,'S3 暂时不可写')
            return original(key,data)
        with patch.object(self.provider,'put_if_absent',side_effect=fail_once):
            task=self.mutate('submit',self.a,self.task,annotations=self.annotation())
        self.assertEqual(self.store.get_task(self.a,task['id'])['submitted_count'],1)
        self.assertEqual(self.store.get_task(self.a,task['id'])['output_sync']['state'],'error')
        self.assertEqual(self.outputs.backfill(self.a,task['id'],self.names)['state'],'ready')
        self.assertEqual(json.loads(self.data('张三/v1/posture.timeline.json'))['task_result']['version'],1)
        self.assertFalse(any('/张三/v2/' in key for key in self.provider.objects))

    def test_same_name_and_rename_never_mix_people_or_move_previous_versions(self):
        self.names['b']='张三'
        task=self.mutate('submit',self.a,self.task,annotations=self.annotation())
        task=self.mutate('submit',self.b,task,annotations=self.annotation())
        folders={key.split('/timeline/')[1].split('/')[0] for key in self.provider.objects if '/timeline/' in key}
        self.assertEqual(len(folders),2)
        self.assertIn('张三',folders)
        task=self.mutate('start_review',self.lead,task)
        task=self.mutate('review',self.lead,task,action='return',return_to=['a'],issues=[{'axis':'posture','start_ms':2000,'end_ms':2500,'reason':'检查'}])
        self.names['a']='已改名'
        self.mutate('submit',self.a,task,annotations=self.annotation())
        self.assertIn(self.prefix+'/timeline/张三/v2/posture.timeline.json',self.provider.objects)
        self.assertFalse(any('/已改名/' in key for key in self.provider.objects))

    def test_empty_final_has_attributable_csv_and_can_backfill_after_restart(self):
        task=self.approved(empty=True)
        delivery=self.deliver(task)
        rows=list(csv.DictReader(io.StringIO(self.data('final/delivery-'+delivery['id']+'.csv').decode('utf-8-sig'))))
        self.assertEqual(len(rows),1)
        self.assertEqual(rows[0]['task_id'],task['id'])
        self.assertEqual(rows[0]['accepted_by_name'],'组长')
        total = list(csv.DictReader(io.StringIO(self.store.artifacts.get(delivery['artifact_key']).decode('utf-8-sig'))))
        self.assertEqual(len(total), 1)
        self.assertEqual(total[0]['final_version_id'], rows[0]['final_version_id'])
        self.assertEqual(total[0]['video_id'], rows[0]['video_id'])
        for key in ('axis', 'segment_id', 'label', 'start_ms', 'end_ms'):
            self.assertEqual(total[0][key], '')
            self.assertEqual(rows[0][key], '')
        restarted=CollectionOutputs(self.store,self.media,self.editor)
        keys=set(self.provider.objects)
        self.assertEqual(restarted.backfill(self.lead,task['id'],self.names)['state'],'ready')
        self.assertEqual(keys,set(self.provider.objects))

    def test_existing_delivered_task_can_backfill_without_rewriting_audit_records(self):
        self.store.outputs=None
        task=self.approved()
        delivery=self.deliver(task)
        immutable=copy.deepcopy(self.provider.objects)
        self.store.outputs=self.outputs
        status=self.outputs.backfill(self.lead,task['id'],{})
        self.assertEqual(status['state'],'ready')
        self.assertIn(self.prefix+'/timeline/张三/v1/posture.timeline.json',self.provider.objects)
        self.assertIn(self.prefix+'/timeline/final/posture.timeline.json',self.provider.objects)
        self.assertIn(self.prefix+'/timeline/final/delivery-'+delivery['id']+'.csv',self.provider.objects)
        for key,record in immutable.items():self.assertEqual(self.provider.objects[key],record)

    def test_unknown_final_file_is_preserved_and_pending_job_survives_restart(self):
        task=self.mutate('submit',self.a,self.task,annotations=self.annotation())
        task=self.mutate('submit',self.b,task,annotations=self.annotation())
        task=self.mutate('start_review',self.lead,task)
        task=self.mutate('review',self.lead,task,action='pass')
        external=self.prefix+'/timeline/final/posture.timeline.json'
        self.provider.put(external,b'{"external":"preserve"}',create=True)
        task=self.mutate('accept',self.lead,task)
        self.assertEqual(task['status'],'accepted')
        self.assertEqual(self.provider.get(external),b'{"external":"preserve"}')
        with self.store.db() as db:self.assertEqual(self.outputs.status(db,task['id'])['state'],'error')
        restarted=CollectionOutputs(self.store,self.media,self.editor)
        self.assertEqual(restarted.flush(task['id'])['state'],'error')
        self.assertEqual(self.provider.get(external),b'{"external":"preserve"}')

    def test_deleted_pending_task_does_not_publish_after_recovery(self):
        original=self.provider.put_if_absent
        def offline(key,data):
            if '/timeline/' in key:raise HTTPException(503,'offline fixture')
            return original(key,data)
        with patch.object(self.provider,'put_if_absent',side_effect=offline):
            task=self.mutate('submit',self.a,self.task,annotations=self.annotation())
        self.store.delete_task(self.lead,task['id'],{'expected_revision':task['revision']})
        self.assertEqual(self.outputs.flush(task['id'])['state'],'error')
        self.assertFalse(any('/timeline/' in key for key in self.provider.objects))
        with self.assertRaises(HTTPException):self.outputs.backfill(self.lead,task['id'],self.names)

    def test_partial_final_publication_is_pending_and_retries_frozen_documents(self):
        task=self.mutate('submit',self.a,self.task,annotations=self.annotation())
        task=self.mutate('submit',self.b,task,annotations=self.annotation())
        task=self.mutate('start_review',self.lead,task)
        task=self.mutate('review',self.lead,task,action='pass')
        original=self.provider.put
        def interrupted(key,payload,**options):
            if key.endswith('/final/posture.timeline.json'):raise HTTPException(503,'interrupted fixture')
            return original(key,payload,**options)
        with patch.object(self.provider,'put',side_effect=interrupted):
            task=self.mutate('accept',self.lead,task)
        first=self.data('final/scene.timeline.json')
        self.assertNotIn(self.prefix+'/timeline/final/.datamark-final.json',self.provider.objects)
        with self.store.db() as db:self.assertEqual(self.outputs.status(db,task['id'])['state'],'error')
        restarted=CollectionOutputs(self.store,self.media,self.editor)
        self.assertEqual(restarted.flush(task['id'])['state'],'ready')
        self.assertEqual(self.data('final/scene.timeline.json'),first)
        completed=json.loads(self.data('final/.datamark-final.json'))
        self.assertEqual(set(completed['files']),{'scene.timeline.json','posture.timeline.json','food.timeline.json'})
