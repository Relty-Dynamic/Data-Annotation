import test from 'node:test';
import assert from 'node:assert/strict';
import { DEFAULT_WINDOW_MS, MIN_WINDOW_MS, timelineWindow, scaleTimelineWindow, visibleTimelineTicks } from '../src/timelineViewport.ts';

const HOUR = DEFAULT_WINDOW_MS;
const near = (actual, expected) => assert.ok(Math.abs(actual - expected) < 1e-6, `${actual} != ${expected}`);

test('initial scale is one hour or the entire short project; zoom has a five-minute floor', () => {
  assert.equal(timelineWindow(8 * HOUR), HOUR);
  assert.equal(timelineWindow(20 * 60_000), 20 * 60_000);
  assert.equal(timelineWindow(8 * HOUR, 1), MIN_WINDOW_MS);
  assert.equal(timelineWindow(2 * 60_000, 1), 2 * 60_000);
  assert.equal(timelineWindow(8 * HOUR, 100 * HOUR), 8 * HOUR);
});

test('zoom preserves the exact time beneath the mouse across consecutive changes', () => {
  const duration = 24 * HOUR;
  const width = 860;
  const pointer = 413;
  let windowMs = HOUR;
  let scrollLeft = 3.25 * width;
  const original = (scrollLeft + pointer) / width * windowMs;
  for (const target of [40 * 60_000, 25 * 60_000, 5 * 60_000, 90 * 60_000, HOUR]) {
    const result = scaleTimelineWindow(duration, windowMs, target, width, scrollLeft, pointer);
    near((result.scrollLeft + pointer) / result.axisWidth * duration, original);
    windowMs = result.windowMs;
    scrollLeft = result.scrollLeft;
  }
});

test('zoom limits clamp scroll at the beginning and end without overshooting', () => {
  const duration = 8 * HOUR;
  const width = 1000;
  const first = scaleTimelineWindow(duration, HOUR, 2 * HOUR, width, 0, width / 2);
  assert.equal(first.scrollLeft, 0);
  const last = scaleTimelineWindow(duration, HOUR, 2 * HOUR, width, 7 * width, width / 2);
  assert.equal(last.scrollLeft, last.axisWidth - width);
  const whole = scaleTimelineWindow(duration, HOUR, 100 * HOUR, width, 4 * width, 300);
  assert.equal(whole.windowMs, duration);
  assert.equal(whole.axisWidth, width);
  assert.equal(whole.scrollLeft, 0);
});

test('short recordings and out-of-plot mouse positions remain bounded', () => {
  const short = scaleTimelineWindow(90_000, HOUR, MIN_WINDOW_MS, 500, 999, -100);
  assert.deepEqual(short, { windowMs: 90_000, scrollLeft: 0, axisWidth: 500 });
  const left = scaleTimelineWindow(5 * HOUR, HOUR, HOUR / 2, 500, 500, -100);
  const right = scaleTimelineWindow(5 * HOUR, HOUR, HOUR / 2, 500, 500, 800);
  assert.equal(left.scrollLeft, 1000);
  assert.equal(right.scrollLeft, 1500);
});

test('a week-long recording only renders the ticks around its five-minute viewport', () => {
  const ticks = visibleTimelineTicks(100 * HOUR, 100 * HOUR + MIN_WINDOW_MS, 30_000, 168 * HOUR);
  assert.equal(ticks.length, 13);
  assert.equal(ticks[0], 100 * HOUR - 30_000);
  assert.equal(ticks.at(-1), 100 * HOUR + MIN_WINDOW_MS + 30_000);
  assert.deepEqual(visibleTimelineTicks(0, 60_000, 30_000, 60_000), [0, 30_000, 60_000]);
});
