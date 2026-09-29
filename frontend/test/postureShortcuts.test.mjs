import test from 'node:test';
import assert from 'node:assert/strict';
import { postureShortcutLabel } from '../src/postureShortcuts.ts';
import { switchCoveredState, uncoveredSpans } from '../src/domain.ts';

const spans = [{start_ms: 0, end_ms: 10000}, {start_ms: 12000, end_ms: 15000}];
const compact = records => records.map(({label,start_ms,end_ms}) => [label,start_ms,end_ms]);

test('four keys annotate successive posture changes while retaining full recorded coverage', () => {
  let records = [];
  for (const [code,time] of [['KeyZ',0],['KeyX',2000],['KeyC',4000],['KeyV',6000]]) {
    records = switchCoveredState(records,time,postureShortcutLabel({code},false),15000,spans);
  }
  assert.deepEqual(compact(records), [['动',0,2000],['坐',2000,4000],['站',4000,6000],['躺',6000,10000],['动',12000,15000]]);
  assert.deepEqual(uncoveredSpans(records,spans), []);
  const original = structuredClone(records);
  const corrected = switchCoveredState(records,3000,'躺',15000,spans);
  assert.deepEqual(compact(corrected), [['动',0,2000],['坐',2000,3000],['躺',3000,4000],['站',4000,6000],['躺',6000,10000],['动',12000,15000]]);
  assert.deepEqual(records,original);
  assert.deepEqual(switchCoveredState(records,11000,'躺',15000,spans),original);
  assert.deepEqual(switchCoveredState(records,15000,'躺',15000,spans),original);
  assert.deepEqual(compact(switchCoveredState(records,4000,'躺',15000,spans)), [['动',0,2000],['坐',2000,4000],['躺',4000,10000],['动',12000,15000]]);
});

test('posture shortcuts preserve text entry, IME, undo, clipboard and modifier chords', () => {
  for (const code of ['KeyZ','KeyX','KeyC','KeyV']) {
    assert.equal(postureShortcutLabel({code},true),null);
    for (const flag of ['ctrlKey','metaKey','altKey','shiftKey','repeat','isComposing']) {
      assert.equal(postureShortcutLabel({code,[flag]:true},false),null);
    }
    assert.equal(postureShortcutLabel({code,keyCode:229},false),null);
  }
  assert.equal(postureShortcutLabel({code:'KeyB'},false),null);
});
