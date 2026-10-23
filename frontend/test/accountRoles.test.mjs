import test from 'node:test';
import assert from 'node:assert/strict';
import {accountRole,accountRoleNames,isManager} from '../src/accountRoles.ts';
import {canAssignAnnotator,canAssignReviewer,canReviewTask,canOpenReviewWorkspace} from '../src/tasks/permissions.ts';
import {filterAccounts} from '../src/tasks/accountFilters.ts';

test('three roles are separate from annotation and review capabilities',()=>{
 for(const role of ['member','leader','admin']){
  const account={role,active:true};assert.equal(accountRole(account),role);
  assert.equal(canAssignAnnotator(account,[]),true);assert.equal(canAssignReviewer(account,[]),true);
 }
 const revoked={role:'member',active:true,task_access:false};
 assert.equal(canAssignAnnotator(revoked,['leader']),false);assert.equal(canAssignReviewer(revoked,['reviewer']),false);
});
test('canonical members never inherit a stale leader capability or self-review exception',()=>{
 const task={annotator_ids:['self'],reviewer_id:'self'};
 assert.equal(isManager({role:'member'},['leader']),false);
 assert.equal(canReviewTask({id:'self',role:'member'},['leader'],task),false);
 for(const role of ['leader','admin'])assert.equal(canReviewTask({id:'self',role},[],task),true);
 assert.equal(canReviewTask({id:'self',role:'leader',task_access:false},['leader'],task),false);
});
test('account role filters follow canonical roles rather than archived grants',()=>{
 const accounts=[{id:'member',display_name:'成员',role:'member',task_roles:['leader']},{id:'lead',display_name:'组长',role:'leader',task_roles:[]},{id:'admin',display_name:'管理员',role:'admin',task_roles:[]}];
 assert.deepEqual(filterAccounts(accounts,'','member').map(a=>a.id),['member']);
 assert.deepEqual(filterAccounts(accounts,'','leader').map(a=>a.id),['lead']);
 assert.deepEqual(filterAccounts(accounts,'','admin').map(a=>a.id),['admin']);
});

test('each canonical role has its required Chinese account label',()=>{
 assert.equal(accountRoleNames.admin,'管理员');
 assert.equal(accountRoleNames.leader,'组长');
 assert.equal(accountRoleNames.member,'成员');
});

test('unmigrated accounts retain explicit legacy leader grants and otherwise display as members',()=>{
 for(const account of [{role:'annotator'},{role:'annotator',task_roles:[]},{role:'annotator',task_roles:['reviewer']}]){
  assert.equal(accountRole(account),'member');
 }
 assert.equal(accountRole({role:'annotator',task_roles:['leader']}),'leader');
 for(const role of ['admin','leader','member']){
  assert.equal(accountRole({role,task_roles:['leader']}),role);
 }
 assert.equal(isManager({role:'annotator'}),false);
 assert.equal(isManager({role:'annotator'},[]),false);
 assert.equal(isManager({role:'annotator'},['reviewer']),false);
 assert.equal(isManager({role:'annotator'},['leader']),true);
 assert.equal(isManager({},['leader']),false);
});

test('inactive or revoked accounts cannot be assigned while legacy reviewers keep their existing duty',()=>{
 for(const role of ['admin','leader','member']){
  for(const account of [{role,active:false},{role,active:true,task_access:false}]){
   assert.equal(canAssignAnnotator(account,['leader']),false);
   assert.equal(canAssignReviewer(account,['leader']),false);
  }
 }
 const reviewer={role:'annotator',active:true};
 assert.equal(canAssignReviewer(reviewer,['reviewer']),true);
 assert.equal(canAssignReviewer(reviewer,[]),false);
 assert.equal(canAssignAnnotator(reviewer,['reviewer']),false);
});

test('reviewers can open every review or final stage while self-review and revoked access stay blocked',()=>{
 const reviewer={id:'reviewer',role:'member'};
 const task={annotator_ids:['worker'],reviewer_id:'reviewer'};
 for(const status of ['submitted','reviewing','adjudicating','accepted','delivered']){
  assert.equal(canOpenReviewWorkspace(reviewer,[],{...task,status}),true,status);
  assert.equal(canOpenReviewWorkspace({id:'outsider',role:'member'},[],{...task,status}),false,status);
  assert.equal(canOpenReviewWorkspace({id:'worker',role:'member'},['leader'],{...task,reviewer_id:'worker',status}),false,status);
  assert.equal(canOpenReviewWorkspace({id:'reviewer',role:'leader',task_access:false},[],{...task,status}),false,status);
 }
});
