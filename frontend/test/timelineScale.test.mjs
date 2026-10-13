import test from 'node:test';
import assert from 'node:assert/strict';
import {timelineScale, recordingScale, zoomRecordingScale, snapDisplayedTime} from '../src/timelineScale.ts';
import {timelineWindow} from '../src/timelineViewport.ts';

const HOUR = 3_600_000;
const near = (a, b) => assert.ok(Math.abs(a - b) < 1e-5, `${a} != ${b}`);

test('long recording gaps shrink while video durations and every real timestamp remain intact', () => {
  const gaps = [{start_ms: HOUR, end_ms: 25 * HOUR}];
  const scale = timelineScale(26 * HOUR, gaps, 60_000);
  assert.equal(scale.duration, 2 * HOUR + 60_000);
  assert.equal(scale.toDisplay(25 * HOUR), HOUR + 60_000);
  assert.equal(scale.toDisplay(25 * HOUR + 5000) - scale.toDisplay(25 * HOUR), 5000);
  for (const time of [0, 5000, HOUR, HOUR + 1, 13 * HOUR, 25 * HOUR - 1, 25 * HOUR, 26 * HOUR]) {
    near(scale.toTime(scale.toDisplay(time)), time);
  }
  assert.deepEqual(gaps, [{start_ms: HOUR, end_ms: 25 * HOUR}]);
});

test('short gaps keep their proportional width and adjacent or overlapping gaps merge safely', () => {
  const scale = timelineScale(10_000, [{start_ms: 1000, end_ms: 1010}], 100);
  assert.equal(scale.duration, 10_000);
  near(scale.toDisplay(1005), 1005);
  const merged = timelineScale(10_000, [{start_ms: 500, end_ms: 5000}, {start_ms: 1000, end_ms: 7000}, {start_ms: 7000, end_ms: 8000}], 100);
  assert.equal(merged.duration, 2600);
  near(merged.toTime(merged.toDisplay(7500)), 7500);
});

test('gaps stay at most 24 pixels wide at all zoom levels and in short projects', () => {
  for (const duration of [26 * HOUR, 25 * HOUR + 1000]) {
    const gaps = [{start_ms: 1000, end_ms: 25 * HOUR}];
    for (const windowMs of [300_000, HOUR, 30 * HOUR]) {
      const scale = recordingScale(duration, gaps, windowMs, 800);
      const width = 800 * scale.duration / timelineWindow(scale.duration, windowMs);
      const gapPixels = (scale.toDisplay(gaps[0].end_ms) - scale.toDisplay(gaps[0].start_ms)) / scale.duration * width;
      assert.ok(gapPixels <= 24 + 1e-6, String(gapPixels));
    }
  }
});

test('zoom after a day-long gap keeps the exact real timestamp under the mouse', () => {
  const duration = 40 * HOUR, gaps = [{start_ms: 2 * HOUR, end_ms: 26 * HOUR}], width = 800, pointer = 310;
  let windowMs = HOUR, scale = recordingScale(duration, gaps, windowMs, width);
  let axisWidth = width * scale.duration / timelineWindow(scale.duration, windowMs);
  const time = 30 * HOUR + 123_456;
  let scrollLeft = scale.toDisplay(time) / scale.duration * axisWidth - pointer;
  for (const next of [300_000, 2 * HOUR, HOUR, 600_000]) {
    const result = zoomRecordingScale(duration, gaps, windowMs, next, width, scrollLeft, pointer);
    scale = recordingScale(duration, gaps, result.windowMs, width);
    near(scale.toTime((result.scrollLeft + pointer) / result.axisWidth * scale.duration), time);
    windowMs = result.windowMs; scrollLeft = result.scrollLeft;
  }
});

test('snapping uses screen distance on both sides of a collapsed gap', () => {
  const scale = timelineScale(26 * HOUR, [{start_ms: HOUR, end_ms: 25 * HOUR}], 60_000);
  const width = 1200;
  assert.equal(snapDisplayedTime(25 * HOUR + 1000, [HOUR, 25 * HOUR], scale, width), 25 * HOUR);
  assert.equal(snapDisplayedTime(HOUR - 1000, [HOUR, 25 * HOUR], scale, width), HOUR);
  assert.equal(snapDisplayedTime(25 * HOUR + 100_000, [HOUR, 25 * HOUR], scale, width), 25 * HOUR + 100_000);
});

test('no-gap coordinates, range lengths and zoom limits match the ordinary timeline', () => {
  const scale = recordingScale(4 * HOUR, [], HOUR, 800);
  assert.equal(scale.duration, 4 * HOUR);
  assert.equal(scale.toDisplay(2 * HOUR), 2 * HOUR);
  const full = zoomRecordingScale(4 * HOUR, [], HOUR, 10 * HOUR, 800, 2000, 400);
  assert.equal(full.axisWidth, 800);
  assert.equal(full.scrollLeft, 0);
  assert.equal(scale.toDisplay(2 * HOUR) - scale.toDisplay(HOUR), HOUR);
});
