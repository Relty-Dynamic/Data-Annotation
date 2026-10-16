"""Shared editor-compatible task checks; empty time and different lanes are valid."""
from ..service import annotation_label_base


def validate_annotations(annotations, rule, duration_ms, draft=False, recording_runs=None):
    errors = []
    axes = {axis['id']: axis for axis in rule['axes']}
    if not isinstance(annotations, dict):
        return [{'reason': '标注必须按时间轴组织'}]
    count = 0
    for key, segments in annotations.items():
        axis = axes.get(key)
        if axis is None or not isinstance(segments, list):
            errors.append({'axis': key, 'reason': '未知时间轴或片段格式不正确'})
            continue
        count += len(segments)
        if count > 100000:
            errors.append({'axis': key, 'reason': '标注片段超过十万条'})
            break
        seen = set()
        occupied = {}
        for item in segments:
            if not isinstance(item, dict):
                errors.append({'axis': key, 'reason': '片段格式不正确'})
                continue
            def fail(reason):
                errors.append({'axis': key, 'segment_id': item.get('id'), 'reason': reason})
            sid = item.get('id')
            if not isinstance(sid, str) or not sid or len(sid) > 100 or sid in seen:
                fail('片段编号必须唯一且不超过一百字')
            seen.add(str(sid))
            labels = [label.get('label', label.get('name')) if isinstance(label, dict) else label for label in axis['labels']]
            label = item.get('label')
            base = annotation_label_base(label, labels) if isinstance(label, str) and len(label) <= 500 else None
            if base is None:
                fail('标签不在任务规则中')
            kind = item.get('kind', 'interval')
            if kind not in ('interval', 'point', 'instant'):
                fail('片段类型不正确')
            point = kind in ('point', 'instant')
            if point and axis['mode'] == 'state':
                fail('状态轴不支持瞬时事件')
            start, end = item.get('start_ms'), item.get('end_ms')
            start_valid = type(start) is int and 0 <= start <= duration_ms
            open_event = draft and not point and end is None
            end_valid = type(end) is int and start_valid and start <= end <= duration_ms
            if not start_valid or not (end_valid or open_event):
                fail('时间必须是有效整数毫秒，且事件须结束')
            elif point and start != end:
                fail('瞬时事件的起止时间必须相同')
            elif not point and end_valid and end <= start:
                fail('持续片段结束时间必须晚于开始时间')
            elif not draft and not point and end_valid and end - start < axis.get('minimum_ms', 0):
                fail('片段短于规则要求')
            if recording_runs is not None and start_valid and (end_valid or open_event):
                limit = start if point or open_event else end
                if not any(run['start_ms'] <= start < run['end_ms'] and limit <= run['end_ms'] for run in recording_runs):
                    fail('标注不能位于或跨越没有视频的真实断档')
            lane = item.get('lane', 0)
            if type(lane) is not int or not 0 <= lane <= 9007199254740991:
                fail('子轴编号不正确')
            elif start_valid and (end_valid or open_event):
                occupied.setdefault(lane, []).append((start, start + 1 if point else duration_ms if open_event else end, sid))
            if not draft and base in axis.get('transport_labels', []) and item.get('transport_type') not in axis.get('transport_types', []):
                fail('请选择有效交通工具类型')
            if 'needs_review' in item and type(item['needs_review']) is not bool:
                fail('待复核标记不正确')
            if not draft and item.get('needs_review') and not str(item.get('review_reason', '')).strip():
                fail('请填写待复核原因')
            for text_key in ('transport_type', 'review_reason', 'note', 'notes'):
                if text_key in item and (not isinstance(item[text_key], str) or len(item[text_key]) > 500):
                    fail('文字字段必须为不超过五百字的文本')
        for spans in occupied.values():
            spans.sort(key=lambda span: span[:2])
            prior_end = -1
            for start, end, sid in spans:
                if start < prior_end:
                    errors.append({'axis': key, 'segment_id': sid, 'reason': '同一子轴内不能重叠，请选择其他子轴'})
                prior_end = max(prior_end, end)
    return errors
