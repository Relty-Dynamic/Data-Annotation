import test from 'node:test';
import assert from 'node:assert/strict';
import {completedUpload,claimUploadCompletion,uploadStateName,predictionMessage,predictionFailureReason} from '../src/uploadFeedback.ts';
const complete=()=>({id:'helper-one',online:true,platform:'darwin',command:null,report:{busy:false,job:{id:'job-one',state:'ready',prefix:'daily/20261014/1014test',progress:100,detail:'published'}}});
test('only a confirmed, idle and error-free published upload can trigger success',()=>{
 const helper=complete();assert.deepEqual(completedUpload(helper),{key:'helper-one:job-one',prefix:'daily/20261014/1014test'});
 for(const state of ['compressing','uploading','validating','validation_error','error','ready_to_compress'])assert.equal(completedUpload({...helper,report:{job:{...helper.report.job,state,progress:100}}}),null);
 assert.equal(completedUpload({...helper,online:false}),null);
 assert.equal(completedUpload({...helper,command:{id:'command',action:'start'}}),null);
 assert.equal(completedUpload({...helper,report:{...helper.report,busy:true}}),null);
 assert.equal(completedUpload({...helper,report:{...helper.report,error:'采集目录已存在，未覆盖。'}}),null);
 assert.equal(completedUpload({...helper,report:{job:{...helper.report.job,id:undefined}}}),null);
 assert.equal(completedUpload(null),null);
});
test('polling and reopening notify once per job while a different finished job can notify again',()=>{
 const seen=new Set(),helper=complete();assert.ok(claimUploadCompletion(helper,seen));
 for(let index=0;index<5;index++)assert.equal(claimUploadCompletion(structuredClone(helper),seen),null);
 const next={...helper,report:{job:{...helper.report.job,id:'job-two'}}};assert.ok(claimUploadCompletion(next,seen));assert.equal(seen.size,2);
});
test('unpublished progress does not consume the later success notification',()=>{
 const seen=new Set(),helper=complete();assert.equal(claimUploadCompletion({...helper,report:{...helper.report,busy:true}},seen),null);assert.equal(seen.size,0);assert.ok(claimUploadCompletion(helper,seen));
 assert.equal(uploadStateName('validating'),'正在核验并发布');assert.equal(uploadStateName('ready'),'上传完成');assert.equal(uploadStateName('error'),'上传未完成');
});
test('partial prediction and inherited clocks are visible even after material upload succeeds',()=>{
 const partial={sha256:'a'.repeat(64),segments:2,needs_alignment_review:true,batch_status:'partial',validated_records:2,total_records:3,inherited_record_ids:['short']};
 const message=predictionMessage(partial);
 assert.match(message,/部分姿势预标注/);assert.match(message,/2\/3/);assert.match(message,/未完成区间需人工标注/);assert.match(message,/同日其他记录/);assert.match(message,/人工复核/);
 assert.equal(predictionFailureReason('inference_failed'),'IMU 推理未完成');
 assert.equal(predictionMessage(undefined),'');
 assert.equal(predictionMessage({sha256:'a',segments:2,needs_alignment_review:false}),'已携带姿势预标注。');
});
