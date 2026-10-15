"""Compare effective labels and interval boundaries, independent of lane IDs."""
def differences(left, right, tolerance_ms=0):
    result = []
    for axis in sorted(set(left) | set(right)):
        a, b = left.get(axis, []), right.get(axis, [])
        def label(item):
            return (item['label'], item.get('transport_type', ''), item.get('note', item.get('notes', '')))
        points = sorted({s[p] for s in a + b for p in ('start_ms', 'end_ms')})
        # Tolerance clusters are anchored to their first boundary, avoiding drift.
        canonical = {}
        anchor = None
        for point in points:
            if anchor is None or point - anchor > tolerance_ms:
                anchor = point
            canonical[point] = anchor
        bounds = sorted(set(canonical.values()))
        def active(items, start, end):
            return {label(s) for s in items if s.get('kind') not in ('instant', 'point') and canonical[s['start_ms']] < end and canonical[s['end_ms']] > start}
        ranges = []
        for start, end in zip(bounds, bounds[1:]):
            if active(a, start, end) != active(b, start, end):
                if ranges and ranges[-1][1] == start:
                    ranges[-1][1] = end
                else:
                    ranges.append([start, end])
        def instants(items):
            return {(canonical[s['start_ms']], label(s)) for s in items if s.get('kind') in ('instant', 'point')}
        for point, _ in instants(a) ^ instants(b):
            if [point, point] not in ranges:
                ranges.append([point, point])
        for start, end in sorted(ranges):
            result.append({'id': f'difference:{axis}:{start}:{end}', 'axis': axis, 'start_ms': start, 'end_ms': end, 'reason': '双方结果不一致'})
    return result
