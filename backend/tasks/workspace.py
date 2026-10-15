"""Use the existing media pipeline and editor without mutating legacy projects."""
import copy
import hashlib
import threading
from fastapi import HTTPException
from ..service import ProjectService, AXES, DEFAULT_TRACK_LABELS, empty_annotations, now, digest, document_bytes


class Workspaces:
    def __init__(self, root, store, media):
        self.store, self.media = store, media
        self.service = ProjectService(root, enable_remote=False)
        self.service.desktop_cache.enabled = False
        if hasattr(media, 'service'):
            self.service.tool = media.service.tool
        self.lock = threading.RLock()

    def project(self, actor, task_id):
        task = self.store.get_task(actor, task_id)
        path, metadata, original = self.media.resolve(task['video']['source'])
        ident = hashlib.sha256((task_id + ':' + actor['id']).encode()).hexdigest()[:32]
        with self.lock:
            try:
                project = self.service.load(ident)
            except HTTPException as error:
                if error.status_code != 404:
                    raise
                video = {**original, 'id': 'v0001', 'start_ms': 0, 'end_ms': task['video']['duration_ms'],
                         'relative_path': path.name, 'url': f'/api/media/{ident}/v0001',
                         'thumbnail_url': f'/api/thumbnails/{ident}/v0001'}
                source = {**metadata, 'path': str(path)}
                source.pop('cache_directory', None)
                axes = task['rule']['axes']
                project = {'id': ident, 'name': task['video']['id'] + ' · ' + task['task_type'],
                    'duration_ms': video['duration_ms'], 'recording_start': video['recording_start'], 'gaps': [],
                    'source_dir': None, 'source_fingerprint': digest(document_bytes([video, source['stamp']])),
                    'videos': [video], '_sources': {'v0001': source}, '_external_hashes': {a: None for a in AXES},
                    'annotations': empty_annotations(), 'fixed_tracks': [a['id'] for a in axes if a['id'] in AXES],
                    'selected_tracks': [a['id'] for a in axes],
                    'custom_tracks': [{k: a[k] for k in ('id','name','mode','labels')} for a in axes if a['id'] not in AXES],
                    'track_labels': {**copy.deepcopy(DEFAULT_TRACK_LABELS), **{a['id']: a['labels'] for a in axes if a['id'] in AXES}},
                    'track_names': {a['id']: a['name'] for a in axes if a['id'] in AXES},
                    'revision': 0, 'updated_at': now(), 'draft_dirty': False, '_task_id': task_id,
                    'collection_folder_name': 'task-' + task_id, '_task_actor': actor['id']}
                for axis in project['custom_tracks']:
                    project['annotations'][axis['id']] = []
                self.service.save(project)
        project = copy.deepcopy(project)
        own_submission = task.get('current_submissions', {}).get(actor['id'])
        annotations = (task.get('draft') or {}).get('annotations')
        if annotations is None:
            annotations = (own_submission or {}).get('annotations', {})
        project['annotations'] = {**project['annotations'], **annotations}
        project['revision'] = task['revision']
        project['task_readonly'] = actor.get('role') == 'admin' or actor['id'] not in task['annotator_ids'] or bool(own_submission) or task['status'] not in ('annotating', 'rework')
        project['task_rule'] = task['rule']
        project['task_feedback'] = [{key: issue[key] for key in ('id', 'axis', 'segment_id', 'start_ms', 'end_ms', 'reason') if key in issue}
                                    for issue in task.get('issues', []) if task['status'] == 'rework' and issue['id'].startswith('review:')]
        project['updated_at'] = task.get('updated_at', project['updated_at'])
        return project

    def public(self, actor, task_id):
        project = self.project(actor, task_id)
        result = self.service.public(project)
        # Legacy serialization deliberately strips task-only fields. The task
        # editor must keep its verified private draft and structured metadata.
        result['annotations'] = copy.deepcopy(project['annotations'])
        result['task_readonly'] = project['task_readonly']
        result['task_rule'] = project['task_rule']
        result['task_feedback'] = project['task_feedback']
        return self.urls(result, task_id, project['id'])

    @staticmethod
    def urls(value, task_id, ident):
        if isinstance(value, list):
            return [Workspaces.urls(v, task_id, ident) for v in value]
        if isinstance(value, dict):
            return {k: Workspaces.urls(v, task_id, ident) for k, v in value.items()}
        if isinstance(value, str) and value.startswith('/api/'):
            for resource in ('projects','media','thumbnails','previews','storyboards','session-media','session-thumbnails','session-storyboards'):
                prefix = f'/api/{resource}/{ident}'
                if value.startswith(prefix + '/') or value.startswith(prefix + '?') or value == prefix:
                    return f'/api/tasks/items/{task_id}/workspace/{"project" if resource == "projects" else resource}' + value[len(prefix):]
        return value

    def save(self, actor, task_id, body):
        project = self.project(actor, task_id)
        if project['task_readonly']:
            raise HTTPException(409, '已提交版本只读，请等待返修。')
        axes = {a['id'] for a in project['task_rule']['axes']}
        annotations = body.get('annotations', {})
        if not isinstance(annotations, dict):
            raise HTTPException(422, '标注格式不正确。')
        if any(records for axis, records in annotations.items() if axis not in axes):
            raise HTTPException(422, '不能修改任务范围之外的轴。')
        self.store.save_draft(actor, task_id, {**body, 'annotations': {a: annotations.get(a, []) for a in axes}})
        return self.public(actor, task_id)

    def close(self):
        self.service.sessions.close()
        self.service.previews.close()
