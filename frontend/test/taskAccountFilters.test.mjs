import test from 'node:test';
import assert from 'node:assert/strict';
import {filterAccounts} from '../src/tasks/accountFilters.ts';

const accounts=[
  {id:'lead',display_name:'李鑫',role:'annotator',task_roles:['leader','annotator']},
  {id:'worker',display_name:'李明',role:'annotator',task_roles:['annotator']},
  {id:'review',display_name:'Alice Chen',role:'annotator',task_roles:['reviewer']},
  {id:'admin',display_name:'管理员',role:'admin',task_roles:[]},
  {id:'unassigned',display_name:'待授权人员',role:'annotator',task_roles:[]},
];
const ids=rows=>rows.map(row=>row.id);
test('partial name search combines with roles and includes people with multiple roles',()=>{
  assert.deepEqual(ids(filterAccounts(accounts,' 李 ','all')),['lead','worker']);
  assert.deepEqual(ids(filterAccounts(accounts,'李','leader')),['lead']);
  assert.deepEqual(ids(filterAccounts(accounts,'李','annotator')),['lead','worker']);
  assert.deepEqual(ids(filterAccounts(accounts,'李','reviewer')),[]);
  assert.deepEqual(ids(filterAccounts(accounts,' ALICE ','all')),['review']);
});
test('superadmin and unassigned filters remain distinct and clearing filters restores all accounts',()=>{
  assert.deepEqual(ids(filterAccounts(accounts,'','admin')),['admin']);
  assert.deepEqual(ids(filterAccounts(accounts,'','none')),['unassigned']);
  assert.deepEqual(ids(filterAccounts(accounts,'   ','all')),ids(accounts));
});
test('filtering keeps account identity and roles unchanged',()=>{
  const original=structuredClone(accounts);
  const filtered=filterAccounts(accounts,'鑫','leader');
  assert.equal(filtered[0],accounts[0]);
  assert.deepEqual(accounts,original);
});
