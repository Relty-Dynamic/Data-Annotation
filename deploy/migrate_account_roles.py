"""Preview and explicitly migrate legacy grants into the shared account store.

Task databases are read-only inputs. Stop all account and task writers before
--apply, and supply every task database that shares the accounts. No media,
drafts, submissions or user identities are rewritten. Reports contain no names
or password material.
"""
from __future__ import annotations
import argparse
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import uuid

from backend.roles import ACCOUNT_ROLES, ROLE_MODEL, ROLE_SCHEMA, legacy_role


class MigrationError(ValueError):
    pass


def readonly(path):
    path = Path(path).absolute()
    if not path.is_file() or any(p.is_symlink() for p in (path, *path.parents)):
        raise MigrationError('数据库必须是现有普通文件，路径不能含符号链接。')
    db = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=30)
    db.row_factory = sqlite3.Row
    if db.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
        db.close()
        raise MigrationError('数据库完整性检查失败。')
    if db.execute('PRAGMA foreign_key_check').fetchall():
        db.close()
        raise MigrationError('数据库关联完整性检查失败。')
    return db


def tables(db):
    return {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def digest(rows):
    return hashlib.sha256(json.dumps(rows, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def inspect(accounts, task_databases=()):
    with closing(readonly(accounts)) as db:
        users = [dict(row) for row in db.execute('SELECT * FROM users ORDER BY id')]
        saved = {row['user_id']: dict(row) for row in db.execute('SELECT * FROM account_roles')} if 'account_roles' in tables(db) else {}
        deleted = {row[0] for row in db.execute('SELECT user_id FROM deleted_accounts')} if 'deleted_accounts' in tables(db) else set()
        fingerprint = digest([users, sorted(saved.items()), sorted(deleted)])
    ids = {user['id'] for user in users}
    grants, assigned, sources, issues = {}, set(), [], []
    paths = sorted({str(Path(path).absolute()) for path in task_databases})
    for path in paths:
        with closing(readonly(path)) as db:
            if not {'roles', 'tasks'} <= tables(db):
                raise MigrationError('任务库缺少 roles 或 tasks 表。')
            role_rows = [tuple(row) for row in db.execute('SELECT user_id,body FROM roles ORDER BY user_id')]
            task_rows = [tuple(row) for row in db.execute('SELECT id,body FROM tasks ORDER BY id')]
            for user_id, body in role_rows:
                try:
                    roles = json.loads(body)
                    role = legacy_role(roles)
                except (ValueError, TypeError):
                    issues.append({'user_id': user_id, 'reason': 'invalid_legacy_roles'})
                    continue
                if user_id not in ids:
                    issues.append({'user_id': user_id, 'reason': 'orphan_legacy_roles'})
                grants.setdefault(user_id, []).append((role, bool(roles)))
            for task_id, body in task_rows:
                try:
                    task = json.loads(body)
                    if not isinstance(task, dict):
                        raise ValueError()
                    people = task.get('annotator_ids', []) + ([task['reviewer_id']] if task.get('reviewer_id') else [])
                    if not isinstance(task.get('annotator_ids', []), list) or any(not isinstance(person, str) for person in people):
                        raise ValueError()
                except (ValueError, TypeError, KeyError):
                    issues.append({'task_id': task_id, 'reason': 'invalid_task_assignment'})
                    continue
                if task.get('status') != 'deleted':
                    assigned.update(people)
            sources.append({'path': path, 'fingerprint': digest([role_rows, task_rows])})
    entries = []
    for user in users:
        ident = user['id']
        if ident in saved:
            row = saved[ident]
            if row['role'] not in ACCOUNT_ROLES or row['task_access'] not in (0, 1) or (user['role'] == 'admin') != (row['role'] == 'admin'):
                issues.append({'user_id': ident, 'reason': 'invalid_existing_account_role'})
            continue
        if user['role'] == 'admin':
            role, access = 'admin', True
        elif user['role'] == 'annotator':
            choices = set(grants.get(ident, []))
            if len(choices) > 1:
                issues.append({'user_id': ident, 'reason': 'conflicting_role_or_revocation_across_task_databases'})
                continue
            if not choices and ident in assigned and user['active'] and ident not in deleted:
                issues.append({'user_id': ident, 'reason': 'assigned_without_legacy_grant'})
                continue
            role, access = next(iter(choices), ('member', True))
        else:
            issues.append({'user_id': ident, 'reason': 'unknown_account_role'})
            continue
        entries.append({'user_id': ident, 'role': role, 'task_access': access,
                        'active': bool(user['active']), 'deleted': ident in deleted})
    # Self-review by managers remains valid. Member self-review needs explicit repair.
    effective = {ident: row['role'] for ident, row in saved.items()}
    effective.update({entry['user_id']: entry['role'] for entry in entries})
    for path in paths:
        with closing(readonly(path)) as db:
            for task_id, body in db.execute('SELECT id,body FROM tasks'):
                try:
                    task = json.loads(body)
                    if not isinstance(task, dict) or not isinstance(task.get('annotator_ids', []), list):
                        continue
                except ValueError:
                    continue
                reviewer = task.get('reviewer_id')
                if task.get('status') not in ('accepted', 'delivered', 'deleted') and reviewer in task.get('annotator_ids', []) and effective.get(reviewer) == 'member':
                    issues.append({'task_id': task_id, 'user_id': reviewer, 'reason': 'member_self_review'})
    return {'model': ROLE_MODEL, 'accounts': str(Path(accounts).absolute()), 'account_count': len(users),
            'already_migrated': len(saved), 'entries': entries, 'issues': issues,
            'account_fingerprint': fingerprint, 'task_sources': sources, 'applied': False}


def migrate(accounts, task_databases=(), *, apply=False, backup_dir=None, no_legacy_tasks=False):
    if apply and not task_databases and not no_legacy_tasks:
        raise MigrationError('须提供所有共用账号的任务库，或明确确认不存在旧任务库。')
    plan = inspect(accounts, task_databases)
    if not apply:
        return plan
    if plan['issues']:
        raise MigrationError('存在迁移异常，未执行。请先查看预检查报告并处理冲突。')
    if not plan['entries']:
        return {**plan, 'applied': True, 'changed': 0, 'backup': None}
    if backup_dir is None:
        raise MigrationError('实际迁移必须指定备份目录。')
    backup_dir = Path(backup_dir).absolute()
    if any(path.is_symlink() for path in (backup_dir, *backup_dir.parents)):
        raise MigrationError('备份目录不能含符号链接。')
    backup_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    backup_dir.chmod(0o700)
    backup = backup_dir / ('auth-before-three-roles-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S') + '-' + uuid.uuid4().hex + '.sqlite3')
    with closing(readonly(accounts)) as original, closing(sqlite3.connect(backup)) as saved:
        original.backup(saved)
        if saved.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
            raise MigrationError('备份完整性检查失败，未执行迁移。')
    backup.chmod(0o600)
    # All relevant writers must be stopped by the operator before --apply.
    # Read snapshots and rechecks detect drift; they are not a distributed lock.
    with closing(sqlite3.connect(accounts, timeout=30)) as db:
        locked = []
        try:
            for source in plan['task_sources']:
                current = readonly(source['path'])
                current.execute('BEGIN')
                role_rows = [tuple(row) for row in current.execute('SELECT user_id,body FROM roles ORDER BY user_id')]
                task_rows = [tuple(row) for row in current.execute('SELECT id,body FROM tasks ORDER BY id')]
                if digest([role_rows, task_rows]) != source['fingerprint']:
                    current.close()
                    raise MigrationError('任务授权或分配已变化，请重新预检查。')
                locked.append(current)
            db.execute('PRAGMA foreign_keys=ON')
            db.execute('BEGIN IMMEDIATE')
            fresh = inspect(accounts, task_databases)
            if fresh != plan:
                raise MigrationError('迁移输入已变化，请重新预检查。')
            # executescript commits implicitly; keep DDL inside this transaction.
            for statement in ROLE_SCHEMA.split(';'):
                if statement.strip():
                    db.execute(statement)
            before = db.execute('SELECT * FROM users ORDER BY id').fetchall()
            for entry in plan['entries']:
                db.execute('INSERT INTO account_roles VALUES (?,?,?)', (entry['user_id'], entry['role'], int(entry['task_access'])))
                db.execute('INSERT INTO account_role_events VALUES (?,?,?,?,?,?,?)', (uuid.uuid4().hex, None, entry['user_id'], int(datetime.now().timestamp()), 'legacy', entry['role'], int(entry['task_access'])))
                db.execute('DELETE FROM sessions WHERE user_id=?', (entry['user_id'],))
                db.execute('DELETE FROM editing_sessions WHERE user_id=?', (entry['user_id'],))
            if before != db.execute('SELECT * FROM users ORDER BY id').fetchall() or db.execute('PRAGMA foreign_key_check').fetchall():
                raise MigrationError('账号身份、密码或关联核验失败，已回滚。')
            # WAL allows writers despite read locks: compare every input again.
            for source in plan['task_sources']:
                with closing(readonly(source['path'])) as current:
                    rows = [tuple(row) for row in current.execute('SELECT user_id,body FROM roles ORDER BY user_id')]
                    tasks = [tuple(row) for row in current.execute('SELECT id,body FROM tasks ORDER BY id')]
                    if digest([rows, tasks]) != source['fingerprint']:
                        raise MigrationError('任务库在迁移期间变化，已回滚。')
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            for current in locked:
                current.close()
    after = inspect(accounts, task_databases)
    if after['entries'] or after['issues']:
        raise MigrationError('迁移后核验失败，请保留备份并暂停使用。')
    return {**plan, 'applied': True, 'changed': len(plan['entries']), 'backup': str(backup)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--accounts', type=Path, required=True)
    parser.add_argument('--tasks', type=Path, action='append', default=[])
    parser.add_argument('--backup-dir', type=Path)
    parser.add_argument('--report', type=Path)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--no-legacy-tasks', action='store_true', help='明确确认没有旧任务库；不能与 --tasks 同用')
    args = parser.parse_args()
    if args.no_legacy_tasks and args.tasks:
        parser.error('--no-legacy-tasks 不能与 --tasks 同用。')
    stream = None
    try:
        if args.report:
            # Reserve before mutating anything, so a bad report path cannot
            # turn a successful migration into an ambiguous CLI failure.
            fd = os.open(args.report, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            stream = os.fdopen(fd, 'w')
        report = migrate(args.accounts, args.tasks, apply=args.apply, backup_dir=args.backup_dir, no_legacy_tasks=args.no_legacy_tasks)
    except (MigrationError, sqlite3.Error, OSError) as error:
        if stream:
            stream.write(json.dumps({'applied': False, 'error': str(error)}, ensure_ascii=False) + '\n')
            stream.close()
        parser.exit(1, str(error) + '\n')
    text = json.dumps(report, ensure_ascii=False, indent=2) + '\n'
    if stream:
        stream.write(text)
        stream.close()
    print(text, end='')
    if report['issues']:
        raise SystemExit(2)


if __name__ == '__main__':
    main()
