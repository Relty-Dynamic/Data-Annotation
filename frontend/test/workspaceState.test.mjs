import test from 'node:test';
import assert from 'node:assert/strict';
import { isSegmentDraftDirty } from '../src/domain.ts';
import { readProjectPreference, readRestorableProjectPreference, writeProjectPreference } from '../src/projectPreferences.ts';
import { preparationFailureMessage, OLD_PLAYBACK_BACKEND } from '../src/preparationErrors.ts';

const interval = {id:'a', label:'喝水', start_ms:1500, end_ms:5000};
const unchanged = {label:'喝水', start:'00:00:01.500', end:'5', kind:'interval'};

test('switching workspace detects unsaved segment changes without blocking equivalent time formats', () => {
  assert.equal(isSegmentDraftDirty(interval, unchanged), false);
  assert.equal(isSegmentDraftDirty(interval, {...unchanged, start:'1.5', end:'00:05.000'}), false);
  for (const change of [{label:'咖啡'}, {start:'2'}, {end:'7'}, {kind:'point'}, {end:'invalid'}]) {
    assert.equal(isSegmentDraftDirty(interval, {...unchanged, ...change}), true);
  }
});

test('instant events ignore the disabled end field while open intervals retain invalid edits', () => {
  const point = {...interval, kind:'point', end_ms:1500};
  assert.equal(isSegmentDraftDirty(point, {...unchanged, kind:'point', end:''}), false);
  const open = {...interval, end_ms:null};
  assert.equal(isSegmentDraftDirty(open, {...unchanged, end:''}), false);
  assert.equal(isSegmentDraftDirty(open, {...unchanged, end:'invalid'}), true);
  assert.equal(isSegmentDraftDirty(undefined, {...unchanged, label:'', start:'', end:''}), false);
  assert.equal(isSegmentDraftDirty(undefined, unchanged), true);
});

test('blocked browser storage cannot turn a completed project operation into an exception', () => {
  const unavailable = {getItem(){throw new Error('blocked');}, setItem(){throw new Error('quota');}, removeItem(){throw new Error('blocked');}};
  assert.equal(readProjectPreference('last', unavailable), null);
  assert.equal(writeProjectPreference('last', 'project', unavailable), false);
  assert.equal(writeProjectPreference('deleted', null, unavailable), false);
});

test('project preferences retain normal remembered-project and deletion behavior', () => {
  const values = new Map();
  const storage = {getItem:key=>values.get(key)??null, setItem:(key,value)=>values.set(key,value), removeItem:key=>values.delete(key)};
  assert.equal(writeProjectPreference('last', 'project', storage), true);
  assert.equal(readProjectPreference('last', storage), 'project');
  assert.equal(writeProjectPreference('last', null, storage), true);
  assert.equal(readProjectPreference('last', storage), null);
});

test('cleared preview stays at project selection until the user explicitly opens a project', () => {
  const values = new Map();
  const storage = {getItem:key=>values.get(key)??null, setItem:(key,value)=>values.set(key,value), removeItem:key=>values.delete(key)};
  writeProjectPreference('last', 'a', storage);
  writeProjectPreference('cleared', JSON.stringify({id:'a', at:1}), storage);
  assert.equal(readRestorableProjectPreference('last', 'cleared', storage), null);
  assert.equal(readProjectPreference('last', storage), null);

  writeProjectPreference('cleared', null, storage);
  writeProjectPreference('last', 'a', storage);
  assert.equal(readRestorableProjectPreference('last', 'cleared', storage), 'a');
});

test('clearing another project does not hide the remembered project', () => {
  const values = new Map([['last', 'b'], ['cleared', JSON.stringify({id:'a', at:1})]]);
  const storage = {getItem:key=>values.get(key)??null, setItem:(key,value)=>values.set(key,value), removeItem:key=>values.delete(key)};
  assert.equal(readRestorableProjectPreference('last', 'cleared', storage), 'b');
});

test('missing session route on confirmed old server gives exact restart steps', () => {
  assert.equal(preparationFailureMessage(404, '接口不存在。', ['source-local-cache']), OLD_PLAYBACK_BACKEND);
  assert.equal(preparationFailureMessage(404, '接口不存在。', []), OLD_PLAYBACK_BACKEND);
});

test('missing project and unreachable health retain their actual error instead of blaming old server', () => {
  assert.equal(preparationFailureMessage(404, '找不到这个标注项目。', ['compact-local-playback']), '找不到这个标注项目。');
  assert.equal(preparationFailureMessage(404, '接口不存在。'), '接口不存在。');
  assert.equal(preparationFailureMessage(403, '没有权限', []), '没有权限');
});
