"""R2 collection projects using the existing editor, caches and JSON format."""
from __future__ import annotations

import copy
import hashlib
import hmac
import json
import os
import re
import secrets
import time
from pathlib import Path

from fastapi import HTTPException

from .r2_store import object_key
from .service import AXES, ProjectService, document_bytes, digest, filename_for_axis, now


CAPTURE = re.compile(r'daily/[0-9]{8}/[\w.-]{1,120}\Z')


def capture_prefix(value):
    if not isinstance(value, str) or not CAPTURE.fullmatch(value) or value.split('/')[-1] in ('.', '..'):
        raise HTTPException(422, '请选择 daily/日期/采集目录。')
    return value


class R2ProjectService(ProjectService):
    def __init__(self, root, provider):
        self.provider = provider
        super().__init__(root, enable_remote=False)
        self.desktop_cache.enabled = False
        secret_path = self.local / 'r2-claim-key'
        try:
            with secret_path.open('xb') as stream:
                os.chmod(secret_path, 0o600)
                stream.write(secrets.token_bytes(32))
        except FileExistsError:
            pass
        self.claim_key = secret_path.read_bytes()
        with self.connection() as con:
            con.execute('CREATE TABLE IF NOT EXISTS r2_editing (project TEXT, tab TEXT, owner TEXT, document TEXT, touched REAL, PRIMARY KEY(project,tab))')

    def attach_source_caches(self, project, **kwargs):
        project['_storage_version'] = 3
        project['_cache_warnings'] = []

    def public(self, project):
        value = super().public(project)
        value.update(storage_mode='s3', storage_prefix=project.get('_r2_prefix'), source_dir=None,
                     cache_directories=[], needs_source_relink=False, owner_id=project.get('_r2_owner'))
        collector = project.get('_r2_manifest', {}).get('collector_name')
        if isinstance(collector, str) and collector.strip():
            value['collector_name'] = collector.strip()
        return value

    def allowed(self, actor, ident):
        return actor.get('role') == 'admin' or self.load(ident).get('_r2_owner') == actor['id']

    def read_prediction(self, project):
        from .prediction_import import MAX_BYTES, verify_inputs
        items = [item for item in project.get('_r2_manifest', {}).get('attachments', [])
                 if item.get('name', '').casefold() == 'prediction/posture.json']
        if not items:
            return None
        if len(items) != 1 or type(items[0].get('size')) is not int or not 0 < items[0]['size'] <= MAX_BYTES:
            raise ValueError('S3 姿势预标注清单无效。')
        item = items[0]
        key = object_key(item['key'])
        if not key.startswith('prediction/'):
            raise ValueError('S3 姿势预标注路径无效。')
        destination = self.local / 'r2-predictions' / project['_r2_capture_sha256'] / 'posture.json'
        self.provider.download(project['_r2_prefix'] + '/' + key, destination, item['sha256'], item['size'])
        raw = destination.read_bytes()
        if len(raw) != item['size'] or hashlib.sha256(raw).hexdigest() != item['sha256']:
            raise ValueError('S3 姿势预标注内容与发布清单不一致。')
        attachments = {row.get('name', '').casefold(): row for row in project['_r2_manifest'].get('attachments', [])}
        verify_inputs(raw, lambda path: attachments.get(path.casefold(), {}).get('sha256'))
        return raw

    def browse(self, prefix=''):
        prefix = prefix or ''
        if prefix and (prefix.startswith('/') or '..' in prefix.split('/') or len(prefix.split('/')) > 3):
            raise HTTPException(422, 'S3 目录无效。')
        if prefix and prefix.split('/')[0] != 'daily':
            raise HTTPException(422, '只能浏览采集目录。')
        entries = []
        for page in self.provider.list(prefix + '/' if prefix else 'daily/', delimiter='/'):
            for item in page.get('CommonPrefixes', []):
                key = item['Prefix'].rstrip('/')
                entries.append({'name': key.split('/')[-1], 'path': key, 'kind': 'directory'})
        if not prefix:
            entries = [{'name': 'daily', 'path': 'daily', 'kind': 'directory'}]
        manifest = capture = None
        if CAPTURE.fullmatch(prefix):
            manifest, _ = self.provider.optional_json(prefix + '/manifest.json')
            imu, _ = self.provider.optional_json(prefix + '/imu.manifest.json')
            capture = manifest or imu
            if capture:
                files = (manifest or {}).get('files', []) + (manifest or {}).get('attachments', []) + (imu or {}).get('attachments', [])
                entries = list({item['key']: {'name': item.get('name', Path(item['key']).name), 'path': prefix + '/' + item['key'],
                            'kind': 'file', 'size': item['size']} for item in files}.values())
        return {'root': '', 'path': prefix, 'parent': '/'.join(prefix.split('/')[:-1]) if prefix else None,
                'entries': sorted(entries, key=lambda item: item['name']), 'page': 0, 'has_more': False,
                'can_open': manifest is not None, 'capture': {key: capture.get(key) for key in
                    ('collector_name','uploader_name','capture_date','note')} if capture else None}

    def collection(self, prefix, *, download=True):
        prefix = capture_prefix(prefix)
        manifest, _ = self.provider.optional_json(prefix + '/manifest.json')
        if (not isinstance(manifest, dict) or manifest.get('prefix') != prefix
                or not isinstance(manifest.get('files'), list) or not 1 <= len(manifest['files']) <= 1000):
            raise HTTPException(422, 'S3 采集目录尚未完整发布。')
        identity = digest(document_bytes(manifest))
        directory = self.local / 'r2-captures' / identity
        names = set()
        for index, item in enumerate(manifest['files']):
            if not isinstance(item, dict):
                raise HTTPException(422, 'S3 视频清单无效。')
            name = item.get('name', Path(item.get('key', '')).name)
            if (not isinstance(name, str) or Path(name).name != name or not name.lower().endswith('.mp4')
                    or name.casefold() in names or not isinstance(item.get('key'), str)
                    or not item['key'].startswith('FPV/') or '\\' in name):
                raise HTTPException(422, 'S3 视频清单无效。')
            names.add(name.casefold())
            object_key(item['key'])
            if download:
                self.provider.download(prefix + '/' + item['key'], directory / 'FPV' / name, item['sha256'], item['size'])
        return manifest, identity, directory

    def open_capture(self, prefix, *, actor=None, body=None, inspect=False):
        body = body or {}
        with self.lock:
            manifest, identity, directory = self.collection(prefix, download=False)
            from .upload_intake import metadata
            source_metadata=metadata(self,prefix,manifest,directory)
            if source_metadata is None:
                if 'cache_profile' in manifest:
                    raise HTTPException(409,'此采集缺少服务端核验元数据，请先恢复上传核验；登记不会重新下载视频。')
                manifest, identity, directory = self.collection(prefix)
            else:
                directory.mkdir(parents=True,exist_ok=True)
            # Validate ownership and the inspection token before any draft replacement.
            pointer, etag = self.provider.optional_json(prefix + '/timeline/.latest.json')
            with self.connection() as con:
                row = con.execute('SELECT document FROM projects WHERE source_key=?', (os.path.normcase(str(directory.resolve())),)).fetchone()
            previous = json.loads(row[0]) if row else {}
            owner = previous.get('_r2_owner')
            def token(value):
                payload = document_bytes([prefix, actor['id'] if actor else '', value.get('_r2_owner'), etag, value.get('revision')])
                return hmac.new(self.claim_key, payload, hashlib.sha256).hexdigest()
            if not inspect:
                takeover = owner and actor and owner != actor['id'] and actor.get('role') != 'admin'
                replaces = body.get('claim_mode') in ('continue', 'restart')
                if takeover or replaces:
                    supplied = body.get('claim_token')
                    if not isinstance(supplied, str) or not hmac.compare_digest(supplied, token(previous)):
                        raise HTTPException(409, '采集目录状态已变化，请重新检查并确认。')
                if takeover and body.get('takeover_confirmations') != 2:
                    raise HTTPException(409, '采集目录已被认领，请完成两次接管确认。')
                if pointer and not previous and not replaces:
                    raise HTTPException(409, '此目录已有标注，请先检查并选择沿用或重新开始。')
            # Read complete committed JSON versions into a private local mirror.
            mirrors = directory / 'timeline'
            if pointer:
                if not isinstance(pointer.get('axes'), dict):
                    raise HTTPException(422, 'S3 标注指针格式无效。')
                expected_files = {filename_for_axis(axis) for axis in pointer['axes']}
                if mirrors.exists():
                    for path in mirrors.glob('*.timeline.json'):
                        if path.name not in expected_files:
                            path.unlink()  # Private mirror only; committed R2 history remains immutable.
                for axis, item in pointer.get('axes', {}).items():
                    filename = filename_for_axis(axis)
                    if not item['key'].startswith(prefix + '/timeline/versions/'):
                        raise HTTPException(409, 'S3 标注版本路径无效。')
                    raw = self.provider.get(item['key'])
                    if hashlib.sha256(raw).hexdigest() != item['sha256']:
                        raise HTTPException(409, 'S3 标注版本内容不一致。')
                    document = json.loads(raw)
                    if document.get('timebase', {}).get('r2_capture_sha256') != identity:
                        raise HTTPException(409, 'S3 标注属于另一批素材。')
                    mirrors.mkdir(exist_ok=True)
                    target = mirrors / filename
                    if not target.is_file() or target.read_bytes() != raw:
                        temporary = target.with_suffix('.tmp')
                        temporary.write_bytes(raw)
                        os.replace(temporary, target)
            # Source files are content-verified; normalize host-specific identity
            # only after matching the immutable R2 collection digest above.
            if mirrors.exists():
                fresh = self.create([directory/'FPV'/name for name in source_metadata] if source_metadata is not None else list((directory/'FPV').iterdir()), directory.name, directory, metadata_by_name=source_metadata)
                for path in mirrors.glob('*.timeline.json'):
                    document = json.loads(path.read_bytes())
                    aliases = document['timebase'].setdefault('source_fingerprint_aliases', [])
                    if fresh['source_fingerprint'] != document['timebase']['source_fingerprint'] and fresh['source_fingerprint'] not in aliases:
                        aliases.append(fresh['source_fingerprint'])
                        path.write_bytes(document_bytes(document))
            project = super().open_path(str(directory), name=body.get('name') or prefix.split('/')[-1],
                                        claim_mode=body.get('claim_mode') if body.get('claim_mode') in ('continue', 'restart') else None, _metadata=source_metadata)
            stored = self.load(project['id'])
            stored.update(_r2_prefix=prefix, _r2_capture_sha256=identity, _r2_manifest=manifest)
            from .upload_playback import reference
            for video in stored['videos']:
                stored['_sources'][video['id']]['r2_playback'] = reference(prefix, manifest, video['name'])
                if source_metadata is not None:
                    stored['_sources'][video['id']]['storage'] = 's3'
            stored.setdefault('_r2_collection_named', False)
            if not stored['_r2_collection_named']:
                stored['collection_folder_name'] = prefix.split('/')[-1]
                stored['_r2_collection_named'] = True
            owner = stored.get('_r2_owner')
            claim_token = token(stored)
            if inspect:
                self.save(stored)
                return {'path': prefix, 'project_id': stored['id'], 'project_name': stored['name'], 'owner_name': stored.get('_r2_owner_name') or owner,
                        'has_platform_draft': bool(stored.get('draft_dirty')), 'has_json': bool(pointer),
                        'requires_choice': bool(pointer) or bool(owner and actor and owner != actor['id']),
                        'requires_takeover': bool(owner and actor and owner != actor['id'] and actor.get('role') != 'admin'),
                        'owner': {'id': owner, 'display_name': '已认领账号'} if owner else None, 'claim_token': claim_token}
            if actor:
                stored['_r2_owner'] = owner if actor.get('role') == 'admin' and owner else actor['id']
                if stored['_r2_owner'] == actor['id']:
                    stored['_r2_owner_name'] = actor.get('display_name') or actor['id']
            if not stored.get('draft_dirty') or '_r2_timeline_etag' not in stored or body.get('claim_mode') in ('continue', 'restart'):
                stored['_r2_timeline_etag'] = etag
            if body.get('claim_mode') == 'restart':
                stored['draft_dirty'] = True
            self.save(stored)
            return self.public(stored)

    def uploaded_playback(self, source, directory, key):
        from .upload_playback import hydrate
        return hydrate(self.provider, source, directory, key)

    def media_source(self, project, video_id):
        source=project['_sources'].get(video_id,{})
        if source.get('storage')=='s3' and source.get('r2_playback'):
            capture=source['r2_playback']
            item=capture['manifest']['files'][0]
            if source['stamp']['size'] != item['size']:
                raise HTTPException(409,'S3 素材记录与冻结清单不一致。')
            return Path(source['path']),source
        path, metadata = super().media_source(project, video_id)
        if 'r2_playback' not in metadata and project.get('_r2_manifest'):
            from .upload_playback import reference
            name = next(video['name'] for video in project['videos'] if video['id'] == video_id)
            metadata = {**metadata, 'r2_playback': reference(project['_r2_prefix'], project['_r2_manifest'], name)}
        return path, metadata

    def direct_session(self, ident):
        from .upload_playback import direct_manifest
        return direct_manifest(self.provider,self.load(ident))

    def session_status(self, ident):
        if self.direct_session(ident) is not None:
            from .session_cache import SessionCache
            result=SessionCache._initial(self.load(ident))
            result.update(state='ready',stage='ready',operation='reuse',ready=result['total'],queued=0,progress=100,
                          detail='上传缓存已就绪，正在交由浏览器校验本机文件。')
            for item in result['items']:item.update(state='ready',operation='reuse',progress=100,detail=None)
            return result
        return self.sessions.status(ident)

    def browser_ready(self, ident):
        return {'released':False} if self.direct_session(ident) is not None else self.sessions.browser_ready(ident)

    def session_manifest(self, ident):
        direct=self.direct_session(ident)
        return direct if direct is not None else self.media_manifest(self,self.sessions.manifest(ident))

    def start_session(self, ident, **kwargs):
        if self.direct_session(ident) is not None:
            return self.session_status(ident)
        from .upload_playback import reference
        with self.lock:
            project = self.load(ident)
            changed = False
            for video in project['videos']:
                source = project['_sources'][video['id']]
                if 'r2_playback' not in source and project.get('_r2_manifest'):
                    source['r2_playback'] = reference(project['_r2_prefix'], project['_r2_manifest'], video['name'])
                    changed = True
            if changed:
                self.save(project)
        return super().start_session(ident, **kwargs)

    def editing(self, ident, actor, tab, *, leave=False):
        if not isinstance(tab, str) or not 1 <= len(tab) <= 200:
            raise HTTPException(422, '页面标识无效。')
        with self.connection() as con:
            con.execute('DELETE FROM r2_editing WHERE touched<?', (time.time() - 45,))
            if leave:
                con.execute('DELETE FROM r2_editing WHERE project=? AND tab=? AND owner=?', (ident, tab, actor['id']))
            else:
                visible = {key: actor.get(key) for key in ('id', 'display_name', 'role')}
                con.execute('INSERT INTO r2_editing VALUES (?,?,?,?,?) ON CONFLICT(project,tab) DO UPDATE SET owner=excluded.owner,document=excluded.document,touched=excluded.touched',
                            (ident, tab, actor['id'], json.dumps(visible), time.time()))
            rows = con.execute('SELECT DISTINCT document FROM r2_editing WHERE project=? AND owner!=?', (ident, actor['id'])).fetchall()
        return {'others': [json.loads(row[0]) for row in rows]}

    def writeback(self, ident, *, expected_revision=None):
        with self.lock:
            project = self.load(ident)
            if expected_revision is not None and expected_revision != project['revision']:
                raise HTTPException(409, '项目版本已更新，请重新打开。')
            save_id, documents = self.export_documents(project, for_writeback=True)
            prefix = project['_r2_prefix']
            pointer_key = prefix + '/timeline/.latest.json'
            pointer, etag = self.provider.optional_json(pointer_key)
            if etag != project.get('_r2_timeline_etag'):
                raise HTTPException(409, 'S3 标注已被其他用户更新，草稿保留。')
            axes = copy.deepcopy((pointer or {}).get('axes', {}))
            mirrors = Path(project['source_dir']) / 'timeline'
            normalized = {}
            for axis, payload in documents.items():
                document = json.loads(payload)
                document['timebase']['r2_capture_sha256'] = project['_r2_capture_sha256']
                payload = document_bytes(document)
                key = prefix + '/timeline/versions/' + save_id + '/' + filename_for_axis(axis)
                self.provider.put_if_absent(key, payload)
                axes[axis] = {'key': key, 'sha256': hashlib.sha256(payload).hexdigest()}
                normalized[axis] = payload
            active = set(AXES) | {track['id'] for track in project.get('custom_tracks', [])}
            axes = {axis: item for axis, item in axes.items() if axis in active}
            payload = document_bytes({'save_id': save_id, 'axes': axes})
            committed = self.provider.put(pointer_key, payload, etag=etag, create=etag is None)
            if self.provider.get(pointer_key) != payload:
                raise HTTPException(503, 'S3 提交读回未确认成功，保留草稿。')
            mirrors.mkdir(exist_ok=True)
            for axis, raw in normalized.items():
                (mirrors / filename_for_axis(axis)).write_bytes(raw)
            project['_external_hashes'] = self.current_external_hashes(Path(project['source_dir']))
            project['_r2_timeline_etag'] = committed
            project['draft_dirty'] = False
            project['last_writeback'] = {'save_id': save_id, 'saved_at': now()}
            project['updated_at'] = now()
            self.save(project)
            return {'paths': [item['key'] for axis, item in axes.items() if axis in documents], 'save_id': save_id}

    def media_manifest(self, service, manifest):
        project_id, version = manifest['project_id'], manifest['version']
        videos, mapping, normal_records = [], {}, {}
        project = service.load(project_id)
        source_files = {item['name']: item for item in project.get('_r2_manifest', {}).get('files', [])}
        video_names = {item['id']: item['name'] for item in project['videos']}
        for video in manifest['videos']:
            ident = video['id']
            capture = project['_sources'][ident].get('r2_playback')
            def asset(kind, filename, index=None):
                if capture:
                    record = next((item for item in capture['manifest'].get('cache_files', [])
                                   if item['video_name'] == capture['video_name'] and item['kind'] == kind and item['index'] == index), None)
                    if record:
                        return self.provider.signed_url(capture['prefix'] + '/' + record['key'])
                path = service.sessions.asset(project_id, kind, ident, index, version=version)
                if path is None:
                    raise HTTPException(409, '播放缓存尚未就绪。')
                key = f'media/{project_id}/{version}/{ident}/{filename}'
                self.provider.put_file(key, path)
                return self.provider.signed_url(key)
            sheets = [asset('sheet', f'sheet-{index}.jpg', index) for index in range(len(video['storyboard']['sheets']))]
            source = source_files.get(video_names[ident])
            if capture:
                source = next(item for item in capture['manifest']['files'] if item['name'] == capture['video_name'])
            prefix = capture['prefix'] if capture else project.get('_r2_prefix')
            normal_url = self.provider.signed_url(prefix + '/' + source['key']) if source else asset('normal', 'normal.mp4')
            if source:
                normal_records[video['url']] = source
            value = {**video, 'url': normal_url, 'fast_url': asset('fast', 'fast.mp4'),
                     'thumbnail_url': asset('thumbnail', 'thumbnail.jpg'),
                     'storyboard': {**video['storyboard'], 'sheets': sheets}}
            videos.append(value)
            mapping.update({video[key]: value[key] for key in ('url', 'fast_url', 'thumbnail_url')})
            mapping.update(zip(video['storyboard']['sheets'], sheets))
        downloads = []
        for item in manifest.get('assets', []):
            fresh = {**item, 'url': mapping[item['url']]}
            source = normal_records.get(item['url'])
            if source:
                fresh.update(size=source['size'], sha256=source['sha256'], file_name=item['file_name'].replace(item['sha256'], source['sha256']))
            downloads.append(fresh)
        return {**manifest, 'videos': videos, 'assets': downloads}
