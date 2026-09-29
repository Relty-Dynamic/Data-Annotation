import test from 'node:test';
import assert from 'node:assert/strict';
import { anchorVideoTime, restoreVideoTime } from '../src/domain.ts';

const video = (id, start_ms, end_ms) => ({id, start_ms, end_ms, duration_ms: end_ms - start_ms});

test('supplementing earlier footage keeps a draft on the same old video frame', () => {
  const before = [video('a', 0, 10000), video('c', 20000, 30000)];
  const after = [video('new', 0, 5000), video('a', 5000, 15000), video('b', 15000, 25000), video('c', 25000, 35000)];
  assert.equal(restoreVideoTime(after, anchorVideoTime(before, 23500)), 28500);
  assert.equal(restoreVideoTime(after, anchorVideoTime(before, 7000)), 12000);
});

test('draft start and end resolve an old seam to their own video side', () => {
  const before = [video('a', 0, 10000), video('b', 10000, 20000)];
  const after = [video('a', 0, 10000), video('inserted', 10000, 12000), video('b', 12000, 22000)];
  assert.equal(restoreVideoTime(after, anchorVideoTime(before, 10000, 'start')), 12000);
  assert.equal(restoreVideoTime(after, anchorVideoTime(before, 10000, 'end')), 10000);
});

test('invalid or gap draft coordinates cannot silently carry over', () => {
  const videos = [video('a', 0, 1000), video('b', 5000, 6000)];
  for (const time of [-1, NaN, Infinity, 2000, 6001]) assert.equal(anchorVideoTime(videos, time), null);
  assert.equal(restoreVideoTime(videos, {videoId: 'removed', offsetMs: 0}), null);
});

test('supplement mapping retains the first start and last end', () => {
  const before = [video('a', 0, 1000)];
  const after = [video('new', 0, 1000), video('a', 1000, 2000)];
  assert.equal(restoreVideoTime(after, anchorVideoTime(before, 0, 'end')), 1000);
  assert.equal(restoreVideoTime(after, anchorVideoTime(before, 1000)), 2000);
});

test('cursor mapping remains bounded to its original video', () => {
  const videos = [video('a', 1000, 2000)];
  assert.equal(restoreVideoTime(videos, {videoId: 'a', offsetMs: -2}), 1000);
  assert.equal(restoreVideoTime(videos, {videoId: 'a', offsetMs: 1002}), 2000);
});
