import test from 'node:test';
import assert from 'node:assert/strict';
import { mediaNeedsBuffering } from '../src/mediaBuffering.ts';

const state = (changes = {}) => ({
  readyState: 4, seeking: false, wantsPlay: false, ended: false, error: null, ...changes,
});

test('a new source needs a decoded frame even when playback is paused', () => {
  assert.equal(mediaNeedsBuffering(state({ readyState: 0 })), true);
  assert.equal(mediaNeedsBuffering(state({ readyState: 1 })), true);
  assert.equal(mediaNeedsBuffering(state({ readyState: 1 }), true), true);
});

test('a paused current frame needs no future frames and clears a stale waiting event', () => {
  assert.equal(mediaNeedsBuffering(state({ readyState: 2 })), false);
  assert.equal(mediaNeedsBuffering(state({ readyState: 3 })), false);
  assert.equal(mediaNeedsBuffering(state()), false);
});

test('playing with only current data reports buffering until frames can advance', () => {
  assert.equal(mediaNeedsBuffering(state({ readyState: 2, wantsPlay: true })), true);
  assert.equal(mediaNeedsBuffering(state({ readyState: 3, wantsPlay: true })), false);
  assert.equal(mediaNeedsBuffering(state({ readyState: 4, wantsPlay: true })), false);
});

test('observed playback progress clears stale waiting while exhausted data reports waiting again', () => {
  const player = state({ readyState: 2, wantsPlay: true });
  assert.equal(mediaNeedsBuffering(player, true), false);
  assert.equal(mediaNeedsBuffering(player, false), true);
});

test('a seeking time jump cannot masquerade as successfully decoded playback progress', () => {
  assert.equal(mediaNeedsBuffering(state({ seeking: true, wantsPlay: true }), true), true);
  assert.equal(mediaNeedsBuffering(state({ seeking: true, readyState: 2 }), true), true);
  assert.equal(mediaNeedsBuffering(state({ seeking: false, readyState: 2 })), false);
});

test('ended media and media errors use their own UI rather than an endless buffering badge', () => {
  assert.equal(mediaNeedsBuffering(state({ ended: true, readyState: 0, seeking: true })), false);
  assert.equal(mediaNeedsBuffering(state({ error: { code: 2 }, readyState: 0, seeking: true })), false);
});

test('network wait recovers from live readiness even without another canplay event', () => {
  const player = state({ wantsPlay: true, readyState: 2 });
  assert.equal(mediaNeedsBuffering(player), true);
  player.readyState = 4;
  assert.equal(mediaNeedsBuffering(player), false);
  player.readyState = 2;
  assert.equal(mediaNeedsBuffering(player), true);
  player.wantsPlay = false;
  assert.equal(mediaNeedsBuffering(player), false);
});
