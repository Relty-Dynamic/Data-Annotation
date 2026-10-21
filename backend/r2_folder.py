"""Local collection selection for public uploads; never used by NAS intake."""
from __future__ import annotations

import json
import time
import uuid
from pathlib import Path, PurePosixPath

from fastapi import HTTPException

from .mock_s3 import MockUploadManager, file_sha256
from .service import parse_recording_start
from .source_cache import is_reparse

DATA_EXTENSIONS = {'.csv', '.tsv', '.txt', '.json', '.bin', '.dat', '.npy', '.npz', '.parquet'}
IMU_EXTENSIONS = DATA_EXTENSIONS | {'.wplay'}


def attachment_key(value):
    if not isinstance(value, str):
        raise HTTPException(422, '附属数据路径无效。')
    parts = value.split('/')
    if (not 2 <= len(parts) <= 6 or parts[0] not in {'IMU', 'HEART', 'prediction'}
            or any(not part or part in {'.', '..'} or len(part) > 120
                   or part != part.strip(' .')
                   or any(ord(c) < 32 or c in '\\:<>"|?*' for c in part) for part in parts)
            or PurePosixPath(value).suffix.lower() not in (
                {'.json'} if parts[0] == 'prediction' else IMU_EXTENSIONS if parts[0] == 'IMU' else DATA_EXTENSIONS)):
        raise HTTPException(422, '仅接受 IMU、HEART 数据文件及 prediction 中的 JSON；timeline 由平台写回。')
    return value


def validate_prediction(path):
    if path.stat().st_size > 32 * 1024**2:
        raise HTTPException(422, '单个预测 JSON 不能超过 32 MiB。')
    try:
        document = json.loads(path.read_text(encoding='utf-8-sig'))
    except (ValueError, UnicodeError):
        raise HTTPException(422, 'prediction 文件不是有效的 UTF-8 JSON。') from None
    if not isinstance(document, (dict, list)):
        raise HTTPException(422, '预测 JSON 必须为对象或数组。')
    # Preserve original algorithm output byte for byte. Interpretation and
    # initializing a posture draft is a separate operation after publication.


def snapshot(path, key):
    if any(is_reparse(item) for item in (path, *path.parents)) or not path.is_file():
        raise HTTPException(422, '采集数据不能包含符号链接或目录联接。')
    stat = path.stat()
    if not 0 < stat.st_size <= MockUploadManager.MAX_FILE_BYTES:
        raise HTTPException(422, '采集文件为空或超过单文件 16 GiB 上限。')
    return {'source': str(path), 'output': key, 'name': path.name, 'size': stat.st_size,
            'mtime_ns': stat.st_mtime_ns, 'device': stat.st_dev, 'inode': stat.st_ino}


def scan_collection(raw):
    root = Path(raw).expanduser().absolute()
    if any(is_reparse(item) for item in (root, *root.parents)) or not root.is_dir():
        raise HTTPException(422, '请选择真实存在的采集文件夹。')
    videos, attachments, ignored, keys = [], [], [], set()
    for path in sorted(root.rglob('*')):
        relative = path.relative_to(root)
        # Metadata, previous caches and timeline are never uploaded implicitly.
        if any(part.startswith('.') for part in relative.parts):
            continue
        if any(is_reparse(item) for item in (path, *path.parents)):
            raise HTTPException(422, '采集数据不能包含符号链接或目录联接。')
        if not path.is_file():
            continue
        parts = relative.parts
        group = parts[0].lower()
        if path.suffix.lower() == '.avi' and (len(parts) == 1 or (len(parts) == 2 and group in {'video', 'fpv'})):
            key, _ = MockUploadManager._relative_path(path.name)
            record = snapshot(path, key)
            record['date'] = parse_recording_start(path.name).strftime('%Y%m%d')
            videos.append(record)
        elif group in {'imu', 'heart', 'prediction'} and len(parts) > 1:
            key = ('prediction' if group == 'prediction' else group.upper()) + '/' + '/'.join(parts[1:])
            attachment_key(key)
            record = snapshot(path, key)
            if group == 'prediction':
                validate_prediction(path)
            attachments.append(record)
        else:
            ignored.append(relative.as_posix())
            continue
        if key.casefold() in keys:
            raise HTTPException(409, '采集目录中有重复的目标文件名。')
        keys.add(key.casefold())
    if not 1 <= len(videos) <= 1000 or len(attachments) > 4000:
        raise HTTPException(422, '请选择包含 1–1000 个 AVI 的采集目录，附属文件最多 4000 个。')
    if sum(item['size'] for item in videos + attachments) > MockUploadManager.MAX_TOTAL_BYTES:
        raise HTTPException(422, '采集数据总量超过 100 GiB。')
    return {'name': root.name, 'videos': videos, 'attachments': attachments, 'ignored': ignored}


class FolderUploadManager(MockUploadManager):
    def select_folder(self, user_id, raw, *, upload_kind='video'):
        if upload_kind == 'imu':
            from .imu_upload import scan_imu
            selection = scan_imu(raw)
        elif upload_kind == 'video':
            selection = scan_collection(raw)
        else:
            raise HTTPException(422, '上传素材类型无效。')
        ident = uuid.uuid4().hex
        with self.lock:
            self.selections[ident] = {'owner': user_id, 'files': selection['videos'], 'selected_at': time.time(),
                                      'root': str(Path(raw).expanduser().absolute()), 'folder': selection}
        return {**selection, 'selection_id': ident,
                'capture_date': min((item['date'] for item in selection['videos']), default='')}

    def start(self, user, collector_name, uploader_name, note, selection_id, *, prediction=None):
        options = prediction or {}
        with self.lock:
            selected = self.selections.get(selection_id)
            kind = selected.get('folder', {}).get('upload_kind', 'video') if selected else 'video'
            if options.get('upload_kind', 'video') != kind:
                raise HTTPException(422, '素材类型已变化，请重新选择文件夹。')
            if kind == 'imu':
                from .imu_upload import start
                return start(self, user, collector_name, uploader_name, note, selection_id, options)
        mode = options.get('prediction', 'keep')
        if mode not in ('generate', 'keep'):
            raise HTTPException(422, '预标注处理方式无效。')
        if mode == 'generate' and (not all(isinstance(options.get(k), str) and options[k].strip()
                                          for k in ('chest_device', 'thigh_device'))
                                   or options['chest_device'].strip() == options['thigh_device'].strip()):
            raise HTTPException(422, '请确认不同的胸部和大腿 IMU 设备编号。')
        with self.lock:
            selected = self.selections.get(selection_id)
            if any(job['state'] == 'preparing_prediction' for job in self.jobs.values()):
                raise HTTPException(409, '本机正在准备预标注，请等待完成。')
            result = super().start(user, collector_name, uploader_name, note, selection_id)
            self.jobs[result['id']]['folder_selection'] = selected
            self.jobs[result['id']]['prediction_options'] = dict(options)
        return result

    def finish(self, ident, user_id, service, can_upload):
        job = self._job(ident, user_id)
        if hasattr(self.store,'hash_guard'):
            with self.store.hash_guard:
                self.store.source_hashes.clear()
                self.store.source_hash_locks.clear()
        if job.get('upload_kind') == 'imu':
            from .imu_upload import finish
            return finish(self, job, service, can_upload)
        if hasattr(self.store, 'require_upload_support'):
            self.store.require_upload_support()
        options = job.get('prediction_options', {})
        if getattr(self.store,'pipeline_enabled',False):
            from .upload_pipeline import run
            def transfer_progress(values):
                with self.lock:
                    if job['state'] != 'error':
                        job.update({key:value for key,value in values.items() if key != 'progress'})
            self.store.transfer_progress=transfer_progress
            return run(self,job,user_id,service,can_upload)
        if options.get('prediction') == 'generate':
            if not can_upload(user_id):
                raise HTTPException(403, '上传权限已关闭。')
            from .prediction_prepare import prepare_prediction
            with self.lock:
                if job['state'] not in ('ready_to_compress', 'error'):
                    raise HTTPException(409, '预标注任务正在处理。')
                job.update(state='preparing_prediction', detail='正在准备姿势预标注。')
                job.pop('prediction_failure', None)
            def progress(detail):
                with self.lock:
                    job['detail'] = detail
                self.store.progress(detail)
            try:
                selection = job['folder_selection']
                refreshed, result = prepare_prediction(self.store, service, selection['root'], selection['folder'],
                    {role: options[role + '_device'].strip() for role in ('chest', 'thigh')}, progress=progress,
                    clock_group=options.get('clock_group') or None)
                selection['folder'] = refreshed
                job['prediction_result'] = result
                with self.lock:
                    job['state'] = 'ready_to_compress'
            except Exception as error:
                detail = error.detail if isinstance(error, HTTPException) and isinstance(error.detail, str) else '预标注准备失败，尚未上传；请检查素材和设备角色后重新选择。'
                with self.lock:
                    job.update(state='error', detail=detail)
                    if hasattr(error, 'prediction_failure'):
                        job['prediction_failure'] = error.prediction_failure
                raise
        if hasattr(self.store, 'transfer_progress'):
            def transfer_progress(values):
                with self.lock:
                    job.update(values)
            self.store.transfer_progress = transfer_progress
        return super().finish(ident, user_id, service, can_upload)

    def status(self, ident, user_id):
        result = super().status(ident, user_id)
        job = self._job(ident, user_id)
        with self.lock:
            result.update({key:job[key] for key in ('clips_total','clips_verified','prediction_detail','prediction_failure') if key in job})
            if job.get('upload_kind') == 'imu':
                result.update(upload_kind='imu', selected=len(job['attachments']), total=len(job['attachments']))
        prediction = job.get('prediction_result')
        if prediction:
            from .prediction_diagnostics import upload_report
            result['prediction'] = upload_report({key: prediction[key] for key in ('sha256', 'segments', 'needs_alignment_review',
                'needs_mounting_review', 'needs_review', 'needs_mounting_review_record_ids', 'mounting_records',
                'job_id', 'export_profile', 'batch_status', 'validated_records', 'total_records',
                'excluded_record_ids', 'excluded_records', 'inherited_record_ids') if key in prediction})
        return result


def attachment_manifest(records, digest=None):
    result = []
    for record in records:
        path = MockUploadManager._check_local_source(record)
        result.append({'key': record['output'], 'size': record['size'], 'sha256': digest(record) if digest else file_sha256(path)})
        MockUploadManager._check_local_source(record)
    return result
