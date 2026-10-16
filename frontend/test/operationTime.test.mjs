import test from 'node:test';
import assert from 'node:assert/strict';
import {formatOperationTime} from '../src/operationTime.ts';
test('operation times use a common Beijing date and clock with seconds only',()=>{
 assert.equal(formatOperationTime('2026-10-16T06:07:08.987654+00:00'),'2026-10-16 14:07:08');
 assert.equal(formatOperationTime('2026-10-16T14:07:08.987+08:00'),'2026-10-16 14:07:08');
 assert.equal(formatOperationTime('2026-10-16 14:07:08'),'2026-10-16 14:07:08');
});
test('offsets and midnight are converted without host timezone or locale formatting',()=>{
 assert.equal(formatOperationTime('2026-10-15T16:00:00Z'),'2026-10-16 00:00:00');
 assert.equal(formatOperationTime('2026-01-01T00:00:00-05:00'),'2026-01-01 13:00:00');
 assert.equal(formatOperationTime('2026-12-31T20:59:59.999Z'),'2027-01-01 04:59:59');
});
test('missing or invalid times do not invent a timestamp or display parser errors',()=>{
 for(const value of [undefined,null,'','  ','not-a-time',{},42])assert.equal(formatOperationTime(value),'—');
});
