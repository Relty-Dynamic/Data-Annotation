"""Three-role identity, authorization, bridge and legacy migration contracts."""
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from fastapi import HTTPException
from fastapi.testclient import TestClient

from backend.auth import AuthStore
from backend.app import create_app
from backend.roles import ROLE_MODEL
from backend.shared_accounts import create_app as shared_app
from backend.tasks.storage import FileArtifacts
from backend.tasks.store import TaskStore
from deploy.migrate_account_roles import MigrationError, inspect, migrate


class AccountRoleTests(unittest.TestCase):
    def setUp(self):
        parent = Path(__file__).resolve().parents[1] / '.tmp'
        parent.mkdir(exist_ok=True)
        temporary = tempfile.TemporaryDirectory(prefix='account-roles-', dir=parent)
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.auth = AuthStore(self.root)
        self.password = 'isolated fixture password'
        self.admin = self.auth.create_user('fixture-admin', '管理员', self.password, 'admin')
        self.tasks = TaskStore(self.root / 'tasks', FileArtifacts(self.root / 'artifacts'))

    def user(self, name, role='annotator'):
        return self.auth.create_user('fixture-' + name, name, self.password, role)

    def legacy(self):
        # Model an actual pre-upgrade store: users exist but no canonical roles.
        with self.auth.connection() as db:
            db.execute('DELETE FROM account_roles')

    def accounts(self):
        with self.auth.connection() as db:
            return db.execute('SELECT * FROM users ORDER BY id').fetchall()

    def task_data(self):
        with self.tasks.db() as db:
            return {table: db.execute('SELECT * FROM ' + table + ' ORDER BY rowid').fetchall()
                    for table in ('roles', 'tasks', 'drafts', 'submissions', 'audit')}

    def test_new_accounts_default_to_member_and_derive_both_task_capabilities(self):
        member = self.user('new', 'member')
        default = self.auth.create_user('fixture-default', '默认', self.password)
        self.assertEqual('member', default['role'])
        self.assertEqual(ROLE_MODEL, member['role_model'])
        self.assertEqual(['annotator', 'reviewer'], self.tasks.roles(member)['roles'])
        self.tasks.set_roles(self.admin, member['id'], ['leader'])
        self.assertEqual(['annotator', 'reviewer'], self.tasks.roles(member)['roles'], 'legacy rows cannot elevate canonical members')
        self.assertFalse(member['can_upload'])
        self.assertNotIn('password_hash', member)

    def test_role_changes_preserve_identity_and_password_and_revoke_sessions(self):
        member = self.user('changes', 'member')
        _, token, _ = self.auth.login(member['username'], self.password, 'fixture')
        with self.auth.connection() as db:
            before = db.execute('SELECT id,username,email,password_hash FROM users WHERE id=?', (member['id'],)).fetchone()
            db.execute('INSERT INTO assignments VALUES (?,?)', ('source-project', member['id']))
        leader = self.auth.set_role(member['id'], 'leader', self.admin['id'])
        self.assertEqual('leader', leader['role'])
        self.assertTrue(leader['can_upload'])
        self.assertIsNone(self.auth.session(token))
        self.assertEqual(['annotator', 'leader', 'reviewer'], self.tasks.roles(leader)['roles'])
        self.assertEqual(member['id'], self.auth.assignment('source-project'))
        with self.auth.connection() as db:
            self.assertEqual(tuple(before), tuple(db.execute('SELECT id,username,email,password_hash FROM users WHERE id=?', (member['id'],)).fetchone()))
        current, token, _ = self.auth.login(member['username'], self.password, 'fixture')
        self.assertEqual('leader', current['role'])
        self.auth.set_role(member['id'], 'member', self.admin['id'])
        self.assertIsNone(self.auth.session(token))
        current, _, _ = self.auth.login(member['username'], self.password, 'fixture')
        self.assertNotIn('leader', self.tasks.roles(current)['roles'])

    def test_only_admin_changes_roles_and_last_active_admin_cannot_be_demoted(self):
        member = self.user('member', 'member')
        for role in ('leader', 'admin'):
            with self.assertRaises(HTTPException) as denied:
                self.auth.set_role(member['id'], role, member['id'])
            self.assertEqual(403, denied.exception.status_code)
        with self.assertRaises(HTTPException) as denied:
            self.auth.set_role(self.admin['id'], 'member', self.admin['id'])
        self.assertEqual(409, denied.exception.status_code)
        self.auth.delete_user(member['id'], self.admin['id'])
        with self.assertRaises(HTTPException):
            self.auth.set_role(member['id'], 'leader', self.admin['id'])

    def test_http_account_defaults_single_role_and_legacy_endpoint_cannot_override(self):
        env = {'DATAMARK_TASKS_ENABLED': '1', 'DATAMARK_TASKS_STATE': str(self.root / 'http-tasks'),
               'DATAMARK_TASKS_ARTIFACTS': str(self.root / 'http-artifacts'), 'DATAMARK_ORIGIN': '',
               'DATAMARK_S3_MOCK': '0', 'DATAMARK_STORAGE_BACKEND': 'local', 'DATAMARK_TASK_RULES_PUBLIC': '0'}
        with patch.dict('os.environ', env), TestClient(create_app(self.root), base_url='http://127.0.0.1') as client:
            login = client.post('/api/auth/login', json={'username': self.admin['username'], 'password': self.password})
            headers = {'Origin': 'http://127.0.0.1', 'X-CSRF-Token': login.json()['csrf']}
            created = client.post('/api/tasks/accounts', json={'email': 'fixture@example.com', 'display_name': '成员', 'password': self.password}, headers=headers)
            self.assertEqual(200, created.status_code, created.text)
            user = created.json()
            self.assertEqual('member', user['role'])
            self.assertEqual(['annotator', 'reviewer'], user['task_roles'])
            bad = client.put('/api/tasks/roles', json={'user_id': user['id'], 'roles': ['leader']}, headers=headers)
            self.assertEqual(409, bad.status_code)
            promoted = client.put('/api/tasks/accounts/' + user['id'] + '/role', json={'role': 'leader'}, headers=headers)
            self.assertEqual(200, promoted.status_code, promoted.text)
            self.assertEqual('leader', promoted.json()['user']['role'])
            listing = client.get('/api/tasks/accounts').json()
            self.assertEqual('leader', next(row for row in listing if row['id'] == user['id'])['role'])
            self.assertFalse(any('password_hash' in row for row in listing))
            invalid = client.put('/api/tasks/accounts/' + user['id'] + '/role', json={'role': 'reviewer'}, headers=headers)
            self.assertEqual(422, invalid.status_code)

    def test_shared_bridge_exposes_same_role_and_revocation_without_password_material(self):
        member = self.user('bridge', 'member')
        with TestClient(shared_app(self.root), base_url='http://accounts') as client:
            admin = client.post('/v1/login', json={'username': self.admin['username'], 'password': self.password, 'client': 'fixture'}).json()
            login = client.post('/v1/login', json={'username': member['username'], 'password': self.password, 'client': 'fixture'}).json()
            response = client.post('/v1/admin/accounts', json={'token': admin['token'], 'csrf': admin['csrf'], 'action': 'role', 'body': {'user_id': member['id'], 'role': 'leader'}})
            self.assertEqual(200, response.status_code, response.text)
            self.assertEqual(401, client.post('/v1/session', json={'token': login['token']}).status_code)
            users = client.post('/v1/users', json={'token': admin['token']}).json()['users']
            row = next(row for row in users if row['id'] == member['id'])
            self.assertEqual(('leader', ROLE_MODEL, True), (row['role'], row['role_model'], row['task_access']))
            self.assertEqual({'id','display_name','role','active','can_upload','role_model','task_access'}, set(row))

    def test_migration_preview_is_read_only_apply_preserves_passwords_drafts_history_and_is_idempotent(self):
        lead, worker, reviewer, revoked = [self.user(name) for name in ('lead','worker','reviewer','revoked')]
        for person, roles in ((lead,['leader']), (worker,['annotator']), (reviewer,['reviewer']), (revoked,[])):
            self.tasks.set_roles(self.admin, person['id'], roles)
        self.legacy()
        _, old_token, _ = self.auth.login(worker['username'], self.password, 'fixture')
        with self.tasks.db() as db:
            body = {'id':'batch','video_id':'source','status':'annotating','annotator_ids':[worker['id']],'reviewer_id':reviewer['id']}
            self.tasks._save(db, 'tasks', body, True)
            db.execute('INSERT INTO drafts VALUES (?,?,?)', ('batch',worker['id'],json.dumps({'annotations':{'posture':[]}})))
        before, evidence = self.accounts(), self.task_data()
        preview = inspect(self.auth.path, [self.tasks.path])
        self.assertFalse(preview['applied'])
        self.assertEqual([], preview['issues'])
        self.assertEqual(before, self.accounts())
        with self.auth.connection() as db:
            self.assertEqual(0, db.execute('SELECT count(*) FROM account_roles').fetchone()[0])
        result = migrate(self.auth.path, [self.tasks.path], apply=True, backup_dir=self.root / 'backups')
        self.assertEqual(5, result['changed'])
        self.assertTrue(Path(result['backup']).is_file())
        self.assertEqual(before, self.accounts())
        self.assertEqual(evidence, self.task_data())
        self.assertIsNone(self.auth.session(old_token))
        current = {row['id']: row for row in self.auth.list_users()}
        self.assertEqual('leader', current[lead['id']]['role'])
        self.assertEqual('member', current[reviewer['id']]['role'])
        self.assertFalse(current[revoked['id']]['task_access'])
        self.assertEqual([], self.tasks.roles(current[revoked['id']])['roles'])
        saved_again = self.auth.set_role(revoked['id'], 'member', self.admin['id'])
        self.assertFalse(saved_again['task_access'], 'saving the same role must not restore revoked access')
        self.assertEqual('member', self.auth.login(worker['username'], self.password, 'fixture')[0]['role'])
        repeated = migrate(self.auth.path, [self.tasks.path], apply=True, backup_dir=self.root / 'backups')
        self.assertEqual(0, repeated['changed'])
        self.assertIsNone(repeated['backup'])
        self.assertNotIn(self.password, json.dumps(result))

    def test_cross_host_conflicts_stop_before_mutation(self):
        lead = self.user('conflict')
        self.tasks.set_roles(self.admin, lead['id'], ['leader'])
        other = TaskStore(self.root/'other-tasks', FileArtifacts(self.root/'other-artifacts'))
        other.set_roles(self.admin, lead['id'], ['reviewer'])
        self.legacy()
        before = self.accounts()
        report = inspect(self.auth.path, [self.tasks.path, other.path])
        self.assertTrue(any(issue['reason'].startswith('conflicting_') for issue in report['issues']))
        with self.assertRaises(MigrationError):
            migrate(self.auth.path, [self.tasks.path,other.path], apply=True, backup_dir=self.root/'backups')
        self.assertEqual(before, self.accounts())
        with self.auth.connection() as db:
            self.assertEqual(0, db.execute('SELECT count(*) FROM account_roles').fetchone()[0])

    def test_inactive_and_deleted_accounts_stay_disabled_and_manager_self_review_is_preserved(self):
        lead, disabled, deleted = [self.user(name) for name in ('selflead','disabled','deleted')]
        self.tasks.set_roles(self.admin, lead['id'], ['leader'])
        self.auth.set_active(disabled['id'], False)
        self.auth.delete_user(deleted['id'], self.admin['id'])
        self.legacy()
        with self.tasks.db() as db:
            self.tasks._save(db,'tasks',{'id':'self','video_id':'source','status':'submitted','annotator_ids':[lead['id']],'reviewer_id':lead['id']},True)
        before = self.task_data()
        migrate(self.auth.path,[self.tasks.path],apply=True,backup_dir=self.root/'backups')
        self.assertEqual(before,self.task_data())
        for user in (disabled,deleted):
            with self.assertRaises(HTTPException):self.auth.login(user['username'],self.password,'fixture')
        with self.auth.connection() as db:
            self.assertEqual(1,db.execute('SELECT count(*) FROM deleted_accounts').fetchone()[0])

    def test_member_self_review_and_ungranted_assignments_require_explicit_repair(self):
        member = self.user('selfmember')
        self.legacy()
        with self.tasks.db() as db:
            self.tasks._save(db,'tasks',{'id':'self','video_id':'source','status':'submitted','annotator_ids':[member['id']],'reviewer_id':member['id']},True)
        reasons={issue['reason'] for issue in inspect(self.auth.path,[self.tasks.path])['issues']}
        self.assertTrue({'assigned_without_legacy_grant','member_self_review'} & reasons)
        self.tasks.set_roles(self.admin,member['id'],['annotator','reviewer'])
        self.assertIn('member_self_review',{issue['reason'] for issue in inspect(self.auth.path,[self.tasks.path])['issues']})

    def test_failed_apply_rolls_back_roles_and_keeps_old_sessions(self):
        worker=self.user('rollback')
        self.tasks.set_roles(self.admin,worker['id'],['annotator'])
        self.legacy()
        _,token,_=self.auth.login(worker['username'],self.password,'fixture')
        with self.auth.connection() as db:
            db.execute("CREATE TRIGGER fail_migration BEFORE INSERT ON account_role_events BEGIN SELECT RAISE(ABORT,'fixture failure'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            migrate(self.auth.path,[self.tasks.path],apply=True,backup_dir=self.root/'backups')
        self.assertIsNotNone(self.auth.session(token))
        with self.auth.connection() as db:self.assertEqual(0,db.execute('SELECT count(*) FROM account_roles').fetchone()[0])
        self.assertEqual(1,len(list((self.root/'backups').glob('*.sqlite3'))))


if __name__ == '__main__':
    unittest.main()
