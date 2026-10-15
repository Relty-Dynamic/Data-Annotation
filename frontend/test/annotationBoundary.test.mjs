import test from 'node:test';
import assert from 'node:assert/strict';
import {createBoundaryEdit, moveAnnotationBoundary} from '../src/annotationBoundary.ts';
import {assignSegmentLanes} from '../src/domain.ts';

const row = (id, start_ms, end_ms, extra = {}) => ({id, label: id, start_ms, end_ms, lane: 0, ...extra});
const project = (rows, extra = {}) => ({id: 'test', name: 'test', source_dir: null, duration_ms: 100,
  videos: [{id: 'video', start_ms: 0, end_ms: 100}],
  annotations: {scene: rows, posture: [row('other-axis', 0, 100)], category: [], habit: []},
  revision: 1, updated_at: '', ...extra});
const compact = annotations => annotations.scene.map(({id, start_ms, end_ms}) => [id, start_ms, end_ms]);
const move = (p, id, edge, target) => {
  const session = createBoundaryEdit(p, 'scene', id, edge);
  assert.ok(session);
  return moveAnnotationBoundary(session, target);
};

test('moving a right edge shrinks and then swallows consecutive neighbors', () => {
  const p = project([row('a', 0, 10), row('b', 10, 20), row('c', 20, 30), row('d', 30, 40)]);
  assert.deepEqual(compact(move(p, 'a', 'end', 15).annotations), [['a', 0, 15], ['b', 15, 20], ['c', 20, 30], ['d', 30, 40]]);
  assert.deepEqual(compact(move(p, 'a', 'end', 20).annotations), [['a', 0, 20], ['c', 20, 30], ['d', 30, 40]]);
  assert.deepEqual(compact(move(p, 'a', 'end', 35).annotations), [['a', 0, 35], ['d', 35, 40]]);
  assert.deepEqual(compact(move(p, 'a', 'end', 1000).annotations), [['a', 0, 100]]);
});

test('moving a left edge symmetrically consumes all preceding neighbors', () => {
  const p = project([row('a', 0, 10), row('b', 10, 20), row('c', 20, 30), row('d', 30, 40)]);
  assert.deepEqual(compact(move(p, 'd', 'start', 15).annotations), [['a', 0, 10], ['b', 10, 15], ['d', 15, 40]]);
  assert.deepEqual(compact(move(p, 'd', 'start', 10).annotations), [['a', 0, 10], ['d', 10, 40]]);
  assert.deepEqual(compact(move(p, 'd', 'start', -100).annotations), [['d', 0, 40]]);
});

test('shrinking a selected interval extends only its originally touching neighbor', () => {
  const p = project([row('left', 0, 20), row('selected', 20, 40), row('right', 40, 60)]);
  assert.deepEqual(compact(move(p, 'selected', 'start', 30).annotations), [['left', 0, 30], ['selected', 30, 40], ['right', 40, 60]]);
  assert.deepEqual(compact(move(p, 'selected', 'end', 30).annotations), [['left', 0, 20], ['selected', 20, 30], ['right', 30, 60]]);
  assert.deepEqual(compact(move(p, 'selected', 'start', 100).annotations), [['left', 0, 39], ['selected', 39, 40], ['right', 40, 60]]);
  assert.deepEqual(compact(move(p, 'selected', 'end', 0).annotations), [['left', 0, 20], ['selected', 20, 21], ['right', 21, 60]]);
});

test('reversing a drag reconstructs swallowed segments from an immutable snapshot', () => {
  const p = project([row('a', 0, 10), row('b', 10, 20, {label: 'b（备注）', created_by: 'user', updated_at: '2026-10-15'}), row('c', 20, 30)]);
  const original = structuredClone(p);
  const session = createBoundaryEdit(p, 'scene', 'a', 'end');
  const swallowed = moveAnnotationBoundary(session, 25);
  const restored = moveAnnotationBoundary(session, 10);
  assert.equal(swallowed.annotations.scene.length, 2);
  assert.deepEqual(restored.annotations, original.annotations);
  assert.deepEqual(moveAnnotationBoundary(session, 15).annotations.scene[1], {...original.annotations.scene[1], start_ms: 15});
  assert.deepEqual(p, original);
  const left = createBoundaryEdit(p, 'scene', 'c', 'start');
  moveAnnotationBoundary(left, 0);
  assert.deepEqual(moveAnnotationBoundary(left, 20).annotations, original.annotations);
});

test('empty space stays empty on contraction and is replaced only where an extension reaches', () => {
  const p = project([row('a', 0, 10), row('b', 20, 30), row('c', 40, 50)]);
  assert.deepEqual(compact(move(p, 'b', 'end', 25).annotations), [['a', 0, 10], ['b', 20, 25], ['c', 40, 50]]);
  assert.deepEqual(compact(move(p, 'b', 'start', 25).annotations), [['a', 0, 10], ['b', 25, 30], ['c', 40, 50]]);
  assert.deepEqual(compact(move(p, 'a', 'end', 25).annotations), [['a', 0, 25], ['b', 25, 30], ['c', 40, 50]]);
  assert.deepEqual(compact(move(p, 'a', 'end', 35).annotations), [['a', 0, 35], ['c', 40, 50]]);
  assert.deepEqual(compact(move(p, 'c', 'start', 25).annotations), [['a', 0, 10], ['b', 20, 25], ['c', 25, 50]]);
});

test('other tracks and saved child lanes retain all their metadata and positions', () => {
  const child = row('child', 0, 50, {lane: 3, mode: 'overlay', kind: 'interval', created_by_name: '张三'});
  const p = project([row('a', 0, 10), child, row('b', 10, 20)]);
  const changed = move(p, 'a', 'end', 40).annotations;
  assert.deepEqual(changed.scene.find(item => item.id === 'child'), child);
  assert.deepEqual(changed.posture, p.annotations.posture);
  assert.deepEqual(move(p, 'child', 'end', 100).annotations.scene.filter(item => item.id !== 'child'), p.annotations.scene.filter(item => item.id !== 'child'));
});

test('legacy annotations without lanes save their original inferred lanes before moving', () => {
  const p = project([row('a', 0, 10, {lane: undefined}), row('b', 10, 20, {lane: undefined}), row('overlap', 0, 30, {lane: undefined})]);
  const changed = move(p, 'a', 'end', 15).annotations;
  assert.deepEqual(compact(changed), [['a', 0, 15], ['b', 15, 20], ['overlap', 0, 30]]);
  assert.deepEqual(changed.scene[2], {...p.annotations.scene[2], lane: 1});
  assert.deepEqual(changed.scene.map(item => item.lane), [0, 0, 1]);
  assert.ok(p.annotations.scene.every(item => item.lane === undefined));
});

test('legacy child lanes cannot jump to the main row when a resize removes their old overlap', () => {
  const p = project([row('main', 0, 10, {lane: undefined}), row('child', 5, 8, {lane: undefined}), row('next', 10, 20, {lane: undefined})]);
  const originalLanes = new Map(assignSegmentLanes(p.annotations.scene, 100).map(item => [item.id, item.lane]));
  for (const target of [1, 15, 30]) {
    const annotations = move(p, 'main', 'end', target).annotations;
    for (const item of assignSegmentLanes(annotations.scene, 100)) assert.equal(item.lane, originalLanes.get(item.id));
    const nextGesture = createBoundaryEdit({...p, annotations}, 'scene', 'child', 'start');
    assert.equal(nextGesture.lane, 1);
  }
});

test('true recording gaps stop resizing while touching videos and continuity bridges connect', () => {
  const p = project([row('a', 0, 10), row('after-gap', 50, 70)], {
    videos: [{id: 'v1', start_ms: 0, end_ms: 20}, {id: 'v2', start_ms: 20, end_ms: 40}, {id: 'v3', start_ms: 50, end_ms: 100}],
    recording_runs: [{start_ms: 0, end_ms: 40, video_ids: ['v1', 'v2']}, {start_ms: 50, end_ms: 100, video_ids: ['v3']}],
  });
  assert.equal(move(p, 'a', 'end', 80).time, 40);
  assert.equal(move(p, 'after-gap', 'start', 0).time, 50);
  const bridged = {...p, continuity_bridges: [{start_ms: 40, end_ms: 50, previous_video_id: 'v2', next_video_id: 'v3', raw_boundary_error_ms: 10, reason: 'adjacent_camera_second_precision'}]};
  assert.deepEqual(compact(move(bridged, 'a', 'end', 60).annotations), [['a', 0, 60], ['after-gap', 60, 70]]);
  assert.equal(move({...p, recording_runs: undefined}, 'a', 'end', 80).time, 40);
  assert.equal(move({...p, recording_runs: []}, 'a', 'end', 80).time, 40);
  assert.equal(move({...bridged, recording_runs: []}, 'a', 'end', 80).time, 80);
  assert.equal(createBoundaryEdit({...p, recording_runs: [], videos: []}, 'scene', 'a', 'end'), null);
  assert.equal(createBoundaryEdit({...p, annotations: {scene: [row('invalid', 10, 60)]}}, 'scene', 'invalid', 'end'), null);
});

test('open intervals keep their unfinished meaning and expose only a left edge', () => {
  const p = project([row('a', 0, 20), row('open', 20, null, {kind: 'interval'})]);
  assert.equal(createBoundaryEdit(p, 'scene', 'open', 'end'), null);
  assert.deepEqual(compact(move(p, 'open', 'start', 10).annotations), [['a', 0, 10], ['open', 10, null]]);
  assert.deepEqual(compact(move(p, 'open', 'start', 30).annotations), [['a', 0, 30], ['open', 30, null]]);
  assert.deepEqual(compact(move(p, 'a', 'end', 50).annotations), [['a', 0, 50], ['open', 50, null]]);
  assert.deepEqual(compact(move(p, 'a', 'end', 100).annotations), [['a', 0, 100]]);
  assert.equal(move(p, 'open', 'start', 100).time, 99);
});

test('historical zero-width records move within blank space and never overwrite intervals', () => {
  for (const extra of [{kind: 'point'}, {}]) {
    const p = project([row('before', 0, 20), row('legacy', 30, 30, extra), row('another', 40, 40, {kind: 'point'}), row('after', 50, 70)]);
    const session = createBoundaryEdit(p, 'scene', 'legacy', 'end');
    assert.ok(session);
    assert.deepEqual(compact(moveAnnotationBoundary(session, 100).annotations), [['before', 0, 20], ['legacy', 39, 39], ['another', 40, 40], ['after', 50, 70]]);
    assert.deepEqual(compact(moveAnnotationBoundary(session, 0).annotations), [['before', 0, 20], ['legacy', 20, 20], ['another', 40, 40], ['after', 50, 70]]);
    assert.deepEqual(moveAnnotationBoundary(session, 30).annotations, p.annotations);
  }
});

test('extending an interval consumes historical zero-width records only within the swept range', () => {
  const p = project([row('a', 0, 10), row('point', 20, 20, {kind: 'point'}), row('b', 30, 40)]);
  assert.equal(move(p, 'a', 'end', 20).annotations.scene.length, 3);
  assert.deepEqual(compact(move(p, 'a', 'end', 21).annotations), [['a', 0, 21], ['b', 30, 40]]);
  assert.deepEqual(compact(move(p, 'b', 'start', 20).annotations), [['a', 0, 10], ['b', 20, 40]]);
});

test('invalid selections are ignored and nonfinite targets leave the original edge in place', () => {
  const p = project([row('a', 10, 20)]);
  assert.equal(createBoundaryEdit(p, 'scene', 'missing', 'end'), null);
  assert.equal(createBoundaryEdit(p, 'missing', 'a', 'end'), null);
  assert.equal(createBoundaryEdit(project([row('a', 10, 5)]), 'scene', 'a', 'end'), null);
  assert.deepEqual(move(p, 'a', 'end', NaN).annotations, p.annotations);
  assert.equal(move(p, 'a', 'end', 12.8).time, 13);
});

test('a contiguous row remains a complete nonoverlapping partition at every drag position', () => {
  const p = project([row('a', 0, 10), row('b', 10, 25), row('selected', 25, 40), row('c', 40, 70), row('d', 70, 100)]);
  for (const edge of ['start', 'end']) {
    const session = createBoundaryEdit(p, 'scene', 'selected', edge);
    for (let target = -10; target <= 110; target += 1) {
      const {annotations} = moveAnnotationBoundary(session, target);
      const rows = [...annotations.scene].sort((a, b) => a.start_ms - b.start_ms);
      assert.equal(rows[0].start_ms, 0);
      assert.equal(rows.at(-1).end_ms, 100);
      assert.ok(rows.some(item => item.id === 'selected'));
      for (let i = 0; i < rows.length; i += 1) {
        assert.ok(rows[i].end_ms - rows[i].start_ms >= 1);
        if (i) assert.equal(rows[i - 1].end_ms, rows[i].start_ms);
      }
    }
  }
});

test('preview retains the selected identity even when labels match, including custom axes', () => {
  const rows = [row('selected', 0, 10, {label: 'same'}), row('next', 10, 20, {label: 'same'})];
  const p = project([], {annotations: {custom_axis: rows}});
  const session = createBoundaryEdit(p, 'custom_axis', 'selected', 'end');
  rows[1].label = 'later unsaved mutation';
  const {annotations} = moveAnnotationBoundary(session, 15);
  assert.deepEqual(annotations.custom_axis.map(item => [item.id, item.label, item.start_ms, item.end_ms]), [
    ['selected', 'same', 0, 15], ['next', 'same', 15, 20],
  ]);
});
