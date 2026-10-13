import test from 'node:test';
import assert from 'node:assert/strict';
import {
  annotationsEqual, applyAnnotationRange, normalizeStateSeams, projectAnnotationLabels,
  projectTrackLabels, splitAnnotationLabel, switchCoveredState,
} from '../src/domain.ts';

const span = [{start_ms: 0, end_ms: 10000}];
const row = (id, start_ms, end_ms, lane = 0, extra = {}) => ({
  id, label: '同一标签（备注）', start_ms, end_ms, kind: 'interval', lane, ...extra,
});
const actorA = {created_by: 'a', created_by_name: '甲', created_at: '2026-10-13T10:00:00Z'};
const actorB = {created_by: 'b', created_by_name: '乙', created_at: '2026-10-13T10:01:00Z'};

test('every interval axis merges adjacent saved records and retains the newest authenticated contributor', () => {
  const axes = ['scene', 'posture', 'category', 'habit', 'weather', 'sound'];
  const annotations = Object.fromEntries(axes.map(axis => [axis, [
    row('first', 0, 3000, 0, actorA),
    row('child-first', 0, 2000, 1, actorB),
    row('child-next', 2000, 3000, 1, {...actorA, updated_by: 'c', updated_by_name: '丙', updated_at: '2026-10-13T10:02:00Z'}),
    row('next', 3000, 6000, 0, actorB),
  ]]));
  const original = structuredClone(annotations);
  const result = normalizeStateSeams(annotations);
  for (const axis of axes) {
    const main = result[axis].find(item => item.lane === 0);
    const child = result[axis].find(item => item.lane === 1);
    assert.equal(result[axis].length, 2, axis);
    assert.deepEqual(main, {...row('first', 0, 6000), ...actorA,
      updated_by: 'b', updated_by_name: '乙', updated_at: actorB.created_at});
    assert.equal(child.id, 'child-first');
    assert.equal(child.end_ms, 3000);
    assert.equal(child.created_by, 'b');
    assert.equal(child.updated_by, 'c');
  }
  assert.deepEqual(annotations, original);
  assert.deepEqual(normalizeStateSeams(result), result);
  assert.ok(annotationsEqual(annotations, result));
});

test('saved attribution does not leave a seam after a later anchor range or ordinary state click', () => {
  for (const track of ['scene', 'posture', 'category', 'habit', 'weather', 'sound']) {
    const before = [row('saved', 0, 3000, 0, actorA)];
    const extended = applyAnnotationRange(before, 3000, 6000, before[0].label, span, track);
    assert.equal(extended.length, 1, track);
    assert.equal(extended[0].id, 'saved');
    assert.equal(extended[0].end_ms, 6000);
    const split = applyAnnotationRange(extended, 2000, 4000, '其他标签', span, track);
    const joinedAgain = applyAnnotationRange(split, 2000, 4000, before[0].label, span, track,
      split.find(item => item.label === '其他标签').id);
    assert.equal(joinedAgain.length, 1, track);
    assert.equal(joinedAgain[0].id, 'saved');
    assert.equal(joinedAgain[0].end_ms, 6000);
    assert.deepEqual(before, [row('saved', 0, 3000, 0, actorA)], 'undo snapshot is not mutated');
  }
  const clicked = switchCoveredState([row('saved', 0, 10000, 0, actorA)], 5000, row('', 0, 0).label, 10000, span);
  assert.equal(clicked.length, 1);
  assert.equal(clicked[0].end_ms, 10000);
});

test('child reuse merges only its own neighboring interval without changing the main axis', () => {
  const rows = [row('main', 0, 10000, 0, actorA), row('child', 1000, 3000, 1, actorB)];
  const result = applyAnnotationRange(rows, 3000, 6000, rows[0].label, span, 'habit', undefined, false, true);
  assert.deepEqual(result, [rows[0], {...rows[1], end_ms: 6000}]);
});

test('normalization preserves point events, different notes, category modes, row boundaries and actual blanks', () => {
  const cases = [
    [row('a', 0, 1000), row('b', 1001, 2000)],
    [row('a', 0, 1000), row('b', 1000, 2000, 1)],
    [row('a', 0, 1000), row('b', 1000, 2000, 0, {label: '同一标签（另一备注）'})],
    [row('a', 0, 1000, 0, {mode: 'state'}), row('b', 1000, 2000, 0, {mode: 'overlay'})],
    [row('a', 1000, 1000, 0, {kind: 'point'}), row('b', 1001, 1001, 0, {kind: 'point'})],
  ];
  for (const records of cases) assert.deepEqual(normalizeStateSeams({habit: records}).habit, records);
});

test('autosave comparison accepts merged server intervals without overlooking changed notes or child lanes', () => {
  const local = {habit: [row('a', 0, 3000, 1, actorA), row('b', 3000, 6000, 1, actorB)]};
  const saved = normalizeStateSeams(local);
  assert.ok(annotationsEqual(local, saved));
  assert.equal(annotationsEqual(local, {...saved, habit: [{...saved.habit[0], lane: 2}]}), false);
  assert.equal(annotationsEqual(local, {...saved, habit: [{...saved.habit[0], label: '同一标签（新备注）'}]}), false);
});

test('old fixed label notes remain editable after the panel adopts the current library label choices', () => {
  const project = {track_labels: {scene: ['室内', '室外'], posture: ['移动'], category: ['工作学习']},
    custom_tracks: [{id: 'custom', name: '自定义', mode: 'state', labels: ['新标签']}]};
  assert.deepEqual(projectTrackLabels(project, 'category'), ['工作学习']);
  assert.deepEqual(splitAnnotationLabel('专注（旧备注）', projectAnnotationLabels(project, 'category')), {base: '专注', note: '旧备注'});
  assert.deepEqual(splitAnnotationLabel('车内（旧备注）', projectAnnotationLabels(project, 'scene')), {base: '车内', note: '旧备注'});
  assert.deepEqual(splitAnnotationLabel('动（旧备注）', projectAnnotationLabels(project, 'posture')), {base: '动', note: '旧备注'});
  assert.deepEqual(projectAnnotationLabels(project, 'custom'), ['新标签']);
});
