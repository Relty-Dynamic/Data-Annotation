"""Optional task hosts share accounts and the editor without touching NAS drafts."""
import hashlib
import json
import subprocess
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException
from fastapi.testclient import TestClient

from backend.app import create_app
from backend.auth import AuthStore
from backend.public_setup import create_app as public_app
from backend.service import empty_annotations, now
from backend.shared_accounts import AccountClient, create_app as account_app
from backend.tasks.storage import FileArtifacts, ObjectArtifacts


class TaskHostTests(unittest.TestCase):
    def setUp(self):
        base = Path(__file__).resolve().parents[1] / '.tmp'
        base.mkdir(exist_ok=True)
        temporary = tempfile.TemporaryDirectory(prefix='tasks-api-', dir=base)
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.state = self.root / 'tasks'
        self.artifacts = self.root / 'artifacts'
        environment = patch.dict('os.environ', {
            'DATAMARK_TASKS_ENABLED': '1', 'DATAMARK_TASKS_STATE': str(self.state),
            'DATAMARK_TASKS_ARTIFACTS': str(self.artifacts),
            'DATAMARK_ORIGIN': 'https://nas.example.com',
            'DATAMARK_PUBLIC_ORIGIN': '', 'DATAMARK_PUBLIC_API_ORIGIN': '',
            'DATAMARK_S3_MOCK': '0', 'DATAMARK_STORAGE_BACKEND': 'nas',
        })
        environment.start()
        self.addCleanup(environment.stop)
        self.auth = AuthStore(self.root)
        self.users = {}
        for name in ('admin', 'lead', 'a', 'b', 'reviewer', 'inactive'):
            self.users[name] = self.auth.create_user('fixture-' + name, name, 'long fixture password',
                                                    'admin' if name == 'admin' else 'annotator')
        self.auth.set_active(self.users['inactive']['id'], False)

    def host(self):
        app = create_app(self.root, source_root=self.root / 'nas')
        client = TestClient(app, base_url='https://nas.example.com')
        client.__enter__()
        self.addCleanup(client.__exit__, None, None, None)
        return app, client

    def login(self, client, name, public=False):
        origin = 'https://web.example.com' if public else 'https://nas.example.com'
        response = client.post('/api/auth/login', json={'username': 'fixture-' + name, 'password': 'long fixture password'},
                               headers={'Origin': origin})
        self.assertEqual(response.status_code, 200, response.text)
        return {'X-CSRF-Token': response.json()['csrf'], 'Origin': origin}

    def request(self, client, method, path, body=None, headers=None, expected=200):
        response = client.request(method, '/api/tasks' + path, json=body, headers=headers)
        self.assertEqual(response.status_code, expected, response.text)
        return response.json()

    def initialize(self, app, client):
        headers = self.login(client, 'admin')
        for name, roles in [('lead', ['leader']), ('a', ['annotator']), ('b', ['annotator']), ('reviewer', ['reviewer'])]:
            self.request(client, 'PUT', '/roles', {'user_id': self.users[name]['id'], 'roles': roles}, headers)
        headers = self.login(client, 'lead')
        rule = self.request(client, 'POST', '/rules', {'name': 'posture', 'task_type': 'posture',
            'axes': [{'id': 'posture', 'name': '姿势', 'mode': 'state', 'labels': ['坐']}]}, headers)
        source = self.root / 'nas' / 'clip.mp4'
        source.parent.mkdir()
        source.write_bytes(b'isolated media identity fixture')
        stamp = {'size': source.stat().st_size, 'mtime_ns': source.stat().st_mtime_ns}
        self.project = {'id': 'a' * 32, 'name': 'fixture', 'recording_start': '2026-10-15T00:00:00+08:00',
            'duration_ms': 10000, 'revision': 0, 'updated_at': now(), 'annotations': empty_annotations(),
            'gaps': [], 'source_fingerprint': '0' * 64, 'source_dir': str(source.parent),
            'videos': [{'id': 'v0001', 'name': 'clip.mp4', 'relative_path': 'clip.mp4', 'duration_ms': 10000,
                'start_ms': 0, 'end_ms': 10000, 'original_media_start_ms': 0,
                'recording_start': '2026-10-15T00:00:00+08:00'}],
            '_sources': {'v0001': {'path': str(source), 'stamp': stamp}}}
        app.state.service.save(self.project)
        self.video = {'video_id': 'clip', 'collector_id': 'collector', 'date': '2026-10-15',
            'scene': 'inside', 'duration_ms': 10000, 'quality': 'passed',
            'source': {'backend': 'nas', 'key': self.project['id'] + '/v0001'}}
        headers = self.login(client, 'admin')
        self.request(client, 'POST', '/videos', self.video, headers)
        headers = self.login(client, 'lead')
        return self.request(client, 'POST', '/items', {'video_id': 'clip', 'rule_id': rule['id'],
            'annotator_ids': [self.users['a']['id'], self.users['b']['id']],
            'reviewer_id': self.users['reviewer']['id'], 'deadline': '2026-10-26'}, headers)

    def test_disabled_has_no_task_state_routes_or_capability(self):
        with patch.dict('os.environ', {'DATAMARK_TASKS_ENABLED': '0'}):
            app, client = self.host()
            self.assertFalse(hasattr(app.state, 'tasks'))
            self.assertEqual(client.get('/api/tasks/me').status_code, 404)
            self.assertNotIn('task-management', client.get('/api/health').json()['capabilities'])
            self.assertFalse(self.state.exists())
            self.assertFalse(self.artifacts.exists())

    def test_delete_published_rule_admin_only_csrf_and_bound_task_preserved(self):
        app, client = self.host()
        task = self.initialize(app, client)
        rule_id = task['rule_id']
        for name in ('lead', 'a', 'reviewer'):
            headers = self.login(client, name)
            self.request(client, 'DELETE', '/rules/' + rule_id, headers=headers, expected=403)
        headers = self.login(client, 'admin')
        self.request(client, 'DELETE', '/rules/' + rule_id, expected=403)
        self.assertTrue(self.request(client, 'GET', '/rules'))
        self.request(client, 'DELETE', '/rules/' + rule_id, headers=headers)
        self.request(client, 'DELETE', '/rules/' + rule_id, headers=headers)
        self.assertEqual([], self.request(client, 'GET', '/rules'))
        detail = self.request(client, 'GET', '/items/' + task['id'])
        self.assertEqual(rule_id, detail['rule']['id'])
        self.request(client, 'POST', '/items', {'video_id': 'clip', 'rule_id': rule_id,
            'annotator_ids': [], 'reviewer_id': None, 'deadline': '2026-10-26'}, headers=headers, expected=409)

    def test_auth_csrf_roster_and_roles_preserve_legacy_identity_with_upload_defaults(self):
        app, client = self.host()
        self.assertEqual(client.get('/api/tasks/me').status_code, 401)
        self.assertIn('task-management', client.get('/api/health').json()['capabilities'])
        original = self.auth.list_users()
        headers = self.login(client, 'admin')
        body = {'user_id': self.users['a']['id'], 'roles': ['leader']}
        self.request(client, 'PUT', '/roles', body, expected=403)
        self.request(client, 'PUT', '/roles', body, headers)
        self.request(client, 'PUT', '/roles', {**body, 'user_id': self.users['inactive']['id']}, headers, 422)
        roster = self.request(client, 'GET', '/users')
        self.assertEqual({'id', 'display_name', 'role', 'active', 'can_upload'}, set(roster[0]))
        self.assertEqual([{**item, 'can_upload': True} if item['id'] == self.users['a']['id'] else item
                          for item in original], self.auth.list_users())
        self.login(client, 'a')
        self.assertEqual(client.get('/api/users').status_code, 403)
        self.assertFalse(self.auth.allowed(self.users['a'], 'f' * 32))
        self.auth.set_active(self.users['a']['id'], False)
        self.assertEqual(client.get('/api/tasks/me').status_code, 401)

    def test_enabled_requires_authentication_and_explicit_absolute_paths(self):
        with self.assertRaises(ValueError):
            create_app(self.root, auth_required=False)
        self.assertFalse(self.state.exists())
        for settings in ({'DATAMARK_TASKS_STATE': ''}, {'DATAMARK_TASKS_ARTIFACTS': 'relative'}):
            with patch.dict('os.environ', settings):
                with self.assertRaises(ValueError):
                    create_app(self.root)
        self.assertFalse(self.state.exists())
        self.assertFalse(self.artifacts.exists())

    def test_email_account_creation_roles_upload_and_default_password(self):
        app, client = self.host()
        headers = self.login(client, 'admin')
        originals = self.auth.list_users()
        body = {'email': ' Li.Xin@Example.com ', 'display_name': '李鑫', 'roles': ['annotator', 'leader']}
        self.request(client, 'POST', '/accounts', {**body, 'roles': ['admin']}, headers, 422)
        self.request(client, 'POST', '/accounts', {**body, 'email': 'not-email'}, headers, 422)
        self.request(client, 'POST', '/accounts', body, expected=403)
        created = self.request(client, 'POST', '/accounts', body, headers)
        self.assertEqual('li.xin@example.com', created['username'])
        self.assertEqual('li.xin@example.com', created['email'])
        self.assertEqual(['annotator', 'leader'], created['task_roles'])
        self.assertTrue(created['can_upload'])
        self.request(client, 'POST', '/accounts', body, headers, 409)
        self.request(client, 'PUT', f"/accounts/{created['id']}/upload", {'can_upload': True}, headers)
        listed = self.request(client, 'GET', '/accounts')
        account = next(item for item in listed if item['id'] == created['id'])
        self.assertTrue(account['can_upload'])
        self.assertNotIn('password_hash', account)
        self.assertNotIn('password', account)
        response = client.post('/api/auth/login', json={'username': 'LI.XIN@example.com', 'password': '1234567890'},
                               headers={'Origin': 'https://nas.example.com'})
        self.assertEqual(200, response.status_code, response.text)
        self.assertEqual(['annotator', 'leader', 'reviewer'], self.request(client, 'GET', '/me')['roles'])
        self.request(client, 'GET', '/accounts', expected=403)
        self.request(client, 'POST', '/accounts', body, expected=403)
        # Creation did not change any existing account or legacy role.
        self.assertEqual(originals, [item for item in self.auth.list_users() if item['id'] != created['id']])

    def test_long_email_login_and_legacy_username_collision(self):
        app, client = self.host()
        headers = self.login(client, 'admin')
        self.auth.create_user('occupied@example.com', '旧账号', '1234567890')
        self.request(client, 'POST', '/accounts', {'email': 'OCCUPIED@example.com',
            'display_name': '重复账号', 'roles': ['annotator']}, headers, 409)
        email = 'worker@' + 'sub.' * 31 + 'example.com'
        created = self.request(client, 'POST', '/accounts', {'email': email,
            'display_name': '长邮箱', 'roles': ['reviewer']}, headers)
        response = client.post('/api/auth/login', json={'username': email, 'password': '1234567890'},
                               headers={'Origin': 'https://nas.example.com'})
        self.assertEqual(200, response.status_code, response.text)
        self.assertEqual(created['id'], response.json()['user']['id'])

    def test_account_role_failure_reports_created_account_and_allows_repair(self):
        app, client = self.host()
        headers = self.login(client, 'admin')
        from backend.tasks.store import TaskError
        with patch.object(app.state.tasks, 'set_roles', side_effect=TaskError(503, 'unavailable')):
            result = self.request(client, 'POST', '/accounts', {'email': 'repair@example.com',
                'display_name': '待授权账号', 'roles': ['reviewer']}, headers, 503)
        detail = result['detail']
        self.assertTrue(detail['account_created'])
        account = next(item for item in self.request(client, 'GET', '/accounts') if item['id'] == detail['user_id'])
        self.assertEqual([], account['task_roles'])
        self.request(client, 'PUT', '/roles', {'user_id': account['id'], 'roles': ['reviewer']}, headers)

    def test_account_list_reflects_existing_account_role_edits_without_changing_identity(self):
        app, client = self.host()
        headers = self.login(client, 'admin')
        original = next(item for item in self.auth.list_users() if item['id'] == self.users['a']['id'])
        for roles in (['annotator', 'leader', 'reviewer'], ['reviewer'], []):
            self.request(client, 'PUT', '/roles', {'user_id': original['id'], 'roles': roles}, headers)
            listed = next(item for item in self.request(client, 'GET', '/accounts') if item['id'] == original['id'])
            self.assertEqual(sorted(roles), listed.pop('task_roles'))
            self.assertEqual({**original, 'can_upload': 'leader' in roles}, listed)
        self.login(client, 'a')
        self.assertEqual([], self.request(client, 'GET', '/me')['roles'])

    def test_role_upload_defaults_allow_manual_override_and_report_partial_failure(self):
        app, client = self.host()
        headers = self.login(client, 'admin')
        user_id = self.users['a']['id']
        self.request(client, 'PUT', '/roles', {'user_id': user_id, 'roles': ['leader']}, headers)
        self.assertTrue(self.auth.can_upload(user_id))
        self.request(client, 'PUT', f'/accounts/{user_id}/upload', {'can_upload': False}, headers)
        self.assertFalse(self.auth.can_upload(user_id))
        self.request(client, 'PUT', '/roles', {'user_id': user_id, 'roles': ['annotator', 'reviewer']}, headers)
        self.assertFalse(self.auth.can_upload(user_id))
        self.request(client, 'PUT', f'/accounts/{user_id}/upload', {'can_upload': True}, headers)
        self.assertTrue(self.auth.can_upload(user_id))
        self.request(client, 'PUT', '/roles', {'user_id': user_id, 'roles': []}, headers)
        self.assertFalse(self.auth.can_upload(user_id))
        with patch('backend.tasks.accounts.LocalAccounts.__call__', side_effect=HTTPException(503, 'offline')):
            result = self.request(client, 'PUT', '/roles', {'user_id': user_id, 'roles': ['leader']}, headers, 503)
        self.assertTrue(result['detail']['roles_saved'])
        self.assertFalse(self.auth.can_upload(user_id))
        self.request(client, 'PUT', '/roles', {'user_id': user_id, 'roles': ['leader']}, headers)
        self.assertTrue(self.auth.can_upload(user_id))
        self.request(client, 'PUT', '/roles', {'user_id': self.users['admin']['id'], 'roles': []}, headers)
        self.assertTrue(self.auth.can_upload(self.users['admin']['id']))

    def test_only_superadmin_deletes_account_preserving_history_and_revoking_sessions(self):
        app, client = self.host()
        task = self.initialize(app, client)
        target = self.users['a']['id']
        for name in ('lead', 'a', 'reviewer'):
            headers = self.login(client, name)
            self.request(client, 'DELETE', '/accounts/' + target, headers=headers, expected=403)
        annotations = {'posture': [{'id': 'saved', 'label': '坐', 'start_ms': 100, 'end_ms': 900}]}
        for name in ('a', 'b'):
            headers = self.login(client, name)
            task = self.request(client, 'POST', f"/items/{task['id']}/submit",
                                {'expected_revision': task['revision'], 'annotations': annotations}, headers)
        history = app.state.tasks.history(self.users['lead'], task['id'])
        self.auth.assign(self.project['id'], target)
        _, old_token, _ = self.auth.login('fixture-a', 'long fixture password', 'fixture-delete-check')
        headers = self.login(client, 'admin')
        self.request(client, 'DELETE', '/accounts/' + target, expected=403)
        self.request(client, 'DELETE', '/accounts/' + self.users['admin']['id'], headers=headers, expected=409)
        self.request(client, 'DELETE', '/accounts/' + target, headers=headers)
        self.request(client, 'DELETE', '/accounts/' + target, headers=headers)
        self.assertFalse(any(item['id'] == target for item in self.request(client, 'GET', '/accounts')))
        historic = next(item for item in self.auth.list_users(include_deleted=True) if item['id'] == target)
        self.assertFalse(historic['active'])
        self.assertEqual('a', historic['display_name'])
        self.assertIsNone(self.auth.session(old_token))
        self.assertEqual(target, self.auth.assignment(self.project['id']))
        self.assertEqual(history, app.state.tasks.history(self.users['lead'], task['id']))
        with patch.object(app.state.service, 'inspect_source_claim', return_value={
                'previous': self.project, 'source': self.root / 'nas', 'hashes': {}, 'has_json': False}):
            inspected = client.post('/api/source-claims/inspect', json={'path': str(self.root / 'nas')}, headers=headers)
        self.assertEqual(200, inspected.status_code, inspected.text)
        self.assertEqual('a', inspected.json()['owner_name'])
        for action, body in [('active', {'active': True}), ('upload', {'can_upload': True}), ('password', {'password': '1234567890'})]:
            self.request(client, 'PUT', f'/accounts/{target}/{action}', body, headers, 409)
        failed = client.post('/api/auth/login', json={'username': 'fixture-a', 'password': 'long fixture password'},
                             headers={'Origin': 'https://nas.example.com'})
        self.assertEqual(401, failed.status_code)

    def test_public_admin_creates_in_shared_account_store(self):
        with patch.dict('os.environ', {'DATAMARK_PUBLIC_ORIGIN': 'https://web.example.com',
                'DATAMARK_PUBLIC_API_ORIGIN': 'https://api.example.com', 'DATAMARK_SHARED_ACCOUNTS': '0'}):
            with TestClient(account_app(self.root), base_url='http://accounts:8766') as bridge:
                with TestClient(public_app(accounts=AccountClient(bridge)), base_url='https://api.example.com') as client:
                    headers = self.login(client, 'admin', public=True)
                    created = self.request(client, 'POST', '/accounts', {'email': 'public@example.com',
                        'display_name': '公网标注人', 'roles': ['annotator', 'reviewer']}, headers)
                    self.assertFalse(created['can_upload'])
                    self.request(client, 'PUT', '/roles', {'user_id': created['id'], 'roles': ['leader']}, headers)
                    self.assertTrue(self.auth.can_upload(created['id']))
                    self.request(client, 'PUT', '/roles', {'user_id': created['id'], 'roles': ['annotator', 'reviewer']}, headers)
                    self.assertFalse(self.auth.can_upload(created['id']))
                    self.assertTrue(any(item['id'] == created['id'] for item in self.auth.list_users()))
                    self.request(client, 'POST', '/accounts', {'email': 'PUBLIC@example.com',
                        'display_name': '重复账号', 'roles': ['annotator']}, headers, 409)
                    self.request(client, 'PUT', f"/accounts/{created['id']}/upload", {'can_upload': True}, headers)
                    login = client.post('/api/auth/login', json={'username': 'public@example.com', 'password': '1234567890'},
                                        headers={'Origin': 'https://web.example.com'})
                    self.assertEqual(200, login.status_code, login.text)
                    self.assertTrue(login.json()['user']['can_upload'])
                    self.assertEqual(['annotator', 'reviewer'], self.request(client, 'GET', '/me')['roles'])
                    self.request(client, 'GET', '/accounts', expected=403)
                    headers = self.login(client, 'admin', public=True)
                    self.request(client, 'DELETE', '/accounts/' + created['id'], headers=headers)
                    self.assertFalse(any(item['id'] == created['id'] for item in self.auth.list_users()))
                    response = client.post('/api/auth/login', json={'username': 'public@example.com', 'password': '1234567890'},
                                           headers={'Origin': 'https://web.example.com'})
                    self.assertEqual(401, response.status_code)

    def test_registered_media_identity_and_upload_permission(self):
        app, client = self.host()
        self.initialize(app, client)
        headers = self.login(client, 'admin')
        self.request(client, 'POST', '/videos', {**self.video, 'video_id': 'changed', 'duration_ms': 9999}, headers, 422)
        self.request(client, 'POST', '/videos', {**self.video, 'video_id': 'unknown',
            'source': {'backend': 'nas', 'key': 'b' * 32 + '/v0001'}}, headers, 404)
        headers = self.login(client, 'a')
        self.request(client, 'POST', '/videos', self.video, headers, 403)
        self.auth.set_upload_permission(self.users['a']['id'], True)
        self.request(client, 'POST', '/videos', self.video, headers, 403)

    def test_register_collection_verified_sources_permissions_and_existing_records(self):
        app, client = self.host()
        task = self.initialize(app, client)
        project = app.state.service.load(self.project['id'])
        project['collection_folder_name'] = '1001lixin'
        second = {**project['videos'][0], 'id':'v0002', 'name':'second.mp4'}
        project['videos'].append(second)
        project['_sources']['v0002'] = dict(project['_sources']['v0001'])
        app.state.service.save(project)
        body = {'collection_id':project['id'], 'collector_id':'lixin', 'date':'2026-10-15', 'scene':'inside', 'quality':'passed'}
        headers = self.login(client, 'admin')
        self.request(client, 'POST', '/collections', body, expected=403)  # CSRF
        self.request(client, 'POST', '/collections', {**body,'collection_id':'../invalid'}, headers, 422)
        self.request(client, 'POST', '/collections', {**body,'collection_id':'b'*32}, headers, 404)
        self.request(client, 'POST', '/collections', {**body,'scene':''}, headers, 422)
        result = self.request(client, 'POST', '/collections', body, headers)
        self.assertEqual(('1001lixin',1,1), (result['collection'],result['registered_count'],result['existing_count']))
        registered = result['videos'][0]
        self.assertEqual(project['id']+'-v0002', registered['id'])
        self.assertEqual('1001lixin', registered['collection_name'])
        self.assertEqual(10000, registered['duration_ms'])
        self.assertEqual('second.mp4', registered['video_name'])
        self.request(client, 'POST', '/collections', body, headers, 409)
        self.assertEqual(task['id'], self.request(client,'GET','/items/'+task['id'])['id'])
        headers = self.login(client, 'a')
        self.request(client, 'POST', '/collections', body, headers, 403)

    def test_task_workers_cannot_mutate_legacy_track_libraries_or_project_axes(self):
        app, client = self.host()
        task = self.initialize(app, client)
        project = app.state.service.load(self.project['id'])
        from backend.track_library import TrackLibrary
        library = TrackLibrary(self.auth)
        for name in ('a','reviewer','lead'):
            library.save(self.users[name]['id'], 'oldaxis', '历史轴', 'state', ['旧标签'], create=True)
            headers = self.login(client, name)
            for method,path,body in [
                ('POST','/api/account/track-library',{'id':'newaxis','name':'新轴','labels':['新标签']}),
                ('PUT','/api/account/track-library/oldaxis',{'id':'oldaxis','name':'修改','labels':['修改']}),
                ('DELETE','/api/account/track-library/oldaxis',{'confirmed':True}),
                ('POST','/api/account/track-library/scene/default',{'is_default':False}),
                ('PUT','/api/account/track-templates',{'json_name':'newaxis','name':'新轴','labels':[]}),
                ('DELETE','/api/account/fixed-track-templates/scene',None),
                ('PUT',f"/api/projects/{project['id']}/track-selection",{'tracks':[], 'expected_revision':project['revision']}),
            ]:
                response = client.request(method,path,json=body,headers=headers)
                self.assertEqual(403,response.status_code,response.text)
                self.assertIn('标注规则',response.json()['detail'])
            self.assertEqual(['旧标签'],next(axis for axis in library.list(self.users[name]['id']) if axis['id']=='oldaxis')['labels'])
        self.assertEqual(project,app.state.service.load(project['id']))
        headers = self.login(client,'admin')
        self.assertEqual(200,client.post('/api/account/track-library',json={'id':'adminaxis','name':'管理轴','labels':['管理']},headers=headers).status_code)
        self.assertEqual(task['id'],self.request(client,'GET','/items/'+task['id'])['id'])

    def test_task_audit_scope_library_and_batch_outcomes(self):
        app, client = self.host()
        task = self.initialize(app, client)
        self.assertTrue(self.request(client, 'GET', '/axis-library'))
        from backend.track_library import TrackLibrary
        library = TrackLibrary(self.auth)
        library.save(self.users['lead']['id'], 'personal-posture', '个人姿势', 'state', ['坐'], create=True)
        copied = self.request(client, 'GET', '/axis-library')
        self.assertEqual('个人姿势', next(axis['name'] for axis in copied if axis['id'] == 'personal-posture'))
        headers = self.login(client, 'lead')
        for participants in (None, 'not a list'):
            self.request(client, 'POST', '/items', {'annotator_ids': participants}, headers, 422)
            self.request(client, 'PUT', '/items/' + task['id'] + '/assignment',
                         {'annotator_ids': participants}, headers, 422)
        headers = self.login(client, 'admin')
        second = json.loads(json.dumps(self.project))
        second['id'] = 'b' * 32
        source = self.root / 'nas/second.mp4'
        source.write_bytes(b'another explicit media identity fixture')
        second['videos'][0].update(name=source.name, relative_path=source.name)
        second['_sources']['v0001'] = {'path':str(source), 'stamp':{
            'size':source.stat().st_size, 'mtime_ns':source.stat().st_mtime_ns}}
        app.state.service.save(second)
        results = self.request(client, 'POST', '/videos/batch', {'videos': [
            {**self.video, 'video_id': 'second', 'source':{'backend':'nas','key':second['id']+'/v0001'}},
            {**self.video, 'video_id':'duplicate-source'}, self.video, None]}, headers)['results']
        self.assertEqual([True, False, False, False], [row['ok'] for row in results])
        self.assertIn('已登记', results[1]['detail'])
        self.login(client, 'a')
        entries = self.request(client, 'GET', '/audit?task_id=' + task['id'])
        self.assertTrue(entries)
        self.request(client, 'GET', '/audit', expected=403)
        self.request(client, 'GET', '/axis-library', expected=403)
        headers = self.login(client, 'admin')
        self.request(client, 'PUT', '/roles', {'user_id': self.users['a']['id'], 'roles': []}, headers)
        self.login(client, 'a')
        self.request(client, 'GET', '/audit?task_id=' + task['id'], expected=403)

    def test_shared_editor_independent_drafts_submission_privacy_and_no_writeback(self):
        app, client = self.host()
        task = self.initialize(app, client)
        task_id = task['id']
        url = f'/items/{task_id}/workspace/project'
        headers = self.login(client, 'a')
        workspace = app.state.task_workspaces
        with patch.object(workspace, 'project', wraps=workspace.project) as resolve:
            opened = self.request(client, 'GET', url)
            self.assertEqual(resolve.call_count, 1, 'serialize the already-authorized project once per request')
        with patch.object(workspace, 'project', wraps=workspace.project) as resolve:
            listed = self.request(client, 'GET', f'/items/{task_id}/workspace/projects')
            self.assertEqual(resolve.call_count, 1)
            self.assertEqual(listed[0]['id'], opened['id'])
        self.assertFalse(opened['task_readonly'])
        self.assertEqual(['posture'], opened['selected_tracks'])
        annotations = {'posture': [{'id': 'one', 'label': '坐', 'start_ms': 100, 'end_ms': 900, 'lane': 0,
                                   'transport_type': 'car', 'needs_review': True, 'review_reason': 'uncertain'}]}
        saved = self.request(client, 'PUT', url + '/draft', {'expected_revision': task['revision'], 'annotations': annotations}, headers)
        self.assertEqual(annotations['posture'], [{k: item.get(k) for k in annotations['posture'][0]}
                                                 for item in saved['annotations']['posture']])
        self.request(client, 'PUT', url + '/draft', {'expected_revision': task['revision'], 'annotations': {}}, headers, 409)
        self.assertEqual(empty_annotations(), app.state.service.load(self.project['id'])['annotations'])
        self.assertEqual(0, app.state.service.load(self.project['id'])['revision'])
        self.assertEqual(client.get('/api/projects/' + self.project['id']).status_code, 403)
        for operation in ('writeback', 'export', 'custom-tracks'):
            self.request(client, 'POST', url + '/' + operation, {}, headers, 404)
        submitted = self.request(client, 'POST', f'/items/{task_id}/submit',
            {'expected_revision': saved['revision'], 'annotations': annotations}, headers)
        self.assertTrue(self.request(client, 'GET', url)['task_readonly'])
        self.request(client, 'PUT', url + '/draft', {'expected_revision': submitted['revision'], 'annotations': {}}, headers, 409)
        self.login(client, 'b')
        self.assertEqual([], self.request(client, 'GET', f'/items/{task_id}')['submissions'])
        self.assertEqual([], self.request(client, 'GET', url)['annotations']['posture'])
        self.login(client, 'reviewer')
        self.assertEqual([], self.request(client, 'GET', f'/items/{task_id}')['submissions'])
        self.assertTrue(self.request(client, 'GET', url)['task_readonly'])

    def test_task_delete_requires_manager_csrf_preserves_history_and_frees_type(self):
        app, client = self.host()
        task = self.initialize(app, client)
        project_before = app.state.service.load(self.project['id'])
        for manager in ('admin', 'lead'):
            path = f"/items/{task['id']}"
            for name in ('a', 'reviewer'):
                headers = self.login(client, name)
                self.request(client, 'DELETE', path, {'expected_revision': task['revision']}, headers, 403)
                self.request(client, 'GET', '/items?include_deleted=true', expected=403)
            headers = self.login(client, manager)
            self.request(client, 'DELETE', path, {'expected_revision': task['revision']}, expected=403)
            self.request(client, 'DELETE', path, {'expected_revision': task['revision'] + 1}, headers, 409)
            deleted = self.request(client, 'DELETE', path, {'expected_revision': task['revision']}, headers)
            self.assertEqual('deleted', deleted['status'])
            self.assertEqual([], self.request(client, 'GET', '/items'))
            self.assertTrue(any(t['id'] == task['id'] for t in self.request(client, 'GET', '/items?include_deleted=true')))
            self.assertTrue(self.request(client, 'GET', path + '/workspace/project')['task_readonly'])
            self.request(client, 'POST', path + '/start-review', {'expected_revision': deleted['revision']}, headers, 410)
            audit = self.request(client, 'GET', '/audit?task_id=' + task['id'])
            self.assertEqual(1, len([e for e in audit if e['action'] == 'delete_task']))
            self.login(client, 'a')
            self.request(client, 'GET', path, expected=403)
            self.request(client, 'GET', path + '/workspace/project', expected=403)
            headers = self.login(client, manager)
            replacement = self.request(client, 'POST', '/items', {'video_id': task['video_id'],
                'rule_id': task['rule_id'], 'annotator_ids': task['annotator_ids'],
                'reviewer_id': task['reviewer_id'], 'deadline': task['deadline']}, headers)
            self.assertNotEqual(task['id'], replacement['id'])
            task = replacement
        self.assertEqual(project_before, app.state.service.load(self.project['id']))

    def test_managers_assignment_self_annotation_review_acceptance_and_export(self):
        app, client = self.host()
        original = self.initialize(app, client)
        headers = self.login(client, 'admin')
        admin_id = self.users['admin']['id']
        self.assertEqual(['annotator', 'leader', 'reviewer'], self.request(client, 'GET', '/me')['roles'])
        for name,pending in [(name,pending) for name in ('admin','lead') for pending in (False,True)]:
            headers=self.login(client,name)
            admin_id=self.users[name]['id']
            self.assertEqual(['annotator','leader','reviewer'],self.request(client,'GET','/me')['roles'])
            with self.subTest(manager=name,pending=pending):
                rule = self.request(client, 'POST', '/rules', {'name': 'admin rule',
                    'task_type': name + '-' + str(pending), 'axes': original['rule']['axes']}, headers)
                assignment = {'annotator_ids': [admin_id], 'reviewer_id': admin_id}
                task = self.request(client, 'POST', '/items', {'video_id': 'clip', 'rule_id': rule['id'],
                    'deadline': '2026-10-26', **({} if pending else assignment)}, headers)
                if pending:
                    task = self.request(client, 'PUT', f"/items/{task['id']}/assignment",
                        {'expected_revision': task['revision'], **assignment}, headers)
                path = f"/items/{task['id']}"
                editor = path + '/workspace/project'
                self.assertFalse(self.request(client, 'GET', editor)['task_readonly'])
                annotations = {'posture': [{'id': 'admin-segment', 'label': '坐', 'start_ms': 100, 'end_ms': 900}]}
                saved = self.request(client, 'PUT', editor + '/draft',
                    {'expected_revision': task['revision'], 'annotations': annotations}, headers)
                task = self.request(client, 'POST', path + '/submit',
                    {'expected_revision': saved['revision']}, headers)
                old_id = task['current_submissions'][admin_id]['id']
                self.assertTrue(self.request(client, 'GET', editor)['task_readonly'])
                task = self.request(client, 'POST', path + '/start-review', {'expected_revision': task['revision']}, headers)
                self.assertTrue(self.request(client, 'GET', editor)['task_readonly'])
                task = self.request(client, 'POST', path + '/review', {'expected_revision': task['revision'],
                    'action': 'return', 'return_to': [admin_id], 'issues': [{'axis': 'posture',
                    'start_ms': 100, 'end_ms': 900, 'reason': 'check boundary'}]}, headers)
                self.assertFalse(self.request(client, 'GET', editor)['task_readonly'])
                task = self.request(client, 'POST', path + '/submit',
                    {'expected_revision': task['revision'], 'annotations': annotations}, headers)
                self.assertNotEqual(old_id, task['current_submissions'][admin_id]['id'])
                history = self.request(client, 'GET', path + '/history')['submissions']
                self.assertEqual(annotations, next(v for v in history if v['id'] == old_id)['annotations'])
                task = self.request(client, 'POST', path + '/start-review', {'expected_revision': task['revision']}, headers)
                task = self.request(client, 'POST', path + '/review', {'expected_revision': task['revision'],
                    'action': 'pass', 'resolved_issue_ids': [i['id'] for i in task['issues'] if i['id'].startswith('review:')]}, headers)
                self.assertEqual(admin_id, task['reviewed_by'])
                self.assertTrue(task['review_complete'])
                task=self.request(client,'POST',path+'/accept',{'expected_revision':task['revision']},headers)
                self.assertEqual('accepted',task['status'])
                self.assertEqual(admin_id,task['accepted_by'])
                delivery=self.request(client,'POST','/deliveries',{'task_ids':[task['id']],'expected_revisions':{task['id']:task['revision']},'request_id':name+str(pending)},headers)
                exported=client.get('/api/tasks/deliveries/'+delivery['id'])
                self.assertEqual(exported.status_code,200,exported.text)
                self.assertIn('admin-segment',exported.text)
        headers=self.login(client,'admin')
        # An administrator can inspect all tasks but cannot annotate unassigned work.
        path = f"/items/{original['id']}"
        self.assertTrue(self.request(client, 'GET', path + '/workspace/project')['task_readonly'])
        self.request(client, 'PUT', path + '/workspace/project/draft',
            {'expected_revision': original['revision'], 'annotations': {}}, headers, 409)
        self.request(client, 'POST', path + '/submit',
            {'expected_revision': original['revision'], 'annotations': {}}, headers, 403)

    def test_public_host_shared_bridge_tasks_without_nas_media(self):
        with patch.dict('os.environ', {'DATAMARK_PUBLIC_ORIGIN': 'https://web.example.com',
                'DATAMARK_PUBLIC_API_ORIGIN': 'https://api.example.com', 'DATAMARK_SHARED_ACCOUNTS': '0'}):
            with TestClient(account_app(self.root), base_url='http://accounts:8766') as bridge:
                with TestClient(public_app(accounts=AccountClient(bridge)), base_url='https://api.example.com') as client:
                    headers = self.login(client, 'admin', public=True)
                    self.request(client, 'PUT', '/roles', {'user_id': self.users['lead']['id'], 'roles': ['leader']}, headers)
                    roster = self.request(client, 'GET', '/users')
                    self.assertEqual({'id', 'display_name', 'role', 'active', 'can_upload'}, set(roster[0]))
                    self.request(client, 'POST', '/videos', {'source': {'backend': 'nas', 'key': '/mnt/nas'}}, headers, 503)
                    self.request(client, 'POST', '/sources/import', {'path': '/mnt/nas'}, headers, 503)
                    headers = self.login(client, 'lead', public=True)
                    self.request(client, 'POST', '/rules', {'name': 'rule', 'task_type': 'posture',
                        'axes': [{'id': 'posture', 'name': '姿势', 'mode': 'state', 'labels': ['坐']}]}, headers)
                    self.assertFalse(client.get('/api/health').json()['storage_ready'])
                    self.auth.set_active(self.users['lead']['id'], False)
                    self.assertEqual(client.get('/api/tasks/me').status_code, 401)

    def test_nas_uses_existing_public_rule_api_with_its_own_login_and_task_snapshots(self):
        import httpx
        from backend.tasks.public_rules import PublicRules
        old_app, old_client = self.host()
        old_task = self.initialize(old_app, old_client)
        project = old_app.state.service.load(self.project['id'])
        project['_sources']['v0001']['duration_ms'] = 10000
        project['_storage_version'] = 3
        old_app.state.service.save(project)
        public_state = self.root / 'public-tasks'
        with patch.dict('os.environ', {'DATAMARK_PUBLIC_ORIGIN': 'https://web.example.com',
                'DATAMARK_PUBLIC_API_ORIGIN': 'https://api.example.com', 'DATAMARK_SHARED_ACCOUNTS': '0',
                'DATAMARK_TASKS_STATE': str(public_state)}):
            with TestClient(account_app(self.root), base_url='http://accounts:8766') as bridge:
                with TestClient(public_app(accounts=AccountClient(bridge)), base_url='https://api.example.com') as public:
                    public_headers = self.login(public, 'admin', public=True)
                    rule = self.request(public, 'POST', '/rules', {'name': 'public original', 'task_type': 'public-type',
                        'axes': [{'id': 'posture', 'name': '姿势', 'mode': 'state', 'labels': ['坐']}]}, public_headers)
                    def forward(request):
                        return public.request(request.method, request.url.path, content=request.content,
                                              headers=dict(request.headers))
                    proxy = PublicRules(transport=httpx.MockTransport(forward))
                    with patch.dict('os.environ', {'DATAMARK_PUBLIC_ORIGIN': '', 'DATAMARK_PUBLIC_API_ORIGIN': '',
                            'DATAMARK_TASKS_STATE': str(self.state), 'DATAMARK_TASK_RULES_PUBLIC': '1'}), \
                         patch('backend.tasks.public_rules.PublicRules', return_value=proxy), \
                         patch('backend.tasks.public_rules.PUBLIC_HOST', 'api.example.com'), \
                         patch('backend.tasks.public_rules.PUBLIC_ORIGIN', 'https://web.example.com'):
                        _, nas = self.host()
                        nas_headers = self.login(nas, 'admin')
                        self.assertEqual([rule], self.request(nas, 'GET', '/rules'))
                        self.assertEqual(old_task['rule'], self.request(nas, 'GET', '/items/' + old_task['id'])['rule'])
                        task = self.request(nas, 'POST', '/items', {'video_id': 'clip', 'rule_id': rule['id'],
                            'deadline': '2026-10-26'}, nas_headers)
                        self.request(public, 'GET', '/items/' + task['id'], expected=404)
                        published = self.request(nas, 'POST', '/rules', {'name': 'from NAS', 'task_type': 'another-type',
                            'axes': [{'id': 'posture', 'name': '姿势', 'mode': 'state', 'labels': ['站']}]}, nas_headers)
                        self.assertIn(published, self.request(public, 'GET', '/rules'))
                        self.request(nas, 'DELETE', '/rules/' + published['id'], headers=nas_headers)
                        self.assertEqual([rule], self.request(public, 'GET', '/rules'))
                        self.request(public, 'DELETE', '/rules/' + rule['id'], headers=public_headers)
                        self.assertEqual([], self.request(nas, 'GET', '/rules'))
                        self.assertEqual(rule, self.request(nas, 'GET', '/items/' + task['id'])['rule'])
                        self.request(nas, 'POST', '/items', {'video_id': 'clip', 'rule_id': rule['id'],
                            'rule_snapshot': rule, 'deadline': '2026-10-26'}, nas_headers, expected=409)

    def test_full_http_review_acceptance_delivery_and_role_revocation(self):
        app, client = self.host()
        task = self.initialize(app, client)
        task_id = task['id']
        annotations = {'posture': [{'id': 'one', 'label': '坐', 'start_ms': 123, 'end_ms': 900, 'lane': 0},
                                    {'id': 'two', 'label': '坐', 'start_ms': 200, 'end_ms': 800, 'lane': 1}]}
        for name in ('a', 'b'):
            headers = self.login(client, name)
            task = self.request(client, 'POST', f'/items/{task_id}/submit',
                {'expected_revision': task['revision'], 'annotations': annotations}, headers)
        self.assertEqual('submitted', task['status'])
        headers = self.login(client, 'reviewer')
        task = self.request(client, 'POST', f'/items/{task_id}/start-review',
                            {'expected_revision': task['revision']}, headers)
        task = self.request(client, 'POST', f'/items/{task_id}/review',
                            {'expected_revision': task['revision']}, headers)
        headers = self.login(client, 'lead')
        task = self.request(client, 'POST', f'/items/{task_id}/accept',
                            {'expected_revision': task['revision']}, headers)
        body = {'task_ids': [task_id], 'expected_revisions': {task_id: task['revision']}, 'request_id': 'api-delivery'}
        delivery = self.request(client, 'POST', '/deliveries', body, headers)
        self.assertEqual(delivery, self.request(client, 'POST', '/deliveries', body, headers))
        csv = client.get('/api/tasks/deliveries/' + delivery['id'])
        self.assertEqual(200, csv.status_code, csv.text)
        self.assertIn('start_ms', csv.text)
        self.assertIn('123', csv.text)
        self.assertEqual('delivered', self.request(client, 'GET', f'/items/{task_id}')['status'])
        self.assertEqual(empty_annotations(), app.state.service.load(self.project['id'])['annotations'])
        headers = self.login(client, 'admin')
        self.request(client, 'PUT', '/roles', {'user_id': self.users['a']['id'], 'roles': []}, headers)
        self.login(client, 'a')
        self.request(client, 'GET', f'/items/{task_id}/workspace/project', expected=403)
        self.assertEqual(403, client.get('/api/tasks/deliveries/' + delivery['id']).status_code)

    def test_superadmin_all_task_functions_without_explicit_grants_and_privacy_still_holds(self):
        app, client = self.host()
        task = self.initialize(app, client)
        headers = self.login(client, 'admin')
        self.assertEqual(['annotator', 'leader', 'reviewer'], self.request(client, 'GET', '/me')['roles'])
        self.assertTrue(self.request(client, 'GET', '/rules'))
        annotations = {'posture': [{'id': 'one', 'label': '坐', 'start_ms': 123, 'end_ms': 900}]}
        headers = self.login(client, 'a')
        task = self.request(client, 'POST', f"/items/{task['id']}/submit",
                            {'expected_revision': task['revision'], 'annotations': annotations}, headers)
        headers = self.login(client, 'admin')
        self.assertEqual([], self.request(client, 'GET', f"/items/{task['id']}")['submissions'])
        self.assertEqual([], self.request(client, 'GET', f"/items/{task['id']}/history")['submissions'])
        headers = self.login(client, 'b')
        task = self.request(client, 'POST', f"/items/{task['id']}/submit",
                            {'expected_revision': task['revision'], 'annotations': annotations}, headers)
        headers = self.login(client, 'admin')
        detail = self.request(client, 'GET', f"/items/{task['id']}")
        self.assertEqual({'admin', 'a', 'b', 'reviewer'}, {p['display_name'] for p in detail['participants']})
        task = self.request(client, 'POST', f"/items/{task['id']}/start-review", {'expected_revision': task['revision']}, headers)
        note = {'axis': 'posture', 'start_ms': 123, 'end_ms': 900, 'reason': 'checked'}
        task = self.request(client, 'POST', f"/items/{task['id']}/review",
                            {'expected_revision': task['revision'], 'action': 'pass', 'notes': [note]}, headers)
        self.assertEqual(self.users['admin']['id'], task['review_notes'][0]['actor_id'])
        task = self.request(client, 'POST', f"/items/{task['id']}/accept", {'expected_revision': task['revision']}, headers)
        delivery = self.request(client, 'POST', '/deliveries', {'task_ids': [task['id']],
            'expected_revisions': {task['id']: task['revision']}, 'request_id': 'superadmin-all'}, headers)
        self.assertEqual(200, client.get('/api/tasks/deliveries/' + delivery['id']).status_code)
        rule = self.request(client, 'POST', '/rules', {'name': 'admin assignment', 'task_type': 'new',
            'axes': [{'id': 'posture', 'name': '姿势', 'mode': 'state', 'labels': ['坐']}]}, headers)
        body = {'video_id': 'clip', 'rule_id': rule['id'], 'annotator_ids': [self.users['admin']['id']],
                'reviewer_id': self.users['admin']['id'], 'deadline': '2026-10-26'}
        admin_task = self.request(client, 'POST', '/items', {**body, 'reviewer_id': self.users['reviewer']['id']}, headers)
        self.assertEqual([self.users['admin']['id']], admin_task['annotator_ids'])
        pending_rule = self.request(client, 'POST', '/rules', {'name': 'pending rule', 'task_type': 'pending',
            'axes': rule['axes']}, headers)
        assigned = self.request(client, 'POST', '/items', {**body, 'rule_id': pending_rule['id'], 'annotator_ids': []}, headers)
        assignment = {'expected_revision': assigned['revision'], 'annotator_ids': [self.users['admin']['id']],
                      'reviewer_id': self.users['reviewer']['id']}
        assigned = self.request(client, 'PUT', f"/items/{assigned['id']}/assignment",
            {**assignment, 'annotator_ids': [self.users['a']['id']], 'reviewer_id': self.users['admin']['id']}, headers)
        self.assertTrue(self.request(client, 'GET', f"/items/{assigned['id']}/workspace/project")['task_readonly'])
        self.request(client, 'POST', f"/items/{assigned['id']}/submit",
                     {'expected_revision': assigned['revision'], 'annotations': annotations}, headers, 403)

    def test_rework_editor_shows_each_feedback_and_old_versions_remain_readonly(self):
        app, client = self.host()
        task = self.initialize(app, client)
        annotations = {'posture': [{'id': 'one', 'label': '坐', 'start_ms': 123, 'end_ms': 900}]}
        for name in ('a', 'b'):
            headers = self.login(client, name)
            task = self.request(client, 'POST', f"/items/{task['id']}/submit",
                                {'expected_revision': task['revision'], 'annotations': annotations}, headers)
        original_id = task['current_submissions'][self.users['a']['id']]['id']
        headers = self.login(client, 'reviewer')
        task = self.request(client, 'POST', f"/items/{task['id']}/start-review", {'expected_revision': task['revision']}, headers)
        task = self.request(client, 'POST', f"/items/{task['id']}/review", {'expected_revision': task['revision'],
            'action': 'return', 'return_to': [self.users['a']['id']],
            'issues': [{'axis': 'posture', 'segment_id': 'one', 'reason': '重新确认切点'},
                       {'axis': 'posture', 'start_ms': 123, 'end_ms': 900, 'reason': '确认标签'}]}, headers)
        headers = self.login(client, 'a')
        editor = self.request(client, 'GET', f"/items/{task['id']}/workspace/project")
        self.assertFalse(editor['task_readonly'])
        self.assertEqual(['重新确认切点', '确认标签'], [issue['reason'] for issue in editor['task_feedback']])
        self.assertEqual([], self.request(client, 'GET', f"/items/{task['id']}")['submissions'])
        task = self.request(client, 'POST', f"/items/{task['id']}/submit",
                            {'expected_revision': task['revision'], 'annotations': annotations}, headers)
        self.assertNotEqual(original_id, task['current_submissions'][self.users['a']['id']]['id'])
        old = next(version for version in self.request(client, 'GET', f"/items/{task['id']}/history")['submissions'] if version['id'] == original_id)
        self.assertEqual(annotations, old['annotations'])
        headers = self.login(client, 'reviewer')
        task = self.request(client, 'POST', f"/items/{task['id']}/start-review", {'expected_revision': task['revision']}, headers)
        task = self.request(client, 'POST', f"/items/{task['id']}/review", {'expected_revision': task['revision'],
            'action': 'pass', 'resolved_issue_ids': [issue['id'] for issue in task['issues'] if issue['id'].startswith('review:')]}, headers)
        self.assertTrue(task['review_complete'])

    def test_collection_output_submission_failure_retry_and_approved_publication(self):
        from backend.tasks.storage import ProjectMedia
        from backend.tasks.collection_outputs import CollectionOutputs
        from backend.test_r2_integration import MemoryObjects
        app,client=self.host()
        self.initialize(app,client)
        provider=MemoryObjects()
        service=app.state.service
        service.provider=provider
        project=service.load(self.project['id'])
        prefix='daily/20261015/fixture'
        project['_r2_prefix']=prefix
        service.save(project)
        workspace=app.state.task_workspaces
        workspace.media=ProjectMedia(service,'s3',lambda actor,ident:True)
        store=app.state.tasks
        store.outputs=CollectionOutputs(store,workspace.media,workspace.service)
        headers=self.login(client,'admin')
        self.request(client,'POST','/collections',{'collection_id':project['id'],'collector_id':'fixture',
            'date':'2026-10-15','scene':'室内','quality':'passed'},headers)
        rule=self.request(client,'POST','/rules',{'name':'S3 output','task_type':'output',
            'axes':[{'id':'posture','name':'姿势','mode':'state','labels':['坐']}]},headers)
        task=self.request(client,'POST','/items',{'collection_id':project['id'],'storage_backend':'s3','rule_id':rule['id'],
            'annotator_ids':[self.users['admin']['id']],'reviewer_id':self.users['admin']['id'],'deadline':'2026-10-26'},headers)
        path='/items/'+task['id']
        with patch.object(provider,'put_if_absent',side_effect=HTTPException(503,'fixture offline')):
            task=self.request(client,'POST',path+'/submit',{'expected_revision':task['revision'],
                'request_id':'one','annotations':{'posture':[{'id':'one','label':'坐','start_ms':0,'end_ms':1000}]}},headers)
        self.assertEqual(task['status'],'submitted')
        self.assertEqual(task['output_sync']['state'],'error')
        self.request(client,'POST',path+'/outputs/retry',{},expected=403)
        sync=self.request(client,'POST',path+'/outputs/retry',{},headers)
        self.assertEqual(sync['state'],'ready')
        self.assertIn(prefix+'/timeline/admin/v1/posture.timeline.json',provider.objects)
        task=self.request(client,'POST',path+'/start-review',{'expected_revision':task['revision']},headers)
        task=self.request(client,'POST',path+'/review',{'expected_revision':task['revision'],'action':'pass'},headers)
        task=self.request(client,'POST',path+'/accept',{'expected_revision':task['revision']},headers)
        self.assertEqual(task['output_sync']['state'],'ready')
        self.assertIn(prefix+'/timeline/final/posture.timeline.json',provider.objects)
        # Copy the platform's actual exported JSON, including its acceptance
        # marker, through the standalone NAS sync without modifying S3 objects.
        from deploy.sync_final_timelines import MARKER, sync_capture
        nas_daily = self.root / 'nas-copy' / 'daily'
        nas_collection = nas_daily / '20261015' / 'fixture'
        nas_collection.mkdir(parents=True)
        copied = sync_capture(provider, nas_daily, prefix, apply=True, directory='final')
        self.assertEqual(copied['copy'], 1)
        for name in ('posture.timeline.json', MARKER):
            self.assertEqual((nas_collection / 'timeline' / 'final' / name).read_bytes(),
                             provider.get(prefix + '/timeline/final/' + name))
        delivery=self.request(client,'POST','/deliveries',{'task_ids':[task['id']],
            'expected_revisions':{task['id']:task['revision']},'request_id':'export'},headers)
        self.assertEqual(delivery['output_sync'][0]['state'],'ready')
        self.assertIn(prefix+'/timeline/final/delivery-'+delivery['id']+'.csv',provider.objects)
        client.cookies.clear()
        self.request(client,'POST',path+'/outputs/retry',{},headers,expected=401)

    def test_direct_task_cache_routes_do_not_start_or_read_server_preparation(self):
        app,client=self.host()
        task=self.initialize(app,client)
        self.login(client,'reviewer')
        workspace=app.state.task_workspaces
        manifest={'version':'verified-upload-version','videos':[{'id':'v0001','url':'https://objects.example/normal',
                  'fast_url':'https://objects.example/fast','thumbnail_url':'https://objects.example/thumbnail',
                  'storyboard':{'sheets':[]}}],'assets':[]}
        with patch.object(workspace.media,'direct_session',create=True,return_value=manifest), \
             patch.object(workspace.service.sessions,'start',side_effect=AssertionError('no server preparation')), \
             patch.object(workspace.service.sessions,'status',side_effect=AssertionError('no server polling')), \
             patch.object(workspace.service.sessions,'manifest',side_effect=AssertionError('no server media copy')):
            path=f"/items/{task['id']}/workspace/project"
            status=self.request(client,'GET',path+'/session')
            self.assertEqual(status['state'],'ready')
            self.assertEqual(status['ready'],status['total'])
            headers=self.login(client,'reviewer')
            self.assertEqual(self.request(client,'POST',path+'/session/prepare',{},headers)['state'],'ready')
            self.assertEqual(self.request(client,'GET',path+'/session/manifest'),manifest)
            self.assertEqual(self.request(client,'POST',path+'/session/browser-ready',{},headers),{'released':False})
            preview=self.request(client,'GET',f"/items/{task['id']}/workspace/previews/v0001?fast=true")
            self.assertEqual(preview['url'],'https://objects.example/fast')
        client.cookies.clear()
        self.request(client,'GET',path+'/session/manifest',expected=401)

    def test_leader_import_reads_real_video_without_copying_and_checks_permissions(self):
        repo = Path(__file__).resolve().parents[1]
        tools = repo / '.tools' / 'ffmpeg' / 'bin'
        ffmpeg = os.getenv('FFMPEG_PATH') or str(tools / 'ffmpeg')
        ffprobe = os.getenv('FFPROBE_PATH') or str(tools / 'ffprobe')
        if not Path(ffmpeg).is_file():
            ffmpeg = shutil.which('ffmpeg') or ffmpeg
        if not Path(ffprobe).is_file():
            ffprobe = shutil.which('ffprobe') or ffprobe
        with patch.dict('os.environ', {'FFMPEG_PATH': ffmpeg, 'FFPROBE_PATH': ffprobe}):
            app, client = self.host()
            self.initialize(app, client)
            source = self.root / 'nas' / 'new-collection'
            source.mkdir()
            video = source / 'A09999_20261014000000_0000.mp4'
            subprocess.run([ffmpeg, '-v', 'error', '-f', 'lavfi', '-i',
                'color=c=blue:s=160x90:r=10', '-t', '1', '-c:v', 'libx264', '-pix_fmt', 'yuv420p', str(video)], check=True)
            original = video.read_bytes()
            headers = self.login(client, 'a')
            self.request(client, 'POST', '/sources/import', {'path': str(source)}, headers, 403)
            headers = self.login(client, 'lead')
            capabilities = self.request(client, 'GET', '/sources/capabilities')
            self.assertEqual('nas', capabilities['backend'])
            self.assertTrue(capabilities['can_browse'])
            listing = self.request(client, 'GET', '/sources/browse')
            self.assertTrue(any(entry['name'] == 'new-collection' for entry in listing['entries']))
            self.request(client, 'POST', '/sources/import', {'path': str(source)}, expected=403)
            self.request(client, 'POST', '/sources/import', {'path': str(self.root)}, headers, 403)
            imported = self.request(client, 'POST', '/sources/import', {'path': str(source)}, headers)
            self.assertEqual(1, len(imported['sources']))
            self.assertEqual(1000, imported['sources'][0]['duration_ms'])
            self.assertEqual(original, video.read_bytes())
            self.assertEqual(str(video), app.state.service.load(imported['project_id'])['_sources']['v0001']['path'])
            self.assertEqual(self.users['lead']['id'], self.auth.assignment(imported['project_id']))
            reopened = self.request(client, 'POST', '/sources/import', {'path': str(source)}, headers)
            self.assertEqual(imported['project_id'], reopened['project_id'])
            headers = self.login(client, 'admin')
            self.request(client, 'POST', '/sources/import', {'paths': [str(video)]}, headers, 409)


    def test_collection_camera_seam_saves_and_submits_for_new_and_legacy_tasks(self):
        app, client = self.host()
        initial = self.initialize(app, client)
        admin = self.login(client, 'admin')
        self.request(client, 'DELETE', '/items/'+initial['id'], {'expected_revision':initial['revision']}, admin)
        project = app.state.service.load(self.project['id'])
        project['videos'][0]['name'] = 'A09999_20261015000000_0000.mp4'
        for index, (start, duration, stamp) in enumerate([
            (10066, 10000, '000010'), (22066, 1000, '000022')], 2):
            path = self.root/'nas'/f'clip{index}.mp4'
            path.write_bytes(f'isolated camera fixture {index}'.encode())
            ident = f'v{index:04d}'
            project['videos'].append({**project['videos'][0], 'id':ident,
                'name':f'A09999_20261015{stamp}_{index-1:04d}.mp4', 'relative_path':path.name,
                'duration_ms':duration, 'start_ms':start, 'end_ms':start+duration,
                'recording_start':f'2026-10-15T00:00:{stamp[-2:]}.066+08:00', 'alignment_offset_ms':66})
            project['_sources'][ident] = {'path':str(path),
                'stamp':{'size':path.stat().st_size, 'mtime_ns':path.stat().st_mtime_ns}}
        project.update(duration_ms=23066, gaps=[{'start_ms':10000,'end_ms':10066},
                                              {'start_ms':20066,'end_ms':22066}])
        app.state.service.save(project)
        for index, duration in [(2, 10000), (3, 1000)]:
            self.request(client, 'POST', '/videos', {**self.video, 'video_id':f'clip{index}',
                'duration_ms':duration, 'source':{'backend':'nas','key':project['id']+f'/v{index:04d}'}}, admin)
        lead = self.login(client, 'lead')
        task = self.request(client, 'POST', '/items', {'collection_id':project['id'], 'storage_backend':'nas',
            'rule_id':initial['rule_id'], 'annotator_ids':[self.users['a']['id']],
            'reviewer_id':self.users['reviewer']['id'], 'deadline':'2026-10-26'}, lead)
        self.assertEqual([(r['start_ms'],r['end_ms']) for r in task['collection']['recording_runs']],
                         [(0,20066),(22066,23066)])
        annotator = self.login(client, 'a')
        endpoint = '/items/'+task['id']+'/workspace/project'
        workspace = self.request(client, 'GET', endpoint)
        self.assertEqual([(r['start_ms'],r['end_ms']) for r in workspace['recording_runs']], [(0,20066),(22066,23066)])
        annotations = {'posture':[{'id':'seam','label':'坐','start_ms':0,'end_ms':20066,'lane':0}]}
        saved = self.request(client, 'PUT', endpoint+'/draft', {
            'expected_revision':workspace['revision'], 'annotations':annotations}, annotator)
        self.assertEqual(saved['annotations']['posture'], annotations['posture'])
        # Recreate the old persisted snapshot: split at every positive seam,
        # lacking the source name and alignment offset added by this fix.
        with app.state.tasks.db() as db:
            old = json.loads(db.execute('SELECT body FROM tasks WHERE id=?',(task['id'],)).fetchone()[0])
            for member in old['collection']['videos']:
                member['video_name'] = member.pop('name')
                member.pop('alignment_offset_ms', None)
            old['collection']['recording_runs'] = [{'start_ms':v['start_ms'],'end_ms':v['end_ms']}
                                                  for v in old['collection']['videos']]
            db.execute('UPDATE tasks SET body=? WHERE id=?', (json.dumps(old), task['id']))
        restored = self.request(client, 'GET', '/items/'+task['id'])
        self.assertEqual([(r['start_ms'],r['end_ms']) for r in restored['collection']['recording_runs']], [(0,20066),(22066,23066)])
        bad = {'posture':[{'id':'real-gap','label':'坐','start_ms':0,'end_ms':23066}]}
        for method, path in [('PUT',endpoint+'/draft'), ('POST','/items/'+task['id']+'/submit')]:
            error = self.request(client, method, path, {'expected_revision':saved['revision'],
                'annotations':bad}, annotator, 422)
            self.assertIn('真实断档', json.dumps(error, ensure_ascii=False))
        saved = self.request(client, 'PUT', endpoint+'/draft', {
            'expected_revision':saved['revision'], 'annotations':annotations}, annotator)
        submitted = self.request(client, 'POST', '/items/'+task['id']+'/submit', {
            'expected_revision':saved['revision']}, annotator)
        self.assertEqual(submitted['status'], 'submitted')
        self.assertEqual(submitted['submissions'][0]['annotations']['posture'], annotations['posture'])

    def test_complete_collection_api_opens_all_clips_and_rejects_partial_registration(self):
        app,client=self.host()
        initial=self.initialize(app,client)
        admin=self.login(client,'admin')
        self.request(client,'DELETE','/items/'+initial['id'],{'expected_revision':initial['revision']},admin)
        project=app.state.service.load(self.project['id'])
        path=self.root/'nas'/'second.mp4';path.write_bytes(b'isolated-second-video-fixture')
        project['videos'].append({**project['videos'][0],'id':'v0002','name':'second.mp4',
            'relative_path':'second.mp4','duration_ms':1000,'start_ms':12000,'end_ms':13000,
            'recording_start':'2026-10-15T00:00:12+08:00'})
        project['_sources']['v0002']={'path':str(path),'stamp':{'size':path.stat().st_size,'mtime_ns':path.stat().st_mtime_ns}}
        project.update(duration_ms=13000,gaps=[{'start_ms':10000,'end_ms':12000}])
        app.state.service.save(project)
        body={'collection_id':project['id'],'storage_backend':'nas','rule_id':initial['rule_id'],
              'annotator_ids':[self.users['a']['id']],'reviewer_id':self.users['reviewer']['id'],'deadline':'2026-10-26'}
        lead=self.login(client,'lead')
        self.request(client,'POST','/items',body,lead,409)
        admin=self.login(client,'admin')
        self.request(client,'POST','/videos',{**self.video,'video_id':'second','duration_ms':1000,
            'source':{'backend':'nas','key':project['id']+'/v0002'}},admin)
        lead=self.login(client,'lead')
        task=self.request(client,'POST','/items',body,lead)
        self.assertEqual(task['video']['video_count'],2)
        self.assertEqual(len(self.request(client,'GET','/items')),1)
        annotator=self.login(client,'a')
        endpoint='/items/'+task['id']+'/workspace/project'
        workspace=self.request(client,'GET',endpoint)
        self.assertEqual([(v['start_ms'],v['end_ms']) for v in workspace['videos']],[(0,10000),(12000,13000)])
        anns={'posture':[{'id':'last','label':'坐','start_ms':12000,'end_ms':13000}]}
        self.request(client,'PUT',endpoint+'/draft',{'expected_revision':task['revision'],'annotations':anns},annotator)
        self.assertEqual(self.request(client,'GET',endpoint)['annotations']['posture'],anns['posture'])
        self.assertEqual(self.request(client,'GET','/items/'+task['id']+'/workspace/previews/v0002')['state'],'queued')


class ArtifactContractTests(unittest.TestCase):
    def test_file_artifacts_immutable_safe_keys_and_readback(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            store = FileArtifacts(root / 'safe')
            payload = b'version'
            self.assertEqual(hashlib.sha256(payload).hexdigest(), store.put('task/v1.json', payload))
            self.assertEqual(payload, store.get('task/v1.json'))
            self.assertEqual(store.put('task/v1.json', payload), store.put('task/v1.json', payload))
            with self.assertRaises(HTTPException) as error:
                store.put('task/v1.json', b'changed')
            self.assertEqual(409, error.exception.status_code)
            for key in ('../escape', '/absolute', 'task//v', 'task/./v'):
                with self.assertRaises(HTTPException):
                    store.put(key, payload)
            with patch.object(store, 'get', return_value=b'corrupt'):
                with self.assertRaises(HTTPException) as error:
                    store.put('task/v2.json', payload)
                self.assertEqual(503, error.exception.status_code)

    def test_file_artifacts_reject_symlinks(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            store = FileArtifacts(root / 'safe')
            try:
                (root / 'safe' / 'link').symlink_to(root, target_is_directory=True)
            except OSError as error:
                if getattr(error, 'winerror', None) == 1314:
                    self.skipTest('Windows account lacks symbolic-link privilege')
                raise
            with self.assertRaises(HTTPException):
                store.put('link/escape', b'version')
            with self.assertRaises(ValueError):
                FileArtifacts(root / 'safe' / 'link')

    def test_object_provider_conditional_create_and_verified_readback(self):
        class Provider:
            def __init__(self):
                self.values = {}
                self.corrupt = False
            def put_if_absent(self, key, payload):
                if key in self.values and self.values[key] != payload:
                    raise HTTPException(409, 'immutable')
                self.values.setdefault(key, payload)
            def get(self, key):
                return b'corrupt' if self.corrupt else self.values[key]
        provider = Provider()
        store = ObjectArtifacts(provider)
        self.assertEqual(hashlib.sha256(b'version').hexdigest(), store.put('v1.json', b'version'))
        self.assertEqual(b'version', store.get('v1.json'))
        store.put('v1.json', b'version')
        with self.assertRaises(HTTPException):
            store.put('v1.json', b'replace')
        with self.assertRaises(HTTPException):
            store.put('../escape', b'version')
        provider.corrupt = True
        with self.assertRaises(HTTPException) as error:
            store.put('v2.json', b'version')
        self.assertEqual(503, error.exception.status_code)
