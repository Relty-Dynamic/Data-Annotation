import test from 'node:test';
import assert from 'node:assert/strict';
import {canAssignAnnotator,canReviewTask} from '../src/tasks/permissions.ts';

test('active admins can annotate without separate grants, inactive accounts cannot',()=>{
 assert.equal(canAssignAnnotator({role:'admin',active:true},[]),true);
 assert.equal(canAssignAnnotator({role:'admin',active:false},[]),false);
 assert.equal(canAssignAnnotator({role:'annotator',active:true},['reviewer']),false);
 assert.equal(canAssignAnnotator({role:'annotator',active:true},['annotator']),true);
});

test('only admins can review their own annotations',()=>{
 const task={annotator_ids:['self'],reviewer_id:'other'};
 assert.equal(canReviewTask({id:'self',role:'admin'},[],task),true);
 assert.equal(canReviewTask({id:'self',role:'annotator'},['leader','reviewer'],task),false);
 assert.equal(canReviewTask({id:'other',role:'annotator'},['reviewer'],task),true);
 assert.equal(canReviewTask({id:'outsider',role:'annotator'},['reviewer'],task),false);
});
