"""Standalone IMU intake; data files remain byte-for-byte unchanged."""
from pathlib import Path
from datetime import datetime
import re
import time
import uuid

from fastapi import HTTPException

from .mock_s3 import MockUploadManager
from .r2_folder import IMU_EXTENSIONS, attachment_key, snapshot
from .source_cache import is_reparse


def scan_imu(raw):
    root = Path(raw).expanduser().absolute()
    if any(is_reparse(item) for item in (root, *root.parents)) or not root.is_dir():
        raise HTTPException(422, '请选择真实存在的 IMU 文件夹或含 IMU 子目录的采集文件夹。')
    candidates = [path for path in root.iterdir() if path.name.casefold() == 'imu']
    if len(candidates) > 1:
        raise HTTPException(422, '存在多个大小写不同的 IMU 目录，请直接选择其中一个。')
    source = candidates[0] if candidates else root
    if not source.is_dir() or is_reparse(source):
        raise HTTPException(422, 'IMU 数据不能包含符号链接或目录联接。')
    records, ignored, names = [], [], set()
    for path in sorted(source.rglob('*')):
        relative = path.relative_to(source)
        if any(part.startswith('.') for part in relative.parts):
            continue
        if any(is_reparse(item) for item in (path, *path.parents)):
            raise HTTPException(422, 'IMU 数据不能包含符号链接或目录联接。')
        if not path.is_file():
            continue
        if (relative.parts[0].casefold() in {'fpv', 'video', 'heart', 'prediction', 'timeline'}
                or path.suffix.lower() not in IMU_EXTENSIONS):
            ignored.append(relative.as_posix())
            continue
        key = attachment_key('IMU/' + relative.as_posix())
        if key.casefold() in names:
            raise HTTPException(422, 'IMU 文件存在大小写重复的目标名称。')
        names.add(key.casefold())
        records.append(snapshot(path, key))
    if not 1 <= len(records) <= 4000:
        raise HTTPException(422, '请选择包含 1–4000 个有效数据文件的 IMU 文件夹。')
    if sum(item['size'] for item in records) > MockUploadManager.MAX_TOTAL_BYTES:
        raise HTTPException(422, 'IMU 数据总量超过 100 GiB。')
    return {'name': root.name, 'videos': [], 'attachments': records, 'ignored': ignored,
            'capture_date': '', 'upload_kind': 'imu'}


def start(manager, user, collector, uploader, note, selection_id, options):
    collector, uploader, note = manager.upload_metadata(user, collector, uploader, note)
    date = options.get('capture_date', '')
    try:
        if not isinstance(date, str) or not re.fullmatch(r'[0-9]{8}', date):
            raise ValueError()
        datetime.strptime(date, '%Y%m%d')
    except ValueError:
        raise HTTPException(422, '请填写有效的 IMU 采集日期。') from None
    if options.get('prediction', 'keep') != 'keep':
        raise HTTPException(422, '仅 IMU 上传不支持生成视频姿势预标注。')
    selected = manager.selections.get(selection_id)
    if not selected or selected['owner'] != user['id'] or time.time() - selected['selected_at'] >= 3600:
        raise HTTPException(404, 'IMU 选择已失效，请重新选择。')
    records = selected['folder']['attachments']
    for record in records:
        manager._check_local_source(record)
    prefix = f'daily/{date}/{date[4:]}{collector}'
    if any(job['prefix'] == prefix and job['state'] not in {'ready', 'error'} for job in manager.jobs.values()):
        raise HTTPException(409, '此采集目录已有上传任务。')
    ident = uuid.uuid4().hex
    manager.jobs[ident] = dict(id=ident, owner=user['id'], prefix=prefix, records=[], attachments=records,
        capture_date=date, collector_name=collector, uploader_name=uploader,
        uploaded_by_username=user['username'], note=note, upload_kind='imu', state='ready_to_upload',
        progress=0, detail='已选择 IMU 数据，等待上传。', updated_at=time.time())
    # Keep the owned, time-limited selection so the same form can retry after
    # network failure. Each start rechecks source identity and active jobs.
    return manager.status(ident, user['id'])


def finish(manager, job, service, can_upload):
    with manager.lock:
        if job['state'] not in {'ready_to_upload', 'error'}:
            raise HTTPException(409, 'IMU 上传任务正在处理或已经完成。')
        job.update(state='uploading', detail='正在读取并上传原始 IMU 数据。')
    try:
        if not can_upload(job['owner']):
            raise HTTPException(403, '上传权限已关闭，未发布数据。')
        for record in job['attachments']:
            manager._check_local_source(record)
        manifest = {key: job[key] for key in ('prefix', 'capture_date', 'collector_name', 'uploader_name',
                                             'note', 'upload_kind')}
        manifest['files'] = []
        manager.store.attachments = job['attachments']
        def progress(values):
            with manager.lock:
                job.update(values)
                job['progress'] = min(99, job.get('progress', 0))
        manager.store.transfer_progress = progress
        manager.store.publish_ready(job['prefix'], manager.staging, manifest, service)
        with manager.lock:
            job.update(state='ready', progress=100, detail='IMU 数据已原样上传并通过完整校验。')
        return manager.status(job['id'], job['owner'])
    except Exception as error:
        from .upload_errors import failure_detail
        with manager.lock:
            job.update(state='error', detail=failure_detail(error))
        raise
