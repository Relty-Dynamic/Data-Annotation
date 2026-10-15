import test from 'node:test';
import assert from 'node:assert/strict';
import {taskFromSearch,taskRequestUrl} from '../src/tasks/transport.ts';
const id='a'.repeat(32),project='b'.repeat(32);
test('disabled task context preserves all legacy URLs',()=>{assert.equal(taskRequestUrl('/api/projects/'+project+'/draft',null),'/api/projects/'+project+'/draft');assert.equal(taskFromSearch('?task=../../NAS'),null)});
test('task context maps workbench and cache routes, not account or task APIs',()=>{assert.equal(taskRequestUrl('/api/projects/'+project+'/draft',id),'/api/tasks/items/'+id+'/workspace/project/draft');assert.equal(taskRequestUrl('/api/session-media/'+project+'/version/v0001?fast=true',id),'/api/tasks/items/'+id+'/workspace/session-media/version/v0001?fast=true');for(const url of ['/api/auth/me','/api/health','/api/tasks/items'])assert.equal(taskRequestUrl(url,id),url)});
