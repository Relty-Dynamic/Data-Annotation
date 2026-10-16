"""Verified immutable result storage and existing-media adapters."""
from __future__ import annotations
import hashlib
import os
import re
from pathlib import Path
from fastapi import HTTPException
from ..desktop_cache import collection_name
from .recording import with_recording_layout


def safe_key(key):
    if not isinstance(key, str) or len(key) > 1024 or not re.fullmatch(r'[A-Za-z0-9_.\-/]+', key) or any(p in ('', '.', '..') for p in key.split('/')):
        raise HTTPException(422, '任务文件编号无效。')
    return key


class FileArtifacts:
    def __init__(self, root):
        self.root = Path(root).absolute()
        if any(p.is_symlink() for p in (self.root, *self.root.parents)):
            raise ValueError('Task storage must not contain symlinks')
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)

    def path(self, key):
        path = self.root / safe_key(key)
        if any(p.is_symlink() for p in (path, *path.parents)) or not path.resolve().is_relative_to(self.root.resolve()):
            raise HTTPException(409, '任务存储路径不安全。')
        return path

    def put(self, key, payload):
        path = self.path(key)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            with path.open('xb') as stream:
                os.chmod(path, 0o600)
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
        except FileExistsError:
            if self.get(key) != payload:
                raise HTTPException(409, '已有任务版本不能覆盖。')
        if self.get(key) != payload:
            raise HTTPException(503, '任务文件读回校验失败。')
        return hashlib.sha256(payload).hexdigest()

    def get(self, key):
        return self.path(key).read_bytes()


class ObjectArtifacts:
    """S3/provider contract. The provider must implement conditional create."""
    def __init__(self, provider, prefix='tasks'):
        self.provider, self.prefix = provider, safe_key(prefix)

    def put(self, key, payload):
        object_key = self.prefix + '/' + safe_key(key)
        self.provider.put_if_absent(object_key, payload)
        if self.get(key) != payload:
            raise HTTPException(503, '任务对象读回校验失败。')
        return hashlib.sha256(payload).hexdigest()

    def get(self, key):
        return self.provider.get(self.prefix + '/' + safe_key(key))


class ProjectMedia:
    """Read media through the current NAS/mock service; never expose paths."""
    def __init__(self, service, backend, authorized):
        self.service, self.backend, self.authorized = service, backend, authorized

    def reference(self, actor, body):
        if not isinstance(body, dict) or not isinstance(body.get('source'), dict):
            raise HTTPException(422, '素材登记格式不正确。')
        source = body.get('source', {})
        if source.get('backend') != self.backend:
            raise HTTPException(422, '素材来源与当前存储不一致。')
        match = re.fullmatch(r'([0-9a-f]{32})/(v[0-9]{4})', str(source.get('key', '')))
        if not match or not self.authorized(actor, match[1]):
            raise HTTPException(403, '不能登记未获授权的项目视频。')
        project = self.service.load(match[1])
        video = next((v for v in project['videos'] if v['id'] == match[2]), None)
        if not video or body.get('duration_ms') != video['duration_ms']:
            raise HTTPException(422, '视频时长或编号与素材不一致。')
        self.service.media_source(project, video['id'])
        return {**body, 'collection_id': project['id'], 'collection_name': collection_name(project), 'collection_video_count': len(project['videos']), 'video_name': video['name']}

    def catalog(self, actor):
        if not actor.get('can_upload'):
            raise HTTPException(403, '管理员尚未开放上传权限。')
        return [{'id': project['id'] + '/' + video['id'], 'name': video['name'],
                 **({'collector_name': project['collector_name']} if project.get('collector_name') else {}),
                 'collection': collection_name(project), 'collection_id': project['id'], 'duration_ms': video['duration_ms'],
                 'source': {'backend': self.backend, 'key': project['id'] + '/' + video['id']}}
                for project in self.service.projects() if self.authorized(actor, project['id'])
                for video in project['videos']]

    def resolve(self, source):
        if source.get('backend') != self.backend:
            raise HTTPException(422, '任务素材存储不匹配。')
        match = re.fullmatch(r'([0-9a-f]{32})/(v[0-9]{4})', str(source.get('key', '')))
        if not match:
            raise HTTPException(422, '任务素材编号无效。')
        project = self.service.load(match[1])
        video = next((v for v in project['videos'] if v['id'] == match[2]), None)
        if not video:
            raise HTTPException(404, '任务视频不存在。')
        path, metadata = self.service.media_source(project, video['id'])
        return path, metadata, video

    def prediction(self, source):
        # The source was resolved against the frozen task membership first.
        match = re.fullmatch(r'([0-9a-f]{32})/(v[0-9]{4})', str(source.get('key', '')))
        if not match or source.get('backend') != self.backend:
            raise HTTPException(422, '任务预标注来源无效。')
        project = self.service.load(match[1])
        return self.service.read_prediction(project)

    def task_collection(self, records):
        """Freeze the complete source timeline; filenames are not summed end to end."""
        first = records[0]
        match = re.fullmatch(r'([0-9a-f]{32})/(v[0-9]{4})', first['source']['key'])
        if not match:
            raise HTTPException(422, '采集素材必须来自已导入的完整项目')
        project = self.service.load(match[1])
        by_source = {item['source']['key']: item for item in records}
        expected = {project['id']+'/'+video['id'] for video in project['videos']}
        if set(by_source) != expected or any(item['source']['backend'] != self.backend for item in records):
            raise HTTPException(409, '采集文件夹的视频尚未全部登记，请先完成整个文件夹的登记')
        members = []
        for video in sorted(project['videos'], key=lambda item: (item['start_ms'],item['id'])):
            record = by_source[project['id']+'/'+video['id']]
            _, metadata = self.service.media_source(project,video['id'])
            if video['duration_ms'] != record['duration_ms']:
                raise HTTPException(409, '登记的视频时长与当前素材不一致')
            members.append({**record,'start_ms':video['start_ms'],'end_ms':video['end_ms'],
                            'recording_start':video['recording_start'], 'name':video['name'],
                            'alignment_offset_ms':video.get('alignment_offset_ms', 0)})
        collection = {'id':project['id'],'backend':self.backend,'name':collection_name(project),
                'duration_ms':project['duration_ms'],'recording_start':project['recording_start'],
                'source_fingerprint':project['source_fingerprint'],'videos':members}
        return with_recording_layout({'collection': collection})['collection']


class UnavailableMedia:
    def catalog(self, actor):
        raise HTTPException(503, '真实 S3 素材尚未接入，暂时没有可登记的视频。')
    def reference(self, actor, body):
        raise HTTPException(503, '真实 S3 素材尚未接入，不能登记或标注视频。')
    def resolve(self, source):
        raise HTTPException(503, '真实 S3 素材尚未接入，不能标注视频。')
