import test from 'node:test';
import assert from 'node:assert/strict';
import { annotationsEqual, applyAnnotationRange, arrangeLanes, formatTime, isPointSegment, locateVideo, normalizeStateSeams, parseTime, projectTracks, switchState, trackName } from '../src/domain.ts';

const segment = (id, label, start_ms, end_ms) => ({ id, label, start_ms, end_ms });
const compact = (segments) => segments.map(({ label, start_ms, end_ms }) => [label, start_ms, end_ms]);

test('project custom axes appear by name and take part in draft comparison',()=>{
  const axis='custom_'+'a'.repeat(32);
  const project={custom_tracks:[{id:axis,name:'环境',mode:'state',labels:['安静']}]};
  assert.deepEqual(projectTracks(project),['scene','posture','category','habit',axis]);
  assert.equal(trackName(project,axis),'环境');
  const first={scene:[],posture:[],category:[],habit:[],[axis]:[]};
  const second={...first,[axis]:[segment('one','安静',0,1000)]};
  assert.equal(annotationsEqual(first,second),false);
  const replaced=applyAnnotationRange([segment('old','安静',0,1000)],250,750,'嘈杂',[{start_ms:0,end_ms:1000}],axis,undefined,true);
  assert.deepEqual(compact(replaced),[['安静',0,250],['嘈杂',250,750],['安静',750,1000]]);
});

test('state switch splits the current interval and preserves later state changes', () => {
  const original = [segment('a', '室内', 0, 20000), segment('b', '室外', 20000, 40000), segment('c', '室内', 40000, 60000)];
  const copy = structuredClone(original);
  const changed = switchState(original, 10000, '室外', 60000);
  assert.deepEqual(compact(changed), [['室内', 0, 10000], ['室外', 10000, 40000], ['室内', 40000, 60000]]);
  assert.deepEqual(original, copy, 'editing must not mutate the stored project');
  assert.equal(changed[2].id, 'c');
});

test('switching a label at an existing boundary replaces it without duplicating timestamps', () => {
  const original = [segment('a', '站', 0, 10000), segment('b', '坐', 10000, 20000), segment('c', '动', 20000, 30000)];
  const changed = switchState(original, 10000, '动', 30000);
  assert.deepEqual(compact(changed), [['站', 0, 10000], ['动', 10000, 30000]]);
  assert.equal(changed[1].id, 'b');
});

test('same-label click does not introduce an unnecessary switch', () => {
  const original = [segment('a', '室内', 0, 20000)];
  const changed = switchState(original, 5000, '室内', 20000);
  assert.deepEqual(changed, original);
});

test('switch before first annotation leaves earlier time unannotated', () => {
  const changed = switchState([segment('b', '坐', 10000, 20000)], 5000, '站', 20000);
  assert.deepEqual(compact(changed), [['站', 5000, 10000], ['坐', 10000, 20000]]);
});

test('end-of-video state click does not create a zero-length state', () => {
  const original = [segment('a', '室内', 0, 20000)];
  assert.deepEqual(switchState(original, 20000, '室外', 20000), original);
  assert.deepEqual(switchState(original, 30000, '室外', 20000), original);
});

test('overlapping behavior labels pack into reusable lanes and stay start-sorted', () => {
  const segments = [segment('d', '看手机', 10000, 12000), segment('b', '喝水', 1000, 2000), segment('a', '吸烟', 0, 10000), segment('c', '走路', 2000, 3000)];
  const packed = arrangeLanes(segments, 12000);
  assert.deepEqual(packed.map(({ segment, lane }) => [segment.id, lane]), [['a', 0], ['b', 1], ['c', 1], ['d', 0]]);
  assert.deepEqual(segments.map(({ id }) => id), ['d', 'b', 'a', 'c']);
});

test('ongoing behavior reserves its lane until duration and same-time labels never overwrite', () => {
  const packed = arrangeLanes([segment('a', '吸烟', 0, null), segment('b', '站立', 0, 5000), segment('c', '看手机', 1000, 5000)], 10000);
  assert.equal(new Set(packed.map(({ lane }) => lane)).size, 3);
  assert.equal(packed.length, 3);
});

test('global video time resolves exact boundaries to the following video', () => {
  const videos = [{ id: 'a', start_ms: 0, end_ms: 10000 }, { id: 'b', start_ms: 10000, end_ms: 20000 }];
  assert.equal(locateVideo(videos, 9999)?.id, 'a');
  assert.equal(locateVideo(videos, 10000)?.id, 'b');
  assert.equal(locateVideo(videos, 20000)?.id, 'b');
  assert.equal(locateVideo([], 0), undefined);
});

test('millisecond time formatting and parsing round-trip over an hour', () => {
  for (const ms of [0, 1, 999, 59999, 60000, 3661234, 360000000]) {
    assert.equal(parseTime(formatTime(ms)), ms);
  }
  assert.equal(parseTime('12.125'), 12125);
  assert.equal(parseTime('02:30.050'), 150050);
  for (const invalid of ['', '-1', '1:70', '1:60:00', '1:20:80', '1.1234', 'Infinity', 'abc']) assert.equal(parseTime(invalid), null);
});


test('point behaviors keep exact equal timestamps and simultaneous points occupy separate lanes', () => {
  const first = { ...segment('p1', '咳嗽', 1234, 1234), kind: 'point' };
  const second = { ...segment('p2', '打喷嚏', 1234, 1234), kind: 'point' };
  const after = { ...segment('p3', '眨眼', 1235, 1235), kind: 'point' };
  const packed = arrangeLanes([after, second, first], 5000);
  assert.deepEqual(packed.map(({ segment, lane }) => [segment.id, lane]), [['p1', 0], ['p2', 1], ['p3', 0]]);
  assert.equal(first.start_ms, first.end_ms);
  assert.equal(isPointSegment(first), true);
  assert.equal(isPointSegment({ ...first, kind: 'interval' }), false);
  assert.equal(isPointSegment(segment('legacy', '瞬时行为', 100, 100)), true);
});

test('point behavior during an interval is not hidden in the interval lane', () => {
  const interval = { ...segment('i1', '走路', 0, 10000), kind: 'interval' };
  const point = { ...segment('p1', '咳嗽', 5000, 5000), kind: 'point' };
  const packed = arrangeLanes([point, interval], 10000);
  assert.deepEqual(packed.map(({ segment, lane }) => [segment.id, lane]), [['i1', 0], ['p1', 1]]);
});

test('seeking into an unrecorded gap resolves to the next recording', () => {
  const videos = [{ id: 'a', start_ms: 0, end_ms: 10000 }, { id: 'b', start_ms: 20000, end_ms: 30000 }];
  assert.equal(locateVideo(videos, 9999)?.id, 'a');
  assert.equal(locateVideo(videos, 10000)?.id, 'b');
  assert.equal(locateVideo(videos, 15000)?.id, 'b');
  assert.equal(locateVideo(videos, 20000)?.id, 'b');
});

test('new state stops at the recording gap instead of covering unrecorded time', () => {
  const videos = [{ start_ms: 0, end_ms: 10000 }, { start_ms: 20000, end_ms: 30000 }];
  const changed = switchState([], 5000, '室内', 30000, videos);
  assert.deepEqual(compact(changed), [['室内', 5000, 10000]]);
  assert.deepEqual(switchState(changed, 15000, '室外', 30000, videos), changed);
});

test('state continues over touching clips, stops at the first gap, and preserves later states', () => {
  const videos = [{ start_ms: 0, end_ms: 10000 }, { start_ms: 10000, end_ms: 20000 }, { start_ms: 30000, end_ms: 40000 }];
  const later = [segment('later', '室外', 35000, 40000)];
  const changed = switchState(later, 5000, '室内', 40000, videos);
  assert.deepEqual(compact(changed), [['室内', 5000, 20000], ['室外', 35000, 40000]]);
  const resumed = switchState(changed, 30000, '室内', 40000, videos);
  assert.deepEqual(compact(resumed), [['室内', 5000, 20000], ['室内', 30000, 35000], ['室外', 35000, 40000]]);
});


test('lost save acknowledgement comparison accepts server normalization but detects actual changes', () => {
  const local = { scene: [segment('s', '室内', 0, 10000)], posture: [], habit: [{ ...segment('b', '咳嗽', 500, 500), kind: 'point' }, { ...segment('a', '眨眼', 500, 500), kind: 'point' }] };
  const remote = structuredClone(local);
  remote.scene[0].kind = 'interval';
  remote.habit.reverse();
  assert.equal(annotationsEqual(local, remote), true);
  remote.scene[0].end_ms = 9000;
  assert.equal(annotationsEqual(local, remote), false);
  remote.scene[0].end_ms = 10000;
  remote.habit[0].label = '其他窗口编辑';
  assert.equal(annotationsEqual(local, remote), false);
});


test('a state spans confirmed file seams but still stops at a real recording break', () => {
  // Two source files have a 234 ms second-resolution timestamp seam.
  const runs = [{ start_ms: 0, end_ms: 1200000 }, { start_ms: 1255000, end_ms: 1800000 }];
  const changed = switchState([], 1000, '室内', 1800000, runs);
  assert.deepEqual(compact(changed), [['室内', 1000, 1200000]]);
  const next = switchState(changed, 600100, '室外', 1800000, runs);
  assert.deepEqual(compact(next), [['室内', 1000, 600100], ['室外', 600100, 1200000]]);
  assert.deepEqual(switchState(next, 1220000, '室内', 1800000, runs), next);
});

test('repeating the same state after a confirmed file seam keeps one annotation', () => {
  const runs = [{ start_ms: 0, end_ms: 1200000 }];
  const original = [segment('continuous', '坐', 0, 1200000)];
  assert.deepEqual(switchState(original, 600234, '坐', 1200000, runs), original);
});


const seamVideos = [
  { id: 'v1', start_ms: 0, end_ms: 10000 },
  { id: 'v2', start_ms: 10234, end_ms: 20000 },
  { id: 'v3', start_ms: 20267, end_ms: 30000 },
];
const stateBridges = [
  { start_ms: 10000, end_ms: 10234, previous_video_id: 'v1', next_video_id: 'v2', raw_boundary_error_ms: 234, reason: 'adjacent_camera_second_precision' },
  { start_ms: 20000, end_ms: 20267, previous_video_id: 'v2', next_video_id: 'v3', raw_boundary_error_ms: 267, reason: 'adjacent_camera_second_precision' },
];

test('custom state seam repair matches server normalization without changing event rows', () => {
  const stateId = 'custom_' + 'a'.repeat(32), eventId = 'custom_' + 'b'.repeat(32);
  const input = { scene: [], posture: [], category: [], habit: [],
    [stateId]: [segment('first', '安静', 0, 10000), segment('second', '安静', 10234, 15000)],
    [eventId]: [segment('first', '响铃', 0, 10000), segment('second', '响铃', 10234, 15000)] };
  const tracks = [{ id: stateId, name: '环境', mode: 'state', labels: ['安静'] },
    { id: eventId, name: '干扰', mode: 'event', labels: [] }];
  const result = normalizeStateSeams(input, stateBridges, seamVideos, tracks);
  assert.deepEqual(result[stateId], [segment('first', '安静', 0, 15000)]);
  assert.deepEqual(result[eventId], input[eventId]);
});

test('state seam repair retains the first ID and leaves overlapping behavior events untouched', () => {
  const input = {
    scene: [segment('right', '室内', 11000, 20000), segment('left', '室内', 0, 10000)],
    posture: [segment('sit-left', '坐', 0, 10000), segment('sit-right', '坐', 10234, 15000)],
    habit: [segment('drink-left', '喝水', 0, 10000), segment('drink-right', '喝水', 10234, 15000), { ...segment('cough', '咳嗽', 11000, 11000), kind: 'point' }],
  };
  const original = structuredClone(input);
  const result = normalizeStateSeams(input, stateBridges, seamVideos);
  assert.deepEqual(result.scene, [segment('left', '室内', 0, 20000)]);
  assert.deepEqual(result.posture, [segment('sit-left', '坐', 0, 15000)]);
  assert.strictEqual(result.habit, input.habit);
  assert.deepEqual(input, original, 'normalization must not mutate input arrays or segment objects');
  assert.deepEqual(normalizeStateSeams(result, stateBridges, seamVideos), result);
});

test('state seam repair preserves real gaps, intentional unlabeled spans, and label changes', () => {
  const cases = [
    [segment('a', '室内', 0, 9999), segment('b', '室内', 11000, 15000)],
    [segment('a', '室内', 0, 10001), segment('b', '室内', 11000, 15000)],
    [segment('a', '室内', 0, 10000), segment('b', '室内', 10233, 15000)],
    [segment('a', '室内', 0, 10000), segment('b', '室内', 20000, 25000)],
    [segment('a', '室内', 0, 10000), segment('b', '室内', 20267, 30000)],
    [segment('a', '室内', 0, 10000), segment('b', '室外', 10234, 20000)],
    [segment('a', '室内', 0, 10000), segment('b', '室外', 10234, 12000), segment('c', '室内', 12000, 20000)],
  ];
  for (const scene of cases) {
    assert.deepEqual(normalizeStateSeams({ scene, posture: [], habit: [] }, stateBridges, seamVideos).scene, scene);
  }
  const unconfirmed = { scene: [segment('a', '室内', 0, 10000), segment('b', '室内', 10234, 20000)], posture: [], habit: [] };
  assert.deepEqual(normalizeStateSeams(unconfirmed, [], seamVideos), { ...unconfirmed, category: [] });
  assert.deepEqual(normalizeStateSeams(unconfirmed, stateBridges, []), { ...unconfirmed, category: [] });
});

test('touching equal states merge consistently with server normalization without filling ordinary gaps', () => {
  const input = { scene: [segment('a', '室内', 0, 5000), segment('b', '室内', 5000, 10000), segment('c', '室内', 11000, 15000)], posture: [], habit: [] };
  assert.deepEqual(normalizeStateSeams(input).scene, [segment('a', '室内', 0, 10000), segment('c', '室内', 11000, 15000)]);
});

test('touching states from different creators retain separate attribution', () => {
  const first = {...segment('a', '室内', 0, 5000), created_by:'user-a', created_at:'2026-09-30T00:00:00Z'};
  const second = {...segment('b', '室内', 5000, 10000), created_by:'user-b', created_at:'2026-09-30T01:00:00Z'};
  const input = {scene:[first,second],posture:[],habit:[]};
  assert.deepEqual(normalizeStateSeams(input).scene,[first,second]);
});

test('several repaired file seams form one continuous state with stable normalization', () => {
  const input = { scene: [segment('a', '室内', 0, 10000), segment('b', '室内', 10234, 20000), segment('c', '室内', 21000, 30000)], posture: [], habit: [] };
  const result = normalizeStateSeams(input, stateBridges, seamVideos);
  assert.deepEqual(result.scene, [segment('a', '室内', 0, 30000)]);
  assert.deepEqual(normalizeStateSeams(result, stateBridges, seamVideos), result);
});

test('a lost save response compares repaired state semantics without overlooking a remote edit', () => {
  const local = { scene: [segment('a', '室内', 0, 10000), segment('b', '室内', 11000, 20000)], posture: [], habit: [] };
  const remote = { scene: [{ ...segment('a', '室内', 0, 20000), kind: 'interval' }], posture: [], habit: [] };
  const canonical = (value) => normalizeStateSeams(value, stateBridges, seamVideos);
  assert.equal(annotationsEqual(canonical(local), canonical(remote)), true);
  remote.scene[0].end_ms = 19000;
  assert.equal(annotationsEqual(canonical(local), canonical(remote)), false);
});
