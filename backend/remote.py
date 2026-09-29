"""Project-scoped, SSH-tunneled client for the optional NAS processing worker."""
from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import zipfile
from pathlib import Path, PurePosixPath, PureWindowsPath

import httpx
from fastapi import HTTPException

from .project_storage import checked_tree

MAX_ARCHIVE_BYTES = 16 * 1024 ** 3
RESERVE_BYTES = 256 * 1024 ** 2
RETRY_WINDOW_SECONDS = 300
RETRY_DELAYS = (1, 2, 4, 8, 15, 30)
LOGGER = logging.getLogger('uvicorn.error')


class _RemoteUnavailable(HTTPException):
    """A transport failure or temporary worker response, not a data error."""


def matching_stamp(left: dict, right: dict) -> bool:
    try:
        return (left['size'] == right['size'] and left['sample_sha256'] == right['sample_sha256']
                and abs(left['mtime_ns'] - right['mtime_ns']) < 1_000_000)
    except (KeyError, TypeError):
        return False


class RemoteClient:
    def __init__(self, root: Path, config: dict):
        self.root = root
        self.config = config
        self._lock = threading.RLock()
        self._tunnel = None
        self._tunnel_ready = False
        self._closed = False
        self._closing = threading.Event()
        self._inspections: dict[str, tuple[float, dict]] = {}
        self._transfer_lock = threading.Lock()
        self._transfer_rates: dict[str, tuple[float, float]] = {}
        self._log = None
        self.local_port = int(config.get('local_port', 18121))
        if not 1024 <= self.local_port <= 65535:
            raise ValueError('Invalid DataMark tunnel port')
        self.base_url = f'http://127.0.0.1:{self.local_port}'
        self.token = Path(config['token_file']).read_text(encoding='utf-8').strip()
        if len(self.token) < 32 or any(c.isspace() for c in self.token):
            raise ValueError('Invalid DataMark worker token file')
        self.mappings = list(config.get('mappings', []))
        for mapping in self.mappings:
            local, remote = mapping['local'], mapping['remote']
            if not PureWindowsPath(local).is_absolute() or not PurePosixPath(remote).is_absolute():
                raise ValueError('DataMark mappings require absolute paths')
            if '..' in PureWindowsPath(local).parts or '..' in PurePosixPath(remote).parts:
                raise ValueError('Invalid DataMark path mapping')
        self.mappings.sort(key=lambda item: len(item['local']), reverse=True)

    @classmethod
    def from_root(cls, root: Path):
        path = root / '.local' / 'remote' / 'worker.json'
        if not path.is_file():
            return None
        config = json.loads(path.read_text(encoding='utf-8-sig'))
        return cls(root, config) if config.get('enabled') is True else None

    def map_path(self, value: str | Path) -> str | None:
        raw = str(value)
        candidate = PureWindowsPath(raw)
        if '..' in candidate.parts:
            raise HTTPException(422, '素材路径不能包含上级目录跳转。')
        if not candidate.is_absolute():
            return None
        for mapping in self.mappings:
            try:
                relative = candidate.relative_to(PureWindowsPath(mapping['local']))
            except ValueError:
                continue
            if any(':' in part or '\x00' in part for part in relative.parts):
                raise HTTPException(422, '素材路径含有不支持的文件名。')
            return str(PurePosixPath(mapping['remote']).joinpath(*relative.parts))
        return None

    def _http(self, *, read_timeout=30):
        return httpx.Client(base_url=self.base_url, headers={'Authorization': 'Bearer ' + self.token},
                            timeout=httpx.Timeout(read_timeout, connect=3), trust_env=False)

    def _async_http(self, *, read_timeout=30):
        return httpx.AsyncClient(base_url=self.base_url, headers={'Authorization': 'Bearer ' + self.token},
                                 timeout=httpx.Timeout(read_timeout, connect=3), trust_env=False)

    def download_rate(self) -> float:
        now = time.monotonic()
        with self._transfer_lock:
            return sum(rate for rate, updated in self._transfer_rates.values() if now - updated < 2)

    def _healthy(self) -> bool:
        try:
            with self._http(read_timeout=2) as client:
                response = client.get('/health')
                data = response.json()
                return response.status_code == 200 and data.get('application') == 'datamark-worker' and data.get('protocol') == 1
        except (httpx.HTTPError, ValueError):
            return False

    def _ensure_tunnel(self, stopping=None):
        with self._lock:
            self._check_stop(stopping)
            if self._closed:
                raise HTTPException(503, '平台正在关闭，请重新打开后连接处理服务器。')
            if (self._tunnel is not None and self._tunnel.poll() is None
                    and self._tunnel_ready):
                # HTTP latency must not tear down the other clip's download.
                # SSH keepalives detect a dead transport; the next retry recreates it.
                return
            if self._tunnel is not None and self._tunnel.poll() is None:
                self._tunnel.terminate()
                try:
                    self._tunnel.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self._tunnel.kill()
                    self._tunnel.wait()
            self._tunnel_ready = False
            if self._log:
                self._log.close()
            host = self.config['host']
            user = self.config['user']
            remote_port = int(self.config.get('port', 18120))
            if (not re.fullmatch(r'[A-Za-z0-9.:-]+', host) or not re.fullmatch(r'[A-Za-z0-9_-]+', user)
                    or not 1024 <= remote_port <= 65535):
                raise HTTPException(503, '处理服务器连接配置无效。')
            key, known = Path(self.config['identity_file']), Path(self.config['known_hosts_file'])
            if not key.is_file() or not known.is_file():
                raise HTTPException(503, '缺少处理服务器的专用连接凭据，请重新运行部署入口。')
            log_path = self.root / '.local' / 'remote' / 'tunnel.log'
            self._log = log_path.open('ab')
            log_offset = self._log.tell()
            args = [shutil.which('ssh') or 'ssh', '-F', os.devnull, '-v', '-N', '-T', '-o', 'BatchMode=yes',
                    '-o', 'IdentitiesOnly=yes', '-o', 'StrictHostKeyChecking=yes', '-o', 'UpdateHostKeys=no',
                    '-o', 'ExitOnForwardFailure=yes', '-o', 'ConnectTimeout=8',
                    '-o', 'ServerAliveInterval=15', '-o', 'ServerAliveCountMax=2',
                    '-o', 'UserKnownHostsFile=' + str(known),
                    '-o', 'HostKeyAlias=' + self.config.get('host_key_alias', host),
                    '-i', str(key), '-L', f'127.0.0.1:{self.local_port}:127.0.0.1:{remote_port}', user + '@' + host]
            try:
                self._tunnel = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=self._log, stderr=self._log,
                                                creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
                deadline = time.monotonic() + 15
                while time.monotonic() < deadline and self._tunnel.poll() is None:
                    self._check_stop(stopping)
                    # Authenticate HTTP only after our SSH process owns the local listener.
                    with log_path.open('rb') as log:
                        log.seek(log_offset)
                        messages = log.read()
                    marker = f'Local forwarding listening on 127.0.0.1 port {self.local_port}.'.encode('ascii')
                    self._tunnel_ready = marker in messages
                    if self._tunnel_ready and self._tunnel.poll() is None and self._healthy():
                        return
                    if stopping is not None:
                        stopping.wait(.2)
                    else:
                        time.sleep(.2)
            except OSError:
                pass
            raise _RemoteUnavailable(503, '无法连接处理服务器；已有本机缓存和草稿仍保留。')

    @staticmethod
    def _check_response(response):
        if response.is_success:
            return
        try:
            detail = response.json().get('detail')
        except (ValueError, AttributeError):
            detail = None
        if response.status_code in {429, 502, 503, 504}:
            raise _RemoteUnavailable(response.status_code,
                                     detail if isinstance(detail, str) else '处理服务器暂时不可用。')
        code = response.status_code if response.status_code in {400, 401, 403, 404, 409, 413, 422, 429, 503, 507} else 502
        raise HTTPException(code, detail if isinstance(detail, str) else '处理服务器请求失败，请检查连接后重试。')

    async def _cancellable_request(self, method, path, body, read_timeout, stopping):
        async with self._async_http(read_timeout=read_timeout) as client:
            pending = asyncio.create_task(client.request(method, path, json=body))
            try:
                while not pending.done():
                    self._check_stop(stopping)
                    await asyncio.wait({pending}, timeout=.2)
                self._check_stop(stopping)
                return await pending
            finally:
                if not pending.done():
                    pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)

    def request(self, method: str, path: str, *, body=None, read_timeout=180, stopping=None):
        self._check_stop(stopping)
        self._ensure_tunnel(stopping=stopping)
        self._check_stop(stopping)
        try:
            if stopping is None:
                with self._http(read_timeout=read_timeout) as client:
                    response = client.request(method, path, json=body)
            else:
                response = asyncio.run(self._cancellable_request(method, path, body, read_timeout, stopping))
            self._check_response(response)
            return response.json()
        except httpx.HTTPError as exc:
            LOGGER.warning('Remote %s %s interrupted (%s)', method, path, type(exc).__name__)
            raise _RemoteUnavailable(503, '处理服务器连接暂时中断；已有本机缓存和草稿仍保留。') from exc
        except ValueError as exc:
            raise HTTPException(502, '处理服务器返回的数据格式不正确，请检查服务器版本。') from exc

    def _retry(self, action, stopping, on_retry):
        deadline = None
        attempt = 0
        while True:
            self._check_stop(stopping)
            remaining = RETRY_WINDOW_SECONDS if deadline is None else deadline - time.monotonic()
            if remaining <= 0:
                raise HTTPException(503, '自动重连仍未成功。已保留缓存和下载进度，请恢复内网连接后点击重试。')
            try:
                return action(remaining)
            except _RemoteUnavailable as exc:
                self._check_stop(stopping)
                if deadline is None:
                    deadline = time.monotonic() + RETRY_WINDOW_SECONDS
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise HTTPException(503, '自动重连仍未成功。已保留缓存和下载进度，请恢复内网连接后点击重试。') from exc
                delay = min(RETRY_DELAYS[min(attempt, len(RETRY_DELAYS) - 1)], remaining)
                attempt += 1
                LOGGER.warning('Remote preparation retry %s in %.1fs (HTTP %s)', attempt, delay, exc.status_code)
                on_retry(delay)
                if stopping.wait(delay):
                    self._check_stop(stopping)

    def inspect(self, path: str | Path) -> dict:
        mapped = self.map_path(path)
        if mapped is None:
            raise HTTPException(422, '素材目录没有配置服务器路径映射。')
        # create() requests probe and stamp in succession; share only that short inspection.
        with self._lock:
            previous = self._inspections.get(mapped)
            if previous and time.monotonic() - previous[0] < 2:
                return copy.deepcopy(previous[1])
        result = self.request('POST', '/v1/inspect', body={'path': mapped})
        with self._lock:
            self._inspections = {key: value for key, value in self._inspections.items() if time.monotonic() - value[0] < 2}
            self._inspections[mapped] = (time.monotonic(), result)
        return copy.deepcopy(result)

    def _check_stop(self, stopping=None):
        if self._closing.is_set() or (stopping is not None and stopping.is_set()):
            raise HTTPException(503, '素材下载已暂停，下次准备时继续。')

    def _download(self, job_id, record, target, update, stopping):
        def reconnect(delay):
            size = target.stat().st_size if target.exists() else 0
            progress = 80 + min(size / record['size'], 1) * 17
            update('reconnect', progress,
                   f'连接暂时中断，{delay:g} 秒后自动重试；已完成的缓存和下载进度保留。')
        self._retry(lambda remaining: self._download_once(job_id, record, target, update, stopping,
                                                         read_timeout=min(20, remaining)),
                    stopping, reconnect)

    def _download_once(self, job_id, record, target, update, stopping, *, read_timeout=20):
        self._check_stop(stopping)
        expected_size, expected_hash = record['size'], record['sha256']
        if type(expected_size) is not int or not 0 < expected_size <= MAX_ARCHIVE_BYTES or not re.fullmatch(r'[a-f0-9]{64}', expected_hash):
            raise HTTPException(422, '服务器缓存包信息无效。')
        if target.exists() and target.stat().st_size > expected_size:
            target.unlink()
        offset = target.stat().st_size if target.exists() else 0
        if shutil.disk_usage(target.parent).free < expected_size - offset + RESERVE_BYTES:
            raise HTTPException(507, '本机磁盘空间不足，无法下载缓存包。')
        if offset != expected_size:
            self._ensure_tunnel(stopping=stopping)
            self._check_stop(stopping)
            try:
                with self._http(read_timeout=read_timeout) as client, client.stream('GET', f'/v1/jobs/{job_id}/archive',
                        headers={'Range': f'bytes={offset}-'} if offset else {}) as response:
                    if response.status_code not in {200, 206}:
                        response.read()
                        self._check_response(response)
                    if response.status_code == 206:
                        match = re.fullmatch(r'bytes (\d+)-(\d+)/(\d+)', response.headers.get('content-range', ''))
                        if not match or int(match[1]) != offset or int(match[3]) != expected_size:
                            raise HTTPException(422, '服务器缓存包续传响应不一致，请重试。')
                    else:
                        offset = 0
                    received = offset
                    sampled_bytes, sampled_at = received, time.monotonic()
                    notified_at = None
                    with target.open('ab' if offset else 'wb') as handle:
                        # Check cancellation on each received chunk, even on a slow connection.
                        for block in response.iter_bytes():
                            self._check_stop(stopping)
                            received += len(block)
                            if received > expected_size:
                                raise HTTPException(422, '服务器缓存包大小不一致。')
                            handle.write(block)
                            now = time.monotonic()
                            # Keep small-chunk cancellation responsive, but avoid recounting
                            # every clip and notifying the UI for every network packet.
                            if notified_at is None or now - notified_at >= .25 or received == expected_size:
                                elapsed = now - sampled_at
                                rate = (received - sampled_bytes) / elapsed if elapsed > 0 else 0
                                with self._transfer_lock:
                                    self._transfer_rates[job_id] = (rate, now)
                                update('download', 80 + received / expected_size * 17, '正在下载服务器生成的缓存包。')
                                sampled_bytes, sampled_at, notified_at = received, now, now
                        handle.flush()
                        os.fsync(handle.fileno())
            except httpx.HTTPError as exc:
                LOGGER.warning('Remote archive download interrupted (%s)', type(exc).__name__)
                raise _RemoteUnavailable(503, '缓存包下载暂时中断，已保留下载进度。') from exc
            finally:
                with self._transfer_lock:
                    self._transfer_rates.pop(job_id, None)
        self._check_stop(stopping)
        if target.stat().st_size < expected_size:
            raise _RemoteUnavailable(503, '缓存包传输尚未完整，正在等待续传。')
        digest = hashlib.sha256()
        with target.open('rb') as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b''):
                self._check_stop(stopping)
                digest.update(block)
        if target.stat().st_size != expected_size or digest.hexdigest() != expected_hash:
            target.unlink(missing_ok=True)
            raise HTTPException(422, '缓存包完整性校验失败，未启用下载内容，请重试。')

    def prepare(self, source: dict, directory: Path, key: str, update, stopping) -> dict:
        from .session_cache import PROFILE
        self._check_stop(stopping)
        started = time.monotonic()
        progress = 0
        cache_origin = 'unknown'
        def report(operation, value, detail):
            nonlocal progress
            progress = max(progress, value)
            if cache_origin == 'nas' and detail == '正在下载服务器生成的缓存包。':
                detail = '正在下载 NAS 已保存的缓存包，无需重新生成。'
            update(operation, progress, detail)
        def reconnect(delay):
            report('reconnect', progress,
                   f'连接暂时中断，{delay:g} 秒后自动重试；已完成的缓存和下载进度保留。')
        def call(method, path, *, body=None, timeout=15):
            return self._retry(lambda remaining: self.request(method, path, body=body,
                read_timeout=min(timeout, remaining), stopping=stopping), stopping, reconnect)
        payload = {
            'path': self.map_path(source['path']), 'stamp': source['stamp'],
            'duration_ms': source['duration_ms'], 'media_start_seconds': source.get('media_start_seconds', 0), 'profile': PROFILE}
        report('nas', 0, '本机缓存不完整，正在检查 NAS 已保存的缓存包。')
        status = call('POST', '/v1/jobs', body=payload, timeout=180)
        restarts = 0
        def job_id(value):
            ident = value['id']
            if not isinstance(ident, str) or not re.fullmatch(r'[a-f0-9]{32,64}', ident):
                raise HTTPException(422, '处理服务器任务编号无效。')
            return ident
        ident = job_id(status)
        while status['state'] != 'ready':
            self._check_stop(stopping)
            if status['state'] in {'failed', 'error', 'partial'}:
                raise HTTPException(422, status.get('detail') or '服务器处理失败，请重试。')
            detail = {'packaging': '服务器正在打包并校验播放素材。',
                      'publishing': '服务器正在将完成的缓存包保存到原 FPV 目录的 NAS 缓存中。'}.get(status.get('stage'))
            report('remote', min(79, max(0, float(status.get('progress') or 0) * .79)),
                   detail or ('服务器正在排队处理素材。' if status['state'] == 'queued'
                              else '服务器正在生成精简视频、倍速视频和缩略图。'))
            if stopping.wait(.5):
                self._check_stop(stopping)
            try:
                status = call('GET', '/v1/jobs/' + ident)
            except HTTPException as exc:
                if exc.status_code != 404 or restarts >= 3:
                    raise
                # A worker restart can lose an in-memory job, but submission is
                # content-addressed and reuses completed server cache stages.
                restarts += 1
                report('reconnect', progress, '服务器任务已中断，正在恢复任务并复用已有缓存。')
                if stopping.wait(1):
                    self._check_stop(stopping)
                status = call('POST', '/v1/jobs', body=payload, timeout=180)
                ident = job_id(status)
        self._check_stop(stopping)
        cache_origin = status.get('cache_origin', 'unknown')
        if cache_origin not in {'nas', 'generated', 'server-local'}:
            cache_origin = 'unknown'
        if cache_origin == 'nas':
            report('nas', 80, '已找到 NAS 完整缓存包，直接下载到本机。')
        prepared_at = time.monotonic()
        record = status['archive']
        if not re.fullmatch(r'[a-f0-9]{64}', record.get('sha256', '')):
            raise HTTPException(422, '服务器缓存包校验信息无效。')
        target = directory / ('.remote-' + ident + '-' + record['sha256'][:16] + '.zip.partial')
        self._download(ident, record, target, report, stopping)
        downloaded_at = time.monotonic()
        report('download', 98, '正在校验并安装本机播放缓存。')
        try:
            entry = self._install(target, directory, key, source, stopping)
        except (zipfile.BadZipFile, KeyError, TypeError, ValueError):
            target.unlink(missing_ok=True)
            raise HTTPException(422, '服务器缓存包格式不正确，未启用下载内容。')
        target.unlink(missing_ok=True)
        LOGGER.info('Remote playback ready %s: cache_origin=%s wait=%.3fs download_verify=%.3fs install=%.3fs worker=%s',
                    ident, cache_origin, prepared_at - started, downloaded_at - prepared_at,
                    time.monotonic() - downloaded_at, json.dumps(status.get('timings', {}), ensure_ascii=True))
        return entry

    def _install(self, archive_path, directory, key, source, stopping):
        from .session_cache import PROFILE, SessionCache
        with zipfile.ZipFile(archive_path) as archive:
            info = archive.getinfo('manifest.json')
            if info.file_size > 1024 * 1024:
                raise ValueError('Oversized manifest')
            manifest = json.loads(archive.read(info))
            if (manifest['protocol'] != 1 or manifest['profile'] != PROFILE
                    or manifest['duration_ms'] != source['duration_ms']
                    or abs(manifest['media_start_seconds'] - source.get('media_start_seconds', 0)) >= .001
                    or not matching_stamp(manifest['source_stamp'], source['stamp'])):
                raise HTTPException(409, '服务器缓存包与当前素材不一致，请重新准备。')
            story = SessionCache._storyboard_metadata(source['duration_ms'])
            if manifest['storyboard'] != story or len(manifest['sheets']) != (story['frame_count'] + 99) // 100:
                raise ValueError('Storyboard mismatch')
            files = manifest['files']
            pairs = [(files['normal'], key + '.compact-v1.mp4', False),
                     (files['fast'], key + '.fast20.mp4', False),
                     (files['thumbnail'], key + '.jpg', True)]
            pairs += [(record, key + f'.sheet-{index:05d}.jpg', True) for index, record in enumerate(manifest['sheets'])]
            names = [record['name'] for record, _, _ in pairs]
            if (len(names) != len(set(names)) or len(archive.infolist()) != len(names) + 1
                    or set(archive.namelist()) != {'manifest.json', *names}):
                raise ValueError('Unexpected archive members')
            total = 0
            for record, _, _ in pairs:
                name = record['name']
                if not re.fullmatch(r'[A-Za-z0-9._-]+', name) or name in {'.', '..'}:
                    raise ValueError('Unsafe filename')
                member = archive.getinfo(name)
                if ((member.external_attr >> 16) & 0o170000) == 0o120000 or type(record['size']) is not int or record['size'] < 4 or member.file_size != record['size']:
                    raise ValueError('Invalid archive file')
                total += record['size']
            if total > MAX_ARCHIVE_BYTES or shutil.disk_usage(directory).free < total + RESERVE_BYTES:
                raise HTTPException(507, '本机磁盘空间不足，无法安装播放缓存。')
            with tempfile.TemporaryDirectory(prefix='.remote-install-', dir=directory) as temporary:
                staging = Path(temporary)
                for record, filename, jpeg in pairs:
                    self._check_stop(stopping)
                    dest = staging / filename
                    digest = hashlib.sha256()
                    with archive.open(record['name']) as incoming, dest.open('xb') as outgoing:
                        for block in iter(lambda: incoming.read(1024 * 1024), b''):
                            self._check_stop(stopping)
                            outgoing.write(block)
                            digest.update(block)
                        outgoing.flush()
                        os.fsync(outgoing.fileno())
                    local_record = SessionCache._file_record(dest, staging)
                    if digest.hexdigest() != record['sha256'] or SessionCache._valid_record(staging, local_record, jpeg=jpeg) is None:
                        raise HTTPException(422, '下载的播放素材校验失败，未启用缓存包。')
                self._check_stop(stopping)
                checked_tree(directory, directory.parent)
                # Every file is verified before replacing; the entry manifest is published last.
                for _, filename, _ in pairs:
                    os.replace(staging / filename, directory / filename)
            records = [SessionCache._file_record(directory / filename, directory) for _, filename, _ in pairs]
            entry = {'profile': PROFILE, 'key': key, 'duration_ms': source['duration_ms'],
                     'normal': records[0], 'fast': records[1], 'thumbnail': records[2],
                     'sheets': records[3:], 'storyboard': story}
            SessionCache._write_json(directory / (key + '.assets.json'), entry)
            SessionCache._write_json(directory / (key + '.compact-v1.json'),
                                     {'profile': PROFILE, 'duration_ms': source['duration_ms'], 'file': records[0]})
            return entry

    def writeback(self, project):
        mapped = copy.deepcopy(project)
        mapped['source_dir'] = self.map_path(project['source_dir'])
        for source in mapped['_sources'].values():
            source['path'] = self.map_path(source['path'])
            if source['path'] is None:
                raise HTTPException(422, '项目包含未配置服务器路径映射的视频，无法通过服务器写回。')
        return self.request('POST', '/v1/writeback', body={'project': mapped})

    def close(self):
        self._closing.set()
        with self._lock:
            self._closed = True
            if self._tunnel is not None and self._tunnel.poll() is None:
                self._tunnel.terminate()
                try:
                    self._tunnel.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self._tunnel.kill()
                    self._tunnel.wait()
            if self._log:
                self._log.close()
                self._log = None
