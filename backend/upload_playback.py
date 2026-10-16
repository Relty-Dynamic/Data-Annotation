"""Uploader-built playback derivatives shared by collection and task sessions."""
import hashlib
import re
import threading
from pathlib import Path

from fastapi import HTTPException

from .desktop_cache import sha256_file, safe_name, collection_name
from .local_playback import render_local_fast, render_local_thumbnail
from .session_cache import PROFILE, SessionCache
from .storyboards import StoryboardCache


def build(service, prepared, manifest, progress):
    records = []
    retained = {}
    stop = threading.Event()
    stories = StoryboardCache(prepared / '.upload-storyboards')
    for number, video in enumerate(manifest['files'], 1):
        progress(f"正在本机生成完整缓存 {number}/{len(manifest['files'])}：{Path(video['key']).name}")
        normal = prepared / video['key']
        name = normal.name
        duration = video['duration_ms']
        digest = sha256_file(normal)
        identity = hashlib.sha256((name + ':' + digest).encode()).hexdigest()
        folder = prepared / 'FPV' / 'cache' / identity
        folder.mkdir(parents=True, exist_ok=True)
        fast, thumbnail = folder / 'fast.mp4', folder / 'thumbnail.jpg'
        render_local_fast(service, normal, fast, duration, lambda value: None, stop)
        render_local_thumbnail(service, normal, thumbnail, duration, stop)
        story = stories.render(normal, digest, duration, service.tool('ffmpeg'), lambda value: None, stop)
        assets = [('fast', None, fast), ('thumbnail', None, thumbnail)]
        for index, sheet in enumerate(story['sheets']):
            target = folder / f'sheet-{index:05d}.jpg'
            SessionCache._copy_asset(stories.directory(digest) / sheet, target, stop)
            assets.append(('sheet', index, target))
        for kind, index, path in assets:
            records.append({'key': path.relative_to(prepared).as_posix(), 'video_name': name,
                            'kind': kind, 'index': index, 'size': path.stat().st_size,
                            'sha256': sha256_file(path)})
            if service.desktop_cache.enabled:
                label = f'storyboard-{index:03d}' if kind == 'sheet' else kind
                retained[path.relative_to(prepared).as_posix()] = service.desktop_cache.retain_upload(manifest['prefix'], name, path, kind=label)
    manifest.update(cache_profile=PROFILE, cache_files=records)
    return retained


def validate(manifest):
    """Validate complete, bounded associations before accepting any cache objects."""
    records = manifest.get('cache_files', [])
    if not records and 'cache_profile' not in manifest:
        return []  # Existing upload manifests remain readable.
    if manifest.get('cache_profile') != PROFILE or not isinstance(records, list) or len(records) > 50000:
        raise HTTPException(422, '播放缓存清单格式或数量无效。')
    videos = {item.get('name', Path(item['key']).name): item for item in manifest['files']}
    seen, keys = set(), set()
    for item in records:
        if not isinstance(item, dict):
            raise HTTPException(422, '播放缓存清单无效。')
        kind, index, name = item.get('kind'), item.get('index'), item.get('video_name')
        key = item.get('key')
        if (name not in videos or kind not in ('fast', 'thumbnail', 'sheet')
                or (kind == 'sheet' and (type(index) is not int or index < 0))
                or (kind != 'sheet' and index is not None)
                or not isinstance(key, str)
                or not re.fullmatch(r'FPV/cache/[a-f0-9]{64}/[a-zA-Z0-9_.-]+\.(mp4|jpg)', key)
                or not key.endswith('.mp4' if kind == 'fast' else '.jpg')
                or not re.fullmatch(r'[a-f0-9]{64}', str(item.get('sha256', '')))
                or type(item.get('size')) is not int or not 0 < item['size'] <= 16 * 1024**3
                or (name, kind, index) in seen or key in keys):
            raise HTTPException(422, '播放缓存关联、路径或文件校验信息无效。')
        seen.add((name, kind, index))
        keys.add(key)
    expected = {(name, kind, None) for name in videos for kind in ('fast', 'thumbnail')}
    expected.update((name, 'sheet', index) for name, video in videos.items()
                    for index in range(((video['duration_ms'] + 999) // 1000 + 99) // 100))
    if seen != expected:
        raise HTTPException(422, '播放缓存不完整，请更新本机上传组件并重新生成。')
    return records


def hydrate(provider, source, directory, key):
    capture = source['r2_playback']
    manifest = capture['manifest']
    records = validate(manifest)
    if not records:
        raise HTTPException(409, '旧采集目录缺少上传者生成的完整缓存，请使用新版本机组件上传到新的采集目录；旧数据保留，服务器不生成替代缓存。')
    name = capture['video_name']
    video = next(item for item in manifest['files'] if item['name'] == name)
    own = [item for item in records if item['video_name'] == name]
    entry = {'profile': PROFILE, 'key': key, 'duration_ms': source['duration_ms'],
             'storyboard': SessionCache._storyboard_metadata(source['duration_ms']), 'sheets': []}
    for item in [dict(video, kind='normal', index=None), *own]:
        kind, index = item['kind'], item['index']
        label = f'sheet-{index:05d}' if kind == 'sheet' else kind
        path = directory / (key + '.' + label + ('.mp4' if kind in ('normal', 'fast') else '.jpg'))
        provider.download(capture['prefix'] + '/' + item['key'], path, item['sha256'], item['size'])
        record = {**SessionCache._file_record(path, directory), 'sha256': item['sha256']}
        if SessionCache._valid_record(directory, record, jpeg=kind not in ('normal', 'fast')) is None:
            raise HTTPException(422, '上传的播放缓存文件格式校验失败。')
        if kind == 'sheet':
            entry['sheets'].append((index, record))
        else:
            entry[kind] = record
    entry['sheets'] = [record for index, record in sorted(entry['sheets'])]
    SessionCache._write_json(directory / (key + '.assets.json'), entry)
    return entry


def reference(prefix, manifest, name):
    """Keep only this clip's records in task/source metadata, never N full manifests."""
    return {'prefix': prefix, 'video_name': name, 'manifest': {
        **({'cache_profile': manifest['cache_profile']} if 'cache_profile' in manifest else {}),
        'files': [dict(item, name=name) for item in manifest['files'] if item.get('name', Path(item['key']).name) == name],
        'cache_files': [item for item in manifest.get('cache_files', []) if item['video_name'] == name]}}


def direct_manifest(provider, project):
    """Describe published upload objects; the browser verifies the actual bytes.

    Publication already validates the complete upload. Task actors must not each
    download another server copy just to obtain the same immutable cache list.
    Legacy captures without an upload bundle keep their existing preparation.
    """
    captures = [project['_sources'][video['id']].get('r2_playback') for video in project['videos']]
    if any(not capture or 'cache_profile' not in capture['manifest'] for capture in captures):
        return None
    videos, assets, identities = [], [], []
    for video, capture in zip(project['videos'], captures):
        manifest = capture['manifest']
        records = validate(manifest)
        normal = next((item for item in manifest['files'] if item['name'] == capture['video_name']), None)
        if normal is None or normal['duration_ms'] != video['duration_ms']:
            raise HTTPException(409, '任务视频与上传缓存清单不一致。')
        own = [dict(normal, kind='normal', index=None),
               *[item for item in records if item['video_name'] == capture['video_name']]]
        urls, sheets = {}, []
        for item in own:
            kind, index = item['kind'], item['index']
            label = f'storyboard-{index:03d}' if kind == 'sheet' else kind
            key = capture['prefix'] + '/' + item['key']
            url = provider.signed_url(key)
            filename = (safe_name(Path(video['name']).stem) + '.' + item['sha256'] + '.' + label
                        + ('.mp4' if kind in ('normal', 'fast') else '.jpg'))
            assets.append({'file_name': filename, 'sha256': item['sha256'], 'size': item['size'],
                           'url': url, 'media_type': 'video/mp4' if kind in ('normal', 'fast') else 'image/jpeg'})
            identities.append((video['id'], kind, index, key, item['size'], item['sha256']))
            if kind == 'sheet':
                sheets.append((index, url))
            else:
                urls[kind] = url
        videos.append({'id': video['id'], 'url': urls['normal'], 'fast_url': urls['fast'],
                       'thumbnail_url': urls['thumbnail'], 'storyboard': {
                           **SessionCache._storyboard_metadata(video['duration_ms']),
                           'state': 'ready', 'detail': None, 'sheets': [url for _, url in sorted(sheets)]}})
    from .service import digest, document_bytes
    return {'project_id': project['id'], 'version': digest(document_bytes([PROFILE, identities]))[:24],
            'profile': PROFILE, 'collection_name': collection_name(project), 'videos': videos,
            'assets': assets, 'cache_location': {'mode': 'browser', 'root': None}}
