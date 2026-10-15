import assert from 'node:assert/strict';
import test from 'node:test';
import { chooseJsonExportDirectory, saveJsonDocuments } from '../src/localJsonExport.ts';

const documents = [
  {name: 'scene.timeline.json', content: JSON.stringify({axis: 'scene', segments: [{label: '室内', start_ms: 0, end_ms: 1200}]}, null, 2)},
  {name: 'posture.timeline.json', content: JSON.stringify({axis: 'posture', segments: []}, null, 2)},
  {name: '自定义.timeline.json', content: JSON.stringify({axis: 'custom', segments: []}, null, 2)},
];

function folder({failAt, stage, corruptRead, abortFails = false} = {}) {
  const files = new Map([['unrelated.txt', 'retain this file']]);
  const opened = [], verified = [], aborted = [];
  const fail = (name, operation) => {
    if (name === failAt && operation === stage) throw new Error(`${operation} denied`);
  };
  return {
    files, opened, verified, aborted,
    async getFileHandle(name, options) {
      assert.deepEqual(options, {create: true});
      opened.push(name);
      fail(name, 'open');
      let staged;
      return {
        async createWritable() {
          fail(name, 'writable');
          return {
            async write(content) { fail(name, 'write'); staged = content; },
            async close() { fail(name, 'close'); files.set(name, staged); },
            async abort() {
              aborted.push(name);
              if (abortFails) throw new Error('abort failed');
            },
          };
        },
        async getFile() {
          fail(name, 'read');
          return {async text() {
            fail(name, 'text');
            verified.push(name);
            return name === corruptRead ? 'incorrect data' : files.get(name);
          }};
        },
      };
    },
  };
}

function replacePicker(t, value) {
  const previous = Object.getOwnPropertyDescriptor(globalThis, 'showDirectoryPicker');
  Object.defineProperty(globalThis, 'showDirectoryPicker', {value, configurable: true, writable: true});
  t.after(() => {
    if (previous) Object.defineProperty(globalThis, 'showDirectoryPicker', previous);
    else delete globalThis.showDirectoryPicker;
  });
}

test('the complete export batch is validated before any file is opened', async () => {
  const invalidBatches = [
    null, [], [documents[0], null],
    [documents[0], {name: '../posture.json', content: '{}'}],
    [documents[0], {name: 'folder\\posture.json', content: '{}'}],
    [documents[0], {name: 'bad\u0000name.json', content: '{}'}],
    [documents[0], {name: 'posture.txt', content: '{}'}],
    [documents[0], {name: 'SCENE.timeline.JSON', content: '{}'}],
    [documents[0], {name: 'posture.json', content: null}],
    [documents[0], {name: 'posture.json', content: '{invalid'}],
  ];
  for (const batch of invalidBatches) {
    const directory = folder();
    await assert.rejects(saveJsonDocuments(directory, batch));
    assert.deepEqual(directory.opened, []);
    assert.deepEqual([...directory.files], [['unrelated.txt', 'retain this file']]);
  }
});

test('exports independent JSON files, verifies every file, and leaves unrelated files alone', async () => {
  const directory = folder();
  directory.files.set(documents[0].name, 'old content');
  assert.equal(await saveJsonDocuments(directory, documents), documents.length);
  assert.deepEqual(directory.opened, documents.map(document => document.name));
  assert.deepEqual(directory.verified, documents.map(document => document.name));
  for (const document of documents) assert.equal(directory.files.get(document.name), document.content);
  assert.equal(directory.files.get('unrelated.txt'), 'retain this file');
  assert.equal(directory.files.size, documents.length + 1);
  assert.deepEqual(directory.aborted, []);
});

test('write and read failures report only verified successes and stop before the next file', async () => {
  for (const stage of ['open', 'writable', 'write', 'close', 'read', 'text']) {
    const directory = folder({failAt: documents[1].name, stage});
    await assert.rejects(saveJsonDocuments(directory, documents), error => {
      assert.match(error.message, /已保存并核验 1\/3 个 JSON/);
      assert.ok(error.message.includes(`${documents[1].name} 保存失败`));
      assert.ok(error.message.includes(`${stage} denied`));
      return true;
    });
    assert.deepEqual(directory.opened, documents.slice(0, 2).map(document => document.name));
    assert.equal(directory.files.get(documents[0].name), documents[0].content);
    assert.equal(directory.files.has(documents[2].name), false);
    assert.deepEqual(directory.aborted, ['write', 'close'].includes(stage) ? [documents[1].name] : []);
  }
});

test('a mismatched readback is a failed export even when the write completed', async () => {
  const directory = folder({corruptRead: documents[1].name});
  await assert.rejects(saveJsonDocuments(directory, documents), /已保存并核验 1\/3 个 JSON.*内容核验失败/);
  assert.equal(directory.files.get(documents[1].name), documents[1].content);
  assert.equal(directory.files.has(documents[2].name), false);
});

test('failure on the first file reports zero success and preserves the write error if abort also fails', async () => {
  const directory = folder({failAt: documents[0].name, stage: 'write', abortFails: true});
  await assert.rejects(saveJsonDocuments(directory, documents), error => {
    assert.match(error.message, /已保存并核验 0\/3 个 JSON/);
    assert.match(error.message, /write denied/);
    assert.ok(!error.message.includes('abort failed'));
    return true;
  });
  assert.deepEqual(directory.opened, [documents[0].name]);
  assert.deepEqual(directory.aborted, [documents[0].name]);
  assert.deepEqual([...directory.files], [['unrelated.txt', 'retain this file']]);
});

test('chooses a writable export folder and returns the user-selected handle', async t => {
  const directory = folder();
  replacePicker(t, async options => {
    assert.deepEqual(options, {id: 'datamark-json-export', mode: 'readwrite'});
    return directory;
  });
  assert.equal(await chooseJsonExportDirectory(), directory);
  assert.deepEqual(directory.opened, []);
});

test('cancelling folder selection returns null without reporting a save failure', async t => {
  replacePicker(t, async () => { throw new DOMException('The user cancelled.', 'AbortError'); });
  assert.equal(await chooseJsonExportDirectory(), null);
});

test('folder permission errors remain errors rather than being treated as cancellation', async t => {
  const denied = new DOMException('Permission denied.', 'SecurityError');
  replacePicker(t, async () => { throw denied; });
  await assert.rejects(chooseJsonExportDirectory(), error => error === denied);
});

test('unsupported browsers report the missing directory picker', async t => {
  replacePicker(t, undefined);
  await assert.rejects(chooseJsonExportDirectory(), /不支持选择保存文件夹/);
});
