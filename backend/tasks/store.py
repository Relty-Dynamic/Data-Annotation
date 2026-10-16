"""Shared task workflow with independent drafts and immutable evidence."""
import csv
import io
import hashlib
import json
import math
import os
import re
import sqlite3
import uuid
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from pathlib import Path
from .validation import validate_annotations
from .diff import differences
from ..service import AXES, valid_custom_axis


class TaskError(Exception):
    def __init__(self, status, detail):
        self.status = self.status_code = status
        self.detail = detail
        super().__init__(str(detail))


def stamp():
    return datetime.now(timezone.utc).isoformat()


def uid():
    return uuid.uuid4().hex


class TaskStore:
    def __init__(self, root, artifacts):
        Path(root).mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = Path(root) / 'tasks.sqlite3'
        self.artifacts = artifacts
        with self.db() as db:
            db.executescript('''
            CREATE TABLE IF NOT EXISTS roles(user_id TEXT PRIMARY KEY, body TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS rules(id TEXT PRIMARY KEY, body TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS deleted_rules(id TEXT PRIMARY KEY REFERENCES rules(id), body TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS videos(id TEXT PRIMARY KEY, body TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS tasks(id TEXT PRIMARY KEY, body TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS drafts(task_id TEXT,user_id TEXT,body TEXT,PRIMARY KEY(task_id,user_id));
            CREATE TABLE IF NOT EXISTS submissions(id TEXT PRIMARY KEY,task_id TEXT,user_id TEXT,body TEXT);
            CREATE TABLE IF NOT EXISTS deliveries(id TEXT PRIMARY KEY, request_id TEXT UNIQUE, body TEXT);
            CREATE TABLE IF NOT EXISTS audit(id TEXT PRIMARY KEY,task_id TEXT,body TEXT);
            CREATE TABLE IF NOT EXISTS requests(id TEXT PRIMARY KEY,body TEXT);
            ''')
            for table in ('rules', 'deleted_rules', 'submissions', 'audit', 'deliveries', 'requests'):
                for action in ('UPDATE', 'DELETE'):
                    db.execute(f"CREATE TRIGGER IF NOT EXISTS immutable_{table}_{action} BEFORE {action} ON {table} BEGIN SELECT RAISE(ABORT,'immutable record'); END")

        if os.name != 'nt':
            os.chmod(self.path, 0o600)

    @contextmanager
    def db(self):
        with closing(sqlite3.connect(self.path, timeout=30)) as db:
            db.execute('PRAGMA foreign_keys=ON')
            with db:
                yield db

    def _read(self, db, table, key):
        row = db.execute(f'SELECT body FROM {table} WHERE id=?', (key,)).fetchone()
        if not row:
            raise TaskError(404, '记录不存在')
        result = json.loads(row[0])
        if table == 'submissions':
            if result.get('type') == 'final':
                payload = {k:v for k,v in result.items() if k != 'artifact_key'}
            else:
                payload = result['annotations']
            if self.artifacts.get(result['artifact_key']) != json.dumps(payload, ensure_ascii=False).encode():
                raise TaskError(503, '保存的标注版本校验失败')
        return result

    def _save(self, db, table, obj, insert=False):
        db.execute(f'{"INSERT INTO" if insert else "UPDATE"} {table} ' + ('(id,body) VALUES (?,?)' if insert else 'SET body=? WHERE id=?'), (obj['id'], json.dumps(obj, ensure_ascii=False)) if insert else (json.dumps(obj, ensure_ascii=False), obj['id']))

    def _audit(self, db, actor, action, task_id=None, **data):
        entry = {'id': uid(), 'task_id': task_id, 'actor_id': actor['id'], 'actor_name': actor.get('display_name', ''), 'action': action, 'at': stamp(), **data}
        db.execute('INSERT INTO audit VALUES (?,?,?)', (entry['id'], task_id, json.dumps(entry, ensure_ascii=False)))

    def _roles(self, db, actor):
        if actor.get('role') == 'admin':
            return ['annotator', 'leader', 'reviewer']
        row = db.execute('SELECT body FROM roles WHERE user_id=?', (actor['id'],)).fetchone()
        roles = json.loads(row[0]) if row else []
        return ['annotator', 'leader', 'reviewer'] if 'leader' in roles else roles

    def _require(self, db, actor, role):
        if role not in self._roles(db, actor):
            raise TaskError(403, '缺少任务角色：' + role)

    def roles(self, actor):
        with self.db() as db:
            return {'roles': self._roles(db, actor)}

    def set_roles(self, actor, user_id, roles):
        if actor.get('role') != 'admin':
            raise TaskError(403, '需要管理员权限')
        if not isinstance(roles, list) or any(r not in ('annotator', 'reviewer', 'leader') for r in roles):
            raise TaskError(422, '任务角色不正确')
        with self.db() as db:
            db.execute('INSERT INTO roles VALUES (?,?) ON CONFLICT(user_id) DO UPDATE SET body=excluded.body', (user_id, json.dumps(sorted(set(roles)))))
            self._audit(db, actor, 'set_roles', user_id=user_id, roles=roles)
        return {'user_id': user_id, 'roles': sorted(set(roles))}

    def list_roles(self, actor):
        with self.db() as db:
            if actor.get('role') != 'admin':
                self._require(db, actor, 'leader')
            return [{'user_id':r[0], 'roles':json.loads(r[1])} for r in db.execute('SELECT user_id,body FROM roles')]

    def list_rules(self, actor):
        with self.db() as db:
            if not self._roles(db, actor):
                raise TaskError(403, '需要任务角色')
            return [json.loads(r[0]) for r in db.execute('SELECT body FROM rules WHERE id NOT IN (SELECT id FROM deleted_rules)')]

    def delete_rule(self, actor, rule_id):
        if actor.get('role') != 'admin':
            raise TaskError(403, '只有超级管理员可删除已发布规则')
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            self._read(db, 'rules', rule_id)
            if not db.execute('SELECT 1 FROM deleted_rules WHERE id=?', (rule_id,)).fetchone():
                entry = {'id': rule_id, 'deleted_by': actor['id'], 'deleted_at': stamp()}
                self._save(db, 'deleted_rules', entry, True)
                self._audit(db, actor, 'delete_rule', rule_id=rule_id)
        return {'id': rule_id, 'deleted': True}

    def publish_rule(self, actor, body):
        with self.db() as db:
            self._require(db, actor, 'leader')
            axes = body.get('axes')
            if not isinstance(body.get('task_type'), str) or not body['task_type'].strip() or len(body['task_type']) > 100 or not body.get('name') or not isinstance(axes, list) or not axes:
                raise TaskError(422, '请填写规则名称、任务类型和时间轴')
            ids = set()
            custom_count = 0
            axes = json.loads(json.dumps(axes))
            def clean_name(value):
                if not isinstance(value, str):
                    raise TaskError(422, '名称及标签必须是文本')
                value = value.strip()
                if not value or len(value) > 80 or any(ord(c) < 32 or 0x7F <= ord(c) <= 0x9F or 0xD800 <= ord(c) <= 0xDFFF for c in value):
                    raise TaskError(422, '名称及标签须为一至八十字且不含控制字符')
                return value
            for axis in axes:
                if not isinstance(axis, dict) or not isinstance(axis.get('id'), str) or not axis['id'] or axis['id'] in ids or (axis['id'] not in AXES and not valid_custom_axis(axis['id'])) or axis.get('mode') not in ('state', 'event') or not isinstance(axis.get('labels'), list) or not axis['labels'] or len(axis['labels']) > 32:
                    raise TaskError(422, '时间轴定义不正确')
                ids.add(axis['id'])
                if axis['id'] in AXES and axis['mode'] != ('event' if axis['id'] == 'habit' else 'state'):
                    raise TaskError(422, '预置时间轴类型不正确')
                custom_count += axis['id'] not in AXES
                if custom_count > 16:
                    raise TaskError(422, '自定义时间轴最多十六条')
                axis['name'] = clean_name(axis.get('name'))
                labels = [label.get('label', label.get('name')) if isinstance(label, dict) else label for label in axis['labels']]
                labels = [clean_name(label) for label in labels]
                axis['labels'] = labels
                if any(not isinstance(label, str) or not label.strip() for label in labels) or len(set(labels)) != len(labels):
                    raise TaskError(422, '标签必须为不重复的非空名称')
                transports = axis.get('transport_labels', [])
                types = axis.get('transport_types', [])
                if not isinstance(transports, list) or not isinstance(types, list) or any(label not in labels for label in transports) or any(not isinstance(kind, str) or not kind.strip() for kind in types) or len(set(types)) != len(types) or (transports and not types):
                    raise TaskError(422, '交通工具规则不正确')
                if isinstance(axis.get('minimum_ms', 0), bool) or not isinstance(axis.get('minimum_ms', 0), (int, float)) or not math.isfinite(axis.get('minimum_ms', 0)) or axis.get('minimum_ms', 0) < 0:
                    raise TaskError(422, '最短片段时长不正确')
            tolerance = body.get('boundary_tolerance_ms')
            if tolerance is not None and (isinstance(tolerance, bool) or not isinstance(tolerance, (int, float)) or not math.isfinite(tolerance) or tolerance < 0):
                raise TaskError(422, '边界容差不正确')
            obj = {**body, 'name': clean_name(body['name']), 'axes': axes, 'id': uid(), 'published_at': stamp(), 'published_by': actor['id'], 'boundary_tolerance_ms': tolerance}
            self._save(db, 'rules', obj, True)
            self._audit(db, actor, 'publish_rule', rule_id=obj['id'])
            return obj

    def list_videos(self, actor):
        with self.db() as db:
            leader = 'leader' in self._roles(db, actor)
            if not leader and not actor.get('can_upload'):
                raise TaskError(403, '需要上传权限或组长角色')
            return [v for r in db.execute('SELECT body FROM videos') if (v := json.loads(r[0])) and (leader or v['uploader_id'] == actor['id'])]

    def _video_record(self, actor, body):
        if not actor.get('can_upload'):
            raise TaskError(403, '需要上传权限')
        vid = body.get('video_id', body.get('id'))
        if not isinstance(vid, str) or not vid.strip() or not all(isinstance(body.get(x), str) and body[x].strip() for x in ('collector_id', 'date', 'scene')) or not isinstance(body.get('source'), dict) or not isinstance(body['source'].get('backend'), str) or not isinstance(body['source'].get('key'), str) or not body['source']['key'] or type(body.get('duration_ms')) is not int or not 0 < body['duration_ms'] <= 9007199254740991 or body.get('quality', 'unknown') not in ('unknown', 'passed', 'failed'):
            raise TaskError(422, '视频信息不完整或不正确')
        return {**body, 'id': vid, 'quality': body.get('quality', 'unknown'), 'uploader_id': actor['id'], 'registered_at': stamp()}

    def register_video(self, actor, body):
        obj = self._video_record(actor, body)
        vid = obj['id']
        with self.db() as db:
            if db.execute('SELECT 1 FROM videos WHERE id=?', (vid,)).fetchone():
                raise TaskError(409, '视频编号已登记，不能重复导入')
            self._save(db, 'videos', obj, True)
            self._audit(db, actor, 'register_video', video_id=vid)
        return obj

    def register_collection(self, actor, bodies):
        """Commit all newly registered members together; never overwrite old records."""
        if not bodies:
            raise TaskError(422, '采集文件夹中没有可登记的视频')
        records = [self._video_record(actor, body) for body in bodies]
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            existing = { (v['source']['backend'], v['source']['key']): v
                        for row in db.execute('SELECT body FROM videos') if (v := json.loads(row[0])) }
            added = []
            for obj in records:
                key = (obj['source']['backend'], obj['source']['key'])
                if key in existing:
                    if existing[key]['uploader_id'] != actor['id']:
                        raise TaskError(409, '该文件夹的视频已由其他账号登记，请联系原登记人')
                    continue
                if db.execute('SELECT 1 FROM videos WHERE id=?', (obj['id'],)).fetchone():
                    raise TaskError(409, '视频编号已登记，不能重复导入')
                self._save(db, 'videos', obj, True)
                self._audit(db, actor, 'register_video', video_id=obj['id'])
                existing[key] = obj
                added.append(obj)
            if not added:
                raise TaskError(409, '该采集文件夹已登记，无需重复登记')
        return {'registered_count': len(added), 'existing_count': len(records) - len(added), 'videos': added}

    def set_quality(self, actor, video_id, quality):
        if isinstance(quality, dict):
            quality = quality.get('quality')
        if quality not in ('passed', 'failed', 'unknown'):
            raise TaskError(422, '质检结论不正确')
        with self.db() as db:
            v = self._read(db, 'videos', video_id)
            if not actor.get('can_upload') or actor['id'] != v['uploader_id']:
                raise TaskError(403, '仅原上传人可修改质检结论')
            v.update(quality=quality, quality_by=actor['id'], quality_at=stamp())
            self._save(db, 'videos', v)
            self._audit(db, actor, 'set_quality', video_id=video_id, quality=quality)
            return v

    def _visible(self, db, actor, task):
        roles = self._roles(db, actor)
        if task['status'] == 'deleted' and 'leader' not in roles:
            raise TaskError(403, '任务已删除，仅管理人员可查看历史')
        if not ('leader' in roles or ('annotator' in roles and actor['id'] in task['annotator_ids']) or ('reviewer' in roles and actor['id'] == task.get('reviewer_id'))):
            raise TaskError(403, '无权访问此任务')

    def _view(self, db, actor, task):
        self._visible(db, actor, task)
        self._verify_final(db, task)
        obj = dict(task)
        submissions = task.get('current_submissions', {})
        complete = bool(task['annotator_ids']) and all(u in submissions for u in task['annotator_ids'])
        obj['submitted_count'] = len(submissions)
        obj['current_submissions'] = {u: self._read(db, 'submissions', sid) for u, sid in submissions.items() if complete or u == actor['id']}
        obj['submissions'] = [{**s, 'actor_id': u} for u, s in obj['current_submissions'].items()]
        row = db.execute('SELECT body FROM drafts WHERE task_id=? AND user_id=?', (task['id'], actor['id'])).fetchone()
        obj['draft'] = json.loads(row[0]) if row else None
        if actor['id'] in task['annotator_ids'] and 'leader' not in self._roles(db, actor) and task['status'] not in ('accepted', 'delivered'):
            for key in ('final_annotations', 'contributors', 'final_version_id', 'resolutions'):
                obj.pop(key, None)
        obj['rule'] = self._read(db, 'rules', task['rule_id'])
        obj['video'] = self._task_video(db, task)
        return obj

    def list_tasks(self, actor, *, include_deleted=False):
        with self.db() as db:
            if include_deleted:
                self._require(db, actor, 'leader')
            result = []
            for row in db.execute('SELECT body FROM tasks'):
                task = json.loads(row[0])
                if task['status'] == 'deleted' and not include_deleted:
                    continue
                try:
                    result.append(self._view(db, actor, task))
                except TaskError as exc:
                    if exc.status != 403:
                        raise
            return result

    def get_task(self, actor, id):
        with self.db() as db:
            return self._view(db, actor, self._read(db, 'tasks', id))

    def history(self, actor, id):
        with self.db() as db:
            task = self._read(db, 'tasks', id)
            self._visible(db, actor, task)
            complete = bool(task['annotator_ids']) and all(u in task['current_submissions'] for u in task['annotator_ids'])
            result = {'submissions': [], 'finals': []}
            for row in db.execute('SELECT id,body FROM submissions WHERE task_id=? ORDER BY rowid', (id,)):
                raw = json.loads(row[1])
                final = raw.get('type') == 'final'
                if final:
                    visible = task['status'] in ('accepted', 'delivered') or (complete and (actor['id'] not in task['annotator_ids'] or 'leader' in self._roles(db, actor)))
                else:
                    visible = complete or raw['user_id'] == actor['id']
                if visible:
                    result['finals' if final else 'submissions'].append(self._read(db, 'submissions', row[0]))
            return result

    def _check_self_review(self, db, actor, annotators, reviewer, participants):
        if reviewer in annotators:
            account = (participants or {}).get(reviewer, actor if reviewer == actor['id'] else {'id': reviewer})
            if 'leader' not in self._roles(db, account):
                raise TaskError(422, '仅管理员和组长可以复核自己的标注')

    @staticmethod
    def _collection_key(video):
        source = video['source']
        ident = video.get('collection_id')
        if not ident:
            match = re.fullmatch(r'([0-9a-f]{32})/v[0-9]{4}', source['key'])
            ident = match[1] if match else None
        return (source['backend'], ident) if ident else None

    def _task_materials(self, db, body):
        if body.get('video_id'):
            first = self._read(db, 'videos', body['video_id'])
            key = self._collection_key(first)
            if key is None:
                return [first], None
        else:
            key = (body.get('storage_backend'), body.get('collection_id'))
            if not all(isinstance(value, str) and value for value in key):
                raise TaskError(422, '请选择完整采集文件夹')
        members = [value for row in db.execute('SELECT body FROM videos')
                   if self._collection_key(value := json.loads(row[0])) == key]
        if not members:
            raise TaskError(404, '采集文件夹尚未登记')
        return members, key

    def task_materials(self, actor, body):
        with self.db() as db:
            self._require(db, actor, 'leader')
            return self._task_materials(db, body)

    def _task_video(self, db, task):
        if not task.get('collection'):
            return self._read(db, 'videos', task['video_id'])
        capture = task['collection']
        members = [self._read(db, 'videos', item['id']) for item in capture['videos']]
        return {**members[0], 'id': task['video_id'], 'collection_id': capture['id'],
                'collection_name': capture['name'], 'video_name': capture['name'],
                'duration_ms': capture['duration_ms'], 'recording_start': capture['recording_start'],
                'video_count': len(members), 'videos': capture['videos'],
                'quality': 'passed' if all(item['quality'] == 'passed' for item in members) else 'unknown'}

    def create_task(self, actor, body, *, participants=None, collection_layout=None):
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            self._require(db, actor, 'leader')
            members, collection_key = self._task_materials(db, body)
            video = members[0]
            if collection_key:
                layout = collection_layout
                if (not isinstance(layout, dict) or layout.get('id') != collection_key[1]
                        or layout.get('backend') != collection_key[0]
                        or {item['id'] for item in layout.get('videos', [])} != {item['id'] for item in members}):
                    raise TaskError(409, '采集素材已变化，请刷新后按完整采集创建任务')
                current = {item['id']: item for item in members}
                if any(current[item['id']]['source'] != item['source'] or current[item['id']]['duration_ms'] != item['duration_ms'] for item in layout['videos']):
                    raise TaskError(409, '采集素材信息已变化，请重新创建任务')
                members = [current[item['id']] for item in layout['videos']]
                video = members[0]
            rule = self._read(db, 'rules', body.get('rule_id'))
            if db.execute('SELECT 1 FROM deleted_rules WHERE id=?', (rule['id'],)).fetchone():
                raise TaskError(409, '规则已删除，请选择其他已发布规则')
            annotators = body.get('annotator_ids', [])
            reviewer = body.get('reviewer_id')
            if not isinstance(annotators, list) or any(not isinstance(u, str) or not u for u in annotators) or len(annotators) > 2 or len(set(annotators)) != len(annotators) or (reviewer is not None and not isinstance(reviewer, str)):
                raise TaskError(422, '任务人员配置不正确')
            self._check_self_review(db, actor, annotators, reviewer, participants)
            try:
                datetime.fromisoformat(body['deadline'].replace('Z', '+00:00'))
            except (KeyError, ValueError, AttributeError):
                raise TaskError(422, '请填写有效截止时间')
            if annotators:
                if any(item['quality'] != 'passed' for item in members) or not reviewer:
                    raise TaskError(409, '视频必须质检合格，并指定复核员')
                for person, role in [(u, 'annotator') for u in annotators] + [(reviewer, 'reviewer')]:
                    participant = (participants or {}).get(person, actor if person == actor['id'] else {'id': person})
                    self._require(db, participant, role)
            for row in db.execute('SELECT body FROM tasks'):
                old = json.loads(row[0])
                old_members = old.get('video_ids', [old['video_id']])
                if old['status'] != 'deleted' and set(old_members).intersection(item['id'] for item in members) and old['task_type'] == rule['task_type']:
                    raise TaskError(409, '该采集已有相同类型任务，或包含尚未删除的旧逐段任务')
            task = {'id': uid(), 'video_id': video['id'], 'rule_id': rule['id'], 'task_type': rule['task_type'], 'annotator_ids': annotators, 'reviewer_id': reviewer, 'deadline': body['deadline'], 'status': 'annotating' if annotators else 'pending', 'revision': 0, 'current_submissions': {}, 'issues': [], 'created_by': actor['id'], 'created_at': stamp()}
            if collection_key:
                task.update(collection=collection_layout, video_ids=[item['id'] for item in members])
            self._save(db, 'tasks', task, True)
            self._audit(db, actor, 'create_task', task['id'])
            return self._view(db, actor, task)

    def _mutation(self, db, actor, id, body):
        if not db.in_transaction:
            db.execute('BEGIN IMMEDIATE')
        task = self._read(db, 'tasks', id)
        self._visible(db, actor, task)
        if task['status'] == 'deleted':
            raise TaskError(410, '任务已删除，不能继续操作')
        if type(body.get('expected_revision')) is not int or body['expected_revision'] != task['revision']:
            raise TaskError(409, {'reason': '任务已更新，请刷新后重试', 'revision': task['revision']})
        return task

    def delete_task(self, actor, id, body):
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            self._require(db, actor, 'leader')
            task = self._read(db, 'tasks', id)
            if task['status'] == 'deleted':
                return {'id': id, 'status': 'deleted', 'revision': task['revision']}
            task = self._mutation(db, actor, id, body)
            previous_status = task['status']
            task.update(status='deleted', deleted_from_status=previous_status,
                        deleted_by=actor['id'], deleted_at=stamp())
            self._finish(db, actor, task, 'delete_task', previous_status=previous_status)
            return {'id': id, 'status': 'deleted', 'revision': task['revision']}

    def assign_task(self, actor, id, body, *, participants=None):
        with self.db() as db:
            task = self._mutation(db, actor, id, body)
            self._require(db, actor, 'leader')
            self._quality(db, task)
            if task['status'] != 'pending':
                raise TaskError(409, '仅待分配任务可以分配人员')
            annotators = body.get('annotator_ids', [])
            reviewer = body.get('reviewer_id')
            if not isinstance(annotators, list) or any(not isinstance(u, str) or not u for u in annotators) or not 1 <= len(annotators) <= 2 or len(set(annotators)) != len(annotators) or not isinstance(reviewer, str) or not reviewer:
                raise TaskError(422, '任务人员配置不正确')
            self._check_self_review(db, actor, annotators, reviewer, participants)
            for person, role in [(u, 'annotator') for u in annotators] + [(reviewer, 'reviewer')]:
                participant = (participants or {}).get(person, actor if person == actor['id'] else {'id': person})
                self._require(db, participant, role)
            deadline = body.get('deadline', task['deadline'])
            try:
                datetime.fromisoformat(deadline.replace('Z', '+00:00'))
            except (ValueError, AttributeError):
                raise TaskError(422, '截止时间不正确')
            task.update(annotator_ids=annotators, reviewer_id=reviewer, deadline=deadline, status='annotating')
            return self._finish(db, actor, task, 'assign_task')

    def _finish(self, db, actor, task, action, **audit_data):
        previous_revision = task['revision']
        task['revision'] += 1
        self._save(db, 'tasks', task)
        self._audit(db, actor, action, task['id'], previous_revision=previous_revision, revision=task['revision'], status=task['status'], **audit_data)
        return self._view(db, actor, task)

    def save_draft(self, actor, id, body):
        with self.db() as db:
            task = self._mutation(db, actor, id, body)
            if actor['id'] not in task['annotator_ids'] or task['status'] not in ('annotating', 'rework') or actor['id'] in task['current_submissions']:
                raise TaskError(403, '当前任务草稿不可编辑')
            self._require(db, actor, 'annotator')
            annotations = body.get('annotations')
            if not isinstance(annotations, dict):
                raise TaskError(422, '请提供标注内容')
            errors = validate_annotations(annotations, self._read(db, 'rules', task['rule_id']), self._task_video(db, task)['duration_ms'], draft=True, recording_runs=task.get('collection', {}).get('recording_runs'))
            if errors:
                raise TaskError(422, {'errors': errors})
            db.execute('INSERT INTO drafts VALUES (?,?,?) ON CONFLICT(task_id,user_id) DO UPDATE SET body=excluded.body', (id, actor['id'], json.dumps({'annotations': annotations}, ensure_ascii=False)))
            return self._finish(db, actor, task, 'save_draft')

    def _quality(self, db, task):
        if self._task_video(db, task)['quality'] != 'passed':
            raise TaskError(409, '视频尚未质检合格')

    def _annotations(self, db, task, annotations):
        errors = validate_annotations(annotations, self._read(db, 'rules', task['rule_id']), self._task_video(db, task)['duration_ms'], recording_runs=task.get('collection', {}).get('recording_runs'))
        if errors:
            raise TaskError(422, {'errors': errors})
        for axis in self._read(db, 'rules', task['rule_id'])['axes']:
            annotations.setdefault(axis['id'], [])

    def _verify_final(self, db, task):
        if task.get('final_version_id'):
            final = self._read(db, 'submissions', task['final_version_id'])
            if any(final.get(stored, {}) != task.get(current, {}) for stored, current in (('annotations', 'final_annotations'), ('contributors', 'contributors'), ('resolutions', 'resolutions'))):
                raise TaskError(503, '最终版本与任务记录不一致')

    def submit(self, actor, id, body):
        with self.db() as db:
            request_id = body.get('request_id')
            request_key = 'submit:' + id + ':' + actor['id'] + ':' + str(request_id)
            if request_id:
                db.execute('BEGIN IMMEDIATE')
                row = db.execute('SELECT body FROM requests WHERE id=?', (request_key,)).fetchone()
                if row:
                    saved = json.loads(row[0])
                    if saved['request'] != body:
                        raise TaskError(409, '重复请求标识对应不同内容')
                    return self._view(db, actor, self._read(db, 'tasks', id))
            task = self._mutation(db, actor, id, body)
            self._quality(db, task)
            if actor['id'] not in task['annotator_ids'] or task['status'] not in ('annotating', 'rework') or actor['id'] in task['current_submissions']:
                raise TaskError(403, '当前任务不可提交或已提交')
            self._require(db, actor, 'annotator')
            annotations = body.get('annotations')
            if annotations is None:
                row = db.execute('SELECT body FROM drafts WHERE task_id=? AND user_id=?', (id, actor['id'])).fetchone()
                annotations = json.loads(row[0])['annotations'] if row else {}
            self._annotations(db, task, annotations)
            sid = uid()
            data = json.dumps(annotations, ensure_ascii=False).encode()
            key = f'tasks/{id}/submissions/{sid}.json'
            self.artifacts.put(key, data)
            if self.artifacts.get(key) != data:
                raise TaskError(503, '提交版本保存后校验失败')
            submission = {'id': sid, 'user_id': actor['id'], 'annotations': annotations, 'artifact_key': key, 'at': stamp()}
            db.execute('INSERT INTO submissions VALUES (?,?,?,?)', (sid, id, actor['id'], json.dumps(submission, ensure_ascii=False)))
            task['current_submissions'][actor['id']] = sid
            if all(u in task['current_submissions'] for u in task['annotator_ids']):
                task['status'] = 'submitted'
            response = self._finish(db, actor, task, 'submit')
            if request_id:
                db.execute('INSERT INTO requests VALUES (?,?)', (request_key,json.dumps({'request':body})))
            return response

    def _reviewer(self, db, actor, task, *, allow_self=False):
        if actor['id'] in task['annotator_ids'] and not (allow_self and 'leader' in self._roles(db, actor)):
            raise TaskError(403, '不能复核、裁定或验收自己标注的任务')
        if actor['id'] != task['reviewer_id'] and 'leader' not in self._roles(db, actor):
            raise TaskError(403, '无权复核此任务')
        if actor['id'] == task['reviewer_id']:
            self._require(db, actor, 'reviewer')

    def start_review(self, actor, id, body):
        with self.db() as db:
            task = self._mutation(db, actor, id, body)
            self._reviewer(db, actor, task, allow_self=True)
            self._quality(db, task)
            if task['status'] != 'submitted':
                raise TaskError(409, '任务尚未完成提交')
            task['status'] = 'reviewing'
            current = [self._read(db, 'submissions', task['current_submissions'][u])['annotations'] for u in task['annotator_ids']]
            tolerance = self._read(db, 'rules', task['rule_id']).get('boundary_tolerance_ms', 0)
            issues = differences(current[0], current[1], tolerance) if len(current) == 2 and tolerance is not None else []
            for u, annotations in zip(task['annotator_ids'], current):
                for axis, segments in annotations.items():
                    for segment in segments:
                        if segment.get('needs_review'):
                            issues.append({'id': f'flag:{u}:{axis}:{segment["id"]}', 'axis': axis,
                                           'segment_id': segment['id'], 'start_ms': segment['start_ms'],
                                           'end_ms': segment['end_ms'], 'reason': segment['review_reason']})
            task['issues'] = issues + [i for i in task.get('issues', []) if i['id'].startswith('review:') and i['id'] not in task.get('resolved_review_issues', [])]
            task.pop('review_complete', None)
            task.pop('resolutions', None)
            task.pop('final_annotations', None)
            task.pop('contributors', None)
            task.pop('final_version_id', None)
            task.pop('adjudicated_by', None)
            return self._finish(db, actor, task, 'start_review')

    def review(self, actor, id, body):
        with self.db() as db:
            task = self._mutation(db, actor, id, body)
            self._reviewer(db, actor, task, allow_self=True)
            self._quality(db, task)
            if task['status'] != 'reviewing' and not (task['status'] == 'adjudicating' and body.get('action') == 'return'):
                raise TaskError(409, '任务不在复核中')
            action = body.get('action', 'return' if body.get('return_to') else 'pass')
            if action not in ('pass', 'return') or (action == 'return' and not body.get('return_to')) or (action == 'pass' and body.get('return_to')):
                raise TaskError(422, '返修操作必须指定标注员，复核通过不能同时退回')
            issues = body.get('issues', [])
            notes = body.get('notes', [])
            if not isinstance(issues, list) or not isinstance(notes, list) or any(not isinstance(i, dict) or not isinstance(i.get('reason'), str) or not i['reason'].strip() or len(i['reason']) > 2000 or not i.get('axis') or not (i.get('segment_id') or ('start_ms' in i and 'end_ms' in i)) for i in issues + notes):
                raise TaskError(422, '复核意见必须包含原因及片段或时间范围')
            rule_axes = {a['id'] for a in self._read(db, 'rules', task['rule_id'])['axes']}
            duration = self._task_video(db, task)['duration_ms']
            for issue in issues + notes:
                if issue['axis'] not in rule_axes:
                    raise TaskError(422, '复核意见的时间轴不存在')
                if issue.get('segment_id'):
                    if not any(s['id'] == issue['segment_id'] for sid in task['current_submissions'].values() for s in self._read(db, 'submissions', sid)['annotations'].get(issue['axis'], [])):
                        raise TaskError(422, '复核意见的片段不存在')
                if 'start_ms' in issue or 'end_ms' in issue:
                    start, end = issue.get('start_ms'), issue.get('end_ms')
                    if not all(type(v) is int for v in (start, end)) or not 0 <= start <= end <= duration:
                        raise TaskError(422, '复核意见的时间范围不正确')
            task['issues'] += [{**i, 'id': 'review:' + uid(), 'actor_id': actor['id']} for i in issues]
            resolved = body.get('resolved_issue_ids', [])
            if not isinstance(resolved, list) or any(i not in [issue['id'] for issue in task['issues'] if issue['id'].startswith('review:')] for i in resolved):
                raise TaskError(422, '待解决复核意见编号不正确')
            task.setdefault('resolved_review_issues', []).extend(resolved)
            task['issues'] = [i for i in task['issues'] if i['id'] not in resolved]
            task['reviewed_by'] = actor['id']
            if notes:
                task.setdefault('review_notes', []).extend([{**note, 'actor_id': actor['id'], 'at': stamp()} for note in notes])
            if action == 'return':
                targets = body['return_to']
                if not isinstance(targets, list) or any(u not in task['annotator_ids'] for u in targets) or len(set(targets)) != len(targets) or not issues:
                    raise TaskError(422, '返修必须指定标注员并填写复核意见')
                for u in targets:
                    sid = task['current_submissions'].pop(u)
                    annotations = self._read(db, 'submissions', sid)['annotations']
                    db.execute('INSERT INTO drafts VALUES (?,?,?) ON CONFLICT(task_id,user_id) DO UPDATE SET body=excluded.body', (id, u, json.dumps({'annotations': annotations})))
                task['status'] = 'rework'
                for key in ('review_complete', 'final_annotations', 'contributors', 'final_version_id', 'resolutions', 'adjudicated_by'):
                    task.pop(key, None)
            elif task['issues']:
                task['status'] = 'adjudicating'
            else:
                task['review_complete'] = True
            return self._finish(db, actor, task, 'review', outcome=action, issues=issues, notes=notes, return_to=body.get('return_to', []), resolved_issue_ids=resolved)

    def adjudicate(self, actor, id, body):
        with self.db() as db:
            task = self._mutation(db, actor, id, body)
            self._reviewer(db, actor, task, allow_self=True)
            self._quality(db, task)
            if task['status'] != 'adjudicating':
                raise TaskError(409, '任务不在待裁定状态')
            resolutions = body.get('resolutions', {})
            if not isinstance(resolutions, dict) or any(not str(resolutions.get(i['id'], '')).strip() for i in task['issues']):
                raise TaskError(422, '每项待裁定意见都必须填写结论')
            final = body.get('annotations', {})
            self._annotations(db, task, final)
            contributors = body.get('contributors', {})
            if not isinstance(contributors, dict):
                raise TaskError(422, '请按片段提供贡献者账号')
            for axis, segments in final.items():
                for s in segments:
                    people = contributors.get(axis + ':' + s['id'])
                    if not isinstance(people, list) or not people or any(p not in task['annotator_ids'] for p in people):
                        raise TaskError(422, '每个最终片段都必须指定标注贡献者')
            task.update(final_annotations=final, contributors=contributors, resolutions=resolutions, adjudicated_by=actor['id'], status='reviewing', review_complete=True)
            self._final_snapshot(db, actor, task)
            return self._finish(db, actor, task, 'adjudicate')

    def _final_snapshot(self, db, actor, task):
        sid = uid()
        snapshot = {'id': sid, 'type': 'final', 'user_id': actor['id'], 'annotations': task['final_annotations'], 'contributors': task['contributors'], 'resolutions': task.get('resolutions', {}), 'at': stamp()}
        key = f'tasks/{task["id"]}/final/{sid}.json'
        data = json.dumps(snapshot, ensure_ascii=False).encode()
        self.artifacts.put(key, data)
        if self.artifacts.get(key) != data:
            raise TaskError(503, '最终版本保存后校验失败')
        snapshot['artifact_key'] = key
        db.execute('INSERT INTO submissions VALUES (?,?,?,?)', (sid, task['id'], actor['id'], json.dumps(snapshot, ensure_ascii=False)))
        task['final_version_id'] = sid

    def accept(self, actor, id, body):
        with self.db() as db:
            task = self._mutation(db, actor, id, body)
            self._require(db, actor, 'leader')
            self._reviewer(db, actor, task, allow_self=True)
            self._quality(db, task)
            if task['status'] != 'reviewing' or not task.get('review_complete') or (task['issues'] and not task.get('resolutions')):
                raise TaskError(409, '复核或裁定尚未完成')
            if 'final_annotations' not in task:
                task['final_annotations'] = self._read(db, 'submissions', task['current_submissions'][task['annotator_ids'][0]])['annotations']
                task['contributors'] = {}
                for axis, items in task['final_annotations'].items():
                    for segment in items:
                        contributors = []
                        for user_id in task['annotator_ids']:
                            original = self._read(db, 'submissions', task['current_submissions'][user_id])['annotations'].get(axis, [])
                            for candidate in original:
                                same_label = candidate['label'] == segment['label'] and candidate.get('transport_type') == segment.get('transport_type') and candidate.get('note', candidate.get('notes', '')) == segment.get('note', segment.get('notes', ''))
                                overlap = max(candidate['start_ms'], segment['start_ms']) < min(candidate['end_ms'], segment['end_ms']) or candidate['start_ms'] == candidate['end_ms'] == segment['start_ms'] == segment['end_ms']
                                if same_label and overlap:
                                    contributors.append(user_id)
                                    break
                        task['contributors'][axis + ':' + segment['id']] = contributors
                self._final_snapshot(db, actor, task)
            task.update(status='accepted', accepted_by=actor['id'], accepted_at=stamp())
            return self._finish(db, actor, task, 'accept')

    def deliver(self, actor, body):
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            self._require(db, actor, 'leader')
            request_id = body.get('request_id')
            ids = body.get('task_ids')
            if not isinstance(request_id, str) or not request_id or not isinstance(ids, list) or not ids or len(set(ids)) != len(ids):
                raise TaskError(422, '请提供不重复的任务及请求标识')
            old = db.execute('SELECT body FROM deliveries WHERE request_id=?', (request_id,)).fetchone()
            if old:
                result = json.loads(old[0])
                if result['task_ids'] != ids:
                    raise TaskError(409, '请求标识已被其他交付任务使用')
                return result
            tasks = [self._read(db, 'tasks', t) for t in ids]
            revisions = body.get('expected_revisions', {})
            for task in tasks:
                self._verify_final(db, task)
                self._quality(db, task)
                if task['status'] != 'accepted' or revisions.get(task['id']) != task['revision']:
                    raise TaskError(409, '仅可交付版本一致且已验收的任务')
            fields = ['video_id','collector_id','uploader_id','final_version_id','task_type','rule_id','axis','segment_id','lane','start_seconds','end_seconds','start_ms','end_ms','label','transport_type','annotators','reviewer','adjudicator']
            fields += ['collection_id','collection_name','video_ids','recording_start']
            stream = io.StringIO(newline='')
            writer = csv.DictWriter(stream, fieldnames=fields)
            writer.writeheader()
            for task in tasks:
                video = self._task_video(db, task)
                for axis, segments in task['final_annotations'].items():
                    for s in segments:
                        writer.writerow({'video_id': task['video_id'], 'collector_id': video['collector_id'], 'uploader_id': video['uploader_id'], 'final_version_id': task['final_version_id'], 'task_type': task['task_type'], 'rule_id': task['rule_id'], 'axis': axis, 'segment_id': s['id'], 'lane': s.get('lane',0), 'start_seconds': f'{s["start_ms"]/1000:.1f}', 'end_seconds': f'{s["end_ms"]/1000:.1f}', 'start_ms': s['start_ms'], 'end_ms': s['end_ms'], 'label': s['label'], 'transport_type': s.get('transport_type',''), 'annotators': '|'.join(task['contributors'][axis+':'+s['id']]), 'reviewer': task.get('reviewed_by',task['reviewer_id']), 'adjudicator': task.get('adjudicated_by',''),
                                         'collection_id':task.get('collection',{}).get('id',''),
                                         'collection_name':task.get('collection',{}).get('name',''),
                                         'video_ids':'|'.join(task.get('video_ids',[task['video_id']])),
                                         'recording_start':task.get('collection',{}).get('recording_start','')})
            did = uid()
            key = f'deliveries/{did}.csv'
            content = stream.getvalue().encode('utf-8-sig')
            self.artifacts.put(key, content)
            if self.artifacts.get(key) != content:
                raise TaskError(503, '交付文件保存后校验失败')
            result = {'id': did, 'request_id': request_id, 'task_ids': ids, 'artifact_key': key, 'sha256': hashlib.sha256(content).hexdigest(), 'size': len(content), 'created_at': stamp(), 'created_by': actor['id'], 'versions': {t['id']:t['revision'] for t in tasks}}
            db.execute('INSERT INTO deliveries VALUES (?,?,?)', (did, request_id, json.dumps(result)))
            for task in tasks:
                task.update(status='delivered', delivery_id=did)
                self._finish(db, actor, task, 'deliver')
            return result

    def delivery(self, actor, id):
        with self.db() as db:
            self._require(db, actor, 'leader')
            result = self._read(db, 'deliveries', id)
            data = self.artifacts.get(result['artifact_key'])
            if len(data) != result['size'] or hashlib.sha256(data).hexdigest() != result['sha256']:
                raise TaskError(503, '交付文件内容校验失败')
            return result

    def list_deliveries(self, actor):
        with self.db() as db:
            self._require(db, actor, 'leader')
            return [json.loads(r[0]) for r in db.execute('SELECT body FROM deliveries ORDER BY rowid DESC')]

    def audit(self, actor, task_id=None):
        with self.db() as db:
            if task_id:
                self._visible(db, actor, self._read(db, 'tasks', task_id))
            else:
                self._require(db, actor, 'leader')
            query = 'SELECT body FROM audit' + (' WHERE task_id=?' if task_id else '') + ' ORDER BY rowid'
            return [json.loads(r[0]) for r in db.execute(query, (task_id,) if task_id else ())]
