"""Desktop orchestration; model dependencies and raw AVI stay separate.

The worker supplies its own small standard-library sampler. This module only
transports IMU, selected motion curves and the verified prediction artifact.
"""
from __future__ import annotations

import hashlib
import importlib
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import zipfile

from fastapi import HTTPException
from .mock_s3 import MockUploadManager, file_sha256
from .r2_folder import scan_collection


def sampler(target, directory, capabilities):
    expected = capabilities.get('client_sha256', '')
    if (capabilities.get('protocol') != 1 or len(expected) != 64
            or any(c not in '0123456789abcdef' for c in expected)):
        raise HTTPException(409, '算法接口版本不兼容，请更新上传组件。')
    payload = target.response('GET', '/api/prediction/client.zip').content
    if len(payload) > 1024**2 or hashlib.sha256(payload).hexdigest() != expected or len(payload) != capabilities['client_size']:
        raise HTTPException(502, '算法本机提取工具校验失败。')
    package = directory / ('client-' + expected)
    package.mkdir(parents=True, exist_ok=True)
    allowed = {'imu_prediction_client/' + name for name in ('__init__.py', 'avi_index.py', 'video_signal.py')}
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        if set(archive.namelist()) != allowed or len(archive.infolist()) != 3:
            raise HTTPException(502, '算法提取工具安装包结构无效。')
        for item in archive.infolist():
            if item.file_size > 1024**2 or item.external_attr >> 16 & 0o170000 == 0o120000:
                raise HTTPException(502, '算法提取工具安装包无效。')
            path = package / Path(item.filename).name
            if path.is_symlink():
                raise HTTPException(409, '算法工具缓存不能包含链接。')
            path.write_bytes(archive.read(item))
    name = 'datamark_prediction_' + expected
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, package / '__init__.py', submodule_search_locations=[str(package)])
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return importlib.import_module(name + '.video_signal')


def _inventory(selection):
    return (selection['videos'], selection['attachments'])


def _publish(path, raw):
    path.parent.mkdir(exist_ok=True)
    if path.parent.is_symlink() or path.is_symlink():
        raise HTTPException(409, 'prediction 不能是链接。')
    if path.exists():
        if path.read_bytes() != raw:
            raise HTTPException(409, '已有姿势预测与本次素材或算法不一致，请明确归档后重试。')
        return
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix='.posture-', delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, path)
    finally:
        if temporary:
            temporary.unlink(missing_ok=True)


def prepare_prediction(target, service, root, selection, roles, *, progress=lambda detail: None,
                       wait=time.sleep, clock=time.monotonic):
    if not all(isinstance(roles.get(k), str) and roles[k].strip() for k in ('chest', 'thigh')) or roles['chest'] == roles['thigh']:
        raise HTTPException(422, '请明确填写不同的胸部和大腿 IMU 设备编号。')
    root = Path(root).absolute()
    current = scan_collection(root)
    if _inventory(current) != _inventory(selection):
        raise HTTPException(409, '采集数据在选择后发生变化，请重新选择。')
    capabilities = target.request('GET', '/api/prediction/capabilities')
    if not capabilities.get('enabled'):
        raise HTTPException(503, '姿势预标注服务尚未启用，可取消预标注后仅上传素材。')
    module = sampler(target, service.local / 'prediction-tools', capabilities)
    inputs = [r for r in current['attachments'] if r['output'].startswith('IMU/') and Path(r['name']).suffix.lower() == '.txt']
    if not inputs:
        raise HTTPException(422, '生成预标注需要完整采集目录中的 WT901 IMU TXT；请勿只选择 FPV 文件夹。')
    videos, records, by_name = [], [], {}
    for index, record in enumerate([*current['videos'], *inputs]):
        progress(f'正在核验预标注素材 {index + 1}/{len(current["videos"]) + len(inputs)}：{record["name"]}')
        path = MockUploadManager._check_local_source(record)
        digest = file_sha256(path)
        MockUploadManager._check_local_source(record)
        if record in current['videos']:
            video = module.probe(path)
            videos.append({k: video[k] for k in ('name', 'start', 'duration')} | dict(size=record['size'], sha256=digest))
            by_name[path.name] = video
        else:
            records.append(dict(path=record['output'], size=record['size'], sha256=digest))
    manifest = dict(protocol=1, collection=current['name'], subject=current['name'], roles=roles, inputs=records, videos=videos)
    job = target.request('POST', '/api/prediction/jobs', json=manifest)
    if job['implementation_sha256'] != capabilities['implementation_sha256']:
        raise HTTPException(409, '算法版本在准备期间发生变化，请重新开始以使用一致版本。')
    prefix = '/api/prediction/jobs/' + job['id']
    existing = [r for r in current['attachments'] if r['output'].casefold() == 'prediction/posture.json']
    if existing and job['state'] != 'completed':
        raise HTTPException(409, '采集目录已有姿势预测，请保留上传原预测，或明确归档后重新生成。')
    for index in job['missing_inputs']:
        source = MockUploadManager._check_local_source(inputs[index])
        progress(f'正在提交 IMU {index + 1}/{len(inputs)}')
        with source.open('rb') as stream:
            target.request('PUT', prefix + f'/inputs/{index}', content=stream, headers={'Content-Length': str(inputs[index]['size'])}, timeout=180)
        MockUploadManager._check_local_source(inputs[index])
    if job['state'] in ('collecting_inputs', 'failed'):
        job = target.request('POST', prefix + '/start')
    deadline = clock() + 7200
    while job['state'] != 'completed':
        if clock() >= deadline:
            raise HTTPException(504, '预标注仍在处理，可稍后重试继续同一任务。')
        state = job['state']
        if state == 'failed':
            raise HTTPException(422, job['error'])
        if state == 'awaiting_motion':
            curves = []
            # The same video window may support multiple IMU sessions.
            cache = {}
            for index, window in enumerate(job['windows']):
                progress(f'正在从本机原视频提取校时信号 {index + 1}/{len(job["windows"])}')
                video = by_name.get(window['video'])
                if video is None or window['span_s'] != 90 or window['offset_s'] not in (30, 210, 390):
                    raise HTTPException(502, '算法返回了无效校时窗口。')
                key = (window['video'], window['offset_s'])
                if key not in cache:
                    cache[key] = module.motion_signal(video, window['offset_s'], ffmpeg=service.tool('ffmpeg'))
                curves.append(dict(id=window['id'], signal=cache[key]))
            job = target.request('PUT', prefix + '/motion', json={'windows': curves})
        else:
            progress('正在生成姿势预标注，请稍候；原视频保留在本机。')
            wait(1)
            job = target.request('GET', prefix)
    raw = target.response('GET', prefix + '/prediction').content
    result = job['result']
    if len(raw) > 20 * 1024**2 or len(raw) != result['size_bytes'] or hashlib.sha256(raw).hexdigest() != result['sha256']:
        raise HTTPException(502, '预标注下载校验失败，可重试同一任务。')
    latest = scan_collection(root)
    if _inventory(latest) != _inventory(current):
        raise HTTPException(409, '预标注期间素材发生变化，未发布预测，请重新选择。')
    destination = Path(existing[0]['source']) if existing else root / 'prediction/posture.json'
    # Honor an existing case-insensitive prediction directory on all platforms.
    if not existing:
        parents = [p for p in root.iterdir() if p.name.casefold() == 'prediction']
        if len(parents) > 1:
            raise HTTPException(409, '采集目录包含多个 prediction 文件夹。')
        if parents:
            destination = parents[0] / 'posture.json'
    _publish(destination, raw)
    rescanned = scan_collection(root)
    before = {r['output']: r for r in current['attachments']}
    after = {r['output']: r for r in rescanned['attachments'] if r['output'].casefold() != 'prediction/posture.json'}
    before = {k: v for k, v in before.items() if k.casefold() != 'prediction/posture.json'}
    if rescanned['videos'] != current['videos'] or before != after:
        raise HTTPException(409, '素材清单发生变化，上传已停止。')
    target.attachments = rescanned['attachments']
    progress('预标注已生成，将与压缩视频一并上传。' + ('校时结果需要人工复核。' if result['needs_alignment_review'] else ''))
    return rescanned, result
