"""Authenticated NAS worker using the same playback pipeline as the desktop app."""
from __future__ import annotations

import copy
import hashlib
import json
import logging
import math
import os
import queue
import re
import secrets
import threading
import time
import zipfile
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path, PurePosixPath, PureWindowsPath

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse

from .service import AXES, FILENAMES, VIDEO_EXTENSIONS, ProjectService, document_bytes, filename_for_axis, validate_annotations, validate_custom_tracks, validate_project_name
from .session_cache import PROFILE
from .nas_archive import NasArchiveCache, sha256

PROTOCOL = 1
MAX_BODY = 8 * 1024 * 1024
MAX_PENDING = 16
DEFAULT_ROOTS = tuple(Path('/mnt/nas') / name for name in ('datasets', 'collector-data', 'homes', 'docker'))
LOGGER = logging.getLogger('uvicorn.error')


def _sha256(path: Path) -> str:
    return sha256(path)


def _same_stamp(expected, actual) -> bool:
    # SMB can round timestamps to milliseconds while reporting identical bytes.
    return (isinstance(expected, dict) and type(expected.get('size')) is int
            and type(expected.get('mtime_ns')) is int
            and expected['size'] == actual['size']
            and abs(expected['mtime_ns'] - actual['mtime_ns']) < 1_000_000
            and expected.get('sample_sha256') == actual['sample_sha256'])


class _RequestBoundary:
    def __init__(self, app, token: str):
        self.app = app
        self.authorization = ('Bearer ' + token).encode('utf-8')

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http':
            return await self.app(scope, receive, send)
        headers = dict(scope.get('headers', []))
        if not secrets.compare_digest(headers.get(b'authorization', b''), self.authorization):
            return await JSONResponse({'detail': 'Authentication required.'}, status_code=401,
                                      headers={'WWW-Authenticate': 'Bearer'})(scope, receive, send)
        try:
            length = int(headers.get(b'content-length', b'0'))
        except ValueError:
            length = MAX_BODY + 1
        if length < 0 or length > MAX_BODY:
            return await JSONResponse({'detail': 'Request exceeds 8 MiB.'}, status_code=413)(scope, receive, send)
        body = bytearray()
        while True:
            message = await receive()
            if message['type'] == 'http.disconnect':
                return
            body.extend(message.get('body', b''))
            if len(body) > MAX_BODY:
                return await JSONResponse({'detail': 'Request exceeds 8 MiB.'}, status_code=413)(scope, receive, send)
            if not message.get('more_body', False):
                break
        delivered = False

        async def limited_receive():
            nonlocal delivered
            if not delivered:
                delivered = True
                return {'type': 'http.request', 'body': bytes(body), 'more_body': False}
            return await receive()

        await self.app(scope, limited_receive, send)


class _WritebackService(ProjectService):
    @staticmethod
    def current_source_fingerprint(project: dict) -> str:
        return (project.get('_remote_verified_client_fingerprint')
                or ProjectService.current_source_fingerprint(project))


class Worker:
    def __init__(self, root: Path, allowed_roots, slots: int = 2):
        if type(slots) is not int or slots not in (1, 2):
            raise RuntimeError('DATAMARK_WORKER_SLOTS must be 1 or 2.')
        self.allowed_roots = tuple(Path(path).absolute() for path in allowed_roots)
        if not self.allowed_roots:
            raise RuntimeError('At least one NAS root must be configured.')
        self.service = ProjectService(root, enable_remote=False)
        self.services = [self.service] + [ProjectService(root / '.local' / 'worker-slots' / str(index),
                                                       enable_remote=False) for index in range(1, slots)]
        # Writeback documents and media jobs must never share a project-ID table.
        self.writeback_service = _WritebackService(root / '.local' / 'writeback-service', enable_remote=False)
        self.directory = root / '.local' / 'worker-jobs'
        self.directory.mkdir(parents=True, exist_ok=True)
        self.guard = threading.RLock()
        self.admission = threading.BoundedSemaphore(2)
        self.pending = queue.Queue(maxsize=MAX_PENDING)
        self.jobs: dict[str, dict] = {}
        self.job_locks: dict[str, threading.Lock] = {}
        self.nas = NasArchiveCache(PROFILE, PROTOCOL)
        self.stopping = threading.Event()
        self.threads: list[threading.Thread] = []

    def path(self, value, *, video=False, directory=False) -> Path:
        if not isinstance(value, str) or not value or '\x00' in value:
            raise HTTPException(422, 'A valid NAS path is required.')
        candidate = Path(value)
        if not candidate.is_absolute() or '..' in candidate.parts:
            raise HTTPException(403, 'Only absolute paths inside configured NAS roots are allowed.')
        try:
            for component in (candidate, *candidate.parents):
                if component.is_symlink() or (hasattr(component, 'is_junction') and component.is_junction()):
                    raise HTTPException(403, 'NAS paths containing links are not allowed.')
            resolved = candidate.resolve(strict=True)
            allowed = any(candidate.is_relative_to(base) and resolved.is_relative_to(base.resolve(strict=True))
                          for base in self.allowed_roots if base.exists())
            if not allowed:
                raise HTTPException(403, 'Path is outside configured NAS roots.')
            if video and (not resolved.is_file() or resolved.suffix.lower() not in VIDEO_EXTENSIONS):
                raise HTTPException(422, 'The source must be a supported video file.')
            if directory and not resolved.is_dir():
                raise HTTPException(422, 'The source directory does not exist.')
            return resolved
        except (OSError, RuntimeError) as exc:
            raise HTTPException(422, 'The NAS path is unavailable.') from exc

    def inspect(self, payload: dict) -> dict:
        if not self.admission.acquire(blocking=False):
            raise HTTPException(429, 'Worker is inspecting other sources; retry shortly.')
        try:
            path = self.path(payload.get('path'), video=True)
            stamp = self.service.source_stamp(path)
            media = self.service.probe(path)
            if self.service.source_stamp(path) != stamp:
                raise HTTPException(409, 'Source changed during inspection.')
            return {'stamp': stamp, 'media': media}
        finally:
            self.admission.release()

    @staticmethod
    def public(job: dict) -> dict:
        result = {key: job[key] for key in ('id', 'state', 'progress', 'detail')}
        result.update({key: copy.deepcopy(job[key]) for key in ('stage', 'cache_origin', 'timings') if key in job})
        if job['state'] == 'ready':
            result['archive'] = {key: job['archive'][key] for key in ('size', 'sha256')}
        return result

    def _local_archive(self, ident: str) -> Path:
        return self.directory / (ident + '.zip')

    def _archive(self, ident: str) -> Path:
        job = self.jobs.get(ident)
        if job and job.get('archive_storage') == 'nas':
            return self.nas.paths(Path(job['path']), ident)[0]
        return self._local_archive(ident)

    def _read_record(self, ident: str) -> dict | None:
        try:
            record = self.directory / (ident + '.json')
            NasArchiveCache._check_file(record, self.directory)
            if record.stat().st_size > 65536:
                return None
            job = json.loads(record.read_bytes())
            if job['id'] != ident or job['profile'] != PROFILE:
                return None
            self.path(job['path'], video=True)
            return job
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def _save_record(self, job: dict):
        record = self.directory / (job['id'] + '.json')
        temporary = record.with_suffix('.json.tmp')
        NasArchiveCache._check_file(record, self.directory)
        NasArchiveCache._check_file(temporary, self.directory)
        try:
            with temporary.open('wb') as handle:
                handle.write(document_bytes({key: value for key, value in job.items() if not key.startswith('_')}))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, record)
        finally:
            temporary.unlink(missing_ok=True)

    def _restore(self, ident: str) -> dict | None:
        job = self._read_record(ident)
        if job is None or job.get('archive_storage') != 'nas':
            return None
        if self.service.source_stamp(Path(job['path'])) != job['stamp']:
            return None
        archive = self.nas.read(job, self.stopping)
        if archive is None:
            return None
        self._verify_source(job)
        self._cleanup(job)
        return {**job, 'state': 'ready', 'stage': 'ready', 'progress': 100,
                'cache_origin': 'nas', 'archive': archive, 'archive_storage': 'nas'}

    def _server_cache(self, job: dict) -> dict | None:
        previous = self._read_record(job['id'])
        if previous is None or previous.get('state') not in ('prepared', 'ready'):
            return None
        archive = previous.get('archive', {})
        NasArchiveCache._check_file(self._local_archive(job['id']), self.directory)
        if not self.nas.validate_archive(self._local_archive(job['id']), job, archive, self.stopping):
            return None
        return archive

    def submit(self, payload: dict) -> dict:
        if self.stopping.is_set():
            raise HTTPException(503, 'Worker is shutting down.')
        if payload.get('profile') != PROFILE:
            raise HTTPException(409, 'Unsupported playback profile; update the worker and client together.')
        if not self.admission.acquire(blocking=False):
            raise HTTPException(429, 'Worker is inspecting other sources; retry shortly.')
        try:
            path = self.path(payload.get('path'), video=True)
            stamp = self.service.source_stamp(path)
            if not _same_stamp(payload.get('stamp'), stamp):
                raise HTTPException(409, 'Source identity changed; inspect the video again.')
            media = self.service.probe(path)
            duration = payload.get('duration_ms')
            start = payload.get('media_start_seconds')
            if (type(duration) is not int or duration != media['duration_ms']
                    or type(start) not in (int, float) or not math.isfinite(start)
                    or abs(start - media.get('media_start_seconds', 0)) > .000001):
                raise HTTPException(409, 'Source timing differs from the requested cache.')
            if self.service.source_stamp(path) != stamp:
                raise HTTPException(409, 'Source changed while its identity was checked.')
            canonical_stamp = {**stamp, 'mtime_ns': stamp['mtime_ns'] // 1_000_000}
            ident = hashlib.sha256(document_bytes([PROTOCOL, PROFILE, str(path), canonical_stamp, duration, start])).hexdigest()
            with self.guard:
                previous = self.jobs.get(ident)
            if previous and previous['state'] == 'ready':
                self.status(ident)
            with self.guard:
                if self.stopping.is_set():
                    raise HTTPException(503, 'Worker is shutting down.')
                previous = self.jobs.get(ident)
                if previous and previous['state'] in ('queued', 'running', 'ready'):
                    return self.public(previous)
                if self.pending.full():
                    raise HTTPException(429, 'Worker queue is full; retry after a running job finishes.')
                job = {'id': ident, 'state': 'queued', 'progress': 0, 'detail': 'Waiting for server processing.',
                       'path': str(path), 'stamp': stamp, 'profile': PROFILE, 'media': media,
                       'stage': 'queued', 'cache_origin': None, 'timings': {}, '_queued_at': time.monotonic()}
                self.jobs[ident] = job
                self.pending.put_nowait(job)
                if not self.threads:
                    for index, service in enumerate(self.services):
                        thread = threading.Thread(target=self._work, args=(service,),
                                                  name=f'nas-worker-{index}', daemon=True)
                        self.threads.append(thread)
                        thread.start()
                return self.public(job)
        finally:
            self.admission.release()

    def status(self, ident: str) -> dict:
        if not re.fullmatch('[0-9a-f]{64}', ident):
            raise HTTPException(404, 'Unknown job.')
        with self.guard:
            job = self.jobs.get(ident)
            lock = self.job_locks.setdefault(ident, threading.Lock()) if job is None else None
        if job is None:
            # Restoring can hash a NAS archive. Never hold the global job lock here.
            with lock:
                with self.guard:
                    job = self.jobs.get(ident)
                if job is None:
                    restored = self._restore(ident)
                    if restored is None:
                        raise HTTPException(404, 'Unknown job.')
                    with self.guard:
                        job = self.jobs.setdefault(ident, restored)
        if job['state'] == 'ready':
            try:
                stat = self._archive(ident).stat()
                if stat.st_size != job['archive']['size'] or stat.st_mtime_ns != job['archive']['mtime_ns']:
                    raise ValueError()
            except (OSError, ValueError):
                with self.guard:
                    job.update(state='error', progress=0, detail='Server cache is missing or changed; submit the job again.')
        with self.guard:
            return self.public(job)

    def _work(self, service):
        while not self.stopping.is_set():
            try:
                job = self.pending.get(timeout=.25)
            except queue.Empty:
                continue
            try:
                with self.guard:
                    lock = self.job_locks.setdefault(job['id'], threading.Lock())
                with lock:
                    with self.guard:
                        job.update(state='running', stage='nas-check', detail='Checking the reusable NAS cache.')
                        job['timings']['queue_seconds'] = round(time.monotonic() - job['_queued_at'], 3)
                    self._prepare(job, service)
            except Exception as exc:
                LOGGER.exception('NAS worker preparation failed: %s', job['id'])
                detail = str(exc.detail) if isinstance(exc, HTTPException) else 'Server cache preparation failed; inspect the worker log and retry.'
                with self.guard:
                    job.update(state='error', progress=0, detail=detail)
            finally:
                self.pending.task_done()

    def _verify_source(self, job: dict):
        path = self.path(job['path'], video=True)
        if self.service.source_stamp(path) != job['stamp']:
            raise HTTPException(409, 'Source changed during cache preparation.')

    def _cleanup(self, job: dict):
        ident = job['id'][:32]
        for service in self.services:
            try:
                project = service.load(ident)
            except HTTPException as exc:
                if exc.status_code == 404:
                    continue
                raise
            sources = list(project.get('_sources', {}).values())
            if len(sources) != 1 or sources[0].get('path') != job['path'] or sources[0].get('stamp') != job['stamp']:
                raise HTTPException(409, 'Worker project identity differs; local cleanup was stopped.')
            # Wait outside the service lock; a finishing renderer may need it.
            service.sessions.invalidate(ident)
            service.previews.cancel_project(ident)
            with service.lock:
                service.storage.remove(ident)
                with service.connection() as connection:
                    connection.execute('DELETE FROM projects WHERE id=?', (ident,))
        local = self._local_archive(job['id'])
        NasArchiveCache._check_file(local, self.directory)
        local.unlink(missing_ok=True)

    def _complete(self, job: dict, archive: dict, origin: str):
        self._verify_source(job)
        with self.guard:
            job.update(archive=archive, archive_storage='nas', cache_origin=origin, stage='cleanup', progress=99)
        # Durable NAS ownership is committed before removing any worker-local copy.
        self._save_record({**job, 'state': 'ready'})
        started = time.monotonic()
        self._cleanup(job)
        with self.guard:
            job['timings']['cleanup_seconds'] = round(time.monotonic() - started, 3)

    def _prepare(self, job: dict, service=None):
        service = service or self.service
        started = time.monotonic()
        self._verify_source(job)
        checked = time.monotonic()
        archive = self.nas.read(job, self.stopping)
        with self.guard:
            job['timings']['nas_check_seconds'] = round(time.monotonic() - checked, 3)
        if archive is not None:
            self._complete(job, archive, 'nas')
        else:
            archive = self._server_cache(job)
            origin = 'server-local' if archive is not None else 'generated'
            if archive is None:
                archive = self._generate(job, service)
            with self.guard:
                job.update(archive=archive, archive_storage='local', stage='publishing', progress=96,
                           cache_origin=origin, detail='Saving a verified reusable cache beside the original video.')
            self._save_record({**job, 'state': 'prepared'})
            publishing = time.monotonic()
            archive = self.nas.publish(job, self._local_archive(job['id']), self.stopping,
                                       lambda: self._verify_source(job))
            with self.guard:
                job['timings']['nas_save_seconds'] = round(time.monotonic() - publishing, 3)
            self._complete(job, archive, origin)
        with self.guard:
            job['timings']['total_seconds'] = round(time.monotonic() - started, 3)
            completed = {**job, 'state': 'ready', 'stage': 'ready', 'progress': 100,
                         'detail': 'Verified NAS cache is ready to download.'}
        self._save_record(completed)
        with self.guard:
            job.update(completed)
        LOGGER.info('NAS cache timing %s (%s): %s', job['id'], job['cache_origin'],
                    json.dumps(job['timings'], sort_keys=True))

    def _generate(self, job: dict, service):
        path = self.path(job['path'], video=True)
        generating = time.monotonic()
        project = service.create([path], path.stem, None, ident=job['id'][:32])
        source = project['_sources'][project['videos'][0]['id']]
        if (source['stamp'] != job['stamp'] or source['duration_ms'] != job['media']['duration_ms']
                or source.get('media_start_seconds', 0) != job['media'].get('media_start_seconds', 0)):
            raise HTTPException(409, 'Source changed before preparation.')
        service.save(project)
        service.sessions.start(project['id'], retry_failed=True)
        while not self.stopping.is_set():
            status = service.sessions.status(project['id'])
            with self.guard:
                job.update(progress=5 + status['progress'] * .8, stage='preparing', detail=status.get('detail'))
            if status['state'] == 'ready':
                break
            if status['state'] in ('error', 'failed', 'partial', 'paused', 'idle'):
                raise HTTPException(422, status.get('detail') or 'Server playback generation failed.')
            self.stopping.wait(.15)
        else:
            raise HTTPException(503, 'Worker stopped; resubmit to resume completed cache stages.')
        self._verify_source(job)
        with self.guard:
            job['timings']['prepare_seconds'] = round(time.monotonic() - generating, 3)
            for item in status.get('items', []):
                job['timings'].update(item.get('timings', {}))
            job.update(progress=88, stage='packaging', detail='Packaging and verifying the completed playback cache.')
        packaging = time.monotonic()
        manifest = service.sessions.manifest(project['id'])
        video = manifest['videos'][0]
        storyboard = {key: video['storyboard'][key] for key in
                      ('version', 'frame_count', 'interval_ms', 'tile_width', 'tile_height', 'columns', 'rows')}
        assets = []
        records = {}
        for kind, name in (('normal', 'normal.mp4'), ('fast', 'fast.mp4'), ('thumbnail', 'thumbnail.jpg')):
            asset = service.sessions.asset(project['id'], kind, video['id'])
            if asset is None:
                raise HTTPException(409, 'A completed cache asset is missing.')
            records[kind] = {'name': name, 'size': asset.stat().st_size, 'sha256': _sha256(asset)}
            assets.append((asset, name))
        sheets = []
        for index in range(len(video['storyboard']['sheets'])):
            asset = service.sessions.asset(project['id'], 'sheet', video['id'], index)
            if asset is None:
                raise HTTPException(409, 'A completed storyboard sheet is missing.')
            name = f'sheet-{index:05d}.jpg'
            sheets.append({'name': name, 'size': asset.stat().st_size, 'sha256': _sha256(asset)})
            assets.append((asset, name))
        package = {'protocol': PROTOCOL, 'profile': PROFILE, 'duration_ms': source['duration_ms'],
                   'media_start_seconds': source.get('media_start_seconds', 0), 'source_stamp': source['stamp'],
                   'files': records, 'sheets': sheets, 'storyboard': storyboard}
        target = self._local_archive(job['id'])
        temporary = target.with_suffix('.zip.tmp')
        NasArchiveCache._check_file(target, self.directory)
        NasArchiveCache._check_file(temporary, self.directory)
        try:
            with zipfile.ZipFile(temporary, 'w', compression=zipfile.ZIP_STORED, allowZip64=True) as archive:
                archive.writestr('manifest.json', document_bytes(package))
                for asset, name in assets:
                    if self.stopping.is_set():
                        raise HTTPException(503, 'Worker is shutting down.')
                    archive.write(asset, name)
            with temporary.open('r+b') as handle:
                os.fsync(handle.fileno())
            os.replace(temporary, target)
            metadata = {'size': target.stat().st_size, 'sha256': _sha256(target), 'mtime_ns': target.stat().st_mtime_ns}
            with self.guard:
                job['timings']['package_seconds'] = round(time.monotonic() - packaging, 3)
            return metadata
        finally:
            temporary.unlink(missing_ok=True)

    def _writeback_project(self, value) -> dict:
        if not isinstance(value, dict) or not re.fullmatch('[0-9a-f]{32}', str(value.get('id', ''))):
            raise HTTPException(422, 'Invalid project ID.')
        project = copy.deepcopy(value)
        project.pop('_remote_verified_client_fingerprint', None)
        project['name'] = validate_project_name(project.get('name'))
        source_dir = self.path(project.get('source_dir'), directory=True)
        project['source_dir'] = str(source_dir)
        videos, sources = project.get('videos'), project.get('_sources')
        if not isinstance(videos, list) or not 1 <= len(videos) <= 1000 or not isinstance(sources, dict):
            raise HTTPException(422, 'Invalid project sources.')
        ids = [v.get('id') if isinstance(v, dict) else None for v in videos]
        if any(not isinstance(ident, str) or not re.fullmatch(r'v\d{4,}', ident) for ident in ids) or len(set(ids)) != len(ids) or set(ids) != set(sources):
            raise HTTPException(422, 'Invalid video identities.')
        duration = project.get('duration_ms')
        if type(duration) is not int or duration <= 0:
            raise HTTPException(422, 'Invalid project duration.')
        cursor = 0
        client_stamps = {}
        for video in videos:
            source = sources[video['id']]
            if not isinstance(source, dict):
                raise HTTPException(422, 'Invalid source metadata.')
            path = self.path(source.get('path'), video=True)
            actual = self.writeback_service.source_stamp(path)
            if not _same_stamp(source.get('stamp'), actual):
                raise HTTPException(409, 'Original video changed; annotation writeback was stopped.')
            client_stamps[video['id']] = copy.deepcopy(source['stamp'])
            source.update(path=str(path), stamp=actual)
            for key in ('cache_directory', 'cache_identity_path', 'cache_identity_stamp'):
                source.pop(key, None)
            for key in ('duration_ms', 'start_ms', 'end_ms', 'original_media_start_ms'):
                if type(video.get(key)) is not int:
                    raise HTTPException(422, 'Invalid video timing.')
            if (video['duration_ms'] <= 0 or video['start_ms'] < cursor or video['end_ms'] > duration
                    or video['end_ms'] != video['start_ms'] + video['duration_ms']
                    or source.get('duration_ms') != video['duration_ms']):
                raise HTTPException(422, 'Invalid video timeline.')
            cursor = video['end_ms']
            if not isinstance(video.get('name'), str) or not video['name']:
                raise HTTPException(422, 'Invalid video name.')
            for key in ('alignment_offset_ms', 'filename_start_ms'):
                if key in video and type(video[key]) is not int:
                    raise HTTPException(422, 'Invalid video alignment.')
            relative = video.get('relative_path')
            if (not isinstance(relative, str) or not relative or '\\' in relative
                    or PurePosixPath(relative).is_absolute() or PureWindowsPath(relative).drive
                    or '..' in PurePosixPath(relative).parts):
                raise HTTPException(422, 'Invalid relative video path.')
            # Relinked uploads retain their basename metadata; supplemented
            # sources outside the collection also export their basename.
            expected_relative = {path.name}
            if path.is_relative_to(source_dir):
                expected_relative.add(path.relative_to(source_dir).as_posix())
            if relative not in expected_relative:
                raise HTTPException(422, 'Relative video path does not match its original source.')
            try:
                datetime.fromisoformat(video['recording_start'])
            except (ValueError, TypeError, KeyError):
                raise HTTPException(422, 'Invalid recording timestamp.')
        if cursor != duration:
            raise HTTPException(422, 'Project and video durations disagree.')
        try:
            datetime.fromisoformat(project['recording_start'])
        except (KeyError, ValueError, TypeError):
            raise HTTPException(422, 'Invalid recording timestamp.')
        if not re.fullmatch('[0-9a-f]{64}', str(project.get('source_fingerprint', ''))):
            raise HTTPException(422, 'Invalid source fingerprint.')
        hashes = project.get('_external_hashes')
        if not isinstance(hashes, dict) or any(value is not None and not re.fullmatch('[0-9a-f]{64}', str(value)) for value in hashes.values()):
            raise HTTPException(422, 'Invalid writeback conflict hashes.')
        project['custom_tracks'] = validate_custom_tracks(project.get('custom_tracks', []))
        project['annotations'] = validate_annotations(project.get('annotations'), duration, final=True,
                                                       videos=videos, require_scene_coverage=False,
                                                       custom_tracks=project['custom_tracks'])
        project.setdefault('gaps', [])
        project.setdefault('warnings', [])
        project.setdefault('updated_at', datetime.now().isoformat())
        # Do not follow redirected output folders or preexisting timeline links.
        for child in ('timeline', '.annotation-backups'):
            path = source_dir / child
            if path.is_symlink() or (path.exists() and not path.is_dir()):
                raise HTTPException(409, 'Annotation destination must be a real directory.')
        for directory in (source_dir, source_dir / 'timeline'):
            for name in [*FILENAMES.values(), *(filename_for_axis(item['id']) for item in project['custom_tracks'])]:
                if (directory / name).is_symlink():
                    raise HTTPException(409, 'Annotation files must not be symbolic links.')
        # Keep the verified Windows precision for fresh-import aliases, while
        # actual server stamps remain authoritative during the NAS write itself.
        client_project = {**project, '_sources': {
            ident: {**source, 'stamp': client_stamps[ident]} for ident, source in sources.items()}}
        project['_remote_verified_client_fingerprint'] = ProjectService.current_source_fingerprint(client_project)
        return project

    def writeback(self, payload: dict) -> dict:
        with self.writeback_service.lock:
            project = self._writeback_project(payload.get('project'))
            try:
                request_id = hashlib.sha256(json.dumps(payload['project'], ensure_ascii=False,
                    sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf-8')).hexdigest()
            except (ValueError, TypeError):
                raise HTTPException(422, 'Invalid writeback request metadata.')
            try:
                previous = self.writeback_service.load(project['id'])
            except HTTPException as exc:
                if exc.status_code != 404:
                    raise
                previous = None
            # SQLite retains this marker with the final successful save, so an
            # interrupted response can be replayed without replacing NAS files.
            if (previous and previous.get('_remote_writeback_request_id') == request_id
                    and previous.get('last_writeback') and previous.get('draft_dirty') is False):
                source_dir = Path(project['source_dir'])
                if self.writeback_service.current_external_hashes(source_dir) != self.writeback_service.expected_external_hashes(previous):
                    raise HTTPException(409, 'Annotations changed after the previous save; writeback was stopped.')
                result = {'paths': [str(source_dir / 'timeline' / filename_for_axis(axis)) for axis in
                                    (*AXES, *(item['id'] for item in project['custom_tracks']))],
                          'save_id': previous['last_writeback']['save_id']}
                return self._writeback_response(result, previous)
            project['_remote_writeback_request_id'] = request_id
            project['last_writeback'] = None
            project['draft_dirty'] = True
            self.writeback_service.save(project)
            result = self.writeback_service.writeback(project['id'])
            saved = self.writeback_service.load(project['id'])
            return self._writeback_response(result, saved)

    @staticmethod
    def _writeback_response(result: dict, project: dict) -> dict:
        return {'result': result, 'project': {key: project.get(key) for key in
                ('_external_hashes', 'draft_dirty', 'warnings', 'last_writeback', 'updated_at')}}

    def close(self):
        self.stopping.set()
        for service in (*self.services, self.writeback_service):
            service.sessions.close()
            service.previews.close()
        for thread in self.threads:
            thread.join(timeout=10)


def create_worker(root=None, token=None, allowed_roots=None, slots=None) -> FastAPI:
    if token is None:
        token_file = os.environ.get('DATAMARK_WORKER_TOKEN_FILE')
        if not token_file:
            raise RuntimeError('DATAMARK_WORKER_TOKEN_FILE must point to a private token file.')
        token = Path(token_file).read_text(encoding='utf-8').strip()
    if not isinstance(token, str) or len(token) < 32 or any(character.isspace() for character in token):
        raise RuntimeError('Worker authentication token is empty or invalid.')
    root = Path(root or os.environ.get('DATAMARK_WORKER_ROOT') or Path(__file__).resolve().parents[1]).resolve()
    if allowed_roots is None:
        configured = os.environ.get('DATAMARK_WORKER_ALLOWED_ROOTS')
        allowed_roots = json.loads(configured) if configured else DEFAULT_ROOTS
    if slots is None:
        try:
            slots = int(os.environ.get('DATAMARK_WORKER_SLOTS', '2'))
        except ValueError as exc:
            raise RuntimeError('DATAMARK_WORKER_SLOTS must be 1 or 2.') from exc
    worker = Worker(root, allowed_roots, slots=slots)

    @asynccontextmanager
    async def lifespan(app):
        yield
        worker.close()

    app = FastAPI(title='DataMark NAS Worker', docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.add_middleware(_RequestBoundary, token=token)
    app.state.worker = worker
    app.state.service = worker.service

    @app.get('/health')
    def health():
        return {'status': 'ok', 'application': 'datamark-worker', 'protocol': PROTOCOL, 'profile': PROFILE,
                'slots': len(worker.services), 'nas_archive_cache': True}

    @app.post('/v1/inspect')
    def inspect(payload: dict):
        return worker.inspect(payload)

    @app.post('/v1/jobs')
    def submit(payload: dict):
        return worker.submit(payload)

    @app.get('/v1/jobs/{ident}')
    def status(ident: str):
        return worker.status(ident)

    @app.get('/v1/jobs/{ident}/archive')
    def archive(ident: str):
        state = worker.status(ident)
        if state['state'] != 'ready':
            raise HTTPException(409, 'The server cache is not ready.')
        return FileResponse(worker._archive(ident), media_type='application/zip', filename=ident + '.zip',
                            headers={'Cache-Control': 'private, no-store'})

    @app.post('/v1/writeback')
    def writeback(payload: dict):
        return worker.writeback(payload)

    return app
