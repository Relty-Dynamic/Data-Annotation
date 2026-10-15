import test from 'node:test';
import assert from 'node:assert/strict';
import {createTaskBatch} from '../src/tasks/assignmentFeedback.ts';

test('success appears only after all task creations finish',async()=>{
 const calls=[];
 const message=await createTaskBatch(['a','b'],false,async id=>{calls.push(id)});
 assert.deepEqual(calls,['a','b']);
 assert.equal(message,'分配成功：已创建并分配 2 个任务。');
 assert.equal(await createTaskBatch(['a'],true,async()=>{}),'创建成功：1 个任务待分配。');
});

test('partial failures preserve progress and stop before later requests',async()=>{
 const calls=[];
 await assert.rejects(createTaskBatch(['a','b','c'],false,async id=>{
  calls.push(id);if(id==='b')throw new Error('该视频已有相同类型任务');
 }),/已分配 1\/3 个任务；视频 b 未成功：该视频已有相同类型任务/);
 assert.deepEqual(calls,['a','b']);
 await assert.rejects(createTaskBatch([],false,async()=>assert.fail()),/请先选择合格视频/);
});
