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
            if hasattr(media.service, 'uploaded_playback'):
                self.service.uploaded_playback = media.service.uploaded_playback
        self.lock = threading.RLock()

    def project(self, actor, task_id):
        task = self.store.get_task(actor, task_id)
        capture = task.get('collection')
        members = capture['videos'] if capture else [task['video']]
        resolved = []
        for index, member in enumerate(members):
            path, metadata, original = self.media.resolve(member['source'])
            if original['duration_ms'] != member['duration_ms']:
                raise HTTPException(409, '任务素材时长已变化，无法恢复原标注坐标')
            start = member['start_ms'] if capture else 0
            end = member['end_ms'] if capture else member['duration_ms']
            if capture and (original['start_ms'] != start or original['end_ms'] != end):
                raise HTTPException(409, '采集素材录制时间已变化，请核对任务素材')
            video_id = f'v{index + 1:04d}'
            resolved.append((video_id, member, path, metadata, original, start, end))
        ident = hashlib.sha256((task_id + ':' + actor['id']).encode()).hexdigest()[:32]
        with self.lock:
            try:
                project = self.service.load(ident)
            except HTTPException as error:
                if error.status_code != 404:
                    raise
                videos, sources = [], {}
                for video_id, member, path, metadata, original, start, end in resolved:
                    videos.append({**original, 'id': video_id, 'start_ms': start, 'end_ms': end,
                        'relative_path': path.name, 'url': f'/api/media/{ident}/{video_id}',
                        'thumbnail_url': f'/api/thumbnails/{ident}/{video_id}'})
                    source = {**metadata, 'path': str(path)}
                    source.pop('cache_directory', None)
                    sources[video_id] = source
                axes = task['rule']['axes']
                project = {'id': ident, 'name': task['video'].get('collection_name',task['video']['id']) + ' · ' + task['task_type'],
                    'duration_ms': task['video']['duration_ms'],
                    'recording_start': capture['recording_start'] if capture else videos[0]['recording_start'],
                    'gaps': capture.get('gaps',[]) if capture else [],
                    'source_dir': None, 'source_fingerprint': digest(document_bytes([videos, sources])),
                    'videos': videos, '_sources': sources, '_external_hashes': {a: None for a in AXES},
                    'annotations': empty_annotations(), 'fixed_tracks': [axis for axis in AXES if any(a['id'] == axis for a in axes)],
                    'selected_tracks': [a['id'] for a in axes],
                    'custom_tracks': [{k: a[k] for k in ('id','name','mode','labels')} for a in axes if a['id'] not in AXES],
                    'track_labels': {**copy.deepcopy(DEFAULT_TRACK_LABELS), **{a['id']: a['labels'] for a in axes if a['id'] in AXES}},
                    'track_names': {a['id']: a['name'] for a in axes},
                    'revision': 0, 'updated_at': now(), 'draft_dirty': False, '_task_id': task_id,
                    'collection_folder_name': capture['name'] if capture else 'task-' + task_id, '_task_actor': actor['id']}
                for axis in project['custom_tracks']:
                    project['annotations'][axis['id']] = []
                self.service.save(project)
            changed = False
            # Older task workspaces mixed custom labels into the fixed-axis map.
            # Repair only known custom keys; retain strict validation of other data.
            custom_ids = {axis['id'] for axis in project.get('custom_tracks', [])}
            labels = project.get('track_labels', {})
            if custom_ids.intersection(labels):
                project['track_labels'] = {axis: values for axis, values in labels.items() if axis not in custom_ids}
                changed = True
            fixed = project.get('fixed_tracks', [])
            ordered = [axis for axis in AXES if axis in fixed]
            if len(ordered) == len(fixed) and ordered != fixed:
                project['fixed_tracks'] = ordered
                changed = True
            for video_id, member, path, metadata, original, start, end in resolved:
                if metadata.get('r2_playback') and project['_sources'][video_id].get('r2_playback') != metadata['r2_playback']:
                    project['_sources'][video_id]['r2_playback'] = copy.deepcopy(metadata['r2_playback'])
                    changed = True
            if changed:
                self.service.save(project)
        project = copy.deepcopy(project)
        task = self.initialize_prediction(actor, task, project, members)
        own_submission = task.get('current_submissions', {}).get(actor['id'])
        annotations = (task.get('draft') or {}).get('annotations')
        if annotations is None:
            annotations = (own_submission or {}).get('annotations', {})
        project['annotations'] = {**project['annotations'], **annotations}
        project['revision'] = task['revision']
        project['task_readonly'] = actor['id'] not in task['annotator_ids'] or bool(own_submission) or task['status'] not in ('annotating', 'rework')
        project['task_rule'] = task['rule']
        project['task_output_sync'] = task.get('output_sync')
        project['prediction_import'] = (task.get('draft') or {}).get('prediction_import', project.get('prediction_import'))
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
        result['task_output_sync'] = project['task_output_sync']
        result['task_feedback'] = project['task_feedback']
        return self.urls(result, task_id, project['id'])

    def initialize_prediction(self, actor, task, project, members):
        if (project.get('_prediction_checked') or task.get('draft') is not None
                or actor['id'] not in task['annotator_ids'] or task['status'] != 'annotating'
                or actor['id'] in task.get('current_submissions', {})
                or not hasattr(self.media, 'prediction') or 'posture' not in project['selected_tracks']):
            return task
        from ..prediction_import import convert
        from .store import TaskError
        with self.lock:
            task = self.store.get_task(actor, task['id'])
            if (task.get('draft') is not None or actor['id'] in task.get('current_submissions', {})
                    or actor['id'] not in task['annotator_ids'] or task['status'] != 'annotating'):
                return task
            try:
                raw = self.media.prediction(members[0]['source'])
                if raw is not None:
                    rows, metadata = convert(raw, project)
                    task = self.store.initialize_prediction(actor, task['id'], {
                        'expected_revision': task['revision'], 'annotations': {'posture': rows},
                        'prediction_import': metadata})
                    project['prediction_import'] = metadata
            except TaskError as error:
                if error.status == 409:
                    # A simultaneous human save or another participant's task
                    # mutation wins. Read it afresh; never replace that draft.
                    return self.store.get_task(actor, task['id'])
                if error.status != 422:
                    raise
                project['prediction_import'] = {'state': 'rejected', 'detail': '预标注不符合当前任务规则，草稿保持空白。'}
            except (ValueError, OSError, TypeError, KeyError, HTTPException):
                project['prediction_import'] = {'state': 'rejected', 'detail': '预标注无法读取或与任务素材、标签不一致，未自动导入。'}
            project['_prediction_checked'] = True
            self.service.save(project)
        return task

    def direct_session(self, project):
        return self.media.direct_session(project) if hasattr(self.media, 'direct_session') else None

    def session_status(self, project, *, start=False, retry_failed=False):
        if self.direct_session(project) is not None:
            from ..session_cache import SessionCache
            status = SessionCache._initial(project)
            status.update(state='ready', stage='ready', operation='reuse', ready=status['total'],
                          queued=0, progress=100, detail='上传缓存清单已就绪，正在交由浏览器校验本机文件。')
            for item in status['items']:
                item.update(state='ready', operation='reuse', progress=100, detail=None)
            return status
        return (self.service.sessions.start(project['id'], retry_failed=retry_failed) if start
                else self.service.sessions.status(project['id']))

    def session_manifest(self, project, task_id):
        direct = self.direct_session(project)
        if direct is not None:
            return direct
        manifest = self.service.sessions.manifest(project['id'])
        return (self.media.media_manifest(self.service, manifest) if hasattr(self.media, 'media_manifest')
                else self.urls(manifest, task_id, project['id']))

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
