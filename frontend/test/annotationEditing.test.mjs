import test from 'node:test';
import assert from 'node:assert/strict';
import {
  TRACKS, TRACK_LABELS, SCENE_LABELS, POSTURE_LABELS, CATEGORY_LABELS,
  annotationsEqual, normalizeStateSeams, switchCoveredState, applyAnnotationRange,
  deleteAnnotation, uncoveredSpans, snapTimelineTime, timelineSnapPoints,
} from '../src/domain.ts';

const segment = (id, label, start_ms, end_ms, extra = {}) => ({ id, label, start_ms, end_ms, ...extra });
const compact = segments => segments.map(({ label, start_ms, end_ms }) => [label, start_ms, end_ms]);
const runs = [{ start_ms: 0, end_ms: 10000 }, { start_ms: 15000, end_ms: 25000 }];

test('annotation dimensions and fixed choices use the requested order', () => {
  assert.deepEqual(TRACKS, ['scene', 'posture', 'category', 'habit']);
  assert.deepEqual(TRACKS.map(track => TRACK_LABELS[track]), ['场景', '姿势', '大类', '习惯']);
  assert.deepEqual(SCENE_LABELS, ['室内', '室外']);
  assert.deepEqual(POSTURE_LABELS, ['动', '坐', '站', '躺']);
  assert.deepEqual(CATEGORY_LABELS, ['专注', '活动', '用餐', '通勤', '社交', '放松', '休息', '其他']);
});

test('the first state selection covers every recording run and later switches preserve gaps', () => {
  const first = switchCoveredState([], 5000, '坐', 25000, runs);
  assert.deepEqual(compact(first), [['坐', 0, 10000], ['坐', 15000, 25000]]);
  assert.deepEqual(uncoveredSpans(first, runs), []);
  const changed = switchCoveredState(first, 7000, '站', 25000, runs);
  assert.deepEqual(compact(changed), [['坐', 0, 7000], ['站', 7000, 10000], ['坐', 15000, 25000]]);
  assert.deepEqual(switchCoveredState(changed, 12000, '动', 25000, runs), changed);
  assert.deepEqual(switchCoveredState([], 25000, '动', 25000, runs), []);
});

test('state initialization merges adjacent clips and does not silently fill an existing blank', () => {
  const adjacent = [{ start_ms: 0, end_ms: 5000 }, { start_ms: 5000, end_ms: 10000 }];
  assert.deepEqual(compact(switchCoveredState([], 7000, '室内', 10000, adjacent)), [['室内', 0, 10000]]);
  const partial = [segment('a', '室内', 2000, 10000)];
  const changed = switchCoveredState(partial, 6000, '室外', 10000, adjacent);
  assert.deepEqual(compact(changed), [['室内', 2000, 6000], ['室外', 6000, 10000]]);
  assert.deepEqual(uncoveredSpans(changed, adjacent), [{ start_ms: 0, end_ms: 2000 }]);
});

test('category single-point changes retain overlays and later state changes', () => {
  const original = [
    segment('a', '活动', 0, 6000, { mode: 'state' }),
    segment('overlay', '社交', 1000, 9000, { mode: 'overlay' }),
    segment('b', '用餐', 6000, 10000, { mode: 'state' }),
  ];
  const copy = structuredClone(original);
  const changed = switchCoveredState(original, 3000, '专注', 10000, [{ start_ms: 0, end_ms: 10000 }], true);
  assert.deepEqual(compact(changed), [['活动', 0, 3000], ['社交', 1000, 9000], ['专注', 3000, 6000], ['用餐', 6000, 10000]]);
  assert.deepEqual(changed.find(item => item.id === 'overlay'), original[1]);
  assert.deepEqual(original, copy);
  assert.ok(changed.filter(item => item.id !== 'overlay').every(item => item.mode === 'state'));
});

test('the first category state can initialize recorded time while retaining an existing range', () => {
  const overlay = segment('overlay', '社交', 1000, 3000, { mode: 'overlay' });
  const result = switchCoveredState([overlay], 5000, '其他', 25000, runs, true);
  assert.deepEqual(uncoveredSpans(result, runs), []);
  assert.deepEqual(result.find(item => item.id === 'overlay'), overlay);
  assert.equal(result.filter(item => item.mode === 'state').length, 2);
});

test('range replacement splits states and retains content outside the selection', () => {
  const original = [segment('a', '坐', 0, 4000), segment('b', '动', 4000, 8000), segment('c', '坐', 8000, 10000)];
  const copy = structuredClone(original);
  const result = applyAnnotationRange(original, 2000, 9000, '站', runs, 'posture');
  assert.deepEqual(compact(result), [['坐', 0, 2000], ['站', 2000, 9000], ['坐', 9000, 10000]]);
  assert.equal(result[0].id, 'a');
  assert.equal(result[2].id, 'c');
  assert.deepEqual(original, copy);
  const split = applyAnnotationRange([segment('one', '室内', 0, 10000)], 2000, 8000, '室外', runs, 'scene');
  assert.deepEqual(compact(split), [['室内', 0, 2000], ['室外', 2000, 8000], ['室内', 8000, 10000]]);
  assert.equal(new Set(split.map(item => item.id)).size, 3);
});

test('range annotations split at real gaps and category/habit ranges preserve overlaps', () => {
  for (const track of ['category', 'habit']) {
    const original = [segment('original', '活动', 0, 10000)];
    const result = applyAnnotationRange(original, 5000, 20000, '社交', runs, track);
    assert.deepEqual(compact(result), [['活动', 0, 10000], ['社交', 5000, 10000], ['社交', 15000, 20000]]);
    assert.equal(new Set(result.map(item => item.id)).size, 3);
    if (track === 'category') assert.ok(result.filter(item => item.id !== 'original').every(item => item.mode === 'overlay'));
  }
});

test('editing removes only the old range and keeps category mode and first ID', () => {
  const original = [
    segment('state', '活动', 0, 10000, { mode: 'state' }),
    segment('edit', '专注', 2000, 6000, { mode: 'overlay' }),
  ];
  const result = applyAnnotationRange(original, 7000, 22000, '社交', runs, 'category', 'edit');
  assert.deepEqual(compact(result), [['活动', 0, 10000], ['社交', 7000, 10000], ['社交', 15000, 22000]]);
  assert.equal(result[1].id, 'edit');
  assert.equal(result[1].mode, 'overlay');
  assert.equal(applyAnnotationRange(original, 3000, 9000, '放松', runs, 'category', 'state').find(item => item.id === 'state').mode, 'state');
  assert.deepEqual(applyAnnotationRange(original, 11000, 12000, '社交', runs, 'category', 'edit'), original);
  assert.deepEqual(applyAnnotationRange(original, 9000, 2000, '社交', runs, 'category', 'edit'), original);
});

test('deletion extends an adjacent predecessor but leaves first and sole segments unannotated', () => {
  const original = [segment('a', '坐', 0, 3000), segment('b', '站', 3000, 6000), segment('c', '动', 6000, 10000)];
  const copy = structuredClone(original);
  assert.deepEqual(compact(deleteAnnotation(original, 'b', runs, 'posture')), [['坐', 0, 6000], ['动', 6000, 10000]]);
  assert.deepEqual(compact(deleteAnnotation(original, 'c', runs, 'posture')), [['坐', 0, 3000], ['站', 3000, 10000]]);
  assert.deepEqual(compact(deleteAnnotation(original, 'a', runs, 'posture')), [['站', 3000, 6000], ['动', 6000, 10000]]);
  assert.deepEqual(deleteAnnotation([original[0]], 'a', runs, 'posture'), []);
  assert.deepEqual(original, copy);
});

test('deletion never bridges pre-existing blank time or a true recording gap', () => {
  const original = [segment('a', '室内', 0, 3000), segment('b', '室外', 5000, 10000), segment('c', '室内', 15000, 25000)];
  assert.deepEqual(compact(deleteAnnotation(original, 'b', runs, 'scene')), [['室内', 0, 3000], ['室内', 15000, 25000]]);
  assert.deepEqual(compact(deleteAnnotation(original, 'c', runs, 'scene')), [['室内', 0, 3000], ['室外', 5000, 10000]]);
});

test('category deletion fills only newly uncovered time, preserving all surviving overlaps', () => {
  const original = [
    segment('previous', '活动', 0, 3000, { mode: 'state' }),
    segment('removed', '专注', 3000, 10000, { mode: 'state' }),
    segment('overlay', '社交', 5000, 10000, { mode: 'overlay' }),
  ];
  const result = deleteAnnotation(original, 'removed', runs, 'category');
  assert.deepEqual(compact(result), [['活动', 0, 5000], ['社交', 5000, 10000]]);
  assert.deepEqual(result.find(item => item.id === 'overlay'), original[2]);
  const fullyCovered = [...original, segment('cover', '通勤', 3000, 10000, { mode: 'overlay' })];
  assert.equal(deleteAnnotation(fullyCovered, 'removed', runs, 'category').find(item => item.id === 'previous').end_ms, 3000);
  assert.deepEqual(deleteAnnotation(original, 'overlay', runs, 'category'), original.slice(0, 2));
  assert.deepEqual(deleteAnnotation(original, 'removed', runs, 'habit'), [original[0], original[2]]);
});

test('coverage unions overlaps and ignores point events and unrecorded time', () => {
  const data = [
    segment('a', '专注', 0, 4000),
    segment('b', '通勤', 2000, 8000),
    segment('point', '事件', 9000, 9000, { kind: 'point' }),
    segment('c', '活动', 18000, null),
  ];
  assert.deepEqual(uncoveredSpans(data, runs), [{ start_ms: 8000, end_ms: 10000 }, { start_ms: 15000, end_ms: 18000 }]);
  assert.deepEqual(uncoveredSpans([], runs), runs);
});

test('legacy annotations default category to empty and category normalization never loses overlaps', () => {
  const legacy = { scene: [], posture: [], habit: [] };
  assert.deepEqual(normalizeStateSeams(legacy), { ...legacy, category: [] });
  assert.equal(annotationsEqual(legacy, { ...legacy, category: [] }), true);
  const category = [
    segment('a', '活动', 0, 5000, { mode: 'state' }),
    segment('b', '活动', 3000, 8000, { mode: 'overlay' }),
    segment('c', '活动', 8000, 10000, { mode: 'overlay' }),
  ];
  const annotations = { ...legacy, category };
  assert.strictEqual(normalizeStateSeams(annotations).category, category);
  const changed = structuredClone(annotations);
  changed.category[1].mode = 'state';
  assert.equal(annotationsEqual(annotations, changed), false);
  const absentMode = { ...legacy, category: [segment('legacy', '活动', 0, 10000)] };
  assert.equal(annotationsEqual(absentMode, { ...legacy, category: [{ ...absentMode.category[0], mode: 'state' }] }), true);
});

test('snapping uses screen pixels, bounds invalid input, and resolves ties consistently', () => {
  assert.equal(snapTimelineTime(5050, [5000], 10000, 1000), 5000);
  assert.equal(snapTimelineTime(5100, [5000], 10000, 1000), 5100);
  assert.equal(snapTimelineTime(5100, [5000], 10000, 500), 5000);
  assert.equal(snapTimelineTime(5000, [5050, 4950], 10000, 1000), 4950);
  assert.equal(snapTimelineTime(5000, [4999], 10000, 0), 5000);
  assert.equal(snapTimelineTime(-5, [-5, NaN, Infinity], 10000, 1000), 0);
  assert.equal(snapTimelineTime(12000, [11000], 10000, 1000), 10000);
});

test('snap points combine recording cuts, annotation endpoints and anchors with legacy compatibility', () => {
  const project = {
    duration_ms: 10000,
    videos: [{ start_ms: 0, end_ms: 4000 }, { start_ms: 5000, end_ms: 10000 }],
    annotations: {
      scene: [segment('scene', '室内', 1000, 4000)],
      posture: [],
      habit: [segment('point', '咳嗽', 6500, 6500), segment('ongoing', '看手机', 7000, null)],
    },
  };
  assert.deepEqual(timelineSnapPoints(project, [3000, 3000, -1, Infinity]), [0, 1000, 3000, 4000, 5000, 6500, 7000, 10000]);
  project.annotations.category = [segment('category', '专注', 2000, 8000, { mode: 'overlay' })];
  assert.ok(timelineSnapPoints(project).includes(8000));
});

test('deleting the first category never extends an overlay nested inside it', () => {
  const original = [
    segment('first', '活动', 0, 10000, { mode: 'state' }),
    segment('nested', '社交', 2000, 4000, { mode: 'overlay' }),
  ];
  const result = deleteAnnotation(original, 'first', runs, 'category');
  assert.deepEqual(result, [original[1]]);
  assert.deepEqual(uncoveredSpans(result, [runs[0]]), [
    { start_ms: 0, end_ms: 2000 }, { start_ms: 4000, end_ms: 10000 },
  ]);
  const resumed = [
    segment('earlier', '活动', 0, 10000, { mode: 'state' }),
    segment('resumed-first', '通勤', 15000, 25000, { mode: 'state' }),
    segment('resumed-overlay', '社交', 17000, 19000, { mode: 'overlay' }),
  ];
  assert.deepEqual(deleteAnnotation(resumed, 'resumed-first', runs, 'category'), [resumed[0], resumed[2]]);
});