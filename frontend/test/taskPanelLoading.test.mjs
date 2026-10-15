import test from 'node:test';
import assert from 'node:assert/strict';
import {taskTabs,loadTaskPanelData} from '../src/tasks/panelLoading.ts';

test('all five admin tabs are visible before the roles response arrives',()=>{
 const expected=['任务列表','素材与质检','任务分配','标注规则','验收交付'];
 assert.deepEqual(taskTabs('admin',[],true).map(tab=>tab[1]),expected);
 assert.deepEqual(taskTabs('admin',['annotator','leader','reviewer'],true).map(tab=>tab[1]),expected);
 assert.deepEqual(taskTabs('annotator',[],false),[['tasks','我的任务']]);
 assert.deepEqual(taskTabs('annotator',['reviewer'],false).map(tab=>tab[0]),['tasks','rules']);
 assert.deepEqual(taskTabs('annotator',['leader'],false).map(tab=>tab[1]),expected);
});

test('a pending source catalog does not delay rules, assignments or deliveries',async()=>{
 const requested=[],received=[],pending=new Map();
 const loading=loadTaskPanelData(['leader'],true,path=>{
  requested.push(path);return new Promise(resolve=>pending.set(path,resolve));
 },path=>received.push(path),()=>assert.fail('unexpected source failure'));
 assert.deepEqual(new Set(requested),new Set(['/items','/rules','/users','/roles','/deliveries','/sources','/videos']));
 for(const path of requested.filter(path=>path!=='/sources'))pending.get(path)([]);
 await new Promise(resolve=>setImmediate(resolve));
 assert.ok(received.includes('/rules'));
 assert.ok(received.includes('/users'));
 assert.ok(received.includes('/deliveries'));
 assert.equal(received.includes('/sources'),false);
 pending.get('/sources')([]);await loading;
});

test('storage failures stay separate and other failures do not discard completed reads',async()=>{
 const received=[],sourceErrors=[];
 await loadTaskPanelData(['leader'],true,async path=>{if(path==='/sources')throw new Error('storage offline');return []},path=>received.push(path),error=>sourceErrors.push(error.message));
 assert.deepEqual(sourceErrors,['storage offline']);
 assert.ok(received.includes('/deliveries'));
 await assert.rejects(loadTaskPanelData(['leader'],false,async path=>{if(path==='/users')throw new Error('roster unavailable');return []},path=>received.push(path),()=>{}),/roster unavailable/);
 assert.ok(received.includes('/rules'));
 const requested=[];
 await loadTaskPanelData(['annotator'],false,async path=>{requested.push(path);return []},()=>{},()=>{});
 assert.deepEqual(requested,['/items','/rules']);
});
