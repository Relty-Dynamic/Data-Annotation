import test from 'node:test';
import assert from 'node:assert/strict';
import {
  annotationsEqual, applyAnnotationPoint, applyAnnotationRange, arrangeLanes,
  assignSegmentLanes, deleteAnnotation, normalizeStateSeams, switchCoveredState,
} from '../src/domain.ts';

const run = [{start_ms: 0, end_ms: 10000}];
const item = (id, start_ms, end_ms, lane, extra = {}) => ({
  id, label: '同一标签', start_ms, end_ms, ...(lane === undefined ? {} : {lane}), ...extra,
});
const ranges = rows => rows.map(({start_ms, end_ms, lane}) => [start_ms, end_ms, lane]);
const addChild = (rows, start, end, spans = run, track = 'posture') =>
  applyAnnotationRange(rows, start, end, '同一标签', spans, track, undefined, false, true);

test('overlap is supported for every built-in and custom axis, including repeated labels', () => {
  for (const track of ['scene', 'posture', 'category', 'habit', 'custom_state', 'custom_event']) {
    const result = addChild([item('main', 0, 10000, 0)], 1000, 8000, run, track);
    assert.deepEqual(ranges(result), [[0, 10000, 0], [1000, 8000, 1]], track);
    assert.equal(result[0].id, 'main');
  }
});

test('child mode prioritizes a free main row, then the lowest reusable child row', () => {
  const original = [item('main', 0, 4000, 0), item('child', 0, 2000, 1), item('higher', 8000, 10000, 2)]
    .map(row => ({...row, label: '已有标签'}));
  assert.equal(addChild(original, 5000, 7000).find(row => !original.some(old => old.id === row.id)).lane, 0);
  assert.equal(addChild(original, 2000, 3000).find(row => !original.some(old => old.id === row.id)).lane, 1);
  assert.equal(addChild(original, 1000, 3000).find(row => !original.some(old => old.id === row.id)).lane, 2);
  assert.deepEqual(ranges(original), [[0, 4000, 0], [0, 2000, 1], [8000, 10000, 2]]);
});

test('any partial overlap moves the whole selection and every recorded piece to one free row', () => {
  const spans = [{start_ms: 0, end_ms: 4000}, {start_ms: 6000, end_ms: 10000}];
  const original = [item('main', 0, 1000, 0), item('child', 7000, 9000, 1)];
  const result = addChild(original, 500, 8000, spans);
  const added = result.filter(row => !['main', 'child'].includes(row.id));
  assert.deepEqual(ranges(added), [[500, 4000, 2], [6000, 8000, 2]]);
  assert.equal(new Set(added.map(row => row.id)).size, 2);
  assert.deepEqual(result.find(row => row.id === 'main'), original[0]);
  assert.deepEqual(result.find(row => row.id === 'child'), original[1]);
});

test('ranges wholly inside a recording gap do nothing and boundary touching reuses the same row', () => {
  const spans = [{start_ms: 0, end_ms: 4000}, {start_ms: 6000, end_ms: 10000}];
  const original = [item('main', 0, 3000, 0), item('child', 6000, 9000, 1)]
    .map(row => ({...row, label: '已有标签'}));
  assert.deepEqual(addChild(original, 4500, 5500, spans), original);
  const added = addChild(original, 3000, 7000, spans).filter(row => !['main', 'child'].includes(row.id));
  assert.deepEqual(ranges(added), [[3000, 4000, 0], [6000, 7000, 0]]);
});

test('child allocation has no fixed maximum and reuses holes left by deletion', () => {
  let rows = [];
  for (let index = 0; index < 45; index++) rows = addChild(rows, 1000, 8000);
  assert.equal(rows.length, 45);
  assert.deepEqual([...new Set(rows.map(row => row.lane))].sort((a, b) => a - b), Array.from({length: 45}, (_, i) => i));
  const deleted = rows.find(row => row.lane === 12);
  rows = deleteAnnotation(rows, deleted.id, run, 'posture');
  assert.equal(rows.find(row => row.lane === 44)?.lane, 44, 'deleting an earlier row does not renumber survivors');
  const reused = addChild(rows, 3000, 5000).find(row => !rows.some(old => old.id === row.id));
  assert.equal(reused.lane, 12);
});

test('unchecked range replacement changes only the main row on every axis', () => {
  for (const track of ['scene', 'posture', 'category', 'habit', 'custom_state', 'custom_event']) {
    const main = item('main', 0, 10000, 0, {label: '原主轴'});
    const child = item('child', 1000, 9000, 1, {label: '子轴', created_by: 'user-a'});
    const result = applyAnnotationRange([main, child], 3000, 7000, '新主轴', run, track);
    assert.deepEqual(result.find(row => row.id === 'child'), child, track);
    assert.deepEqual(ranges(result.filter(row => row.lane === 0)), [[0, 3000, 0], [3000, 7000, 0], [7000, 10000, 0]]);
  }
});

test('ordinary state switching uses main boundaries and preserves child intervals', () => {
  const child = item('child', 2000, 8000, 1, {label: '子轴'});
  const next = item('later', 7000, 10000, 0, {label: '后段'});
  const result = switchCoveredState([item('main', 0, 7000, 0), child, next], 4000, '新状态', 10000, run);
  assert.deepEqual(result.find(row => row.id === 'child'), child);
  assert.deepEqual(ranges(result.filter(row => row.lane === 0)), [[0, 4000, 0], [4000, 7000, 0], [7000, 10000, 0]]);
  const initialized = switchCoveredState([child], 5000, '新状态', 10000, run);
  assert.deepEqual(initialized.find(row => row.id === 'child'), child);
  assert.deepEqual(ranges(initialized.filter(row => row.lane === 0)), [[0, 10000, 0]]);
});

test('editing preserves row and attribution, rejecting collisions without changing inputs', () => {
  const edited = item('edit', 1000, 2000, 1, {created_by: 'user-a', created_at: '2026-10-13', updated_by: 'user-b'});
  const other = item('other', 6000, 8000, 1);
  const original = [item('main', 0, 10000, 0), edited, other];
  const copy = structuredClone(original);
  const result = applyAnnotationRange(original, 3000, 5000, '已编辑', run, 'scene', 'edit');
  assert.deepEqual(result.find(row => row.id === 'edit'), {...edited, start_ms: 3000, end_ms: 5000, label: '已编辑', kind: 'interval'});
  assert.throws(() => applyAnnotationRange(original, 5000, 7000, '冲突', run, 'scene', 'edit'), /重叠/);
  assert.deepEqual(original, copy);
});

test('editing a main interval replaces overlapped main content while preserving children and its ID', () => {
  const edited = item('edit', 1000, 3000, 0, {label: '编辑前', created_by: 'user-a', created_at: '2026-10-13'});
  const earlier = item('earlier', 0, 1000, 0, {label: '之前'});
  const later = item('later', 4000, 8000, 0, {label: '之后'});
  const child = item('child', 2000, 7000, 1, {label: '子轴', created_by: 'user-b'});
  const original = [earlier, edited, child, later];
  const copy = structuredClone(original);
  const result = applyAnnotationRange(original, 500, 6000, '修改后', run, 'scene', 'edit');
  assert.deepEqual(result.find(row => row.id === 'edit'), {...edited, label: '修改后', start_ms: 500, end_ms: 6000, kind: 'interval'});
  assert.deepEqual(result.find(row => row.id === 'child'), child);
  assert.deepEqual(result.find(row => row.id === 'earlier'), {...earlier, end_ms: 500});
  assert.deepEqual(result.find(row => row.id === 'later'), {...later, start_ms: 6000});
  assert.deepEqual(original, copy);
});

test('legacy lane inference is deterministic, respects future explicit rows, and does not mutate data', () => {
  const original = [item('saved', 5000, 10000, 0), item('legacy-a', 0, 8000), item('legacy-b', 1000, 2000), item('saved-child', 0, 10000, 3)];
  const copy = structuredClone(original);
  const expected = new Map([['saved', 0], ['legacy-a', 1], ['legacy-b', 0], ['saved-child', 3]]);
  for (const input of [original, [...original].reverse()]) {
    for (const {segment, lane} of arrangeLanes(input, 10000)) assert.equal(lane, expected.get(segment.id));
  }
  assert.deepEqual(original, copy);
  const survivors = deleteAnnotation([item('a', 0, 10000), item('b', 1000, 8000)], 'a', run, 'scene');
  assert.deepEqual(ranges(survivors), [[1000, 8000, 1]], 'inferred children stay children after the main record is deleted');
});

test('semantic comparison includes lanes but accepts inferred legacy lanes', () => {
  const legacy = [item('a', 0, 10000), item('b', 1000, 8000)];
  const saved = assignSegmentLanes(legacy, 10000);
  assert.equal(annotationsEqual({scene: legacy}, {scene: saved}), true);
  assert.equal(annotationsEqual({scene: saved}, {scene: saved.map(row => row.id === 'b' ? {...row, lane: 2} : row)}), false);
});

test('ongoing legacy events release their row for a point exactly at project end', () => {
  const legacy = {habit: [item('ongoing', 0, null, undefined, {kind: 'interval'}), item('end', 10000, 10000, undefined, {kind: 'point'})]};
  const backend = {habit: legacy.habit.map(row => ({...row, lane: 0}))};
  assert.deepEqual(normalizeStateSeams(legacy, [], [], [], 10000).habit, backend.habit);
  assert.equal(annotationsEqual(legacy, backend, 10000), true);
  const wrongRow = {habit: [backend.habit[0], {...backend.habit[1], lane: 1}]};
  assert.equal(annotationsEqual(legacy, wrongRow, 10000), false);
  const during = {habit: [legacy.habit[0], {...legacy.habit[1], start_ms: 9999, end_ms: 9999}]};
  assert.deepEqual(normalizeStateSeams(during, [], [], [], 10000).habit.map(row => row.lane), [0, 1]);
});

test('legacy tie ordering matches backend Unicode code points instead of browser locale', () => {
  const ids = ['a', 'A', '_', '!', '\u{10000}', '\uE000'];
  const assigned = assignSegmentLanes(ids.map(id => item(id, 0, 1000)), 10000);
  assert.deepEqual(assigned.map(row => [row.id, row.lane]), [
    ['!', 0], ['A', 1], ['_', 2], ['a', 3], ['\uE000', 4], ['\u{10000}', 5],
  ]);
});

test('invalid explicit row assignments are rejected instead of silently moved to the main row', () => {
  for (const lane of [-1, 0.5, NaN, Infinity, Number.MAX_SAFE_INTEGER + 1, null, '1']) {
    assert.throws(() => assignSegmentLanes([item('bad', 0, 1000, lane)], 10000), /非负安全整数/);
  }
});

test('normalization never merges equal labels from different rows or fills explicit saved gaps', () => {
  const rows = [item('main', 0, 5000, 0), item('child', 5000, 10000, 1), item('overlap', 1000, 8000, 2)];
  assert.equal(normalizeStateSeams({scene: rows}).scene.length, 3);
  const bridge = {start_ms: 5000, end_ms: 5100, previous_video_id: 'a', next_video_id: 'b', raw_boundary_error_ms: 100, reason: 'adjacent_camera_second_precision'};
  const explicit = [item('a', 0, 5000, 0), item('b', 5200, 10000, 0)];
  const normalized = normalizeStateSeams({scene: explicit}, [bridge], [{id: 'b', end_ms: 10000}]);
  assert.deepEqual(normalized.scene, explicit);
});

test('points replace only same-time main points and require anchors for new overlap', () => {
  const original = [item('point', 5000, 5000, 0, {kind: 'point'}), item('child', 5000, 5000, 1, {kind: 'point', created_by: 'a'})];
  const result = applyAnnotationPoint(original, 5000, '新事件', 10000);
  assert.equal(result.length, 2);
  assert.equal(result.find(row => row.lane === 0).label, '新事件');
  assert.deepEqual(result.find(row => row.id === 'child'), original[1]);
  assert.throws(() => applyAnnotationPoint([item('main', 0, 10000, 0)], 5000, '事件', 10000), /锚点/);
  const edited = applyAnnotationPoint(original, 6000, '改名', 10000, 'child').find(row => row.id === 'child');
  assert.equal(edited.lane, 1);
  assert.equal(edited.created_by, 'a');
  assert.throws(() => applyAnnotationPoint([...original, item('other', 5500, 7000, 1)], 6000, '改名', 10000, 'child'), /重叠/);
});
