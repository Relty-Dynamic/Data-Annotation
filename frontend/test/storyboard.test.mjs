import test from 'node:test';
import assert from 'node:assert/strict';
import { isStoryboardManifest, storyboardFrame, spritePosition } from '../src/storyboard.ts';

const manifest = {state: 'ready', interval_ms: 1000, tile_width: 160, tile_height: 90, columns: 10, rows: 10, frame_count: 205, sheets: ['/first.jpg', '/second.jpg', '/last.jpg']};

test('sampling stays on the preceding second while the exact cursor can remain fractional', () => {
  assert.equal(storyboardFrame(manifest, 15999.99).index, 15);
  assert.equal(storyboardFrame(manifest, 15999.99).localTime, 15000);
  assert.equal(storyboardFrame(manifest, 16000).index, 16);
});

test('row and sheet boundaries map to the right image and tile', () => {
  assert.deepEqual([9, 10, 99, 100, 204].map((second) => {
    const frame = storyboardFrame(manifest, second * 1000);
    return [frame.url, frame.column, frame.row];
  }), [['/first.jpg', 9, 0], ['/first.jpg', 0, 1], ['/first.jpg', 9, 9], ['/second.jpg', 0, 0], ['/last.jpg', 4, 0]]);
  assert.equal(spritePosition(storyboardFrame(manifest, 99000)), '100% 100%');
});

test('clip start and last partial sheet clamp without displaying padded empty tiles', () => {
  assert.equal(storyboardFrame(manifest, -1).index, 0);
  assert.equal(storyboardFrame(manifest, 205999).index, 204);
  assert.equal(storyboardFrame(manifest, NaN).index, 0);
});

test('incomplete or malformed manifests cannot enter the sprite renderer', () => {
  assert.equal(isStoryboardManifest(manifest), true);
  for (const changed of [{frame_count: 0}, {interval_ms: 0}, {columns: 0}, {sheets: ['/first.jpg']}, {state: 'running'}]) {
    assert.equal(isStoryboardManifest({...manifest, ...changed}), false);
  }
  assert.equal(isStoryboardManifest(null), false);
});
