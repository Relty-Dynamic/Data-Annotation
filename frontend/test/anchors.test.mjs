import assert from 'node:assert/strict';
import {test} from 'node:test';
import {anchorRange, toggleAnchor} from '../src/anchors.ts';

test('anchor toggles keep two most recent positions and remove an existing position', () => {
  const first = toggleAnchor([], 9000);
  assert.deepEqual(first, [9000]);
  const pair = toggleAnchor(first, 2000);
  assert.deepEqual(pair, [9000, 2000]);
  assert.deepEqual(toggleAnchor(pair, 6000), [2000, 6000]);
  assert.deepEqual(toggleAnchor(pair, 9000), [2000]);
  assert.deepEqual(toggleAnchor(pair, 2000), [9000]);
  assert.deepEqual(pair, [9000, 2000]);
});

test('explicit selection uses chronological endpoints and leaves anchors reusable', () => {
  const anchors = [9000, 2000];
  assert.deepEqual(anchorRange(anchors), {start_ms: 2000, end_ms: 9000});
  assert.deepEqual(anchorRange(anchors), {start_ms: 2000, end_ms: 9000});
  assert.deepEqual(anchors, [9000, 2000]);
  for (const invalid of [[], [9000], [9000, 9000], [1, 2, 3], [NaN, 1], [-1, 4]]) assert.equal(anchorRange(invalid), null);
});
