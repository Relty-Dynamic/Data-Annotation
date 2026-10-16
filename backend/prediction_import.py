"""Strict automatic initialization of a blank posture draft from an IMU result.

Manual import retains its existing replace/undo semantics. This path creates
new draft intervals only and follows each video's actual project coordinates.
"""
from datetime import datetime
from bisect import bisect_right
import hashlib
from itertools import islice
import json
from pathlib import PurePosixPath
import re
import uuid
from fastapi import HTTPException

MAX_BYTES = 20 * 1024**2


def verify_inputs(raw, lookup):
    """Bind the prediction's selected IMU files to this collection's bytes."""
    try:
        inputs = json.loads(raw.decode('utf-8-sig'))['provenance']['inputs']
    except (ValueError, UnicodeError, KeyError, TypeError):
        raise ValueError('预标注缺少 IMU 输入摘要，未自动导入。') from None
    if not isinstance(inputs, list) or not 1 <= len(inputs) <= 4000:
        raise ValueError('预标注 IMU 输入清单无效。')
    seen = set()
    for item in inputs:
        if not isinstance(item, dict) or not isinstance(item.get('path'), str):
            raise ValueError('预标注 IMU 输入路径无效。')
        parts = item['path'].split('/')
        if (not 2 <= len(parts) <= 6 or parts[0].casefold() != 'imu'
                or any(not p or p.startswith('.') or p != p.strip(' .') or len(p) > 120
                       or any(ord(c) < 32 or c in '\\:<>"|?*' for c in p) for p in parts)
                or PurePosixPath(parts[-1]).suffix.lower() != '.txt'):
            raise ValueError('预标注 IMU 输入路径无效。')
        key = 'IMU/' + '/'.join(parts[1:])
        expected = item.get('sha256')
        if (key.casefold() in seen or not isinstance(expected, str) or not re.fullmatch('[0-9a-f]{64}', expected)
                or lookup(key) != expected):
            raise ValueError('预标注与本次采集的 IMU 内容摘要不一致。')
        seen.add(key.casefold())


def epoch_ms(value):
    if not isinstance(value, str) or len(value) > 64:
        raise ValueError('预标注缺少完整录制时间。')
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        raise ValueError('预标注录制时间格式无效。') from None
    if parsed.tzinfo is None:
        raise ValueError('预标注录制时间必须包含时区。')
    return round(parsed.timestamp() * 1000)


def convert(raw, project):
    if len(raw) > MAX_BYTES:
        raise ValueError('预标注超过 20 MiB，未自动导入。')
    try:
        doc = json.loads(raw.decode('utf-8-sig'))
    except (ValueError, UnicodeError):
        raise ValueError('预标注不是有效的 UTF-8 JSON。') from None
    if (not isinstance(doc, dict) or doc.get('axis') != 'posture'
            or doc.get('export_profile') != 'imu_fusion_prediction_timeline/1.2.0'):
        raise ValueError('预标注格式版本不受支持。')
    timebase, provenance = doc.get('timebase', {}), doc.get('provenance', {})
    if not isinstance(timebase, dict) or timebase.get('unit') != 'ms' or timebase.get('clock') != 'fpv_camera':
        raise ValueError('预标注必须使用已校时的相机毫秒坐标。')
    if not isinstance(provenance, dict):
        raise ValueError('预标注来源信息无效。')
    origin = epoch_ms(timebase.get('recording_start'))
    duration, rows = timebase.get('duration_ms'), doc.get('segments')
    if type(duration) is not int or duration <= 0 or not isinstance(rows, list) or not 1 <= len(rows) <= 50000:
        raise ValueError('预标注时长或片段数量无效。')
    allowed = project.get('track_labels', {}).get('posture', [])
    ordered, prior = [], -1
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError('预标注片段无效。')
        a, b, label = row.get('start_ms'), row.get('end_ms'), row.get('label')
        if (type(a) is not int or type(b) is not int or not 0 <= a < b <= duration
                or row.get('kind', 'interval') != 'interval' or type(row.get('lane', 0)) is not int or row.get('lane', 0) != 0
                or not isinstance(label, str) or label not in allowed):
            raise ValueError('预标注时间、主轴或标签与当前规则不一致。')
        ordered.append((a, b, label))
    ordered.sort()
    for a, b, _ in ordered:
        if a < prior:
            raise ValueError('预标注姿势主轴存在重叠。')
        prior = b
    inventory = provenance.get('videos')
    if not isinstance(inventory, list) or not inventory:
        raise ValueError('预标注缺少原始视频时间清单。')
    by_stem = {}
    for video in inventory:
        if not isinstance(video, dict) or not isinstance(video.get('name'), str):
            raise ValueError('预标注视频清单无效。')
        name = video['name']
        if '/' in name or '\\' in name or PurePosixPath(name).suffix.lower() != '.avi':
            raise ValueError('预标注视频逻辑名称无效。')
        key = PurePosixPath(name).stem.casefold()
        if key in by_stem or type(video.get('duration_ms')) is not int or video['duration_ms'] <= 0:
            raise ValueError('预标注视频清单重复或时长无效。')
        by_stem[key] = (epoch_ms(video.get('recording_start')), video['duration_ms'])
    result = []
    raw_hash = hashlib.sha256(raw).hexdigest()
    source_ends = [b for _, b, _ in ordered]
    for video in project['videos']:
        source = by_stem.get(PurePosixPath(video['name']).stem.casefold())
        if not source or abs(source[1] - video['duration_ms']) > 250:
            raise ValueError('预标注视频名称或时长与当前素材不一致。')
        # The source uses filename camera time. The project may shift a clip to
        # resolve its second-precision seam; that shift is applied once here.
        nominal = epoch_ms(video.get('filename_recording_start', video.get('recording_start')))
        if nominal != source[0]:
            raise ValueError('预标注视频录制时间与当前素材不一致。')
        source_start = source[0] - origin
        source_end = source_start + min(source[1], video['duration_ms'])
        for a, b, label in islice(ordered, bisect_right(source_ends, source_start), None):
            if a >= source_end:
                break
            left, right = max(a, source_start), min(b, source_end)
            if left < right:
                start, end = video['start_ms'] + left - source_start, video['start_ms'] + right - source_start
                result.append(dict(id=uuid.uuid5(uuid.NAMESPACE_URL, f'prediction:{raw_hash}:{video["id"]}:{left}:{right}').hex,
                    label=label, start_ms=start, end_ms=end, kind='interval', mode='state', lane=0))
    if not result:
        raise ValueError('预标注与实际视频没有交集。')
    if len(result) > 100000:
        raise ValueError('按视频拆分后的预标注超过平台片段上限。')
    recordings = doc.get('recordings')
    if not isinstance(recordings, list) or not recordings or any(not isinstance(r, dict) or type(r.get('needs_alignment_review')) is not bool for r in recordings):
        raise ValueError('预标注缺少校时复核状态。')
    metadata = dict(state='applied', sha256=raw_hash, algorithm_version=provenance.get('version'),
        implementation_sha256=provenance.get('implementation_sha256'), source_fingerprint=project['source_fingerprint'],
        imported_segments=len(result), needs_alignment_review=any(r['needs_alignment_review'] for r in recordings))
    return sorted(result, key=lambda r: (r['start_ms'], r['end_ms'])), metadata


def initialize(service, project):
    if not project.pop('_prediction_eligible', False):
        return
    if (any(project.get('annotations', {}).values()) or project.get('draft_dirty') or project.get('last_writeback')
            or any(project.get('_external_hashes', {}).values())):
        return
    from .service import selected_tracks
    if 'posture' not in selected_tracks(project):
        return
    try:
        raw = service.read_prediction(project)
        if raw is None:
            return
        rows, metadata = convert(raw, project)
        project['annotations']['posture'] = rows
        project['prediction_import'] = metadata
        project['draft_dirty'] = True
    except (ValueError, OSError, TypeError, KeyError, HTTPException) as error:
        project['prediction_import'] = dict(state='rejected', detail=str(error) if isinstance(error, ValueError) else '预标注文件无法读取，未自动导入。')


def warning(metadata):
    if not metadata:
        return None
    if metadata.get('state') == 'rejected':
        return metadata['detail']
    if metadata.get('needs_alignment_review'):
        return '姿势预标注的相机校时尚需人工复核，请结合视频检查。'
    return None
