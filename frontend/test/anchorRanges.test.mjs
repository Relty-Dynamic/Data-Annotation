import test from 'node:test';
import assert from 'node:assert/strict';
import {annotationsEqual, applyAnnotationRange, deleteAnnotation, normalizeStateSeams} from '../src/domain.ts';

const recording = [{start_ms: 0, end_ms: 20000}];
const row = (id, label, start_ms, end_ms, lane = 0, extra = {}) =>
  ({id, label, start_ms, end_ms, lane, kind: 'interval', ...extra});
const anchor = (rows, start, end, label, spans = recording, track = 'posture') =>
  applyAnnotationRange(rows, start, end, label, spans, track, undefined, false, true);
const content = rows => rows.map(({label, start_ms, end_ms, lane}) => [label, start_ms, end_ms, lane]);

test('anchor saves retain matching main content and add only the conflicting tail in a child', () => {
  const before = [row('stand', '站', 0, 10000), row('sit', '坐', 10000, 15000)];
  const result = anchor(before, 0, 15000, '站');
  assert.deepEqual(content(result).sort((a, b) => a[1] - b[1] || a[3] - b[3]), [['站', 0, 10000, 0], ['坐', 10000, 15000, 0], ['站', 10000, 15000, 1]]);
  assert.deepEqual(result.find(item => item.id === 'stand'), before[0]);
  assert.deepEqual(result.find(item => item.id === 'sit'), before[1]);
});

test('matching selections inside or equal to the main interval do not produce duplicate annotations', () => {
  const before = [row('stand', '站', 0, 15000, 0, {created_by: 'a', created_at: '2026-10-14T10:00:00Z'})];
  for (const [start, end] of [[0, 15000], [2000, 9000]]) {
    const result = anchor(before, start, end, '站');
    assert.deepEqual(result, before);
    assert.equal(annotationsEqual({posture: before}, {posture: result}, 20000), true);
  }
});

test('one save merges same-label portions and fills main blanks while isolating every conflicting boundary', () => {
  const before = [row('left', '站', 1000, 3000), row('middle', '坐', 5000, 7000), row('right', '站', 9000, 12000)];
  const result = anchor(before, 0, 10000, '站');
  assert.deepEqual(content(result).sort((a, b) => a[3] - b[3] || a[1] - b[1]), [
    ['站', 0, 5000, 0], ['坐', 5000, 7000, 0], ['站', 7000, 12000, 0], ['站', 5000, 7000, 1],
  ]);
  assert.deepEqual(result.find(item => item.id === 'middle'), before[1]);
});

test('empty main saves only the selected recorded pieces and never initializes the whole project', () => {
  const spans = [{start_ms: 0, end_ms: 4000}, {start_ms: 6000, end_ms: 10000}];
  assert.deepEqual(content(anchor([], 2000, 8000, '站', spans)), [['站', 2000, 4000, 0], ['站', 6000, 8000, 0]]);
  assert.deepEqual(anchor([], 4500, 5500, '站', spans), []);
});

test('exact label notes and category mode decide whether the main interval already represents the selection', () => {
  const rows = [
    row('same', '站（窗边）', 0, 3000, 0, {mode: 'state'}),
    row('other-note', '站（门边）', 3000, 6000, 0, {mode: 'state'}),
    row('overlay', '站（窗边）', 6000, 9000, 0, {mode: 'overlay'}),
    row('plain', '站', 9000, 12000, 0, {mode: 'state'}),
  ];
  const result = anchor(rows, 0, 12000, '站（窗边）', recording, 'category');
  assert.deepEqual(result.filter(item => item.lane === 0), rows);
  const child = result.find(item => item.lane === 1);
  assert.deepEqual([child.label, child.start_ms, child.end_ms, child.mode], ['站（窗边）', 3000, 12000, 'state']);
  assert.equal(result.length, 5);
});

test('child conflicts split between lower available lanes instead of requiring one entirely free child', () => {
  const rows = [
    row('first-conflict', '坐', 0, 3000), row('match', '站', 3000, 7000), row('second-conflict', '躺', 7000, 10000),
    row('child-one', '事件一', 1000, 2000, 1), row('child-two', '事件二', 8000, 9000, 2),
    row('child-three', '事件三', 4000, 6000, 3),
  ];
  const result = anchor(rows, 0, 10000, '站');
  const added = result.filter(item => !rows.some(old => old.id === item.id));
  assert.deepEqual(content(added), [['站', 0, 1000, 1], ['站', 1000, 2000, 2], ['站', 2000, 3000, 1], ['站', 7000, 10000, 1]]);
  for (const child of rows.filter(item => item.lane > 0)) assert.deepEqual(result.find(item => item.id === child.id), child);
});

test('conflict fragments split at true gaps and share a reusable child without filling missing footage', () => {
  const spans = [{start_ms: 0, end_ms: 4000}, {start_ms: 6000, end_ms: 10000}];
  const rows = [row('left', '坐', 0, 4000), row('right', '躺', 6000, 10000), row('child', '事件', 0, 1000, 1)];
  const added = anchor(rows, 2000, 8000, '站', spans).filter(item => !rows.some(old => old.id === item.id));
  assert.deepEqual(content(added), [['站', 2000, 4000, 1], ['站', 6000, 8000, 1]]);
});

test('adjacent child additions merge only on their assigned row and do not overwrite another child', () => {
  const rows = [row('main', '坐', 0, 10000), row('previous', '站', 0, 3000, 1), row('occupied', '看手机', 4000, 6000, 2)];
  const result = anchor(rows, 3000, 7000, '站');
  assert.deepEqual(result.find(item => item.id === 'previous'), {...rows[1], end_ms: 7000});
  assert.deepEqual(result.find(item => item.id === 'occupied'), rows[2]);
  assert.deepEqual(result.find(item => item.id === 'main'), rows[0]);
});

test('overlapping food anchors extend the existing food child instead of creating an extra axis', () => {
  const spans = [{start_ms: 0, end_ms: 30000}];
  const main = row('leisure', '休闲娱乐', 0, 30000, 0, {mode: 'state'});
  const food = row('food', '饮食', 16000, 21000, 1, {
    mode: 'state', created_by: 'annotator-a', created_by_name: '甲', created_at: '2026-10-14T10:00:00Z',
    updated_by: 'annotator-b', updated_by_name: '乙', updated_at: '2026-10-14T10:01:00Z',
  });
  const before = [main, food];
  const snapshot = structuredClone(before);
  const result = anchor(before, 20000, 23000, '饮食', spans, 'category');
  assert.deepEqual(result, [main, {...food, end_ms: 23000}]);
  assert.deepEqual(before, snapshot, 'the undo snapshot and authenticated metadata remain intact');
  assert.deepEqual(anchor(result, 20000, 23000, '饮食', spans, 'category'), result, 'repeating the same anchor is idempotent');
});

test('filling before a matching saved row preserves that upper row identity and creator metadata', () => {
  const spans = [{start_ms: 0, end_ms: 30000}];
  const main = row('leisure', '休闲娱乐', 0, 30000, 0, {mode: 'state'});
  const food = row('food', '饮食', 16000, 21000, 1, {
    mode: 'state', created_by: 'annotator-a', created_by_name: '甲', created_at: '2026-10-14T10:00:00Z',
    updated_by: 'annotator-b', updated_by_name: '乙', updated_at: '2026-10-14T10:01:00Z',
  });
  const result = anchor([main, food], 14000, 18000, '饮食', spans, 'category');
  assert.deepEqual(result, [main, {...food, start_ms: 14000}]);
  const mainOnly = {...food, lane: 0};
  assert.deepEqual(anchor([mainOnly], 14000, 18000, '饮食', spans, 'category'), [{...mainOnly, start_ms: 14000}]);
});

test('an anchor joining saved child intervals retains the first saved ID and newest contributor', () => {
  const main = row('main', '休闲娱乐', 0, 20000);
  const first = row('first', '饮食', 3000, 5000, 1, {created_by: 'a', created_at: '2026-10-14T10:00:00Z'});
  const next = row('next', '饮食', 7000, 9000, 1, {created_by: 'b', created_by_name: '乙', created_at: '2026-10-14T10:01:00Z'});
  const result = anchor([main, first, next], 1000, 11000, '饮食');
  assert.deepEqual(result, [main, {...first, start_ms: 1000, end_ms: 11000,
    updated_by: 'b', updated_by_name: '乙', updated_at: next.created_at}]);
});

test('an anchor does not migrate or delete already saved lower duplicate intervals', () => {
  const main = row('main', '休闲娱乐', 0, 20000);
  const upper = row('upper', '饮食', 3000, 10000, 1, {created_by: 'a'});
  const lower = row('lower', '饮食', 4000, 12000, 2, {created_by: 'b'});
  const result = anchor([main, upper, lower], 5000, 9000, '饮食');
  assert.deepEqual(result, [main, upper, lower]);
  assert.deepEqual(normalizeStateSeams({category: result}).category, [main, upper, lower]);
});

test('partial child conflicts continue downward while matching pieces on both children are reused', () => {
  const spans = [{start_ms: 0, end_ms: 30000}];
  const before = [
    row('leisure', '休闲娱乐', 0, 30000), row('food', '饮食', 16000, 21000, 1),
    row('work', '工作学习', 21000, 22000, 1), row('other-food', '饮食', 21000, 22000, 2),
  ];
  const result = anchor(before, 20000, 23000, '饮食', spans);
  assert.equal(Math.max(...result.map(item => item.lane)), 2);
  assert.equal(result.length, 5);
  assert.deepEqual(content(result.filter(item => !before.some(old => old.id === item.id))), [['饮食', 22000, 23000, 1]]);
  for (const existing of before) assert.deepEqual(result.find(item => item.id === existing.id), existing);
  assert.deepEqual(anchor(result, 20000, 23000, '饮食', spans), result);
});

test('different fragments fill disjoint free child slots without allocating an unnecessary third child', () => {
  const before = [row('main', '休闲娱乐', 0, 10000), row('first', '工作学习', 0, 5000, 1), row('second', '出行', 5000, 10000, 2)];
  const result = anchor(before, 0, 10000, '饮食');
  const added = result.filter(item => !before.some(old => old.id === item.id));
  assert.deepEqual(content(added), [['饮食', 0, 5000, 2], ['饮食', 5000, 10000, 1]]);
  assert.deepEqual(anchor(result, 0, 10000, '饮食'), result);
});

test('child matching compares complete notes and category modes before reusing its content', () => {
  const before = [
    row('main', '休闲娱乐', 0, 12000, 0, {mode: 'state'}),
    row('same-note', '饮食（餐厅）', 0, 3000, 1, {mode: 'state'}),
    row('other-note', '饮食（厨房）', 3000, 6000, 1, {mode: 'state'}),
    row('other-mode', '饮食（餐厅）', 6000, 9000, 1, {mode: 'overlay'}),
  ];
  const result = anchor(before, 0, 9000, '饮食（餐厅）', recording, 'category');
  for (const existing of before) assert.deepEqual(result.find(item => item.id === existing.id), existing);
  const added = result.filter(item => !before.some(old => old.id === item.id));
  assert.equal(added.length, 1);
  assert.deepEqual([added[0].label, added[0].start_ms, added[0].end_ms, added[0].lane, added[0].mode], ['饮食（餐厅）', 3000, 9000, 2, 'state']);
  assert.deepEqual(anchor(result, 0, 9000, '饮食（餐厅）', recording, 'category'), result);
});

test('a child point stays instantaneous while only its occupied millisecond continues to the next child', () => {
  const main = row('main', '其他事件', 0, 10000);
  const point = row('point', '饮食', 3000, 3000, 1, {kind: 'point', created_by: 'a'});
  const result = anchor([main, point], 1000, 6000, '饮食', recording, 'habit');
  assert.deepEqual(result.find(item => item.id === 'point'), point);
  assert.deepEqual(content(result.filter(item => !['main', 'point'].includes(item.id))), [
    ['饮食', 1000, 3000, 1], ['饮食', 3000, 3001, 2], ['饮食', 3001, 6000, 1],
  ]);
  assert.deepEqual(anchor(result, 1000, 6000, '饮食', recording, 'habit'), result);
});

test('matching child intervals merge within recordings but never bridge a true recording gap', () => {
  const spans = [{start_ms: 0, end_ms: 4000}, {start_ms: 6000, end_ms: 10000}];
  const before = [row('main-left', '休闲娱乐', 0, 4000), row('main-right', '休闲娱乐', 6000, 10000),
    row('food-left', '饮食', 1000, 3000, 1), row('food-right', '饮食', 7000, 9000, 1)];
  const result = anchor(before, 2000, 8000, '饮食', spans);
  assert.deepEqual(content(result.filter(item => item.lane === 1)), [['饮食', 1000, 4000, 1], ['饮食', 6000, 9000, 1]]);
  assert.deepEqual(anchor(result, 2000, 8000, '饮食', spans), result);
  assert.deepEqual(result.filter(item => item.lane === 0), before.slice(0, 2));
});

test('a sparse saved child number cannot force allocation to enumerate all intermediate IDs', () => {
  const main = row('main', '休闲娱乐', 0, 10000);
  const sparse = row('sparse', '工作学习', 0, 10000, Number.MAX_SAFE_INTEGER);
  const result = anchor([main, sparse], 1000, 2000, '饮食');
  assert.deepEqual(result.find(item => item.id === 'sparse'), sparse);
  assert.deepEqual(content(result.filter(item => !['main', 'sparse'].includes(item.id))), [['饮食', 1000, 2000, 1]]);
});

test('main points stay exact points and reserve only their occupied millisecond during anchor insertion', () => {
  const point = row('point', '同标签', 3000, 3000, 0, {kind: 'point'});
  const atEnd = row('endpoint', '末尾', 6000, 6000, 0, {kind: 'point'});
  const result = anchor([point, atEnd], 1000, 6000, '同标签', recording, 'habit');
  assert.deepEqual(result.find(item => item.id === 'point'), point);
  assert.deepEqual(result.find(item => item.id === 'endpoint'), atEnd);
  assert.deepEqual(content(result.filter(item => item.kind === 'interval')), [
    ['同标签', 1000, 3000, 0], ['同标签', 3000, 3001, 1], ['同标签', 3001, 6000, 0],
  ]);
});

test('anchor addition leaves input snapshots unchanged for undo and retains saved child positions', () => {
  const before = [row('main', '坐', 0, 10000), row('old-child', '站', 2000, 5000, 7, {created_by: 'a'})];
  const undoSnapshot = structuredClone(before);
  const after = anchor(before, 5000, 15000, '站');
  assert.deepEqual(before, undoSnapshot);
  assert.deepEqual(after.find(item => item.id === 'old-child'), before[1]);
  const removed = deleteAnnotation(before, 'main', recording, 'posture');
  assert.deepEqual(removed, [before[1]]);
  assert.deepEqual(normalizeStateSeams({posture: removed}).posture, [before[1]], 'deletion and reload do not lift the child into the blank main row');
  const refilled = anchor(removed, 2000, 5000, '站');
  assert.deepEqual(refilled.find(item => item.id === 'old-child'), before[1]);
  assert.equal(refilled.some(item => item.lane === 0 && item.label === '站'), true);
});

test('ordinary edits and existing color edits keep their established overwrite and lane rules', () => {
  const main = row('main', '坐', 0, 10000);
  const child = row('child', '站', 1000, 3000, 1);
  const other = row('other', '躺', 5000, 7000, 1);
  const ordinary = applyAnnotationRange([main, child], 2000, 6000, '站', recording, 'posture');
  assert.deepEqual(content(ordinary.filter(item => item.lane === 0)), [['坐', 0, 2000, 0], ['站', 2000, 6000, 0], ['坐', 6000, 10000, 0]]);
  assert.deepEqual(ordinary.find(item => item.id === 'child'), child);
  const edited = applyAnnotationRange([main, child], 3000, 4000, '移动', recording, 'posture', 'child', false, true);
  assert.deepEqual(edited.find(item => item.id === 'child'), {...child, label: '移动', start_ms: 3000, end_ms: 4000});
  assert.throws(() => applyAnnotationRange([main, child, other], 3000, 6000, '移动', recording, 'posture', 'child', false, true), /重叠/);
});
