"""Bounded direct S3 transfer; retries count unique bytes, never duplicate progress."""
from concurrent.futures import ThreadPoolExecutor
import threading
import time

import httpx

from .mock_s3 import MockUploadManager


class TransferProgress:
    def __init__(self, target, items):
        self.target = target
        self.total = sum(i.get('size', 0) for i in items)
        self.count = len(items)
        self.bytes = {}
        self.done = set()
        self.started = time.monotonic()
        self.last = 0
        self.wire_bytes = 0
        self.lock = threading.RLock()

    def update(self, index, part, size, *, done=False, force=False, measured=False):
        with self.lock:
            if measured:
                self.wire_bytes += max(0, size - self.bytes.get((index, part), 0))
            self.bytes[index, part] = size
            if done:
                self.done.add(index)
            now = time.monotonic()
            if not force and now - self.last < .25:
                return
            self.last = now
            sent = min(self.total, sum(self.bytes.values()))
            speed = self.wire_bytes / max(.01, now - self.started)
            self.target.transfer_progress(dict(state='uploading', progress=round(sent / max(1, self.total) * 100, 1),
                bytes_uploaded=sent, total_bytes=self.total, speed_bps=round(speed),
                files_done=len(self.done), files_total=self.count))
            self.target.progress(f'正在上传 {len(self.done)}/{self.count} 个文件 · '
                f'{sent / 1024**2:.1f}/{self.total / 1024**2:.1f} MiB · {speed / 1024**2:.2f} MiB/s')


def transfer_files(target, job, prepared, manifest, *, completed_keys=None):
    check_cancel = getattr(target, 'check_cancel', lambda: None)
    check_cancel()
    items = manifest['files'] + manifest.get('attachments', []) + manifest.get('cache_files', [])
    tracker = TransferProgress(target, items)
    # Three simultaneous requests at most; files and multipart chunks share the same pool.
    with httpx.Client(timeout=httpx.Timeout(180, connect=15), follow_redirects=False, trust_env=False,
                      limits=httpx.Limits(max_connections=3, max_keepalive_connections=3)) as direct, \
            ThreadPoolExecutor(max_workers=3) as parts, ThreadPoolExecutor(max_workers=3) as files:
        def send(index, source, number, offset, length, url, headers, cancelled):
            if cancelled.is_set():
                raise RuntimeError('本轮文件传输已暂停，等待续传。')
            def payload():
                sent = 0
                with source.open('rb') as stream:
                    stream.seek(offset)
                    while sent < length:
                        check_cancel()
                        if cancelled.is_set():
                            raise RuntimeError('本轮文件传输已暂停，等待续传。')
                        chunk = stream.read(min(64 * 1024, length - sent))
                        if not chunk:
                            raise RuntimeError('上传期间源文件长度发生变化。')
                        yield chunk
                        sent += len(chunk)
                        tracker.update(index, number, sent, measured=True)
            tracker.update(index, number, 0)
            try:
                response = direct.put(url, content=payload(), headers={**headers, 'Content-Length': str(length)})
                if not response.is_success:
                    raise RuntimeError('S3 上传未确认成功。')
            except Exception:
                cancelled.set()
                tracker.update(index, number, 0, force=True)
                raise
            tracker.update(index, number, length)
        def upload(index):
            item = items[index]
            if (item['key'],item.get('sha256')) in (completed_keys or set()):
                tracker.update(index,0,item.get('size',0),done=True,force=True)
                return
            offset = index - len(manifest['files'])
            record = target.attachments[offset] if 0 <= offset < len(target.attachments) else None
            source = MockUploadManager._check_local_source(record) if record else prepared / item['key']
            for attempt in range(3):
                check_cancel()
                plan = target.request('GET', f"/api/r2/uploads/{job['id']}/files/{index}?single=1")
                if plan['complete']:
                    # A retried file may already exist; account for all bytes without retransmission.
                    with tracker.lock:
                        for key in list(tracker.bytes):
                            if key[0] == index:
                                del tracker.bytes[key]
                    tracker.update(index, 0, item.get('size', 0), done=True, force=True)
                    break
                try:
                    futures = []
                    cancelled = threading.Event()
                    if plan.get('mode') == 'single':
                        futures.append(parts.submit(send,index,source,0,0,item['size'],plan['url'],plan['headers'],cancelled))
                    else:
                        for part in plan['parts']:
                            number = part['number']
                            start = (number - 1) * plan['part_size']
                            length = min(plan['part_size'], item['size'] - start)
                            if part['uploaded']:
                                tracker.update(index, number, length)
                            else:
                                futures.append(parts.submit(send,index,source,number,start,length,part['url'],{},cancelled))
                    failure = None
                    for future in futures:
                        try:
                            future.result()
                        except Exception as error:
                            failure = error
                    if failure:
                        raise failure
                    if plan.get('mode') != 'single':
                        target.request('POST', f"/api/r2/uploads/{job['id']}/files/{index}/complete")
                    tracker.update(index, -1, 0, done=True, force=True)
                    break
                except (httpx.HTTPError, RuntimeError):
                    if attempt == 2:
                        raise RuntimeError('上传中断，可重试续传；已完成文件和分片保留。') from None
                    time.sleep(1)
            if record:
                MockUploadManager._check_local_source(record)
        # Wait for every worker on both success and failure; no detached upload continues after return.
        failures = []
        for result in [files.submit(upload, i) for i in range(len(items))]:
            try:
                result.result()
            except Exception as error:
                failures.append(error)
        if failures:
            raise failures[0]
