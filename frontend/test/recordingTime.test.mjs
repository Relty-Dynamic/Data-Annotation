import test from 'node:test';
import assert from 'node:assert/strict';
import { parseRecordingOrigin, recordingDateTime } from '../src/recordingTime.ts';

test('video clock uses Beijing time regardless of browser timezone or explicit source offset', () => {
  for (const start of ['2026-09-11 13:13:30', '2026-09-11T05:13:30Z', '2026-09-11T13:13:30+08:00']) {
    assert.equal(recordingDateTime(parseRecordingOrigin(start), 2_500), '2026-09-11 13:13:32.500');
  }
});

test('video clock preserves elapsed gaps and crosses midnight with the correct date', () => {
  const origin = parseRecordingOrigin('2026-09-11 23:59:30');
  assert.equal(recordingDateTime(origin, 60_000), '2026-09-12 00:00:30.000');
  assert.equal(recordingDateTime(origin, 86_460_000), '2026-09-13 00:00:30.000');
});

test('missing or invalid recording start does not invent a real clock', () => {
  for (const start of [undefined, null, '', ' ', 'invalid']) {
    assert.equal(parseRecordingOrigin(start), null);
    assert.equal(recordingDateTime(parseRecordingOrigin(start), 60_000), null);
  }
});

test('ruler clock matches annotation JSON HHMMSS including gaps and fractional midnight', () => {
  // Same timebase and expected values as backend annotation export regression cases.
  const cases = [
    ['2026-09-14T15:04:45.250+08:00', 11250, '150456', '2026-09-14'],
    ['2026-09-14T15:04:45.250+08:00', 11999, '150457', '2026-09-14'],
    ['2026-09-14T23:59:59.500+08:00', 499, '235959', '2026-09-14'],
    ['2026-09-14T23:59:59.500+08:00', 999, '000000', '2026-09-15'],
  ];
  for (const [start, offset, clock, date] of cases) {
    const displayed = recordingDateTime(parseRecordingOrigin(start), offset);
    assert.equal(displayed.slice(11, 19).replaceAll(':', ''), clock);
    assert.equal(displayed.slice(0, 10), date);
  }
});
