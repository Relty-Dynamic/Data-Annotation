import test from 'node:test';
import assert from 'node:assert/strict';
import {stageLabel,elapsedLabel,stageStatus} from '../src/uploadStageMetrics.ts';
test('stage labels distinguish real processing from completion and total wall time',()=>{
 assert.equal(stageStatus({state:'running',completed:0,total:20}),'进行中 · 0/20 段');
 assert.equal(stageStatus({state:'done',completed:20,total:20}),'完成 · 20/20 段');
 assert.equal(stageStatus({state:'stopped',completed:0,total:1}),'已停止');
 assert.equal(stageLabel('verify'),'云端校验');
 assert.equal(elapsedLabel(125000),'2 分 5 秒');
 assert.equal(elapsedLabel(990),'0 秒');
});
