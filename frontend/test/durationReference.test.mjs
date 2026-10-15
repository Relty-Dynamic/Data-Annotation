import test from 'node:test';
import assert from 'node:assert/strict';
import { durationReferenceLabels } from '../src/durationReference.ts';

test('roomy reference labels every five seconds from the right', () => {
  const labels = durationReferenceLabels(600, 800);
  assert.equal(labels.length, 12);
  assert.equal(labels.find(label => label.seconds === 5).right, 37);
  assert.equal(labels.find(label => label.seconds === 30).right, 287);
});

test('compact reference prioritizes 5, 30 and 60 seconds without overlapping labels', () => {
  const labels = durationReferenceLabels(120, 200);
  assert.deepEqual(labels.map(label => label.seconds), [60, 30, 5]);
  for (const width of [10, 30, 60, 120, 600, 1200]) {
    const result = durationReferenceLabels(width, 300);
    for (const label of result) {
      assert.ok(label.right >= 0 && label.right + 26 <= 300);
      for (const other of result) {
        if (label !== other) assert.ok(Math.abs(label.right - other.right) >= 31);
      }
    }
  }
});

test('clipped reference omits out-of-view durations instead of rescaling them', () => {
  const labels = durationReferenceLabels(1200, 400);
  assert.ok(labels.every(label => label.seconds <= 20));
  assert.equal(labels.find(label => label.seconds === 5).right, 87);
  assert.deepEqual(durationReferenceLabels(600, 20), []);
});
