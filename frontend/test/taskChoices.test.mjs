import test from 'node:test';
import assert from 'node:assert/strict';
import {assignedTaskChoices,reviewStage} from '../src/tasks/taskChoices.ts';
const task=(id,extra={})=>({id,video_id:id,annotator_ids:['annotator'],reviewer_id:'reviewer',status:'annotating',deadline:'2026-10-20',rule:{name:'姿势规则'},...extra});
test('workers see only their assigned tasks with distinct annotation and review destinations',()=>{
 const tasks=[task('own'),task('review',{annotator_ids:['someone'],reviewer_id:'annotator',status:'submitted'}),task('other',{annotator_ids:['someone']}),task('deleted',{status:'deleted'}),task('pending',{status:'pending'})];
 const choices=assignedTaskChoices({id:'annotator'},tasks);
 assert.deepEqual(choices.map(c=>c.id).sort(),['own','review']);
 assert.equal(choices.find(c=>c.id==='own').url,'?task=own');assert.equal(choices.find(c=>c.id==='review').url,'?task=review&review=1');
});
test('review assignments remain visible but unavailable until annotations are submitted',()=>{
 const choices=assignedTaskChoices({id:'reviewer'},[task('waiting'),task('ready',{status:'submitted'}),task('returned',{status:'rework'})]);
 assert.equal(choices[0].id,'ready');assert.equal(choices[0].waiting,false);
 assert.equal(choices.find(c=>c.id==='waiting').waiting,true);assert.equal(choices.find(c=>c.id==='returned').waiting,true);
 assert.equal(assignedTaskChoices({id:'reviewer'},[task('self',{annotator_ids:['reviewer']})]).filter(c=>c.kind==='review').length,0);
});
test('task names identify collection and video, and no unrelated metadata is needed',()=>{
 const choice=assignedTaskChoices({id:'annotator'},[task('one',{video:{collection_name:'1001lixin',video_name:'clip.mp4'}})])[0];
 assert.equal(choice.name,'1001lixin · clip.mp4');assert.match(choice.detail,/姿势规则/);
});
test('review stages distinguish pending review, adjudication, completed review and read-only results',()=>{
 assert.match(reviewStage({status:'submitted'}),/待开始/);assert.match(reviewStage({status:'reviewing'}),/进行中/);
 assert.match(reviewStage({status:'reviewing',review_complete:true}),/等待管理员或组长验收/);
 assert.match(reviewStage({status:'adjudicating'}),/裁定/);assert.match(reviewStage({status:'accepted'}),/只读/);
});


test('manager assigned to annotate can also select the review workspace for that task',()=>{
 const tasks=[task('own',{annotator_ids:['self'],reviewer_id:'self',status:'submitted'})];
 for(const [user,roles] of [[{id:'self',role:'admin'},[]],[{id:'self',role:'annotator'},['leader']]]){
  const choices=assignedTaskChoices(user,tasks,roles);
  assert.deepEqual(choices.map(choice=>choice.kind).sort(),['annotate','review']);
  assert.equal(choices.find(choice=>choice.kind==='review').waiting,false);
 }
 assert.deepEqual(assignedTaskChoices({id:'self',role:'annotator'},tasks,['annotator','reviewer']).map(choice=>choice.kind),['annotate']);
});
