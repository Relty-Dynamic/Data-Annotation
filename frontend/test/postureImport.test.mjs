import test from 'node:test';
import assert from 'node:assert/strict';
import { importPostureTimeline } from '../src/postureImport.ts';
import { DEFAULT_TRACK_LABELS, normalizeStateSeams } from '../src/domain.ts';

const origin = '2026-10-13T12:22:57.000+08:00';
const segment = (start_ms, end_ms, label = '坐', extra = {}) => ({start_ms, end_ms, label, ...extra});
const project = (extra = {}) => ({
  id: 'p', name: '测试项目', duration_ms: 12000, recording_start: origin,
  videos: [{id:'v1',start_ms:0,end_ms:4000},{id:'v2',start_ms:6000,end_ms:12000}],
  // A bridged run must never turn non-video time into imported annotations.
  recording_runs: [{start_ms:0,end_ms:12000}],
  fixed_tracks: ['scene','posture','category','habit'],
  track_labels: DEFAULT_TRACK_LABELS,
  annotations: {scene:[],posture:[],category:[],habit:[]},
  ...extra,
});
const file = (segments, extra = {}) => ({
  schema_version:2, export_profile:'imu_fusion_prediction_timeline/1.0.0', axis:'posture',
  timebase:{unit:'ms',origin:'recording_datetime',recording_start:origin},
  segments, ...extra,
});
const imported = (data, target = project()) => importPostureTimeline(JSON.stringify(data), target);
const ranges = result => result.annotations.posture.map(s=>[s.label,s.start_ms,s.end_ms,s.lane]);

test('prediction import keeps timestamped video intersections and splits real gaps', () => {
  const target = project(), original = structuredClone(target);
  const result = imported(file([segment(1000,14000)]),target);
  assert.deepEqual(ranges(result),[['坐',1000,4000,0],['坐',6000,12000,0]]);
  assert.equal(result.importedDurationMs,9000);
  assert.equal(result.discardedDurationMs,4000);
  assert.equal(result.importedCount,2);
  assert.deepEqual(target,original);
});

test('absolute origins convert to project coordinates without applying camera or recording offsets', () => {
  const data=file([segment(2000,3500,'骑行')],{
    timebase:{unit:'ms',recording_start:'2026-10-13T04:22:56.000Z'},
    recordings:[{id:'r',camera_minus_imu_offset_s:24.5,start_ms:0,end_ms:12000}],
    provenance:{clock:'original IMU clock; video offset never applied to prediction timestamps'},
  });
  assert.deepEqual(ranges(imported(data)),[['骑行',1000,2500,0]]);
});

test('source holes stay blank even when video exists; skipped videos are excluded', () => {
  const result=imported(file([segment(1000,2000,'站'),segment(6500,7500,'移动')]));
  assert.deepEqual(ranges(result),[['站',1000,2000,0],['移动',6500,7500,0]]);
  assert.equal(result.importedDurationMs,2000);
});

test('replace only imported main-row coverage, retain holes, notes, other tracks and saved child rows', () => {
  const main=segment(0,12000,'站（已有备注）',{id:'old',lane:0,created_by:'alice'});
  const child=segment(0,12000,'躺',{id:'child',lane:7});
  const scene=[segment(0,4000,'室内',{id:'scene',lane:0})];
  const target=project({annotations:{scene,posture:[main,child],category:[],habit:[]}});
  const before=structuredClone(target);
  const result=imported(file([segment(1000,2000,'坐'),segment(7000,8000,'移动')]),target);
  assert.deepEqual(result.annotations.posture.filter(s=>s.lane===0).map(s=>[s.label,s.start_ms,s.end_ms]),[
    ['站（已有备注）',0,1000],['坐',1000,2000],['站（已有备注）',2000,7000],
    ['移动',7000,8000],['站（已有备注）',8000,12000],
  ]);
  assert.deepEqual(result.annotations.posture.find(s=>s.id==='child'),child);
  assert.equal(result.annotations.posture.find(s=>s.id==='old').created_by,'alice');
  assert.equal(result.annotations.scene,scene);
  assert.equal(result.replacedCount,1);
  assert.equal(new Set(result.annotations.posture.map(s=>s.id)).size,result.annotations.posture.length);
  assert.deepEqual(target,before);
});

test('matching labels across genuine and subsecond non-video gaps never bridge during draft normalization', () => {
  const target=project({videos:[{id:'v1',start_ms:0,end_ms:4000},{id:'v2',start_ms:4300,end_ms:12000}]});
  const result=imported(file([segment(0,12000)]),target);
  const normalized=normalizeStateSeams(result.annotations,[{start_ms:4000,end_ms:4300,next_video_id:'v2'}],target.videos,[],12000);
  assert.deepEqual(normalized.posture.map(s=>[s.start_ms,s.end_ms]),[[0,4000],[4300,12000]]);
});

test('touching or overlapping video files contribute coverage once and millisecond precision survives', () => {
  const target=project({videos:[{start_ms:6000,end_ms:12000},{start_ms:0,end_ms:4000},{start_ms:3000,end_ms:7000}]});
  const result=imported(file([segment(125,11876,'骑行')]),target);
  assert.deepEqual(ranges(result),[['骑行',125,11876,0]]);
  assert.equal(result.importedDurationMs,11751);
});

test('invalid, unfinished, wrong-axis, unknown-label and child-row imports fail without mutating the project', () => {
  const target=project(),before=structuredClone(target);
  for (const data of [file([]),file([segment(0,null)]),file([segment(-1,1000)]),
    file([segment(0,1000,'蹦跳')]),file([segment(0,1000)],{axis:'scene'}),
    file([segment(0,1000)],{timebase:{unit:'s',recording_start:origin}}),
    file([segment(0,1000)],{timebase:{unit:'ms'}}),
    file([segment(0,1000,{})]),file([segment(0,1000,'坐',{lane:1})]),
    file([segment(0,1000,'坐',{kind:'point'})]),file([segment(0,1.5)]),
    file([segment(0,1000)],{timebase:{recording_start:'invalid'}})]) {
    assert.throws(()=>imported(data,target));
    assert.deepEqual(target,before);
  }
  assert.throws(()=>importPostureTimeline('bad json',target),/有效的 JSON/);
  assert.throws(()=>imported(file([segment(0,1000)]),project({selected_tracks:['scene']})),/勾选姿势轴/);
});

test('files entirely outside video coverage or located on a gap do not change the project', () => {
  for (const records of [[segment(4000,6000)],[segment(12000,14000)]]) {
    const target=project(),before=structuredClone(target);
    assert.throws(()=>imported(file(records),target),/没有.*(重叠|交集)/);
    assert.deepEqual(target,before);
  }
});

test('ambiguous overlapping predictions are rejected as a whole', () => {
  const target=project(),before=structuredClone(target);
  assert.throws(()=>imported(file([segment(0,2000),segment(1000,3000,'站')]),target),/重叠/);
  assert.deepEqual(target,before);
});

test('UTF-8 BOM and dated imports crossing midnight retain their true timestamps', () => {
  const target=project({recording_start:'2026-10-13T23:59:59+08:00'});
  const data=file([segment(0,2500,'移动')],{timebase:{unit:'ms',recording_start:'2026-10-14T00:00:00+08:00'}});
  assert.deepEqual(ranges(importPostureTimeline('\uFEFF'+JSON.stringify(data),target)),[['移动',1000,3500,0]]);
});
