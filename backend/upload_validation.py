"""Resumable, bounded publication checks. Only verified collections are published."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import copy
from pathlib import Path

from fastapi import HTTPException

from .r2_folder import validate_prediction
from .service import document_bytes
from .upload_playback import validate as validate_playback
from .upload_proofs import policy


def validate_upload(uploads, ident, user, authorize=None, *, prepare=False):
    # A slow validation blocks only this collection, never another upload.
    with uploads.job_lock(ident, 'validation'):
        job = uploads.load(ident, user)
        if job['state'] == 'ready':
            return uploads.public(job)
        def permitted():
            current = authorize() if authorize else user
            if current.get('id') != user['id'] or not current.get('can_upload'):
                raise HTTPException(403, '上传权限或会话已失效，未发布采集目录。')
            return current
        def update(**changes):
            with uploads.job_lock(ident):
                current = uploads.load(ident, user)
                current.update(changes)
                uploads.save(current)
                return current
        manifest = job['manifest']
        items = uploads.items(job)
        validate_playback(manifest)
        update(state='validating', validation_error='')
        def read_progress(index, count, reset=False):
            with uploads.job_lock(ident):
                current = uploads.load(ident, user)
                counts = current.setdefault('validation_read_bytes', {})
                counts[str(index)] = 0 if reset else min(items[index]['size'], counts.get(str(index), 0) + count)
                uploads.save(current)
        def verify(index):
            permitted()
            item = items[index]
            key = manifest['prefix'] + '/' + item['key']
            before = uploads.provider.head(key)
            if before['ContentLength'] != item['size'] or before.get('Metadata', {}).get('sha256') != item['sha256']:
                raise HTTPException(409, '上传文件大小或摘要信息与清单不一致。')
            etag = before.get('ETag')
            with uploads.job_lock(ident):
                saved=uploads.load(ident,user)
                checkpoint = saved.get('verified', {}).get(str(index))
            read_progress(index, 0, reset=True)
            contract = policy(manifest, index)
            proof = uploads.proofs.get(key, etag, contract)
            media_info=(proof or {}).get('info') or saved.get('media_infos',{}).get(str(index))
            if etag and (checkpoint == etag or proof) and (index >= len(manifest['files']) or media_info):
                read_progress(index, item['size'])
                with uploads.job_lock(ident):
                    current = uploads.load(ident, user)
                    current.setdefault('verified', {})[str(index)] = etag
                    if index < len(manifest['files']):
                        current.setdefault('media_infos',{})[str(index)]=media_info
                    uploads.save(current)
                return
            version = hashlib.sha256(str(etag).encode()).hexdigest()[:16]
            destination = uploads.service.local / 'upload-validation' / item['sha256'] / version / Path(item['key']).name
            reader = getattr(uploads.provider, 'download_for_validation', None)
            if reader:
                reader(key, destination, item['sha256'], item['size'], etag, lambda n: read_progress(index, n))
            else:
                uploads.provider.download(key, destination, item['sha256'], item['size'])
                read_progress(index, item['size'])
            info = None
            if index < len(manifest['files']):
                info = uploads.service.probe(destination)
                if info['codec'] != 'h264' or abs(info['duration_ms'] - item['duration_ms']) > 250:
                    raise HTTPException(422, '仅允许上传经本机压缩且时长校验通过的 H.264 视频。')
            elif index >= len(manifest['files']) + len(manifest.get('attachments', [])) and item.get('kind') == 'fast':
                duration = next(v['duration_ms'] for v in manifest['files'] if v['name'] == item['video_name'])
                info = uploads.service.probe(destination)
                if info['codec'] != 'h264' or abs(info['duration_ms'] - duration / 20) > 70 or abs(info.get('media_start_seconds', 0)) >= .001:
                    raise HTTPException(422, '上传的倍速缓存时间或格式无效。')
            elif index >= len(manifest['files']) + len(manifest.get('attachments', [])) and item.get('kind') in ('thumbnail', 'sheet'):
                if not uploads.service.valid_thumbnail(destination):
                    raise HTTPException(422, '上传的 JPG 缓存无效。')
            elif item['name'].startswith('prediction/'):
                validate_prediction(destination)
            after = uploads.provider.head(key)
            if after.get('ETag') != etag or after['ContentLength'] != item['size']:
                raise HTTPException(409, '核验期间对象版本发生变化，请重试。')
            uploads.proofs.save(key, etag, contract, destination, info)
            with uploads.job_lock(ident):
                current = uploads.load(ident, user)
                current.setdefault('verified', {})[str(index)] = etag
                if index < len(manifest['files']):
                    current.setdefault('media_infos',{})[str(index)]=info
                uploads.save(current)
        try:
            with ThreadPoolExecutor(max_workers=3) as pool:
                # Consume every result, including errors; publication never races unfinished checks.
                list(pool.map(verify, range(len(items))))
            permitted()
            if prepare:
                return uploads.public(update(state='verified', validation_error=''))
            checked=uploads.load(ident,user)
            published=copy.deepcopy(manifest)
            published['media_metadata']={item['name']:checked['media_infos'][str(i)] for i,item in enumerate(manifest['files'])}
            uploads.provider.put_if_absent(manifest['prefix'] + '/manifest.json', document_bytes(published))
            return uploads.public(update(state='ready', validation_error='',manifest=published))
        except Exception as error:
            detail = (error.detail if isinstance(error, HTTPException) and isinstance(error.detail, str)
                      else '采集目录核验未完成，可重试继续；已上传内容保留。')
            update(state='validation_error', validation_error=detail)
            raise
