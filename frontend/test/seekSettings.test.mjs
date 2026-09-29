import test from 'node:test';
import assert from 'node:assert/strict';
import { DEFAULT_SEEK_SETTINGS, SEEK_SETTING_FIELDS, parseSeekStepSeconds, readSeekSettings, writeSeekSettings, wheelSeekDelta } from '../src/seekSettings.ts';

const KEY = 'datamark-seek-settings';
const stored = value => ({ getItem(key) { assert.equal(key, KEY); return value; } });
const custom = { arrowMs: 1, shiftArrowMs: 1234, ctrlArrowMs: 120000, wheelMs: 3600000, shiftWheelMs: 250 };

test('defaults and shortcut labels describe the five independent jump steps', () => {
  assert.deepEqual(DEFAULT_SEEK_SETTINGS, { arrowMs: 100, shiftArrowMs: 1000, ctrlArrowMs: 30000, wheelMs: 5000, shiftWheelMs: 1000 });
  assert.deepEqual(SEEK_SETTING_FIELDS, [
    { key: 'arrowMs', label: '← / →' },
    { key: 'shiftArrowMs', label: 'Shift + ← / →' },
    { key: 'ctrlArrowMs', label: 'Ctrl + ← / →' },
    { key: 'wheelMs', label: 'Ctrl + Alt + 滚轮' },
    { key: 'shiftWheelMs', label: 'Shift + 滚轮' },
  ]);
});

test('decimal seconds convert to exact integer milliseconds including precision and range limits', () => {
  for (const [input, expected] of [
    ['0.001', 1], ['.001', 1], ['0.1', 100], ['.1', 100], ['1', 1000], ['1.', 1000],
    ['1.001', 1001], ['1.234', 1234], ['30', 30000], ['3599.999', 3599999],
    ['3600', 3600000], ['3600.000', 3600000], [' 0.500 ', 500], ['0001.050', 1050],
  ]) assert.equal(parseSeekStepSeconds(input), expected, input);
});

test('invalid, non-decimal, overprecise and out-of-range seconds are rejected', () => {
  for (const input of [
    '', ' ', '\t\n', '.', 'NaN', 'Infinity', '-Infinity', 'abc', '1 second', '1,5',
    '1 2', '0x10', '1e3', '1e-3', '+1', '-1', '0', '-0', '0.000',
    '0.0001', '1.2345', '1.0000', '3600.001', '3601', '9999999999999999999999999999999',
  ]) assert.equal(parseSeekStepSeconds(input), null, JSON.stringify(input));
});

test('missing, malformed and non-object stored settings fall back to defaults', () => {
  for (const raw of [null, '', '{bad json', 'null', 'true', '12', '"text"', '[]', '[100,1000,30000,5000]']) {
    assert.deepEqual(readSeekSettings(stored(raw)), DEFAULT_SEEK_SETTINGS, String(raw));
  }
});

test('valid stored fields survive independently when other fields are missing or damaged', () => {
  assert.deepEqual(readSeekSettings(stored(JSON.stringify({ arrowMs: 250, shiftArrowMs: '2000', wheelMs: 1, extra: 45 }))), {
    arrowMs: 250, shiftArrowMs: 1000, ctrlArrowMs: 30000, wheelMs: 1, shiftWheelMs: 1000,
  });
  assert.deepEqual(readSeekSettings(stored(JSON.stringify(custom))), custom);
});

test('each stored field requires positive bounded integer milliseconds', () => {
  for (const { key } of SEEK_SETTING_FIELDS) {
    for (const invalid of [0, -1, 1.5, 3600001, null, true, '1000', {}, []]) {
      const input = { ...custom, [key]: invalid };
      const expected = { ...custom, [key]: DEFAULT_SEEK_SETTINGS[key] };
      assert.deepEqual(readSeekSettings(stored(JSON.stringify(input))), expected, key + ': ' + JSON.stringify(invalid));
    }
  }
  assert.equal(readSeekSettings(stored('{"arrowMs":1e999}')).arrowMs, 100);
});

test('read errors use fresh defaults that callers cannot mutate globally', () => {
  const blocked = { getItem() { throw new Error('storage blocked'); } };
  const settings = readSeekSettings(blocked);
  assert.deepEqual(settings, DEFAULT_SEEK_SETTINGS);
  settings.arrowMs = 999;
  assert.equal(DEFAULT_SEEK_SETTINGS.arrowMs, 100);
  assert.equal(readSeekSettings(blocked).arrowMs, 100);
});

test('write stores only the five integer millisecond fields and round-trips', () => {
  const values = new Map();
  const storage = { getItem: key => values.get(key) ?? null, setItem: (key, value) => values.set(key, value) };
  assert.equal(writeSeekSettings({ ...custom, ignored: 'extra' }, storage), true);
  assert.deepEqual([...values.keys()], [KEY]);
  assert.deepEqual(JSON.parse(values.get(KEY)), custom);
  assert.deepEqual(readSeekSettings(storage), custom);
  assert.equal(writeSeekSettings(DEFAULT_SEEK_SETTINGS, storage), true);
  assert.deepEqual(readSeekSettings(storage), DEFAULT_SEEK_SETTINGS);
});

test('write failures return false without changing the supplied settings', () => {
  const settings = { ...custom };
  const blocked = { setItem() { throw new Error('quota exceeded'); } };
  assert.equal(writeSeekSettings(settings, blocked), false);
  assert.deepEqual(settings, custom);
});

test('invalid runtime settings are never persisted', () => {
  let writes = 0;
  const storage = { setItem() { writes++; } };
  for (const { key } of SEEK_SETTING_FIELDS) {
    for (const invalid of [0, -1, 0.5, 3600001, NaN, Infinity, undefined, null, '10']) {
      assert.equal(writeSeekSettings({ ...custom, [key]: invalid }, storage), false, key);
    }
  }
  assert.equal(writeSeekSettings(null, storage), false);
  assert.equal(writes, 0);
});

test('default browser storage access is protected when localStorage itself throws', () => {
  const previous = Object.getOwnPropertyDescriptor(globalThis, 'localStorage');
  Object.defineProperty(globalThis, 'localStorage', { configurable: true, get() { throw new Error('access denied'); } });
  try {
    assert.deepEqual(readSeekSettings(), DEFAULT_SEEK_SETTINGS);
    assert.equal(writeSeekSettings(custom), false);
  } finally {
    if (previous) Object.defineProperty(globalThis, 'localStorage', previous);
    else delete globalThis.localStorage;
  }
});

test('existing four-field preferences keep customized steps and gain a default Shift wheel step', () => {
  const legacy = {arrowMs:200,shiftArrowMs:1000,ctrlArrowMs:30000,wheelMs:2000};
  assert.deepEqual(readSeekSettings(stored(JSON.stringify(legacy))), {...legacy,shiftWheelMs:1000});
});

test('wheel seeking uses independent steps and leaves zoom and Alt panning intact', () => {
  const event = {ctrlKey:false,altKey:false,shiftKey:true,metaKey:false,deltaX:0,deltaY:120};
  assert.equal(wheelSeekDelta(event,custom),250);
  assert.equal(wheelSeekDelta({...event,deltaY:-120},custom),-250);
  assert.equal(wheelSeekDelta({...event,deltaY:0,deltaX:-60},custom),-250);
  assert.equal(wheelSeekDelta({...event,deltaY:0},custom),0);
  assert.equal(wheelSeekDelta({...event,deltaY:NaN},custom),0);
  assert.equal(wheelSeekDelta({...event,ctrlKey:true,altKey:true},custom),3600000);
  assert.equal(wheelSeekDelta({...event,shiftKey:false,ctrlKey:true,altKey:true,deltaY:-1},custom),-3600000);
  assert.equal(wheelSeekDelta({...event,ctrlKey:true},custom),null);
  assert.equal(wheelSeekDelta({...event,shiftKey:false,ctrlKey:true},custom),null);
  assert.equal(wheelSeekDelta({...event,shiftKey:false,altKey:true},custom),null);
  assert.equal(wheelSeekDelta({...event,altKey:true},custom),null);
  assert.equal(wheelSeekDelta({...event,metaKey:true},custom),null);
  assert.equal(wheelSeekDelta({...event,shiftKey:false},custom),null);
});
