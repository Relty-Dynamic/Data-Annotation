import test from 'node:test';
import assert from 'node:assert/strict';
import {saveBeforeNavigation} from '../src/tasks/saveBeforeNavigation.ts';

test('return navigation happens only after saving and clearing the busy state',async()=>{
 let busy=false,release;const events=[];
 const pending=new Promise(resolve=>{release=resolve});
 const finished=saveBeforeNavigation(async save=>{busy=true;try{await save()}finally{busy=false;events.push('idle')}},async()=>{await pending;events.push('saved')},()=>{assert.equal(busy,false);events.push('navigate')});
 assert.equal(busy,true);assert.deepEqual(events,[]);release();await finished;
 assert.deepEqual(events,['saved','idle','navigate']);
});
test('save failure reported by the busy runner keeps the user on the current page',async()=>{
 const errors=[];let busy=false;
 await saveBeforeNavigation(async save=>{busy=true;try{await save()}catch(error){errors.push(error.message)}finally{busy=false}},async()=>{throw new Error('草稿保存失败')},()=>assert.fail('must not leave unsaved work'));
 assert.equal(busy,false);assert.deepEqual(errors,['草稿保存失败']);
});
test('a rejected or skipped operation never authorizes navigation',async()=>{
 await assert.rejects(saveBeforeNavigation(async save=>save(),async()=>{throw new Error('版本冲突')},()=>assert.fail()),/版本冲突/);
 await saveBeforeNavigation(async()=>{},async()=>assert.fail('busy operation must not run twice'),()=>assert.fail('must not navigate when the runner did not execute'));
});
