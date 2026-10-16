import test from 'node:test';
import assert from 'node:assert/strict';
import {canAssignAnnotator,canAssignReviewer,canReviewTask} from '../src/tasks/permissions.ts';

test('active admins can annotate without separate grants, inactive accounts cannot',()=>{
 assert.equal(canAssignAnnotator({role:'admin',active:true},[]),true);
 assert.equal(canAssignAnnotator({role:'admin',active:false},[]),false);
 assert.equal(canAssignAnnotator({role:'annotator',active:true},['reviewer']),false);
 assert.equal(canAssignAnnotator({role:'annotator',active:true},['annotator']),true);
});

test('admins and leaders can self review while ordinary annotators and reviewers cannot',()=>{
 const task={annotator_ids:['self'],reviewer_id:'other'};
 assert.equal(canReviewTask({id:'self',role:'admin'},[],task),true);
 assert.equal(canReviewTask({id:'self',role:'annotator'},['leader'],task),true);
 assert.equal(canReviewTask({id:'other',role:'annotator'},['reviewer'],task),true);
 assert.equal(canReviewTask({id:'outsider',role:'annotator'},['reviewer'],task),false);
});

test('review workbench entry requires an eligible task and existing reviewer permissions',async()=>{
 const {canOpenReviewWorkspace}=await import('../src/tasks/permissions.ts');
 const task={annotator_ids:['annotator'],reviewer_id:'reviewer',status:'submitted'};
 assert.equal(canOpenReviewWorkspace({id:'reviewer',role:'annotator'},['reviewer'],task),true);
 assert.equal(canOpenReviewWorkspace({id:'outsider',role:'annotator'},['reviewer'],task),false);
 assert.equal(canOpenReviewWorkspace({id:'annotator',role:'annotator'},['reviewer','leader'],task),true);
 assert.equal(canOpenReviewWorkspace({id:'annotator',role:'admin'},[],task),true);
 for(const status of ['pending','annotating','rework','deleted'])assert.equal(canOpenReviewWorkspace({id:'reviewer',role:'annotator'},['reviewer'],{...task,status}),false);
});

test('task workers use rule axes while admins retain legacy definition maintenance',async()=>{
 const {canUseLegacyTrackTools}=await import('../src/tasks/permissions.ts');
 assert.equal(canUseLegacyTrackTools({role:'annotator'},true),false);
 assert.equal(canUseLegacyTrackTools({role:'admin'},true),true);
 assert.equal(canUseLegacyTrackTools({role:'annotator'},false),true);
});


test('leader role alone qualifies for annotation and reviewer assignment',()=>{
 const leader={active:true,role:'annotator'};
 assert.equal(canAssignAnnotator(leader,['leader']),true);
 assert.equal(canAssignReviewer(leader,['leader']),true);
 assert.equal(canAssignReviewer({...leader,active:false},['leader']),false);
 assert.equal(canAssignReviewer(leader,['annotator']),false);
 const own={annotator_ids:['self'],reviewer_id:'self'};
 assert.equal(canReviewTask({id:'self',role:'annotator'},['annotator','reviewer'],own),false);
});
