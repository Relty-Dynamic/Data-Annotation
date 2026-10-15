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


def attachment_key(value):
    if not isinstance(value, str):
        raise HTTPException(422, '附属数据路径无效。')
    parts = value.split('/')
    if (not 2 <= len(parts) <= 6 or parts[0] not in {'IMU', 'HEART', 'prediction'}
            or any(not part or part in {'.', '..'} or len(part) > 120
                   or part != part.strip(' .')
                   or any(ord(c) < 32 or c in '\\:<>"|?*' for c in part) for part in parts)
            or PurePosixPath(value).suffix.lower() not in ({'.json'} if parts[0] == 'prediction' else DATA_EXTENSIONS)):
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
    # applying predictions to an axis are a separate, future operation.


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
    def select_folder(self, user_id, raw):
        selection = scan_collection(raw)
        ident = uuid.uuid4().hex
        with self.lock:
            self.selections[ident] = {'owner': user_id, 'files': selection['videos'], 'selected_at': time.time()}
        return {**selection, 'selection_id': ident,
                'capture_date': min(item['date'] for item in selection['videos'])}


def attachment_manifest(records):
    result = []
    for record in records:
        path = MockUploadManager._check_local_source(record)
        result.append({'key': record['output'], 'size': record['size'], 'sha256': file_sha256(path)})
        MockUploadManager._check_local_source(record)
    return result
