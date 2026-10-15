"""Resumable, account-authorized multipart uploads of compressed videos only."""
import copy
import hashlib
import json
import math
import re
import sqlite3
import threading
import uuid
from contextlib import closing
from pathlib import Path

from fastapi import HTTPException

from .r2_service import capture_prefix
from .service import document_bytes, now, parse_recording_start
from .r2_folder import attachment_key, validate_prediction


class R2Uploads:
    PART_SIZE = 16 * 1024 * 1024

    def __init__(self, root, provider, service):
        self.path = root / 'r2-uploads.sqlite3'
        root.mkdir(parents=True, exist_ok=True)
        self.provider, self.service = provider, service
        self.lock = threading.RLock()
        with closing(sqlite3.connect(self.path)) as con, con:
            con.execute('CREATE TABLE IF NOT EXISTS uploads (id TEXT PRIMARY KEY, owner TEXT NOT NULL, digest TEXT NOT NULL, document TEXT NOT NULL)')
            con.execute('CREATE UNIQUE INDEX IF NOT EXISTS upload_request ON uploads(owner,digest)')
        self.path.chmod(0o600)

    def save(self, job):
        with closing(sqlite3.connect(self.path)) as con, con:
            con.execute('INSERT INTO uploads VALUES (?,?,?,?) ON CONFLICT(id) DO UPDATE SET document=excluded.document',
                        (job['id'], job['owner'], job['digest'], json.dumps(job, ensure_ascii=False)))

    def load(self, ident, user):
        if not user.get('can_upload'):
            raise HTTPException(403, '上传权限已关闭。')
        with closing(sqlite3.connect(self.path)) as con, con:
            row = con.execute('SELECT document FROM uploads WHERE id=? AND owner=?', (ident, user['id'])).fetchone()
        if not row:
            raise HTTPException(404, '上传任务不存在。')
        return json.loads(row[0])

    def start(self, user, manifest):
        if not user.get('can_upload'):
            raise HTTPException(403, '尚未获得上传权限。')
        if not isinstance(manifest, dict):
            raise HTTPException(422, '上传清单无效。')
        manifest = copy.deepcopy(manifest)
        prefix = capture_prefix(manifest.get('prefix'))
        files = manifest.get('files')
        if not isinstance(files, list) or not 1 <= len(files) <= 1000:
            raise HTTPException(422, '请选择 1–1000 个压缩视频。')
        names = set()
        for item in files:
            if (not isinstance(item, dict) or not re.fullmatch(r'FPV/[^/\\:]+\.mp4', str(item.get('key', '')), re.I)
                    or not re.fullmatch(r'[a-f0-9]{64}', str(item.get('sha256', '')))
                    or type(item.get('size')) is not int or not 0 < item['size'] <= 16 * 1024**3
                    or type(item.get('duration_ms')) is not int or not 0 < item['duration_ms'] <= 24 * 3600 * 1000):
                raise HTTPException(422, '只接受含大小、时长和 SHA-256 的压缩 MP4 清单。')
            name = Path(item['key']).name
            if name.casefold() in names:
                raise HTTPException(422, '压缩视频名称重复。')
            names.add(name.casefold())
            parse_recording_start(name)
            item['name'] = name
        attachments = manifest.get('attachments', [])
        if not isinstance(attachments, list) or len(attachments) > 4000:
            raise HTTPException(422, '附属数据文件数量超过限制。')
        attachment_names = set()
        for item in attachments:
            if (not isinstance(item, dict) or not re.fullmatch(r'[a-f0-9]{64}', str(item.get('sha256', '')))
                    or type(item.get('size')) is not int or not 0 < item['size'] <= 16 * 1024**3):
                raise HTTPException(422, '附属数据清单无效。')
            key = attachment_key(item.get('key'))
            if key.casefold() in attachment_names:
                raise HTTPException(422, '附属数据文件名称重复。')
            attachment_names.add(key.casefold())
            item['name'] = key
        all_files = files + attachments
        if sum(item['size'] for item in all_files) > 100 * 1024**3:
            raise HTTPException(422, '上传数据总量超过 100 GiB。')
        for field in ('collector_name', 'uploader_name'):
            if not isinstance(manifest.get(field), str) or not 1 <= len(manifest[field]) <= 80:
                raise HTTPException(422, '请填写采集人和上传人。')
        note = manifest.get('note')
        if note is not None and (not isinstance(note, str) or len(note) > 500):
            raise HTTPException(422, '备注超过限制。')
        request_digest = hashlib.sha256(document_bytes(manifest)).hexdigest()
        with self.lock:
            with closing(sqlite3.connect(self.path)) as con, con:
                previous = con.execute('SELECT document FROM uploads WHERE owner=? AND digest=?', (user['id'], request_digest)).fetchone()
            if previous:
                return self.public(json.loads(previous[0]))
            current, _ = self.provider.optional_json(prefix + '/manifest.json')
            if current:
                raise HTTPException(409, '采集目录已存在，未覆盖。')
            ident = uuid.uuid4().hex
            manifest.update(schema_version=1, uploaded_by=user['id'], uploaded_by_username=user['username'], created_at=now())
            for item in all_files:
                # Immutable, content-addressed objects prevent concurrent uploads
                # from overwriting bytes before the manifest is committed.
                folder, name = item['key'].rsplit('/', 1)
                item['key'] = folder + '/' + item['sha256'] + '-' + name
            job = {'id': ident, 'owner': user['id'], 'digest': request_digest, 'manifest': manifest,
                   'state': 'uploading', 'files': [{} for _ in all_files]}
            self.save(job)
            return self.public(job)

    @staticmethod
    def public(job):
        return {'id': job['id'], 'prefix': job['manifest']['prefix'], 'state': job['state'], 'file_count': len(job['files'])}

    @staticmethod
    def items(job):
        return job['manifest']['files'] + job['manifest'].get('attachments', [])

    def plan(self, ident, index, user):
        with self.lock:
            job = self.load(ident, user)
            if not 0 <= index < len(job['files']):
                raise HTTPException(404, '上传文件不存在。')
            item = self.items(job)[index]
            key = job['manifest']['prefix'] + '/' + item['key']
            record = job['files'][index]
            try:
                head = self.provider.head(key)
            except HTTPException as error:
                if error.status_code != 404:
                    raise
                head = None
            if head:
                if head['ContentLength'] != item['size'] or head.get('Metadata', {}).get('sha256') != item['sha256']:
                    raise HTTPException(409, '已有缓存与上传清单不一致。')
                return {'complete': True, 'parts': []}
            for attempt in range(2):
                if 'upload_id' not in record:
                    content_type = 'video/mp4' if index < len(job['manifest']['files']) else 'application/octet-stream'
                    record['upload_id'] = self.provider.call('create_multipart_upload', Key=key, ContentType=content_type, Metadata={'sha256': item['sha256']})['UploadId']
                    self.save(job)
                completed, marker = {}, 0
                try:
                    while True:
                        response = self.provider.call('list_parts', Key=key, UploadId=record['upload_id'], PartNumberMarker=marker)
                        completed.update({part['PartNumber']: part['ETag'] for part in response.get('Parts', [])})
                        if not response.get('IsTruncated'):
                            break
                        marker = response['NextPartNumberMarker']
                    break
                except HTTPException as error:
                    if error.status_code != 404 or attempt:
                        raise
                    # Expired/aborted multipart state must not permanently trap
                    # the persistent request in an unresumable upload ID.
                    record.clear()
                    self.save(job)
            count = math.ceil(item['size'] / self.PART_SIZE)
            return {'complete': False, 'part_size': self.PART_SIZE, 'parts': [
                {'number': number, 'uploaded': number in completed,
                 'url': self.provider.signed_url(key, operation='upload_part', expires=900,
                    UploadId=record['upload_id'], PartNumber=number) if number not in completed else None}
                for number in range(1, count + 1)]}

    def complete_file(self, ident, index, user):
        with self.lock:
            job = self.load(ident, user)
            if not 0 <= index < len(job['files']):
                raise HTTPException(404, '上传文件不存在。')
            plan = self.plan(ident, index, user)
            if plan['complete']:
                return {'ok': True}
            if not all(part['uploaded'] for part in plan['parts']):
                raise HTTPException(409, '上传分片尚未齐全。')
            item = self.items(job)[index]
            key = job['manifest']['prefix'] + '/' + item['key']
            upload_id = self.load(ident, user)['files'][index]['upload_id']
            parts, marker = [], 0
            while True:
                page = self.provider.call('list_parts', Key=key, UploadId=upload_id, PartNumberMarker=marker)
                parts.extend({'ETag': part['ETag'], 'PartNumber': part['PartNumber']} for part in page.get('Parts', []))
                if not page.get('IsTruncated'):
                    break
                marker = page['NextPartNumberMarker']
            self.provider.call('complete_multipart_upload', Key=key, UploadId=upload_id, MultipartUpload={'Parts': parts})
            head = self.provider.head(key)
            if head['ContentLength'] != item['size']:
                raise HTTPException(409, '上传视频大小不一致。')
            return {'ok': True}

    def finish(self, ident, user):
        with self.lock:
            job = self.load(ident, user)
            manifest = job['manifest']
            prefix = manifest['prefix']
            for item in manifest['files']:
                destination = self.service.local / 'upload-validation' / item['sha256'] / item['name']
                self.provider.download(prefix + '/' + item['key'], destination, item['sha256'], item['size'])
                info = self.service.probe(destination)
                if info['codec'] != 'h264' or abs(info['duration_ms'] - item['duration_ms']) > 250:
                    raise HTTPException(422, '仅允许上传经本机压缩且时长校验通过的 H.264 视频。')
            for item in manifest.get('attachments', []):
                destination = self.service.local / 'upload-validation' / item['sha256'] / Path(item['key']).name
                self.provider.download(prefix + '/' + item['key'], destination, item['sha256'], item['size'])
                if item['name'].startswith('prediction/'):
                    validate_prediction(destination)
            # Manifest is the publication point. Files from an interrupted or
            # revoked upload are never listed as a usable collection.
            self.provider.put_if_absent(prefix + '/manifest.json', document_bytes(manifest))
            job['state'] = 'ready'
            self.save(job)
            return self.public(job)
