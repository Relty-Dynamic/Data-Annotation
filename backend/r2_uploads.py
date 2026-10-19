"""Resumable uploads of compressed video collections or standalone IMU data."""
import copy
import hashlib
import json
import math
import re
import sqlite3
import threading
import uuid
import weakref
from contextlib import closing
from pathlib import Path

from fastapi import HTTPException

from .r2_service import capture_prefix
from .service import document_bytes, now, parse_recording_start
from .r2_folder import attachment_key
from .upload_playback import validate as validate_playback
from .capture_publication import manifest_key, check_compatible


class R2Uploads:
    PART_SIZE = 16 * 1024 * 1024

    def __init__(self, root, provider, service):
        self.path = root / 'r2-uploads.sqlite3'
        root.mkdir(parents=True, exist_ok=True)
        self.provider, self.service = provider, service
        self.lock = threading.RLock()
        self.locks_guard = threading.RLock()
        self.job_locks = weakref.WeakValueDictionary()
        self.validations = set()
        self.validation_slots = threading.BoundedSemaphore(2)
        with closing(sqlite3.connect(self.path)) as con, con:
            con.execute('CREATE TABLE IF NOT EXISTS uploads (id TEXT PRIMARY KEY, owner TEXT NOT NULL, digest TEXT NOT NULL, document TEXT NOT NULL)')
            con.execute('CREATE UNIQUE INDEX IF NOT EXISTS upload_request ON uploads(owner,digest)')
        self.path.chmod(0o600)
        from .upload_proofs import UploadProofs
        self.proofs = UploadProofs(self.path)

    def job_lock(self, ident, scope='state'):
        with self.locks_guard:
            return self.job_locks.setdefault((ident, scope), threading.RLock())

    def save_file(self, ident, index, record, user):
        with self.job_lock(ident):
            current = self.load(ident, user)
            current['files'][index] = dict(record)
            self.save(current)

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
        manifest.pop('media_metadata',None)  # Only server verification can publish media metadata.
        prefix = capture_prefix(manifest.get('prefix'))
        files = manifest.get('files')
        kind = manifest.get('upload_kind', 'video')
        if not isinstance(kind, str) or kind not in {'video', 'imu'}:
            raise HTTPException(422, '上传素材类型无效。')
        if kind == 'imu':
            from datetime import datetime
            if files != [] or manifest.get('cache_files') or 'cache_profile' in manifest:
                raise HTTPException(422, '仅 IMU 上传不能包含视频或播放缓存。')
            date = manifest.get('capture_date', '')
            try:
                if not isinstance(date, str) or not re.fullmatch(r'[0-9]{8}', date):
                    raise ValueError()
                datetime.strptime(date, '%Y%m%d')
            except ValueError:
                raise HTTPException(422, '请填写有效的 IMU 采集日期。') from None
            if prefix != f"daily/{date}/{date[4:]}{manifest.get('collector_name', '')}":
                raise HTTPException(422, 'IMU 保存目录与采集日期或采集人不一致。')
        elif not isinstance(files, list) or not 1 <= len(files) <= 1000:
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
        if kind == 'imu' and (not attachments or any(not item['name'].startswith('IMU/') for item in attachments)):
            raise HTTPException(422, '仅 IMU 上传必须包含 IMU 数据，不能混入心率、视频或预测文件。')
        cache_files = validate_playback(manifest)
        all_files = files + attachments + cache_files
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
            check_compatible(self.provider, manifest)
            current, _ = self.provider.optional_json(manifest_key(manifest))
            if current:
                raise HTTPException(409, '该采集目录已有同类素材，未覆盖；可上传缺少的另一类素材。')
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
        return {'id': job['id'], 'prefix': job['manifest']['prefix'], 'state': job['state'], 'file_count': len(job['files']),
                'verified_files': len(job.get('verified', {})),
                'validation_bytes': sum(job.get('validation_read_bytes', {}).values()),
                'validation_total_bytes': sum(item['size'] for item in R2Uploads.items(job)), 'error': job.get('validation_error', '')}

    @staticmethod
    def items(job):
        return job['manifest']['files'] + job['manifest'].get('attachments', []) + job['manifest'].get('cache_files', [])

    def plan(self, ident, index, user, *, single=False):
        with self.job_lock(ident, index):
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
            cache = index >= len(job['manifest']['files']) + len(job['manifest'].get('attachments', []))
            content_type = ('video/mp4' if index < len(job['manifest']['files']) or cache and item.get('kind') == 'fast'
                            else 'image/jpeg' if cache and item.get('kind') in ('thumbnail', 'sheet') else 'application/octet-stream')
            if single and item['size'] <= self.PART_SIZE:
                record['single'] = True
                self.save_file(ident, index, record, user)
                return {'complete': False, 'mode': 'single',
                        'url': self.provider.signed_url(key, operation='put_object', expires=900,
                            Metadata={'sha256': item['sha256']}, ContentType=content_type),
                        'headers': {'x-amz-meta-sha256': item['sha256'], 'Content-Type': content_type}}
            for attempt in range(2):
                if 'upload_id' not in record:
                    record['upload_id'] = self.provider.call('create_multipart_upload', Key=key, ContentType=content_type, Metadata={'sha256': item['sha256']})['UploadId']
                    self.save_file(ident, index, record, user)
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
                    self.save_file(ident, index, record, user)
            count = math.ceil(item['size'] / self.PART_SIZE)
            return {'complete': False, 'part_size': self.PART_SIZE, 'parts': [
                {'number': number, 'uploaded': number in completed,
                 'url': self.provider.signed_url(key, operation='upload_part', expires=900,
                    UploadId=record['upload_id'], PartNumber=number) if number not in completed else None}
                for number in range(1, count + 1)]}

    def complete_file(self, ident, index, user):
        with self.job_lock(ident, index):
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

    def finish(self, ident, user, *, authorize=None, prepare=False):
        from .upload_validation import validate_upload
        with self.job_lock(ident, 'validation'), self.validation_slots:
            return validate_upload(self, ident, user, authorize, prepare=prepare)

    def status(self, ident, user):
        result = self.public(self.load(ident, user))
        with self.locks_guard:
            result['validation_active'] = ident in self.validations
        return result

    def begin_finish(self, ident, user, authorize, *, prepare=False):
        # Authentication runs both before enqueue and again throughout publication.
        with self.job_lock(ident):
            current = self.load(ident, user)
            with self.locks_guard:
                if current['state'] == 'ready' or prepare and current['state'] == 'verified' or ident in self.validations:
                    return self.public(current)
                if len(self.validations) >= 8:
                    raise HTTPException(429, '核验队列繁忙，请稍后重试；已上传内容保留。')
                self.validations.add(ident)
            try:
                current.update(state='validating', validation_error='')
                self.save(current)
                def work():
                    try:
                        self.finish(ident, user, authorize=authorize, prepare=prepare)
                    except Exception:
                        pass  # Persisted, sanitized errors are returned by status.
                    finally:
                        with self.locks_guard:
                            self.validations.discard(ident)
                threading.Thread(target=work, daemon=True, name='upload-validation').start()
            except BaseException:
                with self.locks_guard:
                    self.validations.discard(ident)
                raise
            return self.public(current)
