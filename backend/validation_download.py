"""Version-pinned parallel reads; whole-file SHA-256 remains the publish gate."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import os
from pathlib import Path
import re
import shutil
import uuid

from fastapi import HTTPException

CHUNK = 4 * 1024**2
THRESHOLD = 8 * 1024**2


def download(store, key, destination, checksum, size, etag, progress=lambda n: None):
    if not re.fullmatch('[0-9a-f]{64}', str(checksum)) or type(size) is not int or not 0 < size <= 16 * 1024**3:
        raise HTTPException(422, '核验文件清单无效。')
    if destination.is_file() and destination.stat().st_size == size:
        from .r2_store import content_sha
        if content_sha(destination) == checksum:
            progress(size)
            return
    if size < THRESHOLD or not etag:
        with store.validation_download_slots:
            store.download(key, destination, checksum, size)
        progress(size)
        return
    identity = hashlib.sha256((key+'\0'+etag+'\0'+checksum+':'+str(size)+'\0'+destination.name).encode()).hexdigest()
    parts = destination.parent / ('.ranges-' + identity)
    if parts.is_symlink():
        raise HTTPException(409, '核验分段目录无效。')
    parts.mkdir(parents=True, exist_ok=True)
    def read_part(index):
        start = index * CHUNK
        end = min(size, start + CHUNK) - 1
        length = end-start+1
        path = parts / f'{index:06d}.bin'
        proof = path.with_suffix('.sha256')
        if path.is_file() and proof.is_file() and path.stat().st_size == length:
            from .r2_store import content_sha
            try:
                cached = content_sha(path) == proof.read_text()
            except (OSError, UnicodeError):
                cached = False
            if cached:
                progress(length)
                return path
        temporary = path.with_name(path.name+'.'+uuid.uuid4().hex+'.tmp')
        proof_temp = temporary.with_suffix('.proof')
        try:
            with store.validation_download_slots:
                response = store.call('get_object', Key=key, Range=f'bytes={start}-{end}', IfMatch=etag)
                with response['Body'] as body, temporary.open('xb') as output:
                    if (response.get('ContentRange') != f'bytes {start}-{end}/{size}'
                            or response.get('ContentLength') != length or response.get('ETag') != etag):
                        raise HTTPException(409, '存储返回的核验分段或对象版本不一致。')
                    digest, received = hashlib.sha256(), 0
                    while chunk := body.read(256*1024):
                        received += len(chunk)
                        if received > length:
                            raise HTTPException(409, '核验分段超过声明长度。')
                        output.write(chunk); digest.update(chunk)
                    if received != length:
                        raise HTTPException(409, '核验分段传输不完整，可重试续传。')
                    output.flush(); os.fsync(output.fileno())
            proof_temp.write_text(digest.hexdigest())
            os.replace(temporary, path)
            os.replace(proof_temp, proof)
            progress(length)
            return path
        finally:
            temporary.unlink(missing_ok=True); proof_temp.unlink(missing_ok=True)
    # Only four range readers per file; all files share the store's six-request cap.
    with ThreadPoolExecutor(max_workers=4) as pool:
        chunks = list(pool.map(read_part, range((size+CHUNK-1)//CHUNK)))
    temporary = destination.with_name('.'+destination.name+'.'+uuid.uuid4().hex+'.tmp')
    try:
        digest = hashlib.sha256()
        with temporary.open('xb') as output:
            for path in chunks:
                with path.open('rb') as source:
                    while chunk := source.read(256*1024):
                        output.write(chunk); digest.update(chunk)
            output.flush(); os.fsync(output.fileno())
        if temporary.stat().st_size != size or digest.hexdigest() != checksum:
            shutil.rmtree(parts)
            raise HTTPException(409, 'R2 文件完整摘要校验失败，未发布采集目录。')
        os.replace(temporary, destination)
        shutil.rmtree(parts)
    finally:
        temporary.unlink(missing_ok=True)
