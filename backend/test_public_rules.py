"""Fixed-target rule proxy and immutable snapshots in independent NAS tasks."""
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

import httpx
from fastapi import HTTPException

from backend.tasks.public_rules import PublicRules, PUBLIC_API, PUBLIC_HOST, PUBLIC_ORIGIN
from backend.tasks.store import TaskStore, TaskError
from backend.test_tasks_store import Artifacts


class PublicRuleClientTests(unittest.TestCase):
    def setUp(self):
        self.request = SimpleNamespace(cookies=dict(datamark_session='fixture-session', datamark_csrf='fixture-csrf',
            unrelated='must-not-forward'), headers={'x-csrf-token': 'fixture-csrf', 'authorization': 'must-not-forward'})

    def test_only_rules_and_the_callers_own_session_are_forwarded(self):
        calls = []
        def handler(request):
            calls.append(request)
            return httpx.Response(200, json=[] if request.method == 'GET' else {'id': 'a' * 32})
        client = PublicRules(transport=httpx.MockTransport(handler))
        client.request(self.request)
        client.request(self.request, 'POST', body={'name': 'rule'})
        client.request(self.request, 'DELETE', rule_id='a' * 32)
        for request in calls:
            self.assertTrue(str(request.url).startswith(PUBLIC_API + '/api/tasks/rules'))
            self.assertEqual(PUBLIC_HOST, request.headers['host'])
            self.assertEqual(PUBLIC_ORIGIN, request.headers['origin'])
            self.assertNotIn('authorization', request.headers)
            self.assertNotIn('unrelated', request.headers['cookie'])
            self.assertIn('datamark_session=fixture-session', request.headers['cookie'])
        self.assertNotIn('x-csrf-token', calls[0].headers)
        self.assertEqual('fixture-csrf', calls[1].headers['x-csrf-token'])
        self.assertEqual({'name': 'rule'}, json.loads(calls[1].content))
        with self.assertRaises(HTTPException) as error:
            client.request(self.request, 'DELETE', rule_id='../items')
        self.assertEqual(404, error.exception.status_code)
        self.assertEqual(3, len(calls))

    def test_upstream_permissions_and_missing_session_are_not_bypassed(self):
        for status in (401, 403, 404, 409, 422):
            with self.subTest(status=status):
                client = PublicRules(transport=httpx.MockTransport(lambda _: httpx.Response(status, json={'detail': 'rejected'})))
                with self.assertRaises(HTTPException) as error:
                    client.request(self.request)
                self.assertEqual(status, error.exception.status_code)
        with self.assertRaises(HTTPException) as error:
            client.request(SimpleNamespace(cookies={}, headers={}))
        self.assertEqual(401, error.exception.status_code)

    def test_failed_or_unconfirmed_writes_are_not_retried_or_reported_successful(self):
        calls = []
        def interrupted(request):
            calls.append(request)
            raise httpx.ReadTimeout('injected interruption')
        client = PublicRules(transport=httpx.MockTransport(interrupted))
        with self.assertRaises(HTTPException) as error:
            client.request(self.request, 'POST', body={'name': 'rule'})
        self.assertEqual(503, error.exception.status_code)
        self.assertIn('核对', error.exception.detail)
        self.assertEqual(1, len(calls))
        for response in (httpx.Response(302, headers={'Location': 'https://outside.example'}),
                         httpx.Response(500, json={'detail': 'private error'}),
                         httpx.Response(200, content=b'invalid JSON'), httpx.Response(200, json={})):
            with self.subTest(status=response.status_code):
                client = PublicRules(transport=httpx.MockTransport(lambda _: response))
                with self.assertRaises(HTTPException):
                    client.request(self.request)

    def test_task_rule_must_be_in_the_current_public_list(self):
        rule = {'id': 'a' * 32, 'name': 'public rule'}
        client = PublicRules(transport=httpx.MockTransport(lambda _: httpx.Response(200, json=[rule])))
        self.assertEqual(rule, client.for_task(self.request, rule['id']))
        with self.assertRaises(HTTPException) as error:
            client.for_task(self.request, 'b' * 32)
        self.assertEqual(409, error.exception.status_code)


class PublicRuleSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.admin = dict(id='admin', role='admin', can_upload=True)
        self.artifacts = Artifacts()
        self.public = TaskStore(self.root / 'public', Artifacts())
        self.nas = TaskStore(self.root / 'nas', self.artifacts)
        self.old_rule = self.publish(self.nas, 'old NAS')
        self.rule = self.publish(self.public, 'public rule')
        for store in (self.nas, self.public):
            store.register_video(self.admin, dict(id='v', collector_id='c', date='2026-10-19', scene='inside',
                quality='passed', duration_ms=10000, source=dict(backend='nas', key='v')))
        self.old_task = self.create(self.nas, self.old_rule)
        self.nas = TaskStore(self.root / 'nas', self.artifacts, public_rules=True)

    def publish(self, store, name):
        return store.publish_rule(self.admin, dict(name=name, task_type=name,
            axes=[dict(id='posture', name='姿势', mode='state', labels=['坐'])]))

    def create(self, store, rule, **kwargs):
        return store.create_task(self.admin, dict(video_id='v', rule_id=rule['id'], deadline='2026-10-26',
            annotator_ids=['admin'], reviewer_id='admin'), **kwargs)

    def test_no_local_fallback_or_local_rule_writes_after_enabling_public_source(self):
        for operation in (lambda: self.nas.list_rules(self.admin), lambda: self.publish(self.nas, 'forbidden'),
                          lambda: self.nas.delete_rule(self.admin, self.old_rule['id']),
                          lambda: self.create(self.nas, self.old_rule)):
            with self.assertRaises(TaskError) as error:
                operation()
            self.assertEqual(503, error.exception.status)
        self.assertEqual(self.old_rule, self.nas.get_task(self.admin, self.old_task['id'])['rule'])

    def test_new_rule_snapshot_survives_public_deletion_and_full_task_lifecycle(self):
        task = self.create(self.nas, self.rule, rule_snapshot=self.rule)
        self.public.delete_rule(self.admin, self.rule['id'])
        annotations = {'posture': [dict(id='s', label='坐', start_ms=0, end_ms=10000)]}
        for method, body in [('save_draft', dict(annotations=annotations)), ('submit', dict(annotations=annotations)),
                             ('start_review', {}), ('review', dict(action='pass', issues=[])), ('accept', {})]:
            task = getattr(self.nas, method)(self.admin, task['id'], dict(expected_revision=task['revision'], **body))
        self.assertEqual('accepted', task['status'])
        self.assertEqual(self.rule, task['rule'])
        self.nas.deliver(self.admin, dict(task_ids=[task['id']],
            expected_revisions={task['id']: task['revision']}, request_id='public-rule-delivery'))
        with self.assertRaises(TaskError):
            self.public.get_task(self.admin, task['id'])
        reopened = TaskStore(self.root / 'nas', self.artifacts, public_rules=True)
        self.assertEqual(self.rule, reopened.get_task(self.admin, task['id'])['rule'])

    def test_conflicting_rule_id_preserves_history_and_rolls_back_task_creation(self):
        collision = {**self.rule, 'name': 'different historical definition'}
        with self.nas.db() as db:
            self.nas._save(db, 'rules', collision, True)
        with self.assertRaises(TaskError) as error:
            self.create(self.nas, self.rule, rule_snapshot=self.rule)
        self.assertEqual(409, error.exception.status)
        with self.nas.db() as db:
            self.assertEqual(collision, self.nas._read(db, 'rules', self.rule['id']))
            self.assertEqual(1, db.execute('SELECT count(*) FROM tasks').fetchone()[0])
