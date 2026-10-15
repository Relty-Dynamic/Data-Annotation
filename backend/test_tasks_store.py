import csv
import io
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from backend.tasks.store import TaskStore, TaskError
from backend.tasks.diff import differences


class Artifacts:
    def __init__(self):
        self.values = {}
        self.fail = False
    def put(self, key, value):
        if self.fail:
            raise OSError('unavailable')
        if key in self.values:
            raise ValueError('immutable')
        self.values[key] = value
    def get(self, key):
        return self.values[key]


class TasksTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.artifacts = Artifacts()
        self.store = TaskStore(Path(self.tmp.name), self.artifacts)
        self.admin = {'id':'admin','role':'admin','can_upload':True}
        self.lead = {'id':'lead','role':'annotator'}
        self.a = {'id':'a','role':'annotator'}
        self.b = {'id':'b','role':'annotator'}
        self.reviewer = {'id':'reviewer','role':'annotator'}
        for actor, roles in ((self.lead,['leader']), (self.a,['annotator']), (self.b,['annotator']), (self.reviewer,['reviewer'])):
            self.store.set_roles(self.admin, actor['id'], roles)
        self.rule = self.store.publish_rule(self.lead, {'name':'rule','task_type':'posture','axes':[{'id':'posture','name':'Posture','mode':'state','labels':['sit','bus'],'transport_labels':['bus'],'transport_types':['car']}]})
        self.store.register_video(self.admin, {'video_id':'v','collector_id':'collector','date':'2026-10-15','scene':'inside','duration_ms':10000,'source':{'backend':'nas','key':'v'},'quality':'passed'})

    def task(self, dual=False):
        return self.store.create_task(self.lead, {'video_id':'v','rule_id':self.rule['id'],'annotator_ids':['a','b'] if dual else ['a'],'reviewer_id':'reviewer','deadline':'2026-10-26T12:00:00+08:00'})

    def annotations(self, label='sit'):
        return {'posture':[{'id':'s','label':label,'start_ms':123,'end_ms':3000,'lane':0}, {'id':'t','label':'sit','start_ms':1000,'end_ms':2000,'lane':1}]}

    def mutate(self, method, actor, task, **body):
        return getattr(self.store,method)(actor,task['id'],{'expected_revision':task['revision'],**body})

    def expect(self, status, fn):
        with self.assertRaises(TaskError) as caught:
            fn()
        self.assertEqual(status,caught.exception.status)

    def test_db_commits_and_closes_connection(self):
        with self.store.db() as db:
            self.assertEqual(1, db.execute('PRAGMA foreign_keys').fetchone()[0])
            db.execute('INSERT INTO roles VALUES (?,?)', ('committed', '["annotator"]'))
        with self.assertRaisesRegex(sqlite3.ProgrammingError, 'closed'):
            db.execute('SELECT 1')
        self.assertEqual(['annotator'], self.store.roles({'id': 'committed'})['roles'])

    def test_db_rolls_back_and_closes_connection_on_error(self):
        with self.assertRaisesRegex(RuntimeError, 'abort transaction'):
            with self.store.db() as db:
                db.execute('UPDATE roles SET body=? WHERE user_id=?', ('["leader"]', self.a['id']))
                db.execute('INSERT INTO roles VALUES (?,?)', ('rolled-back', '["reviewer"]'))
                raise RuntimeError('abort transaction')
        with self.assertRaisesRegex(sqlite3.ProgrammingError, 'closed'):
            db.execute('SELECT 1')
        self.assertEqual(['annotator'], self.store.roles(self.a)['roles'])
        self.assertEqual([], self.store.roles({'id': 'rolled-back'})['roles'])

    def test_permissions_qc_and_unique(self):
        self.expect(422,lambda:self.store.publish_rule(self.admin,{}))
        self.expect(403,lambda:self.store.set_roles(self.a,'a',['leader']))
        self.store.set_quality(self.admin,'v','failed')
        self.expect(409,lambda:self.task())
        self.store.set_quality(self.admin,'v','passed')
        task=self.task()
        self.expect(409,lambda:self.task())
        self.expect(403,lambda:self.store.get_task(self.b,task['id']))
        self.expect(403,lambda:self.mutate('start_review',self.a,task))
        self.expect(409,lambda:self.store.register_video(self.admin,{'video_id':'v','collector_id':'c','date':'d','scene':'s','duration_ms':1,'source':{'backend':'nas','key':'v'}}))

    def test_admin_deletes_rule_without_breaking_bound_task_or_history(self):
        task = self.task()
        for actor in (self.lead, self.a, self.reviewer):
            self.expect(403, lambda: self.store.delete_rule(actor, self.rule['id']))
        self.store.delete_rule(self.admin, self.rule['id'])
        self.store.delete_rule(self.admin, self.rule['id'])
        self.assertEqual([], self.store.list_rules(self.lead))
        self.expect(409, lambda: self.task())
        self.assertEqual(self.rule, self.store.get_task(self.a, task['id'])['rule'])
        task = self.mutate('save_draft', self.a, task, annotations=self.annotations())
        task = self.mutate('submit', self.a, task, annotations=self.annotations())
        task = self.mutate('start_review', self.reviewer, task)
        task = self.mutate('review', self.reviewer, task, action='pass', issues=[])
        task = self.mutate('accept', self.lead, task)
        self.assertEqual('accepted', task['status'])
        self.assertEqual(self.rule, task['rule'])
        delivery = self.store.deliver(self.lead, {'task_ids': [task['id']],
            'expected_revisions': {task['id']: task['revision']}, 'request_id': 'deleted-rule-delivery'})
        rows = list(csv.DictReader(io.StringIO(self.artifacts.get(delivery['artifact_key']).decode('utf-8-sig'))))
        self.assertTrue(rows)
        self.assertTrue(all(row['rule_id'] == self.rule['id'] for row in rows))
        reopened = TaskStore(Path(self.tmp.name), self.artifacts)
        self.assertEqual([], reopened.list_rules(self.admin))
        self.assertEqual(self.rule, reopened.get_task(self.lead, task['id'])['rule'])
        with self.store.db() as db:
            events = [json.loads(row[0]) for row in db.execute('SELECT body FROM audit')]
            deleted = [event for event in events if event['action'] == 'delete_rule']
            self.assertEqual(1, len(deleted))
            self.assertEqual(self.admin['id'], deleted[0]['actor_id'])
            self.assertTrue(deleted[0]['at'])
            for table in ('rules', 'deleted_rules'):
                with self.assertRaises(sqlite3.IntegrityError):
                    db.execute(f'DELETE FROM {table}')
        self.expect(404, lambda: self.store.delete_rule(self.admin, 'missing'))

    def test_invalid_rework_targets_and_contributor_format_leave_task_unchanged(self):
        task = self.task()
        task = self.mutate('submit', self.a, task, annotations=self.annotations())
        task = self.mutate('start_review', self.reviewer, task)
        issue = {'axis': 'posture', 'start_ms': 123, 'end_ms': 3000, 'reason': 'check'}
        self.expect(422, lambda: self.mutate('review', self.reviewer, task,
                    action='return', return_to=['a', 'a'], issues=[issue]))
        self.assertEqual(task['revision'], self.store.get_task(self.reviewer, task['id'])['revision'])
        task = self.mutate('review', self.reviewer, task, issues=[issue])
        self.expect(422, lambda: self.mutate('adjudicate', self.reviewer, task,
                    annotations=self.annotations(), contributors=None,
                    resolutions={i['id']: 'checked' for i in task['issues']}))
        self.assertEqual('adjudicating', self.store.get_task(self.reviewer, task['id'])['status'])

    def test_dual_privacy_immutable_and_revision(self):
        task=self.task(True)
        task=self.mutate('submit',self.a,task,annotations=self.annotations())
        self.assertEqual('annotating',task['status'])
        self.assertEqual(0,len(self.store.get_task(self.b,task['id'])['submissions']))
        self.assertEqual(0,len(self.store.get_task(self.lead,task['id'])['submissions']))
        self.assertEqual(0,len(self.store.get_task(self.reviewer,task['id'])['submissions']))
        self.expect(409,lambda:self.store.save_draft(self.b,task['id'],{'expected_revision':0,'annotations':{}}))
        task=self.mutate('submit',self.b,task,annotations=self.annotations())
        self.assertEqual(2,len(task['submissions']))
        self.expect(403,lambda:self.mutate('save_draft',self.a,task,annotations={}))
        with self.store.db() as db:
            for table in ('rules','submissions','audit'):
                with self.assertRaises(sqlite3.IntegrityError):
                    db.execute(f'UPDATE {table} SET body="{{}}"')

    def test_validation_allows_gaps_overlap_requires_fields(self):
        task=self.task()
        task=self.mutate('save_draft',self.a,task,annotations=self.annotations())
        bad=self.annotations('bus')
        self.expect(422,lambda:self.mutate('submit',self.a,task,annotations=bad))
        bad['posture'][0]['transport_type']='car'
        bad['posture'][0]['needs_review']=True
        self.expect(422,lambda:self.mutate('submit',self.a,task,annotations=bad))
        bad['posture'][0]['review_reason']='uncertain'
        bad['posture'][0]['end_ms']=None
        self.expect(422,lambda:self.mutate('submit',self.a,task,annotations=bad))
        task=self.mutate('submit',self.a,task,annotations=self.annotations())
        self.assertEqual('submitted',task['status'])

    def test_single_review_delivery_atomic_idempotent(self):
        task=self.task()
        task=self.mutate('submit',self.a,task,annotations=self.annotations())
        task=self.mutate('start_review',self.reviewer,task)
        self.expect(409,lambda:self.mutate('accept',self.lead,task))
        task=self.mutate('review',self.reviewer,task)
        task=self.mutate('accept',self.lead,task)
        body={'task_ids':[task['id']],'expected_revisions':{task['id']:task['revision']},'request_id':'request'}
        self.artifacts.fail=True
        with self.assertRaises(OSError):
            self.store.deliver(self.lead,body)
        self.assertEqual('accepted',self.store.get_task(self.lead,task['id'])['status'])
        self.artifacts.fail=False
        result=self.store.deliver(self.lead,body)
        self.assertEqual(result,self.store.deliver(self.lead,body))
        content=self.artifacts.get(result['artifact_key']).decode('utf-8-sig')
        rows=list(csv.DictReader(io.StringIO(content)))
        self.assertEqual(2,len(rows))
        self.assertEqual('123',rows[0]['start_ms'])
        self.assertEqual('collector',rows[0]['collector_id'])
        self.assertEqual('admin',rows[0]['uploader_id'])
        self.assertTrue(rows[0]['final_version_id'])
        self.assertEqual('0.1',rows[0]['start_seconds'])
        self.assertEqual('delivered',self.store.get_task(self.lead,task['id'])['status'])
        self.assertEqual(result,self.store.delivery(self.lead,result['id']))
        self.artifacts.values[result['artifact_key']]=b'corrupted'
        self.expect(503,lambda:self.store.delivery(self.lead,result['id']))

    def test_return_then_adjudicate_preserves_versions(self):
        task=self.task(True)
        task=self.mutate('submit',self.a,task,annotations=self.annotations())
        task=self.mutate('submit',self.b,task,annotations={})
        task=self.mutate('start_review',self.reviewer,task)
        task=self.mutate('review',self.reviewer,task,issues=[{'axis':'posture','segment_id':'s','reason':'check'}],return_to=['a'])
        self.assertEqual('rework',task['status'])
        self.assertEqual(self.annotations(),self.store.get_task(self.a,task['id'])['draft']['annotations'])
        task=self.mutate('submit',self.a,task,annotations=self.annotations())
        task=self.mutate('start_review',self.reviewer,task)
        task=self.mutate('review',self.reviewer,task)
        self.assertEqual('adjudicating',task['status'])
        self.expect(403,lambda:self.mutate('adjudicate',self.a,task,annotations={}))
        resolutions={i['id']:'confirmed' for i in task['issues']}
        task=self.mutate('adjudicate',self.reviewer,task,annotations=self.annotations(),resolutions=resolutions,contributors={'posture:s':['a'],'posture:t':['a']})
        task=self.mutate('accept',self.lead,task)
        with self.store.db() as db:
            self.assertEqual(3,db.execute("SELECT COUNT(*) FROM submissions WHERE json_extract(body,'$.type') IS NULL").fetchone()[0])
            self.assertEqual(1,db.execute("SELECT COUNT(*) FROM submissions WHERE json_extract(body,'$.type')='final'").fetchone()[0])
        self.assertEqual('accepted',task['status'])

    def test_no_self_review_even_with_all_roles_and_rules_snapshot(self):
        self.store.set_roles(self.admin,'a',['annotator','leader','reviewer'])
        task=self.task()
        task=self.mutate('submit',self.a,task,annotations={})
        self.expect(403,lambda:self.mutate('start_review',self.a,task))
        newer=self.store.publish_rule(self.lead,{'name':'new','task_type':'posture','axes':[{'id':'posture','name':'New','mode':'state','labels':['new']}]})
        self.assertNotEqual(newer['id'],task['rule']['id'])
        self.assertEqual(['sit','bus'],task['rule']['axes'][0]['labels'])

    def test_pending_assignment_revoked_role_and_failed_quality(self):
        pending=self.store.create_task(self.lead,{'video_id':'v','rule_id':self.rule['id'],'deadline':'2026-10-26'})
        self.assertEqual('pending',pending['status'])
        task=self.mutate('assign_task',self.lead,pending,annotator_ids=['a'],reviewer_id='reviewer')
        self.store.set_roles(self.admin,'a',[])
        self.expect(403,lambda:self.store.get_task(self.a,task['id']))
        self.expect(403,lambda:self.store.history(self.a,task['id']))
        self.assertEqual([],self.store.list_tasks(self.a))
        self.expect(403,lambda:self.mutate('submit',self.a,task,annotations={}))
        self.store.set_roles(self.admin,'a',['annotator'])
        self.store.set_quality(self.admin,'v','failed')
        self.expect(409,lambda:self.mutate('submit',self.a,task,annotations={}))

    def test_differences_semantic_lanes_and_tolerance(self):
        one={'a':[{'id':'x','label':'sit','start_ms':0,'end_ms':1000,'lane':0}]}
        split={'a':[{'id':'y','label':'sit','start_ms':0,'end_ms':500,'lane':8},{'id':'z','label':'sit','start_ms':500,'end_ms':1000,'lane':2}]}
        self.assertEqual([],differences(one,split))
        shifted={'a':[{'id':'x','label':'sit','start_ms':1,'end_ms':1001,'lane':1}]}
        self.assertEqual([],differences(one,shifted,2))
        self.assertTrue(differences(one,shifted))

    def test_minimum_unclosed_and_final_artifact_failure(self):
        rule=self.store.publish_rule(self.lead,{'name':'short','task_type':'short','axes':[{'id':'a','name':'a','mode':'event','labels':['x'],'minimum_ms':1000}]})
        task=self.store.create_task(self.lead,{'video_id':'v','rule_id':rule['id'],'annotator_ids':['a'],'reviewer_id':'reviewer','deadline':'2026-10-26'})
        anns={'a':[{'id':'x','label':'x','start_ms':0,'end_ms':100}]}
        self.expect(422,lambda:self.mutate('submit',self.a,task,annotations=anns))
        anns['a'][0].update(kind='instant',end_ms=0)
        task=self.mutate('submit',self.a,task,annotations=anns)
        task=self.mutate('start_review',self.reviewer,task)
        task=self.mutate('review',self.reviewer,task)
        self.artifacts.fail=True
        with self.assertRaises(OSError):
            self.mutate('accept',self.lead,task)
        unchanged=self.store.get_task(self.lead,task['id'])
        self.assertEqual(task['revision'],unchanged['revision'])
        self.assertEqual('reviewing',unchanged['status'])

    def test_submit_replay_and_blob_tamper(self):
        task=self.task()
        body={'expected_revision':task['revision'],'annotations':self.annotations(),'request_id':'s1'}
        submitted=self.store.submit(self.a,task['id'],body)
        replay=self.store.submit(self.a,task['id'],body)
        self.assertEqual(submitted['revision'],replay['revision'])
        self.expect(409,lambda:self.store.submit(self.a,task['id'],{**body,'annotations':{}}))
        key=submitted['submissions'][0]['artifact_key']
        self.artifacts.values[key]=b'{}'
        self.expect(503,lambda:self.store.get_task(self.a,task['id']))

    def test_revoked_quality_blocks_review_and_uploader_list(self):
        task=self.task()
        task=self.mutate('submit',self.a,task,annotations={})
        self.store.set_quality(self.admin,'v','failed')
        self.expect(409,lambda:self.mutate('start_review',self.reviewer,task))
        self.assertEqual(1,len(self.store.list_videos(self.admin)))
        self.expect(403,lambda:self.store.list_videos(self.a))

    def test_history_private_round_and_immutable_final(self):
        task=self.task(True)
        task=self.mutate('submit',self.a,task,annotations=self.annotations())
        self.assertEqual(1,len(self.store.history(self.a,task['id'])['submissions']))
        self.assertEqual([],self.store.history(self.b,task['id'])['submissions'])
        self.assertEqual([],self.store.history(self.lead,task['id'])['submissions'])
        task=self.mutate('submit',self.b,task,annotations={})
        task=self.mutate('start_review',self.reviewer,task)
        task=self.mutate('review',self.reviewer,task)
        task=self.mutate('adjudicate',self.reviewer,task,annotations={},resolutions={i['id']:'empty confirmed' for i in task['issues']},contributors={})
        self.assertEqual([],self.store.history(self.a,task['id'])['finals'])
        self.assertNotIn('final_annotations',self.store.get_task(self.a,task['id']))
        self.assertEqual(1,len(self.store.history(self.lead,task['id'])['finals']))
        task=self.mutate('accept',self.lead,task)
        self.assertEqual(1,len(self.store.history(self.a,task['id'])['finals']))

    def test_structural_validation_and_incomplete_draft(self):
        task=self.task()
        valid={'posture':[{'id':'s','label':'sit（note）','start_ms':0,'end_ms':None}]}
        task=self.mutate('save_draft',self.a,task,annotations=valid)
        self.expect(422,lambda:self.mutate('submit',self.a,task,annotations=valid))
        for fields in ({'kind':'mystery'}, {'start_ms':0.5}, {'start_ms':0,'end_ms':0}, {'kind':'point','start_ms':0,'end_ms':0}):
            invalid={'posture':[{**valid['posture'][0],'end_ms':10,**fields}]}
            self.expect(422,lambda:self.mutate('save_draft',self.a,task,annotations=invalid))
        same_lane=self.annotations()
        same_lane['posture'][1]['lane']=0
        self.expect(422,lambda:self.mutate('submit',self.a,task,annotations=same_lane))
        valid['posture'][0]['end_ms']=1000
        task=self.mutate('submit',self.a,task,annotations=valid)
        self.assertEqual('sit（note）',task['submissions'][0]['annotations']['posture'][0]['label'])

    def test_invalid_rule_and_review_issue_no_mutation(self):
        base={'name':'bad','task_type':'bad','axes':[{'id':'a','name':'a','mode':'state','labels':['x']}]}
        for overrides in ({'labels':['x','x']},{'minimum_ms':True},{'transport_labels':['x'],'transport_types':[]}):
            body={**base,'axes':[{**base['axes'][0],**overrides}]}
            self.expect(422,lambda:self.store.publish_rule(self.lead,body))
        for duration in (True,'100',float('nan'),0,1.5):
            self.expect(422,lambda:self.store.register_video(self.admin,{'video_id':'bad','collector_id':'c','date':'d','scene':'s','duration_ms':duration,'source':{'backend':'nas','key':'v'}}))
        task=self.task()
        task=self.mutate('submit',self.a,task,annotations=self.annotations())
        task=self.mutate('start_review',self.reviewer,task)
        for body in ({'action':'return','return_to':[]},{'issues':[{'axis':'wrong','segment_id':'s','reason':'x'}]},{'issues':[{'axis':'posture','segment_id':'missing','reason':'x'}]},{'issues':[{'axis':'posture','start_ms':0,'end_ms':20000,'reason':'x'}]}):
            self.expect(422,lambda:self.mutate('review',self.reviewer,task,**body))
            self.assertEqual(task['revision'],self.store.get_task(self.reviewer,task['id'])['revision'])

    def test_quality_recheck_adjudication_accept_and_delivery(self):
        task=self.task(True)
        task=self.mutate('submit',self.a,task,annotations=self.annotations())
        task=self.mutate('submit',self.b,task,annotations={})
        task=self.mutate('start_review',self.reviewer,task)
        self.store.set_quality(self.admin,'v','failed')
        self.expect(409,lambda:self.mutate('review',self.reviewer,task))
        self.store.set_quality(self.admin,'v','passed')
        task=self.mutate('review',self.reviewer,task)
        self.store.set_quality(self.admin,'v','failed')
        verdict={'annotations':{},'resolutions':{i['id']:'empty' for i in task['issues']},'contributors':{}}
        self.expect(409,lambda:self.mutate('adjudicate',self.reviewer,task,**verdict))
        self.store.set_quality(self.admin,'v','passed')
        task=self.mutate('adjudicate',self.reviewer,task,**verdict)
        self.store.set_quality(self.admin,'v','failed')
        self.expect(409,lambda:self.mutate('accept',self.lead,task))
        self.store.set_quality(self.admin,'v','passed')
        task=self.mutate('accept',self.lead,task)
        self.store.set_quality(self.admin,'v','failed')
        self.expect(409,lambda:self.store.deliver(self.lead,{'task_ids':[task['id']],'expected_revisions':{task['id']:task['revision']},'request_id':'q'}))
        self.assertEqual('accepted',self.store.get_task(self.lead,task['id'])['status'])

    def test_flagged_range_can_be_returned_from_adjudication(self):
        task = self.task()
        annotations = self.annotations()
        annotations['posture'][0].update(needs_review=True, review_reason='请复核')
        task = self.mutate('submit', self.a, task, annotations=annotations)
        task = self.mutate('start_review', self.reviewer, task)
        flag = next(issue for issue in task['issues'] if issue['id'].startswith('flag:'))
        self.assertEqual(annotations['posture'][0]['start_ms'], flag['start_ms'])
        self.assertEqual(annotations['posture'][0]['end_ms'], flag['end_ms'])
        task = self.mutate('review', self.reviewer, task, action='pass')
        self.assertEqual(task['status'], 'adjudicating')
        task = self.mutate('review', self.reviewer, task, action='return', return_to=[self.a['id']],
                           issues=[{'axis':'posture', 'start_ms':flag['start_ms'], 'end_ms':flag['end_ms'], 'reason':'请调整'}])
        self.assertEqual(task['status'], 'rework')

    def test_rules_always_shared_editor_compatible(self):
        base={'name':' Rule ','task_type':'new','axes':[{'id':'custom_valid','name':' Axis ','mode':'state','labels':[{'name':' x '}]}]}
        rule=self.store.publish_rule(self.lead,base)
        self.assertEqual('Rule',rule['name'])
        self.assertEqual('Axis',rule['axes'][0]['name'])
        self.assertEqual(['x'],rule['axes'][0]['labels'])
        for overrides in ({'id':'../bad'}, {'mode':'overlay'}, {'labels':['x']*33}, {'name':'x'*81}, {'labels':['x'*81]}, {'id':'posture','mode':'event'}, {'id':'habit','mode':'state'}):
            self.expect(422,lambda:self.store.publish_rule(self.lead,{**base,'axes':[{**base['axes'][0],**overrides}]}))
        many=[{'id':f'custom_{i}','name':f'axis {i}','mode':'state','labels':['x']} for i in range(17)]
        self.expect(422,lambda:self.store.publish_rule(self.lead,{**base,'axes':many}))

    def test_superadmin_inherits_roles_and_only_superadmin_can_adjudicate_own_work(self):
        self.assertEqual(['annotator', 'leader', 'reviewer'], self.store.roles(self.admin)['roles'])
        rule = self.store.publish_rule(self.admin, {'name': 'Admin rule', 'task_type': 'admin-type',
            'axes': [{'id': 'posture', 'name': 'Posture', 'mode': 'state', 'labels': ['sit']}]})
        body = {'video_id': 'v', 'rule_id': rule['id'],
            'annotator_ids': ['admin'], 'reviewer_id': 'reviewer', 'deadline': '2026-10-26'}
        task = self.store.create_task(self.admin, body,
                    participants={'admin': self.admin, 'reviewer': self.reviewer})
        task = self.mutate('save_draft', self.admin, task, annotations=self.annotations())
        annotations = self.annotations()
        annotations['posture'][0].update(needs_review=True, review_reason='check')
        task = self.mutate('submit', self.admin, task, annotations=annotations)
        self.expect(403, lambda: self.mutate('submit', self.admin, task, annotations=annotations))
        task = self.mutate('start_review', self.admin, task)
        task = self.mutate('review', self.admin, task)
        self.assertEqual('admin', task['reviewed_by'])
        self.expect(409, lambda: self.mutate('accept', self.lead, task))
        task = self.mutate('adjudicate', self.admin, task, annotations=annotations,
            contributors={'posture:'+s['id']: ['admin'] for s in annotations['posture']},
            resolutions={i['id']: 'confirmed' for i in task['issues']})
        self.assertEqual('admin', task['adjudicated_by'])
        self.assertEqual(1, len(self.store.history(self.admin, task['id'])['finals']))
        self.expect(403, lambda: self.mutate('accept', self.admin, task))
        task = self.mutate('accept', self.lead, task)
        self.assertEqual('accepted', task['status'])
        self.store.set_roles(self.admin, 'a', ['annotator', 'leader'])
        own = self.task()
        own = self.mutate('submit', self.a, own, annotations=annotations)
        self.expect(403, lambda: self.mutate('start_review', self.a, own))
        own = self.mutate('start_review', self.reviewer, own)
        self.expect(403, lambda: self.mutate('review', self.a, own))
        own = self.mutate('review', self.reviewer, own)
        self.expect(403, lambda: self.mutate('adjudicate', self.a, own,
            annotations=annotations, contributors={}, resolutions={i['id']: 'confirmed' for i in own['issues']}))

    def test_delete_task_roles_revision_history_and_recreation(self):
        for manager in (self.admin, self.lead):
            task = self.task()
            task = self.mutate('save_draft', self.a, task, annotations=self.annotations())
            task = self.mutate('submit', self.a, task)
            before_history = self.store.history(self.admin, task['id'])
            before_files = dict(self.artifacts.values)
            for actor in (self.a, self.reviewer):
                self.expect(403, lambda: self.mutate('delete_task', actor, task))
            self.expect(409, lambda: self.store.delete_task(manager, task['id'], {'expected_revision': 0}))
            deleted = self.mutate('delete_task', manager, task)
            self.assertEqual('deleted', deleted['status'])
            self.assertEqual([], self.store.list_tasks(self.admin))
            self.assertEqual(before_history, self.store.history(manager, task['id']))
            self.assertEqual(before_files, self.artifacts.values)
            self.assertEqual('deleted', self.store.get_task(manager, task['id'])['status'])
            self.assertEqual(manager['id'], self.store.get_task(manager, task['id'])['deleted_by'])
            self.expect(403, lambda: self.store.get_task(self.a, task['id']))
            self.expect(403, lambda: self.store.list_tasks(self.reviewer, include_deleted=True))
            self.expect(410, lambda: self.mutate('start_review', manager, deleted))
            self.expect(410, lambda: self.mutate('save_draft', manager, deleted, annotations={}))
            again = self.store.delete_task(manager, task['id'], {'expected_revision': task['revision']})
            self.assertEqual(deleted, again)
            audit = [e for e in self.store.audit(manager, task['id']) if e['action'] == 'delete_task']
            self.assertEqual(1, len(audit))
            self.assertEqual(manager['id'], audit[0]['actor_id'])
        self.assertEqual(2, len(self.store.list_tasks(self.admin, include_deleted=True)))
        replacement = self.task()
        self.assertNotEqual(task['id'], replacement['id'])
        self.assertEqual([replacement['id']], [t['id'] for t in self.store.list_tasks(self.a)])

    def test_delete_delivered_task_preserves_delivery_and_audit(self):
        task = self.task()
        task = self.mutate('submit', self.a, task, annotations=self.annotations())
        task = self.mutate('start_review', self.admin, task)
        task = self.mutate('review', self.admin, task, action='pass')
        task = self.mutate('accept', self.admin, task)
        delivery = self.store.deliver(self.admin, {'task_ids': [task['id']],
            'expected_revisions': {task['id']: task['revision']}, 'request_id': 'delete-delivered'})
        task = self.store.get_task(self.admin, task['id'])
        before = self.store.history(self.admin, task['id'])
        files = dict(self.artifacts.values)
        deleted = self.mutate('delete_task', self.lead, task)
        self.assertEqual(delivery, self.store.delivery(self.lead, delivery['id']))
        self.assertEqual(before, self.store.history(self.lead, task['id']))
        self.assertEqual(files, self.artifacts.values)
        for action in ('submit', 'start_review', 'review', 'adjudicate', 'accept', 'assign_task'):
            self.expect(410, lambda: self.mutate(action, self.admin, deleted))
        self.expect(409, lambda: self.store.deliver(self.admin, {'task_ids': [task['id']],
            'expected_revisions': {task['id']: deleted['revision']}, 'request_id': 'deleted-new-delivery'}))
        with self.store.db() as db:
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute('DELETE FROM audit WHERE task_id=?', (task['id'],))

    def test_passing_review_can_keep_location_notes_without_forcing_rework(self):
        task = self.task()
        task = self.mutate('submit', self.a, task, annotations=self.annotations())
        task = self.mutate('start_review', self.admin, task)
        note = {'axis': 'posture', 'start_ms': 123, 'end_ms': 3000, 'reason': 'Checked against video'}
        task = self.mutate('review', self.admin, task, action='pass', notes=[note])
        self.assertTrue(task['review_complete'])
        self.assertEqual('admin', task['review_notes'][0]['actor_id'])
        audit = [item for item in self.store.audit(self.admin, task['id']) if item['action'] == 'review']
        self.assertEqual([note], audit[0]['notes'])
        task = self.mutate('accept', self.admin, task)
        delivery = self.store.deliver(self.admin, {'task_ids': [task['id']],
            'expected_revisions': {task['id']: task['revision']}, 'request_id': 'admin-delivery'})
        self.assertEqual('admin', delivery['created_by'])


if __name__=='__main__':
    unittest.main()
