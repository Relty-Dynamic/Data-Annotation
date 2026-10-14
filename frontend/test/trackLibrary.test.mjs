import test from 'node:test';
import assert from 'node:assert/strict';
import {projectAllTracks, projectTracks, DEFAULT_TRACK_LABELS, trackName} from '../src/domain.ts';
import {selectionOptions} from '../src/trackLibrary.ts';

const scene={id:'scene',name:'场景新版',labels:['新版'],mode:'state',is_default:true};
const food={id:'food',name:'饮食新版',labels:['吃饭'],mode:'state',is_default:false};
const project={fixed_tracks:['scene','posture','category'],selected_tracks:['scene','food'],
  track_names:{scene:'原场景'},track_labels:{scene:['原室内'],posture:['坐'],category:['工作学习'],habit:[]},
  custom_tracks:[{id:'food',name:'原饮食',labels:['进食'],mode:'state'}],
  annotations:{scene:[{id:'one',label:'原室内',start_ms:0,end_ms:1000}],posture:[],category:[],habit:[],food:[]}};

test('selection affects visible axes without changing saved axes or annotations',()=>{
  const snapshot=structuredClone(project);
  assert.deepEqual(projectTracks(project),['scene','food']);
  assert.deepEqual(projectTracks({...project,selected_tracks:[]}),[]);
  assert.deepEqual(projectAllTracks(project),['scene','posture','category','food']);
  assert.deepEqual(project,snapshot);
});

test('updated library options retain project names and labels; removed axes can be selected again',()=>{
  const options=selectionOptions(project,[scene,food]);
  assert.equal(options.find(track=>track.id==='scene').name,'原场景');
  assert.deepEqual(options.find(track=>track.id==='food').labels,['进食']);
  const removed=selectionOptions(project,[scene]).find(track=>track.id==='food');
  assert.equal(removed.retained,true);
  assert.equal(removed.name,'原饮食');
  assert.equal(trackName(project,'scene'),'原场景');
});

test('new defaults match all three requested label lists',()=>{
  assert.deepEqual(DEFAULT_TRACK_LABELS.scene,['室内','室外','半开放','隧道','电梯','交通工具内','Unknown']);
  assert.deepEqual(DEFAULT_TRACK_LABELS.posture,['坐','站','躺','移动','骑行','交通工具坐','交通工具站','交通工具躺','Unknown']);
  assert.deepEqual(DEFAULT_TRACK_LABELS.category,['工作学习','运动锻炼','饮食','出行','社交','休闲娱乐','休息','日常事务','Unknown']);
});
