import test from 'node:test';
import assert from 'node:assert/strict';
import { parseExternalTimeline } from '../src/externalTimeline.ts';

const project = {duration_ms:10000, recording_start:'2026-09-25T09:00:00+08:00'};
const segment = (start_ms=0,end_ms=1000,label='坐') => ({start_ms,end_ms,label});
const parse = (data, track='posture') => parseExternalTimeline(JSON.stringify(data),track,project,'外部.json');

test('standard export aligns absolute recording origin, clips and reports outside records',()=>{
  const data={axis:'posture',timebase:{unit:'ms',recording_start:'2026-09-25T08:59:59+08:00'},segments:[segment(0,2000),segment(2000,15000),segment(20000,21000)]};
  const original=structuredClone(data);
  const value=parse(data);
  assert.deepEqual(value.segments.map(s=>[s.start_ms,s.end_ms]),[[0,1000],[1000,10000]]);
  assert.match(value.alignment,/-1 秒/);
  assert.equal(value.warnings.length,2);
  assert.deepEqual(data,original);
});
test('timezone-equivalent and unzoned origins use the same alignment',()=>{
  for(const recording_start of ['2026-09-25T01:00:00Z','2026-09-25 09:00:00']) {
    assert.equal(parse({timebase:{recording_start},segments:[segment()]}).segments[0].start_ms,0);
  }
});
test('arrays preserve arbitrary external labels, overlapping intervals and duplicate ids independently',()=>{
  const result=parse([{...segment(400,900,'模型-A'),id:'same'},{...segment(0,700,'模型-B'),id:'same'}],'category');
  assert.deepEqual(result.segments.map(s=>s.label),['模型-B','模型-A']);
  assert.equal(new Set(result.segments.map(s=>s.id)).size,2);
  assert.match(result.alignment,/项目起点/);
});
test('habit supports points at project end and ongoing intervals',()=>{
  const result=parse([{label:'喝水',start_ms:10000,kind:'point'},segment(0,null,'抽烟')],'habit');
  assert.equal(result.segments[0].end_ms,null);
  assert.equal(result.segments[1].end_ms,10000);
  assert.equal(result.segments[1].kind,'point');
});
test('invalid records fail atomically with the row number',()=>{
  for(const invalid of [segment(-1,10),segment(2,1),segment('0',100),segment(0.1,1),segment(0,1,''),{label:'坐',start_ms:0},null]) {
    assert.throws(()=>parse([segment(),invalid]),/第 2 条/);
  }
  assert.throws(()=>parse([{...segment(),kind:'point'}],'habit'),/起止时间必须相同/);
  assert.throws(()=>parse([segment(2,2)]),/请选择事件轴/);
});
test('custom event axes accept points while custom state axes reject them',()=>{
  const customProject={...project,custom_tracks:[
    {id:'custom_'+'a'.repeat(32),name:'事件',mode:'event',labels:[]},
    {id:'custom_'+'b'.repeat(32),name:'环境',mode:'state',labels:['安静']},
  ]};
  const point=[{label:'门铃',start_ms:500,kind:'point'}];
  assert.equal(parseExternalTimeline(JSON.stringify(point),customProject.custom_tracks[0].id,customProject,'x').segments[0].kind,'point');
  assert.throws(()=>parseExternalTimeline(JSON.stringify(point),customProject.custom_tracks[1].id,customProject,'x'),/请选择事件轴/);
});
test('reject invalid JSON, unsupported shapes, units, origins and disjoint files',()=>{
  assert.throws(()=>parseExternalTimeline('not json','scene',project,'x'),/有效的 JSON/);
  assert.throws(()=>parse({foo:[]}),/segments/);
  assert.throws(()=>parse({timebase:{unit:'s'},segments:[]}),/毫秒/);
  assert.throws(()=>parse({timebase:{recording_start:'bad'},segments:[]}),/录制起点无效/);
  assert.throws(()=>parse([segment(11000,12000)]),/没有交集/);
  assert.throws(()=>parseExternalTimeline(JSON.stringify({timebase:{recording_start:project.recording_start},segments:[segment()]}),'scene',{duration_ms:1000},'x'),/当前项目没有有效录制起点/);
});
test('empty and mismatched axes are visible warnings and user selection is honored',()=>{
  const result=parse({axis:'scene',segments:[]},'category');
  assert.equal(result.track,'category');
  assert.equal(result.warnings.length,2);
});
test('UTF-8 BOM files are accepted',()=>{
  assert.equal(parseExternalTimeline('\uFEFF'+JSON.stringify([segment()]),'posture',project,'x').segments.length,1);
});
