import test from 'node:test';
import assert from 'node:assert/strict';
import { advanceVideoTime, playbackProfile, seekMediaTime, sourceTime } from '../src/playback.ts';

const video = (id, start_ms, end_ms) => ({ id, start_ms, end_ms });
const gapped = [video('first', 0, 1000), video('second', 11000, 12000), video('third', 22000, 24000)];

test('forward movement consumes video duration while skipping one or several recording gaps', () => {
  assert.equal(advanceVideoTime(gapped, 900, 250), 11150);
  assert.equal(advanceVideoTime(gapped, 900, 1500), 22400);
  assert.equal(advanceVideoTime(gapped, 500, 2000), 22500);
});

test('forward movement ending exactly at a clip boundary selects the following recording start', () => {
  assert.equal(advanceVideoTime(gapped, 900, 100), 11000);
  assert.equal(advanceVideoTime(gapped, 0, 2000), 22000);
  assert.equal(advanceVideoTime(gapped, 23900, 100), 24000);
});

test('backward 100 ms from the next recording start reaches the previous recording end minus 100 ms', () => {
  assert.equal(advanceVideoTime(gapped, 11000, -100), 900);
  assert.equal(advanceVideoTime(gapped, 22000, -100), 11900);
  assert.equal(advanceVideoTime(gapped, 22500, -2000), 500);
});

test('reverse exact boundaries resolve to the preceding recording end and both extremes clamp', () => {
  assert.equal(advanceVideoTime(gapped, 11100, -100), 1000);
  assert.equal(advanceVideoTime(gapped, 0, -100), 0);
  assert.equal(advanceVideoTime(gapped, 24000, 100), 24000);
  assert.equal(advanceVideoTime(gapped, 500, -10000), 0);
  assert.equal(advanceVideoTime(gapped, 500, 10000), 24000);
});

test('a cursor inside a gap resumes in the requested direction before consuming movement', () => {
  assert.equal(advanceVideoTime(gapped, 5000, 100), 11100);
  assert.equal(advanceVideoTime(gapped, 5000, -100), 900);
  assert.equal(advanceVideoTime(gapped, 5000, 0), 5000);
});

test('touching video clips behave as one continuous recording in both directions', () => {
  const continuous = [video('a', 0, 1000), video('b', 1000, 2500), video('c', 2500, 3000)];
  assert.equal(advanceVideoTime(continuous, 900, 200), 1100);
  assert.equal(advanceVideoTime(continuous, 2500, -2000), 500);
  assert.equal(advanceVideoTime(continuous, 900, 100), 1000);
  assert.equal(advanceVideoTime(continuous, 1100, -100), 1000);
  assert.equal(advanceVideoTime(continuous, 2990, 100), 3000);
});

test('fractional playback increments retain submillisecond precision and input videos stay unchanged', () => {
  const input = [video('b', 11000, 12000), video('a', 0, 1000)];
  const original = structuredClone(input);
  assert.equal(advanceVideoTime(input, 999.5, 1), 11000.5);
  assert.deepEqual(input, original);
});

test('empty coverage and out-of-range global cursors return bounded positions', () => {
  assert.equal(advanceVideoTime([], 123, 100), 0);
  assert.equal(advanceVideoTime(gapped, -5000, 100), 100);
  assert.equal(advanceVideoTime(gapped, 50000, -100), 23900);
  assert.equal(advanceVideoTime(gapped, 500, Infinity), 24000);
  assert.equal(advanceVideoTime(gapped, 500, -Infinity), 0);
});

test('50x and 100x use supported continuous playback rates and full media on pause', () => {
  assert.deepEqual(playbackProfile(50, true), { fast: true, scale: 20, nativeRate: 2.5 });
  assert.deepEqual(playbackProfile(100, true), { fast: true, scale: 20, nativeRate: 5 });
  assert.deepEqual(playbackProfile(100, false), { fast: false, scale: 1, nativeRate: 1 });
  assert.deepEqual(playbackProfile(10, true), { fast: false, scale: 1, nativeRate: 10 });
});

test('compressed video time maps to original annotation coordinates including clip offsets', () => {
  const clip = video('offset', 10000, 70000);
  assert.equal(sourceTime(1.234, 20, clip), 34680);
  assert.equal(sourceTime(24.68, 1, clip), 34680);
  assert.equal(sourceTime(3.1, 20, clip), 70000);
  assert.equal(sourceTime(-1, 20, clip), 10000);
});

test('pausing high-speed playback for manual seeking restores normal-frame precision', () => {
  const clip = video('offset', 10000, 70000);
  for (const rate of [50, 100]) {
    const before = playbackProfile(rate, true);
    const target = sourceTime(1.234, before.scale, clip);
    const paused = playbackProfile(rate, false);
    const player = { currentTime: 0 };
    seekMediaTime(player, (target - clip.start_ms) / 1000, paused.scale);
    assert.equal(paused.fast, false);
    assert.equal(paused.nativeRate, 1);
    assert.equal(sourceTime(player.currentTime, paused.scale, clip), target);
  }
});

test('repeated click/release requests do not restart a seek to the same destination', () => {
  let current = 0;
  const writes = [];
  const player = { get currentTime() { return current; }, set currentTime(value) { current = value; writes.push(value); } };
  assert.equal(seekMediaTime(player, 0), false, 'loading at zero should not start a redundant seek');
  assert.equal(seekMediaTime(player, 120.125), true);
  assert.equal(seekMediaTime(player, 120.125), false);
  assert.equal(seekMediaTime(player, 120.1250001), false);
  assert.deepEqual(writes, [120.125]);
  assert.equal(seekMediaTime(player, 120.126), true, 'a different millisecond remains seekable');
});

test('compressed preview seeks preserve source millisecond accuracy and avoid duplicate decoder work', () => {
  const player = { currentTime: 6 };
  assert.equal(seekMediaTime(player, 120, 20), false);
  assert.equal(seekMediaTime(player, 120.001, 20), true);
  assert.equal(player.currentTime, 120.001 / 20);
  assert.equal(seekMediaTime(player, 120.001, 20), false);
  assert.equal(seekMediaTime(player, 120.101, 20), true);
  assert.equal(sourceTime(player.currentTime, 20, video('v', 10000, 300000)), 130101);
});
