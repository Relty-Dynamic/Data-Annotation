"""Copy published, accepted S3 timelines to the corresponding mounted NAS folders."""
from __future__ import annotations

import argparse
from collections import Counter
from contextlib import contextmanager, nullcontext
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import uuid

from fastapi import HTTPException

from backend.r2_store import R2Store, object_key


APPLICATION = 'DataMark-task-timeline-v1'
MARKER = '.datamark-final.json'
SOURCE = '.datamark-sync-source.json'
ROOT = Path(__file__).resolve().parents[1]


class SyncError(Exception):
    pass


def sha(data):
    return hashlib.sha256(data).hexdigest()


def manifest(raw):
    try:
        value = json.loads(raw)
        files = value['files']
        if value['application'] != APPLICATION or not isinstance(files, dict) or not files:
            raise ValueError
        for name, checksum in files.items():
            if (not isinstance(name, str) or name.startswith('.') or '/' in name
                    or not name.endswith('.json') or object_key(name) != name
                    or not isinstance(checksum, str) or not re.fullmatch('[a-f0-9]{64}', checksum)):
                raise ValueError
        metadata = value['metadata']
        if not all(isinstance(metadata.get(key), str) and metadata[key]
                   for key in ('task_id', 'final_version_id', 'accepted_at', 'accepted_by')):
            raise ValueError
        return value
    except (ValueError, TypeError, KeyError, AttributeError, HTTPException):
        raise SyncError('验收完成清单无效，未复制。') from None


def safe_path(root, parts):
    """Reject symlink components before any read or write under the NAS root."""
    path = root
    for part in parts:
        if not part or part in ('.', '..') or any(c in part for c in '/\\:'):
            raise SyncError('目标路径无效。')
        path = path / part
        if path.is_symlink():
            raise SyncError('目标路径包含符号链接，未复制。')
    return path


def atomic_write(path, data):
    temp = path.with_name('.' + path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        with temp.open('xb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        if temp.read_bytes() != data:
            raise SyncError('NAS 临时文件读回不一致。')
        os.replace(temp, path)
        if path.read_bytes() != data:
            raise SyncError('NAS 目标文件读回不一致。')
    finally:
        temp.unlink(missing_ok=True)


def discover(store, prefix, *, directories=None):
    """Use completion markers, never a mere directory or uncommitted JSON."""
    found = {}
    for page in store.list(prefix + '/'):
        for item in page.get('Contents', []):
            key = item['Key']
            for directory in ('final', 'finally'):
                suffix = '/timeline/' + directory + '/' + MARKER
                if key.startswith(prefix + '/') and key.endswith(suffix):
                    capture = key[:-len(suffix)]
                    object_key(capture)
                    if capture.startswith('daily/') and len(capture.split('/')) in (2, 3):
                        if capture not in found or directory == 'final':
                            found[capture] = directory
    if directories is not None:
        directories.update(found)
    return sorted(found)


def find_collections(root, captures):
    """Index exact collection names in the NAS's existing directory hierarchy."""
    names = {capture.rsplit('/', 1)[-1] for capture in captures}
    found = {name: [] for name in names}
    if not names:
        return found

    def failed(error):
        raise error  # An unreadable subtree must not silently hide a duplicate.

    for directory, children, _ in os.walk(root, followlinks=False, onerror=failed):
        for name in children:
            if name in names:
                path = Path(directory) / name
                safe_path(root, path.relative_to(root).parts)
                found[name].append(path.relative_to(root).parts)
        children[:] = [name for name in children if not name.startswith('.')
                       and name not in ('timeline', 'FPV', 'IMU')
                       and not (Path(directory) / name).is_symlink()]
    return found


def sync_capture(store, root, capture, *, apply=False, collections=None, directory='finally'):
    object_key(capture)
    if not capture.startswith('daily/') or len(capture.split('/')) not in (2, 3):
        raise SyncError('采集路径须为 daily/采集目录 或 daily/日期/采集目录。')
    if directory not in ('final', 'finally'):
        raise SyncError('验收目录须为 final 或历史 finally。')
    if collections is None:
        collections = find_collections(root, [capture])
    candidates = collections.get(capture.rsplit('/', 1)[-1], [])
    if not candidates:
        raise SyncError('NAS 中找不到对应采集目录，未新建目录。')
    if len(candidates) != 1:
        raise SyncError('NAS 中有多个同名采集目录，无法确定目标，未复制。')
    parts = list(candidates[0])
    collection = safe_path(root, parts)
    if not collection.is_dir():
        raise SyncError('NAS 对应采集目录已不可用，未复制。')
    target = safe_path(root, parts + ['timeline', directory])
    marker_path = safe_path(root, parts + ['timeline', directory, MARKER])
    source_path = safe_path(root, parts + ['timeline', directory, SOURCE])
    source_raw = source_path.read_bytes() if source_path.exists() else None
    expected_source = json.dumps({'application': APPLICATION, 'capture_prefix': capture}, ensure_ascii=False).encode()
    if source_raw is not None and source_raw != expected_source:
        raise SyncError('NAS 目录已绑定其他 S3 采集路径或来源记录已改变，未复制。')
    remote_directory = capture + '/timeline/' + directory + '/'
    remote_key = remote_directory + MARKER
    remote_raw = store.get(remote_key)
    remote = manifest(remote_raw)
    local_raw = marker_path.read_bytes() if marker_path.exists() else None
    local = manifest(local_raw) if local_raw is not None else {'files': {}}
    downloads, originals = {}, {}
    total = 0
    for name, checksum in sorted(remote['files'].items()):
        path = safe_path(root, parts + ['timeline', directory, name])
        old = path.read_bytes() if path.exists() else None
        originals[name] = old
        if old is not None and sha(old) == checksum:
            continue
        if old is not None and sha(old) != local['files'].get(name):
            raise SyncError('NAS 存在不同内容的未知文件或人工修改，原文件已保留：' + name)
        data = store.get(remote_directory + name)
        total += len(data)
        if total > 128 * 1024 * 1024:
            raise SyncError('单个采集的待下载 JSON 超过 128 MiB。')
        if sha(data) != checksum:
            raise SyncError('S3 JSON 与验收清单摘要不一致，稍后重试：' + name)
        try:
            document = json.loads(data)
            result = document['task_result']
            if not all(result.get(key) for key in ('final_version_id', 'accepted_at', 'accepted_by')):
                raise ValueError
        except (ValueError, TypeError, KeyError, AttributeError):
            raise SyncError('JSON 缺少验收结果信息，未复制：' + name) from None
        downloads[name] = data
    # Publication writes its marker last. A changing/incomplete publication must
    # never become a locally completed batch.
    if store.get(remote_key) != remote_raw:
        raise SyncError('S3 验收版本在读取期间变化，稍后重试。')
    if local_raw is not None and remote['metadata']['accepted_at'] < local['metadata']['accepted_at']:
        raise SyncError('S3 验收清单早于 NAS 已保存版本，未覆盖。')
    if not apply:
        return {'copy': len(downloads), 'unchanged': len(remote['files']) - len(downloads),
                'marker': local_raw != remote_raw, 'target': str(target)}
    # Recheck local files before backing up or changing any destination.
    for name, old in originals.items():
        path = safe_path(root, parts + ['timeline', directory, name])
        if (path.read_bytes() if path.exists() else None) != old:
            raise SyncError('NAS 文件在同步期间变化，未开始写入。')
    if (marker_path.read_bytes() if marker_path.exists() else None) != local_raw:
        raise SyncError('NAS 完成清单在同步期间变化，未开始写入。')
    if (source_path.read_bytes() if source_path.exists() else None) != source_raw:
        raise SyncError('NAS 来源记录在同步期间变化，未开始写入。')
    if not downloads and local_raw == remote_raw and source_raw == expected_source:
        return {'copy': 0, 'unchanged': len(remote['files']), 'marker': False, 'target': str(target)}
    backup = None
    if local_raw is not None or any(originals[name] is not None for name in downloads):
        stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-' + uuid.uuid4().hex
        backup = safe_path(root, parts + ['timeline', '.final-sync-backups', stamp])
        backup.mkdir(parents=True, exist_ok=False)
        for name in downloads:
            if originals[name] is not None:
                atomic_write(backup / name, originals[name])
        if local_raw is not None:
            atomic_write(backup / MARKER, local_raw)
    target.mkdir(parents=True, exist_ok=True)
    # Persist the source binding before JSON writes, so an interrupted run cannot
    # later mix another year's identically named collection into this directory.
    if source_raw is None:
        atomic_write(source_path, expected_source)
    for name, data in downloads.items():
        atomic_write(safe_path(root, parts + ['timeline', directory, name]), data)
    # Verify every selected file, including reused files, before publishing marker.
    for name, checksum in remote['files'].items():
        if sha(safe_path(root, parts + ['timeline', directory, name]).read_bytes()) != checksum:
            raise SyncError('NAS 整批读回校验失败，未更新完成清单。')
    if store.get(remote_key) != remote_raw:
        raise SyncError('S3 验收版本在写入期间变化，未更新完成清单；下次重试。')
    atomic_write(marker_path, remote_raw)
    return {'copy': len(downloads), 'unchanged': len(remote['files']) - len(downloads),
            'marker': local_raw != remote_raw, 'backup': str(backup) if backup else None,
            'target': str(target)}


@contextmanager
def run_lock(state_dir, root):
    # Keep the lock on the runner's local disk; NAS file locking may be disabled.
    import fcntl
    state_dir.mkdir(parents=True, exist_ok=True)
    with (state_dir / (sha(os.fsencode(root)) + '.lock')).open('a') as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SyncError('已有同步进程使用此 NAS 根目录，本次未执行。') from None
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, required=True, help='平台现有受保护 R2 JSON 配置（600）')
    parser.add_argument('--nas-root', type=Path, required=True, help='已挂载的 homes/datacollection/daily 绝对路径')
    parser.add_argument('--prefix', default='daily', help='S3 范围，如 daily/20261021；NAS 按采集目录名查找')
    parser.add_argument('--apply', action='store_true', help='实际写入；默认只读预演')
    parser.add_argument('--state-dir', type=Path, default=ROOT / '.local/final-sync', help='运行机本地锁目录')
    args = parser.parse_args(argv)
    try:
        root = args.nas_root
        if not root.is_absolute() or not root.is_dir() or root.is_symlink():
            raise SyncError('NAS 根目录须为已存在的绝对目录，请先核对挂载。')
        root = root.resolve()
        prefix = args.prefix.rstrip('/')
        object_key(prefix)
        if prefix != 'daily' and not prefix.startswith('daily/'):
            raise SyncError('--prefix 必须位于 daily 下。')
        store = R2Store(args.config)
        with run_lock(args.state_dir, root) if args.apply else nullcontext():
            directories = {}
            captures = discover(store, prefix, directories=directories)
            collections = find_collections(root, captures)
            names = Counter(capture.rsplit('/', 1)[-1] for capture in captures)
            failed = copied = unchanged = 0
            for capture in captures:
                try:
                    if names[capture.rsplit('/', 1)[-1]] != 1:
                        raise SyncError('S3 范围内有多个同名采集目录，无法唯一对应 NAS，未复制。')
                    result = sync_capture(store, root, capture, apply=args.apply,
                                          collections=collections, directory=directories[capture])
                    copied += result['copy']
                    unchanged += result['unchanged']
                    print(json.dumps({'capture': capture, **result}, ensure_ascii=False), flush=True)
                except (SyncError, HTTPException, OSError, ValueError) as error:
                    failed += 1
                    # Never print SDK exceptions, which can expose signed URLs.
                    detail = str(error) if isinstance(error, SyncError) else '读取或写入失败，请检查权限、网络及存储。'
                    print(json.dumps({'capture': capture, 'error': detail}, ensure_ascii=False), flush=True)
            print(json.dumps({'mode': 'apply' if args.apply else 'dry-run', 'collections': len(captures),
                              'copy': copied, 'unchanged': unchanged, 'failed': failed}, ensure_ascii=False))
            return 1 if failed else 0
    except (SyncError, HTTPException, OSError, ValueError, ImportError) as error:
        print(str(error) if isinstance(error, SyncError) else '同步未完成，请核对受保护配置、项目依赖、目录权限和网络。', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
