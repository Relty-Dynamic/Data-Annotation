import test from 'node:test';
import assert from 'node:assert/strict';
import {formatReadRate} from '../src/preparationPresentation.ts';
test('read rate distinguishes no sample, stalled reads and measured throughput', () => {
  for (const value of [undefined, null, NaN, Infinity, -1]) assert.equal(formatReadRate(value), '采样中…');
  assert.equal(formatReadRate(0), '0.00 MB/s');
  assert.equal(formatReadRate(1550000), '1.55 MB/s');
});
