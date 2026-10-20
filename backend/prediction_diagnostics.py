"""Bounded, public prediction diagnostics; never display worker paths or exceptions."""
import math
import re
import json


def upload_report(value):
    """Bound UI-only lists; the verified artifact and stored metadata stay complete."""
    limit = 64 * 1024
    if len(json.dumps(value)) <= limit:
        return value
    keys = ('mounting_records', 'excluded_records', 'inherited_record_ids',
            'needs_mounting_review_record_ids', 'excluded_record_ids')
    result = {key: item for key, item in value.items() if key not in keys}
    result['details_truncated'] = True
    for key in keys:
        if key in value:
            result[key] = []
            result[key + '_total'] = len(value[key])
    for key in keys:
        for item in value.get(key, []):
            result[key].append(item)
            if len(json.dumps(result)) > limit:
                result[key].pop()
                break
    return result


def diagnostics(value):
    if not isinstance(value, list) or len(value) > 3:
        raise ValueError('预标注阶段诊断无效。')
    result, stages = [], set()
    for item in value:
        if not isinstance(item, dict):
            raise ValueError('预标注阶段诊断无效。')
        code, stage = item.get('code'), item.get('stage')
        if (not isinstance(code, str) or not re.fullmatch('[A-Z_]{1,80}', code)
                or stage not in ('chest', 'leg', 'fusion', 'leg.mounting') or stage in stages):
            raise ValueError('预标注阶段诊断无效。')
        stages.add(stage)
        clean = dict(code=code, stage=stage)
        if code == 'LEG_MOUNTING_EVIDENCE_INSUFFICIENT':
            if stage != 'leg.mounting':
                raise ValueError('预标注安装方向诊断无效。')
            for key in ('near_vertical_samples', 'required_near_vertical_samples',
                        'qualified_near_vertical_samples', 'support_bins'):
                number = item.get(key)
                if type(number) is not int or not 0 <= number <= 10**10:
                    raise ValueError('预标注安装方向诊断计数无效。')
                clean[key] = number
            for key, maximum in (('qualified_covered_seconds', 10**9), ('sign_agreement', 1)):
                number = item.get(key)
                if type(number) not in (int, float) or not math.isfinite(number) or not 0 <= number <= maximum:
                    raise ValueError('预标注安装方向诊断时长或一致性无效。')
                clean[key] = number
            reason = item.get('rejection_reason')
            if (reason not in ('insufficient_time_coverage', 'conflicting_direction_evidence')
                    or clean['qualified_near_vertical_samples'] > clean['near_vertical_samples']):
                raise ValueError('预标注安装方向拒绝原因无效。')
            clean['rejection_reason'] = reason
        result.append(clean)
    return result


def failure_reason(reason, details=()):
    for item in details:
        if item['code'] == 'LEG_MOUNTING_EVIDENCE_INSUFFICIENT':
            cause = ('有效近竖直覆盖时长不足' if item['rejection_reason'] == 'insufficient_time_coverage'
                     else '安装方向证据冲突')
            return (f'腿部安装方向未确认：{cause}；近竖直样本 '
                    f'{item["near_vertical_samples"]}/{item["required_near_vertical_samples"]}，'
                    f'有效覆盖 {item["qualified_covered_seconds"]:.2f} 秒，'
                    f'支持窗口 {item["support_bins"]} 个，方向一致性 {item["sign_agreement"]:.1%}')
    stages = {'chest': '胸部推理', 'leg': '腿部推理', 'fusion': '胸腿融合', 'leg.mounting': '腿部安装方向判断'}
    if details:
        return '、'.join(stages[item['stage']] for item in details) + '未完成'
    return {'inference_failed': 'IMU 推理未完成', 'invalid_prepared_prediction': '推理结果无法读取',
            'alignment_unresolved': '未获得可靠校时', 'failed': '校时或导出失败',
            'no_video_overlap': '校时后与视频无交集'}.get(reason, '预标注未完成')


def mounting_metadata(recordings, batch, sources):
    """Optional for old producers; new per-record flags must match the batch."""
    fields = ('needs_mounting_review', 'leg_mounting_method', 'leg_mounting_version')
    rows = []
    for record in recordings:
        if not any(key in record for key in fields):
            continue
        review, method, version = (record.get(key) for key in fields)
        if (type(review) is not bool or method not in ('legacy_sample_count', 'short_duration_consistency')
                or version != '1.0.0' or review != (method == 'short_duration_consistency')):
            raise ValueError('预标注安装方向复核信息无效或版本不受支持。')
        rows.append(dict(record_id=record['id'], path=sources[record['id']],
                         needs_mounting_review=review, method=method, version=version))
    expected = [row['record_id'] for row in rows if row['needs_mounting_review']]
    key = 'needs_mounting_review_record_ids'
    if rows or key in batch:
        listed = batch.get(key)
        if (not isinstance(listed, list) or any(not isinstance(i, str) for i in listed)
                or len(set(listed)) != len(listed) or set(listed) != set(expected)):
            raise ValueError('预标注安装方向复核清单与记录不一致。')
    return dict(needs_mounting_review=bool(expected),
                needs_mounting_review_record_ids=expected, mounting_records=rows)
