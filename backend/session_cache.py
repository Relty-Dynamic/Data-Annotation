from __future__ import annotations

import copy
import errno
import hashlib
import json
import logging
import os
import shutil
import threading
import time
from concurrent.futures import FIRST_COMPLETED, Future, wait
from pathlib import Path

from fastapi import HTTPException

from .previews import PreviewSpec, run_ffmpeg
from .io_rate import read_rates
from .local_playback import render_local_fast, render_local_thumbnail
from .storyboards import StoryboardCache
from .project_storage import checked_tree
from .encoding import encoder_threads
from .video_selection import active_videos

LOGGER = logging.getLogger('uvicorn.error')
PROFILE = 'playback-480x270-256k32k-v1'
RESERVE_BYTES = 256 * 1024 * 1024
README = '''# DataMark 本机精简播放缓存

这里保存本项目的精简播放视频、高倍速视频、时间轴封面和悬停图片。
普通视频保留原视频帧时序，缩小到 480×270，使用较低码率，供流畅定位和标注。
原始视频及原目录中的完整预览不受影响；本文件夹不是原始视频备份。
打开项目时会检查并复用完整缓存；缺失或失效的内容会在进入标注前补齐。
平台的“清理本机预览”只清理此文件夹，保留项目记录、标注草稿、原视频和 NAS 缓存。
请不要把原始素材或个人文件存入此文件夹。
'''


def _document(value) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode('utf-8')


def _digest(value) -> str:
    return hashlib.sha256(_document(value)).hexdigest()


class _Cancelled(Exception):
    pass


class _PreparationStop:
    """Batch-wide failure cancels writers without becoming a user cancellation."""

    def __init__(self, parent: threading.Event):
        self.parent = parent
        self.internal = threading.Event()

    def is_set(self) -> bool:
        return self.parent.is_set() or self.internal.is_set()

    def set(self) -> None:
        self.internal.set()

    def wait(self, timeout: float | None = None) -> bool:
        deadline = None if timeout is None else time.monotonic() + timeout
        while not self.is_set():
            remaining = .05 if deadline is None else min(.05, deadline - time.monotonic())
            if remaining <= 0:
                return False
            self.internal.wait(remaining)
        return True


class SessionCache:
    """Prepare a complete persistent local playback set before exposing its URLs.

    One coordinator prepares at most two clips. HTTP status and asset requests use SQLite and
    local files; they never resolve, validate, or read the original NAS paths.
    """

    def __init__(self, service):
        self.service = service
        self._condition = threading.Condition(threading.RLock())
        self._entries: dict[str, dict] = {}
        self._pending: list[dict] = []
        self._current: dict | None = None
        self._worker: threading.Thread | None = None
        self._closed = False
        self._local_storyboards = StoryboardCache(service.storage.root)

    @staticmethod
    def _signature(project: dict) -> str:
        return _digest([PROFILE, project['source_fingerprint'], project['_sources'],
                        [(v['id'], v['duration_ms']) for v in active_videos(project)]])

    @staticmethod
    def _source_key(source: dict) -> str:
        # A supplement may renumber videos or change the project fingerprint.
        # The source identity remains stable, so completed local files survive it.
        return _digest([PROFILE, os.path.normcase(source.get('cache_identity_path', source['path'])),
                        source.get('cache_identity_stamp', source['stamp']), source['duration_ms'],
                        source.get('media_start_seconds', 0)])[:32]

    @staticmethod
    def _initial(project: dict) -> dict:
        return {'project_id': project['id'], 'state': 'idle', 'stage': 'cache', 'operation': 'check',
                'requested': False, 'total': len(project['videos']), 'ready': 0,
                'running': 0, 'queued': len(project['videos']), 'failed': 0,
                'progress': 0, 'detail': '等待准备本机精简播放素材。',
                'items': [{'video_id': v['id'], 'name': v['name'], 'state': 'queued',
                           'progress': 0, 'operation': 'check', 'detail': '等待检查本机播放素材。'} for v in project['videos']]}

    def start(self, ident: str, retry_failed: bool = False) -> dict:
        project = self.service.load(ident)
        project = {**project, 'videos': active_videos(project)}
        signature = self._signature(project)
        with self._condition:
            if ident in getattr(self.service, '_clearing_playback', set()):
                raise HTTPException(409, '正在清理本机预览，请稍后重新准备。')
            if self._closed:
                raise HTTPException(503, '平台正在关闭，请重新打开后重试。')
            previous = self._entries.get(ident)
            if previous and previous['signature'] == signature:
                if previous['status']['state'] == 'running' and not previous['stop'].is_set() and not previous['done'].is_set():
                    return copy.deepcopy(previous['status'])
                if previous['status']['state'] == 'ready' and self._validate_ready(previous):
                    # Another tab reopening this project must not invalidate the
                    # versioned URLs already playing in the first tab.
                    return copy.deepcopy(previous['status'])
            # Opening another project prioritizes that project. The stopped job
            # keeps atomically completed files and can resume on its next open.
            if self._current:
                self._current['stop'].set()
            for pending in self._pending:
                pending['stop'].set()
                self._pause(pending)
                pending['done'].set()
            self._pending.clear()
            status = self._initial(project)
            status.update(state='running', requested=True, detail='正在检查本机精简播放素材。')
            job = {'project': project, 'signature': signature, 'status': status,
                   'retry_failed': retry_failed, 'stop': threading.Event(),
                   'done': threading.Event(), 'assets': {}, 'manifest': None}
            self._entries[ident] = job
            self._pending.append(job)
            if self._worker is None or not self._worker.is_alive():
                self._worker = threading.Thread(target=self._work, name='local-playback-prepare', daemon=True)
                self._worker.start()
            self._condition.notify_all()
            return copy.deepcopy(status)

    def _matching(self, ident: str) -> tuple[dict, dict | None]:
        project = self.service.load(ident)
        project = {**project, 'videos': active_videos(project)}
        signature = self._signature(project)
        with self._condition:
            job = self._entries.get(ident)
            if job and job['signature'] != signature:
                job['stop'].set()
                self._entries.pop(ident, None)
                job = None
            return project, job

    @staticmethod
    def _asset_matches(path: Path, expected: dict) -> bool:
        try:
            stamp = path.stat()
            return stamp.st_size == expected['size'] and stamp.st_mtime_ns == expected['mtime_ns']
        except OSError:
            return False

    def _mark_missing(self, job: dict, video_id: str) -> None:
        with self._condition:
            if self._entries.get(job['project']['id']) is not job:
                return
            job['status'].update(state='partial', detail='本机播放素材缺失或已更改，请重新准备后进入标注。')
            item = next((item for item in job['status']['items'] if item['video_id'] == video_id), None)
            if item:
                item.update(state='error', progress=0, detail=job['status']['detail'])
            self._counts(job)

    def _validate_ready(self, job: dict) -> bool:
        # Only local stat calls: readiness cannot outlive a removed cache file,
        # and inspecting it must never restart NAS reads or transcoding.
        with self._condition:
            if job['status']['state'] != 'ready':
                return False
            records = list(job['assets'].items())
        for (_, video_id, _), (path, expected) in records:
            if not self._asset_matches(path, expected):
                self._mark_missing(job, video_id)
                return False
        return True

    def status(self, ident: str) -> dict:
        project, job = self._matching(ident)
        if job:
            self._validate_ready(job)
        with self._condition:
            result = copy.deepcopy(job['status'] if job else self._initial(project))
        result['read_bytes_per_second'] = (read_rates.snapshot(self.service.storage.directory(ident))
                                           if result['state'] == 'running' else 0)
        remote = getattr(self.service, 'remote', None)
        if remote is not None and callable(getattr(remote, 'download_rate', None)):
            result['download_bytes_per_second'] = remote.download_rate() if result['state'] == 'running' else 0
        return result

    def manifest(self, ident: str) -> dict:
        _, job = self._matching(ident)
        if job:
            self._validate_ready(job)
        with self._condition:
            if not job or job['status']['state'] != 'ready' or not job['manifest']:
                raise HTTPException(409, '本机播放素材尚未准备完成，请先完成素材准备。')
            return copy.deepcopy(job['manifest'])

    def failed_for_skip(self, ident: str, video_ids: list[str]) -> list[dict]:
        _, job = self._matching(ident)
        with self._condition:
            if (not job or not job['done'].is_set() or job['stop'].is_set()
                    or job['status']['state'] != 'partial'):
                raise HTTPException(409, '请等待其余片段准备完成后再选择跳过。')
            items = job['status']['items']
            failed = [item for item in items if item['state'] == 'error']
            if (not failed or not any(item['state'] == 'ready' for item in items)
                    or any(item['state'] not in {'ready', 'error'} for item in items)):
                raise HTTPException(409, '至少需要一段可用视频，且不能有仍在等待或处理的片段。')
            if len(set(video_ids)) != len(video_ids) or set(video_ids) != {item['video_id'] for item in failed}:
                raise HTTPException(409, '失败片段列表已改变，请重新查看后确认。')
            return copy.deepcopy(failed)

    def asset(self, ident: str, kind: str, video_id: str, index: int | None = None,
              *, version: str | None = None) -> Path | None:
        _, job = self._matching(ident)
        with self._condition:
            if not job or job['status']['state'] != 'ready' or not job['manifest']:
                return None
            if version is not None and version != job['manifest']['version']:
                raise HTTPException(409, '本机播放素材版本已更新，请重新打开项目。')
            record = job['assets'].get((kind, video_id, index))
        if record is None:
            return None
        path, expected = record
        if self._asset_matches(path, expected):
            return path
        self._mark_missing(job, video_id)
        return None

    def invalidate(self, ident: str, preserve_ready: bool = False) -> None:
        with self._condition:
            jobs = []
            for job in [self._entries.get(ident), self._current, *self._pending]:
                if job and job['project']['id'] == ident and all(job is not item for item in jobs):
                    if preserve_ready and job['status']['state'] == 'ready':
                        continue
                    job['stop'].set()
                    jobs.append(job)
            self._pending = [job for job in self._pending if job not in jobs]
            for job in jobs:
                if job is not self._current:
                    job['done'].set()
            retained = self._entries.get(ident)
            if not (preserve_ready and retained and retained['status']['state'] == 'ready'):
                self._entries.pop(ident, None)
            self._condition.notify_all()
        deadline = time.monotonic() + 30
        for job in jobs:
            if not job['done'].wait(max(0, deadline - time.monotonic())):
                raise HTTPException(409, '正在停止本机素材准备，请稍后重试操作。')

    def close(self) -> None:
        with self._condition:
            self._closed = True
            for job in [self._current, *self._pending]:
                if job:
                    job['stop'].set()
            for job in self._pending:
                job['done'].set()
            self._pending.clear()
            worker = self._worker
            self._condition.notify_all()
        if worker:
            worker.join(timeout=30)
        with self._condition:
            self._entries.clear()

    def _work(self) -> None:
        while True:
            with self._condition:
                if self._closed or not self._pending:
                    self._worker = None
                    return
                job = self._pending.pop(0)
                self._current = job
            try:
                self._prepare(job)
            except _Cancelled:
                self._pause(job)
            except Exception as error:
                if job['stop'].is_set() or self._closed:
                    # FFmpeg reports cancellation as HTTPException. A normal
                    # project switch is a pause, not a failed media conversion.
                    self._pause(job)
                else:
                    detail = (str(error.detail) if isinstance(error, HTTPException) else
                              '本机素材准备失败，请检查源目录、磁盘空间后重试。')
                    LOGGER.warning('Local playback preparation failed: %s: %s', job['project']['id'], detail,
                                   exc_info=not isinstance(error, (HTTPException, OSError)))
                    with self._condition:
                        job['status'].update(state='partial', detail=detail)
                        if not any(x['state'] == 'error' for x in job['status']['items']):
                            item = next((x for x in job['status']['items'] if x['state'] == 'running'), None)
                            if item is None:
                                item = next((x for x in job['status']['items'] if x['state'] != 'ready'), None)
                            if item:
                                item.update(state='error', detail=detail, progress=0)
                        for peer in job['status']['items']:
                            if peer['state'] == 'running':
                                peer.update(state='queued', detail='等待重试后继续准备。')
                        self._counts(job)
            finally:
                with self._condition:
                    job['done'].set()
                    if self._current is job:
                        self._current = None
                    self._condition.notify_all()

    def _pause(self, job: dict) -> None:
        with self._condition:
            job['status'].update(state='idle', detail='素材准备已暂停，重新打开项目时会继续。')
            for item in job['status']['items']:
                if item['state'] in {'running', 'queued'}:
                    item.update(state='idle', detail='等待继续准备。')
            self._counts(job)

    def _check(self, job: dict) -> None:
        if job['stop'].is_set() or self._closed:
            raise _Cancelled()
        current = self.service.load(job['project']['id'])
        if self._signature(current) != job['signature']:
            raise _Cancelled()

    @staticmethod
    def _counts(job: dict) -> None:
        status = job['status']
        items = status['items']
        for name, state in [('ready', 'ready'), ('running', 'running'), ('queued', 'queued'), ('failed', 'error')]:
            status[name] = sum(item['state'] == state for item in items)
        status['progress'] = round(sum(item['progress'] or 0 for item in items) / len(items), 1) if items else 100
        if status['state'] == 'running':
            current = next((item for item in items if item['state'] == 'running'), None)
            if current:
                for field in ('stage', 'operation', 'detail'):
                    if field in current:
                        status[field] = current[field]

    def _progress(self, job: dict, video_id: str, stage: str, progress: float, detail: str, *, operation: str = 'check') -> None:
        with self._condition:
            if job['stop'].is_set():
                return
            job['status'].update(stage=stage, operation=operation, detail=detail)
            item = next(x for x in job['status']['items'] if x['video_id'] == video_id)
            item.update(state='running', progress=min(99, max(item['progress'] or 0, progress)), stage=stage, operation=operation, detail=detail)
            self._counts(job)

    @staticmethod
    def _write_json(path: Path, value: dict) -> None:
        temporary = path.with_suffix(path.suffix + '.partial')
        try:
            temporary.write_bytes(_document(value))
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _file_record(path: Path, directory: Path) -> dict:
        stamp = path.stat()
        return {'path': path.relative_to(directory).as_posix(), 'size': stamp.st_size, 'mtime_ns': stamp.st_mtime_ns}

    @staticmethod
    def _valid_record(directory: Path, record: dict, jpeg: bool = False) -> Path | None:
        try:
            path = directory / record['path']
            # Metadata is local but still must not grant access outside the cache.
            if Path(record['path']).is_absolute() or '..' in Path(record['path']).parts:
                return None
            if path.is_symlink() or (hasattr(path, 'is_junction') and path.is_junction()):
                return None
            if not path.resolve().is_relative_to(directory.resolve()):
                return None
            stamp = path.stat()
            if stamp.st_size != record['size'] or stamp.st_mtime_ns != record['mtime_ns'] or stamp.st_size < 4:
                return None
            with path.open('rb') as handle:
                head = handle.read(32)
                if jpeg:
                    handle.seek(-2, os.SEEK_END)
                    return path if head[:2] == b'\xff\xd8' and handle.read(2) == b'\xff\xd9' else None
                return path if len(head) == 32 and head[4:8] == b'ftyp' else None
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def _read_entry(self, directory: Path, key: str, source: dict) -> dict | None:
        try:
            entry = json.loads((directory / (key + '.assets.json')).read_bytes())
            if entry['profile'] != PROFILE or entry['key'] != key or entry['duration_ms'] != source['duration_ms']:
                return None
            story = entry['storyboard']
            count = (source['duration_ms'] + 999) // 1000
            expected = {'version': 1, 'frame_count': count, 'interval_ms': 1000,
                        'tile_width': 160, 'tile_height': 90, 'columns': 10, 'rows': 10}
            if any(type(story.get(name)) is not int or story[name] != value for name, value in expected.items()):
                return None
            if not isinstance(entry['sheets'], list) or len(entry['sheets']) != (count + 99) // 100:
                return None
            if not all(isinstance(record, dict) for record in [entry['normal'], entry['fast'], entry['thumbnail'], *entry['sheets']]):
                return None
            return entry
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            return None

    def _load_entry(self, directory: Path, key: str, source: dict) -> dict | None:
        entry = self._read_entry(directory, key, source)
        if entry is None:
            return None
        for kind in ('normal', 'fast', 'thumbnail'):
            if self._valid_record(directory, entry[kind], jpeg=kind == 'thumbnail') is None:
                return None
        if not all(self._valid_record(directory, record, jpeg=True) for record in entry['sheets']):
            return None
        return entry

    def _restore_progress(self, directory: Path, key: str, source: dict, previous: dict) -> dict:
        try:
            saved = json.loads((directory / (key + '.progress.json')).read_bytes())
            if saved.get('profile') != PROFILE or saved.get('key') != key or saved.get('duration_ms') != source['duration_ms']:
                return previous
            result = dict(previous)
            for kind in ('normal', 'fast', 'thumbnail'):
                if isinstance(saved.get(kind), dict):
                    result[kind] = saved[kind]
            if isinstance(saved.get('sheets'), list) and all(record is None or isinstance(record, dict) for record in saved['sheets']):
                result['sheets'] = saved['sheets']
            return result
        except (OSError, ValueError, TypeError, AttributeError):
            return previous

    def _save_progress(self, directory: Path, key: str, source: dict, records: dict) -> None:
        self._write_json(directory / (key + '.progress.json'),
                         {**records, 'profile': PROFILE, 'key': key, 'duration_ms': source['duration_ms']})

    def _reuse_file(self, target: Path, record: dict | None, *, jpeg: bool = False) -> bool:
        return bool(record and record.get('path') == target.name and
                    self._valid_record(target.parent, record, jpeg=jpeg) == target)

    def _existing_source_spec(self, project: dict, video_id: str, target: Path) -> PreviewSpec:
        source = project['_sources'][video_id]
        try:
            # Existing full previews are optional inputs. Never enqueue a full
            # NAS transcode just to make the small local playback set.
            return self.service.preview_spec(project, video_id, check_source=False, create_cache=False)
        except (HTTPException, OSError):
            return PreviewSpec(project['id'] + '_' + self._source_key(source), Path(source['path']),
                               source, target, target.with_suffix('.error.json'), project_id=project['id'])

    def _compact_ready(self, source: dict, target: Path) -> bool:
        try:
            metadata = json.loads(target.with_suffix('.json').read_bytes())
            return (metadata.get('profile') == PROFILE and metadata.get('duration_ms') == source['duration_ms']
                    and self._reuse_file(target, metadata['file']))
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            return False

    @staticmethod
    def _storyboard_metadata(duration_ms: int) -> dict:
        return {'version': 1, 'frame_count': (duration_ms + 999) // 1000,
                'interval_ms': 1000, 'tile_width': 160, 'tile_height': 90,
                'columns': 10, 'rows': 10}

    def _copy_existing(self, source: Path | None, target: Path, stopping: threading.Event) -> bool:
        if source is None:
            return False
        try:
            self._copy_asset(source, target, stopping)
            return True
        except OSError:
            # If the share drops after the normal video is ready locally, the
            # remaining small derivatives can still be built from that video.
            return False

    @staticmethod
    def _check_space(directory: Path, needed: int) -> None:
        if shutil.disk_usage(directory).free < max(0, needed) + RESERVE_BYTES:
            raise HTTPException(507, '本机磁盘空间不足，无法准备精简播放素材。请释放空间后重试。')

    @staticmethod
    def _copy_asset(source: Path, target: Path, stopping: threading.Event) -> None:
        stamp = source.stat()
        temporary = target.with_suffix(target.suffix + '.partial')
        try:
            with source.open('rb') as incoming, temporary.open('wb') as outgoing:
                while True:
                    if stopping.is_set():
                        raise _Cancelled()
                    block = incoming.read(1024 * 1024)
                    if not block:
                        break
                    outgoing.write(block)
                outgoing.flush()
                os.fsync(outgoing.fileno())
            after = source.stat()
            if (after.st_size, after.st_mtime_ns) != (stamp.st_size, stamp.st_mtime_ns) or temporary.stat().st_size != stamp.st_size:
                raise HTTPException(409, '源预览在复制时发生变化，请重新准备。')
            if stopping.is_set():
                raise _Cancelled()
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)

    def _render_compact(self, spec, target: Path, update, stopping: threading.Event) -> None:
        marker = target.with_suffix('.json')
        if self._compact_ready(spec.source, target):
            update(100)
            return
        if spec.source.get('precompressed'):
            self.service.check_media_source(spec.path, spec.source)
            self._copy_asset(spec.path, target, stopping)
            self._write_json(marker, {'profile': PROFILE, 'duration_ms': spec.source['duration_ms'],
                                      'file': self._file_record(target, target.parent)})
            update(100)
            return
        normal = self.service.cached_preview_for_spec(spec)
        if normal is None or normal == target:
            self.service.check_media_source(spec.path, spec.source)
            normal = spec.path
        raw_input = normal == spec.path
        audio_start = spec.source.get('media_start_seconds', 0) if raw_input else 0
        ffmpeg = self.service.tool('ffmpeg')
        if not ffmpeg:
            raise HTTPException(503, '缺少 FFmpeg，无法准备精简播放素材。')
        temporary = target.with_suffix('.partial.mp4')
        duration = spec.source['duration_ms']
        filters = ('setpts=PTS-STARTPTS,scale=480:270:force_original_aspect_ratio=decrease:reset_sar=1,'
                   'pad=480:270:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1')
        args = [str(ffmpeg), '-hide_banner', '-loglevel', 'error', '-nostdin', '-y',
                '-progress', 'pipe:1', '-nostats', '-stats_period', '0.5', '-threads', '2', '-filter_threads', '1',
                '-copyts', '-i', str(normal), '-map', '0:v:0', '-map', '0:a?', '-vf', filters,
                '-c:v', 'libx264', '-preset', 'veryfast', '-threads', encoder_threads(4), '-b:v', '256k',
                '-maxrate', '320k', '-bufsize', '512k', '-g', '15', '-keyint_min', '15',
                '-pix_fmt', 'yuv420p', '-fps_mode', 'passthrough', '-c:a', 'aac', '-b:a', '32k',
                '-af', f'asetpts=PTS-({audio_start:.9f})/TB,atrim=start=0:end={duration / 1000:.6f}',
                '-movflags', '+faststart', str(temporary)]
        try:
            run_ffmpeg(args, duration, target.with_suffix('.ffmpeg.log'), update, stopping)
            rendered = self.service.probe(temporary)
            if abs(rendered.get('media_start_seconds', 0)) >= .001 or abs(rendered['duration_ms'] - duration) > 250:
                raise HTTPException(422, '精简播放视频的起点或时长校验失败，已停止使用，避免标注错位。')
            if stopping.is_set():
                raise _Cancelled()
            if raw_input:
                self.service.check_media_source(spec.path, spec.source)
            else:
                self.service.source_available(spec.path, spec.source)
            os.replace(temporary, target)
            self._write_json(marker, {'profile': PROFILE, 'duration_ms': duration,
                                      'file': self._file_record(target, target.parent)})
        finally:
            temporary.unlink(missing_ok=True)

    def _prepare_video(self, job: dict, video: dict, directory: Path) -> dict:
        project = job['project']
        self._check(job)
        clip_started = time.monotonic()
        timings = {}
        video_id = video['id']
        LOGGER.info('Preparing local playback: %s', video['name'])
        source = project['_sources'][video_id]
        key = self._source_key(source)
        try:
            remote = getattr(self.service, 'remote', None)
            if remote is not None and remote.map_path(source['path']) is not None:
                # Ready local playback never needs a NAS or server connection.
                entry = self._load_entry(directory, key, source)
                if entry is None:
                    entry = remote.prepare(source, directory, key,
                        lambda operation, value, detail: self._progress(job, video_id,
                            'compact' if operation in {'remote', 'nas'} or (operation == 'reconnect' and value < 80)
                            else 'local', value, detail, operation=operation),
                        job['stop'])
                    self._check(job)
                    if self._load_entry(directory, key, source) is None:
                        raise HTTPException(422, '下载后的本机播放素材校验失败，请重试。')
                else:
                    self._progress(job, video_id, 'local', 99, '复用已完成的全部本机播放素材。', operation='reuse')
                with self._condition:
                    if job['stop'].is_set():
                        raise _Cancelled()
                    item = next(item for item in job['status']['items'] if item['video_id'] == video_id)
                    item.update(state='ready', progress=100, detail=None)
                    self._counts(job)
                return entry
            self._progress(job, video_id, 'cache', 0, '正在检查原视频身份和本机缓存。', operation='check')
            # An accessible but changed original fails validation. Offline
            # originals do not prevent reuse or repairs from a valid local normal.
            available = self.service.source_available(Path(source['path']), source)
            entry = self._load_entry(directory, key, source)
            if entry is None:
                previous = self._restore_progress(directory, key, source, self._read_entry(directory, key, source) or {})
                target = directory / (key + '.compact-v1.mp4')
                normal_ready = self._reuse_file(target, previous.get('normal')) or self._compact_ready(source, target)
                if not available and not normal_ready:
                    raise HTTPException(404, '本机播放视频尚未完成，原视频目录当前不可访问。请连接原磁盘或共享目录后重新准备。')
                spec = self._existing_source_spec(project, video_id, target) if available else None
                duration = source['duration_ms']
                local_fast = directory / (key + '.fast20.mp4')
                local_thumbnail = directory / (key + '.jpg')
                fast_ready = self._reuse_file(local_fast, previous.get('fast'))
                thumbnail_ready = self._reuse_file(local_thumbnail, previous.get('thumbnail'), jpeg=True)
                storyboard = self._storyboard_metadata(duration)
                previous_sheets = previous.get('sheets', [])
                local_sheets = [directory / (key + f'.sheet-{index:05d}.jpg')
                                for index in range((storyboard['frame_count'] + 99) // 100)]
                missing_sheets = [index for index, local in enumerate(local_sheets)
                                  if not self._reuse_file(local, previous_sheets[index] if index < len(previous_sheets) else None, jpeg=True)]
                # Budget only missing outputs. The original is never copied, and
                # the full-resolution NAS video is no longer an intermediate.
                needed = 0 if normal_ready else int(duration / 1000 * 352000 / 8 * 1.2)
                needed += (0 if fast_ready else int(duration / 1000 * 8000))
                needed += (0 if thumbnail_ready else 65536) + len(missing_sheets) * 512000
                if missing_sheets:
                    # Rendering repairs creates a whole temporary set before copying.
                    needed += len(local_sheets) * 512000
                with self._condition:
                    self._check_space(directory, needed + sum(job['_reserved_bytes'].values()))
                    job['_reserved_bytes'][video_id] = needed
                if normal_ready:
                    self._progress(job, video_id, 'compact', 80, '复用已完成的本机精简播放视频。', operation='reuse')
                else:
                    started = time.monotonic()
                    self._progress(job, video_id, 'compact', 0, '正在直接生成本机精简播放视频。', operation='generate')
                    self._render_compact(spec, target,
                                         lambda value: self._progress(job, video_id, 'compact', value * .8, '正在直接生成本机精简播放视频。', operation='generate'),
                                         job['stop'])
                    timings['normal_seconds'] = round(time.monotonic() - started, 3)
                self._check(job)
                previous['normal'] = self._file_record(target, directory)
                self._save_progress(directory, key, source, previous)
                self._progress(job, video_id, 'local', 80, '正在准备本机高倍速视频和全部缩略图。', operation='assets')
                if not fast_ready:
                    started = time.monotonic()
                    fast = self.service.cached_fast_preview(spec) if spec is not None else None
                    if not self._copy_existing(fast, local_fast, job['stop']):
                        render_local_fast(self.service, target, local_fast, duration,
                                          lambda value: self._progress(job, video_id, 'local', 80 + value * .1, '正在从本机视频生成高倍速预览。', operation='assets'),
                                          job['stop'])
                    timings['fast_seconds'] = round(time.monotonic() - started, 3)
                previous['fast'] = self._file_record(local_fast, directory)
                self._save_progress(directory, key, source, previous)
                self._check(job)
                if not thumbnail_ready:
                    started = time.monotonic()
                    thumbnail = self.service.cached_thumbnail(spec) if spec is not None else None
                    if not self._copy_existing(thumbnail, local_thumbnail, job['stop']):
                        render_local_thumbnail(self.service, target, local_thumbnail, duration, job['stop'])
                    timings['thumbnail_seconds'] = round(time.monotonic() - started, 3)
                previous['thumbnail'] = self._file_record(local_thumbnail, directory)
                previous['sheets'] = [previous_sheets[index] if index < len(previous_sheets) else None for index in range(len(local_sheets))]
                self._save_progress(directory, key, source, previous)
                self._progress(job, video_id, 'local', 92, '正在准备本机悬停图片。', operation='assets')
                generated_directory = None
                if missing_sheets:
                    started = time.monotonic()
                    remote_manifest = self.service.storyboards.read(spec.key, duration) if spec is not None else None
                    remote_directory = self.service.storyboards.directory(spec.key) if remote_manifest else None
                    generate = []
                    for index in missing_sheets:
                        self._check(job)
                        remote = remote_directory / remote_manifest['sheets'][index] if remote_manifest else None
                        if not self._copy_existing(remote, local_sheets[index], job['stop']):
                            generate.append(index)
                        else:
                            previous['sheets'][index] = self._file_record(local_sheets[index], directory)
                            self._save_progress(directory, key, source, previous)
                    if generate:
                        work_root = checked_tree(directory / '.storyboard-work', directory)
                        self._local_storyboards.register_directory(key, work_root)
                        ffmpeg = self.service.tool('ffmpeg')
                        if not ffmpeg:
                            raise HTTPException(503, '缺少 FFmpeg，无法准备本机悬停图片。')
                        generated = self._local_storyboards.render(target, key, duration, ffmpeg,
                            lambda value: self._progress(job, video_id, 'local', 92 + value * .07, '正在从本机视频生成悬停图片。', operation='assets'), job['stop'])
                        generated_directory = self._local_storyboards.directory(key)
                        for index in generate:
                            self._check(job)
                            self._copy_asset(generated_directory / generated['sheets'][index], local_sheets[index], job['stop'])
                            previous['sheets'][index] = self._file_record(local_sheets[index], directory)
                            self._save_progress(directory, key, source, previous)
                    timings['storyboard_seconds'] = round(time.monotonic() - started, 3)
                self._check(job)
                # Recheck the original stamp before publishing even when the
                # expensive stages read only the completed local normal video.
                self.service.source_available(Path(source['path']), source)
                entry = {'profile': PROFILE, 'key': key, 'duration_ms': duration,
                         'normal': self._file_record(target, directory), 'fast': self._file_record(local_fast, directory),
                         'thumbnail': self._file_record(local_thumbnail, directory),
                         'storyboard': storyboard,
                         'sheets': [self._file_record(local, directory) for local in local_sheets]}
                self._write_json(directory / (key + '.assets.json'), entry)
                if self._load_entry(directory, key, source) is None:
                    raise HTTPException(422, '本机播放素材完整性校验失败，请重试。')
                (directory / (key + '.progress.json')).unlink(missing_ok=True)
                if generated_directory is not None:
                    # Only this exact generated scratch entry is disposable;
                    # original/legacy caches and published local assets stay put.
                    checked = checked_tree(generated_directory, generated_directory.parent)
                    if checked.is_relative_to(directory) and checked.name == key:
                        shutil.rmtree(checked)
            else:
                self._progress(job, video_id, 'local', 99, '复用已完成的全部本机播放素材。', operation='reuse')
            LOGGER.info('Local playback ready: %s (%.1fs)', video['name'], time.monotonic() - clip_started)
            with self._condition:
                if job['stop'].is_set():
                    raise _Cancelled()
                item = next(item for item in job['status']['items'] if item['video_id'] == video_id)
                item.update(state='ready', progress=100, detail=None)
                self._counts(job)
            return entry
        finally:
            with self._condition:
                job['_reserved_bytes'].pop(video_id, None)
                timings['total_seconds'] = round(time.monotonic() - clip_started, 3)
                item = next(item for item in job['status']['items'] if item['video_id'] == video_id)
                item['timings'] = timings
            LOGGER.info('Playback preparation timing %s: %s', video_id, json.dumps(timings, sort_keys=True))

    def _prepare_videos(self, job: dict, directory: Path) -> list[tuple[str, dict]]:
        videos = job['project']['videos']
        keys = [self._source_key(job['project']['_sources'][video['id']]) for video in videos]
        # Duplicate source identities share output paths, so serialize those jobs.
        remote = getattr(self.service, 'remote', None)
        all_remote = remote is not None and all(remote.map_path(source['path']) is not None
                                                for source in job['project']['_sources'].values())
        workers = 2 if len(keys) == len(set(keys)) and (all_remote or (os.cpu_count() or 1) >= 4) else 1
        stopping = _PreparationStop(job['stop'])
        worker_job = {**job, 'stop': stopping, '_reserved_bytes': {}}
        results = {}
        remaining = iter(videos)
        pending = {}
        threads = []

        def prepare(video, future):
            if not future.set_running_or_notify_cancel():
                return
            try:
                result = self._prepare_video(worker_job, video, directory)
            except BaseException as error:
                future.set_exception(error)
            else:
                future.set_result(result)

        try:
            while True:
                self._check(job)
                while len(pending) < workers:
                    video = next(remaining, None)
                    if video is None:
                        break
                    future = Future()
                    # A NAS stat can block beyond application shutdown. Daemon
                    # threads avoid an executor's unbounded interpreter-exit join.
                    thread = threading.Thread(target=prepare, args=(video, future),
                                              name='playback-clip-' + video['id'], daemon=True)
                    thread.start()
                    threads.append(thread)
                    pending[future] = (video['id'], thread)
                if not pending:
                    break
                done, _ = wait(pending, timeout=.25, return_when=FIRST_COMPLETED)
                self._check(job)
                # Inspect all completed tasks before submitting another clip.
                for future in done:
                    video_id, thread = pending.pop(future)
                    thread.join()
                    try:
                        results[video_id] = future.result()
                    except _Cancelled:
                        raise
                    except Exception as error:
                        self._check(job)
                        detail = str(error.detail) if isinstance(error, HTTPException) else '本机素材准备失败，请重试。'
                        with self._condition:
                            item = next(item for item in job['status']['items'] if item['video_id'] == video_id)
                            item.update(state='error', detail=detail, progress=0)
                            self._counts(job)
                        LOGGER.warning('Playback clip preparation failed: %s/%s: %s',
                                       job['project']['id'], video_id, detail,
                                       exc_info=not isinstance(error, (HTTPException, OSError)))
                        # Invalid media is isolated to its clip. Resource failures
                        # affect every writer, so preserve the batch-wide stop.
                        if (isinstance(error, MemoryError)
                                or isinstance(error, HTTPException) and error.status_code == 507
                                or isinstance(error, OSError) and error.errno in
                                {errno.ENOSPC, getattr(errno, 'EDQUOT', errno.ENOSPC), errno.ENOMEM}):
                            raise
        finally:
            stopping.set()
            for future in pending:
                future.cancel()
            # Normal cancellation/deletion still waits for every writer. Only
            # final process exit may abandon an unresponsive NAS daemon thread.
            for thread in threads:
                thread.join()
        return [(video['id'], results[video['id']]) for video in videos if video['id'] in results]

    def _prepare(self, job: dict) -> None:
        project = job['project']
        ident = project['id']
        self._check(job)
        parent = self.service.storage.directory(ident)
        directory = checked_tree(parent / 'playback', parent)
        directory.mkdir(parents=True, exist_ok=True)
        (directory / 'README.md').write_text(README, encoding='utf-8')
        entries = self._prepare_videos(job, directory)
        self._check(job)
        if len(entries) != len(project['videos']):
            with self._condition:
                self._counts(job)
                detail = (
                    f"已完成 {job['status']['ready']} 段，{job['status']['failed']} 段准备失败；"
                    '其余片段已处理完毕。重试会复用已完成缓存，仅补齐失败或缺失的素材。')
                # A server worker prepares one clip; its API must retain the
                # original validation error rather than a batch-only summary.
                if len(project['videos']) == 1:
                    detail = job['status']['items'][0]['detail']
                job['status'].update(state='partial', detail=detail)
            return
        version = _digest([job['signature'], entries])[:24]
        assets = {}
        videos = []
        for video_id, entry in entries:
            for kind in ('normal', 'fast', 'thumbnail'):
                assets[(kind, video_id, None)] = (directory / entry[kind]['path'], entry[kind])
            for index, record in enumerate(entry['sheets']):
                assets[('sheet', video_id, index)] = (directory / record['path'], record)
            url = f'/api/session-media/{ident}/{version}/{video_id}'
            videos.append({'id': video_id, 'url': url, 'fast_url': url + '?fast=true',
                           'thumbnail_url': f'/api/session-thumbnails/{ident}/{version}/{video_id}',
                           'storyboard': {**entry['storyboard'], 'state': 'ready', 'detail': None,
                                          'sheets': [f'/api/session-storyboards/{ident}/{version}/{video_id}/{index}' for index in range(len(entry['sheets']))]}})
        manifest = {'project_id': ident, 'version': version, 'profile': PROFILE, 'videos': videos}
        self._write_json(directory / 'manifest.json', {**manifest, 'signature': job['signature']})
        self._check(job)
        with self._condition:
            if job['stop'].is_set():
                raise _Cancelled()
            job.update(assets=assets, manifest=manifest)
            job['status'].update(state='ready', stage='local', progress=100, detail='全部本机精简播放素材已就绪。')
            self._counts(job)
