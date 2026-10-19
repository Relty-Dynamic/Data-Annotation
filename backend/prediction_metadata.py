"""Validate the public prediction contract without dropping partial-result warnings."""
import hashlib
import re
from pathlib import PurePosixPath

PROFILES = {'imu_fusion_prediction_timeline/1.2.0', 'imu_fusion_prediction_timeline/1.3.0'}


def imu_path(value):
    if not isinstance(value, str):
        raise ValueError('预标注 IMU 输入路径无效。')
    parts = value.split('/')
    if (not 2 <= len(parts) <= 6 or parts[0].casefold() != 'imu'
            or any(not p or p.startswith('.') or p != p.strip(' .') or len(p) > 120
                   or any(ord(c) < 32 or c in '\\:<>"|?*' for c in p) for p in parts)
            or PurePosixPath(parts[-1]).suffix.lower() != '.txt'):
        raise ValueError('预标注 IMU 输入路径无效。')
    return value


def metadata(doc):
    if (not isinstance(doc, dict) or doc.get('axis') != 'posture'
            or not isinstance(doc.get('export_profile'), str) or doc['export_profile'] not in PROFILES):
        raise ValueError('预标注格式版本不受支持。')
    recordings = doc.get('recordings')
    if (not isinstance(recordings, list) or not 1 <= len(recordings) <= 4000
            or any(not isinstance(r, dict) or type(r.get('needs_alignment_review')) is not bool for r in recordings)):
        raise ValueError('预标注缺少校时复核状态。')
    result = dict(export_profile=doc['export_profile'], needs_alignment_review=any(r['needs_alignment_review'] for r in recordings))
    if doc['export_profile'].endswith('/1.2.0'):
        if (isinstance(doc.get('provenance'), dict) and 'upload_batch' in doc['provenance']
                or any(r.get('alignment_status') == 'same_day_inherited' for r in recordings)):
            raise ValueError('部分批次或继承校时的预标注须使用 1.3.0 格式。')
        return result
    provenance = doc.get('provenance')
    batch = provenance.get('upload_batch') if isinstance(provenance, dict) else None
    def fail():
        raise ValueError('预标注批次状态、来源或未完成记录清单不一致。')
    if not isinstance(batch, dict) or batch.get('batch_status') not in ('complete', 'partial'):
        fail()
    inputs, excluded = provenance.get('inputs'), batch.get('excluded_records')
    if (not isinstance(inputs, list) or not 1 <= len(inputs) <= 4000
            or not isinstance(excluded, list) or len(excluded) > 4000):
        fail()
    source_ids, source_paths = set(), set()
    for source in inputs:
        if not isinstance(source, dict):
            fail()
        path = imu_path(source.get('path'))
        ident = hashlib.sha256(path.encode()).hexdigest()[:16]
        if source.get('record_id') != ident or ident in source_ids or path.casefold() in source_paths:
            fail()
        source_ids.add(ident)
        source_paths.add(path.casefold())
    excluded_rows, excluded_ids = [], set()
    for row in excluded:
        if not isinstance(row, dict):
            fail()
        path = imu_path(row.get('path'))
        ident = hashlib.sha256(path.encode()).hexdigest()[:16]
        reason = row.get('reason')
        if (row.get('record_id') != ident or ident in excluded_ids or ident in source_ids
                or path.casefold() in source_paths or not isinstance(reason, str) or not re.fullmatch('[a-z_]{1,80}', reason)):
            fail()
        excluded_ids.add(ident)
        source_paths.add(path.casefold())
        excluded_rows.append(dict(record_id=ident, path=path, reason=reason))
    listed = batch.get('excluded_record_ids')
    recording_ids = [r.get('id') for r in recordings]
    if (any(not isinstance(i, str) for i in recording_ids) or len(set(recording_ids)) != len(recording_ids)
            or set(recording_ids) != source_ids or not isinstance(listed, list)
            or any(not isinstance(i, str) for i in listed) or len(listed) != len(excluded_ids) or set(listed) != excluded_ids
            or type(batch.get('validated_records')) is not int or batch['validated_records'] != len(recordings)
            or type(batch.get('total_records')) is not int or batch['total_records'] != len(source_ids) + len(excluded_ids)
            or batch['total_records'] > 4000 or (batch['batch_status'] == 'partial') != bool(excluded)):
        fail()
    inherited = []
    for recording in recordings:
        if recording.get('alignment_status') == 'same_day_inherited':
            donor = recording.get('alignment_source_record_id')
            if (not isinstance(donor, str) or donor not in source_ids | excluded_ids or donor == recording['id']
                    or not recording['needs_alignment_review'] or not isinstance(batch.get('clock_group'), str)
                    or not batch['clock_group'].strip()):
                fail()
            inherited.append(recording['id'])
    result.update(batch_status=batch['batch_status'], validated_records=len(recordings),
        total_records=batch['total_records'], excluded_record_ids=sorted(excluded_ids),
        excluded_records=excluded_rows, inherited_record_ids=inherited,
        needs_alignment_review=result['needs_alignment_review'] or bool(excluded) or bool(inherited))
    return result
