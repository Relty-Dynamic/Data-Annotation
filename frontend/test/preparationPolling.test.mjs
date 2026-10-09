import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import ts from 'typescript';

function mountPreparation(responses, complete = false) {
  const slots = [], timers = new Map(), requests = [];
  let cursor = 0, dirty = false, effects = [], nextTimer = 0, result;
  const same = (left, right) => left?.length === right?.length && left?.every((value, index) => Object.is(value, right[index]));
  const slot = create => slots[cursor] ?? (slots[cursor] = create());
  const react = {
    useRef(value) {const current = slot(() => ({current: value}));cursor += 1;return current;},
    useState(initial) {
      const current = slot(() => ({value: initial}));cursor += 1;
      return [current.value, value => {
        const next = typeof value === 'function' ? value(current.value) : value;
        if (!Object.is(next, current.value)) {current.value = next;dirty = true;}
      }];
    },
    useCallback(fn, deps) {
      const current = slot(() => ({}));cursor += 1;
      if (!same(current.deps, deps)) {current.deps = deps;current.fn = fn;}
      return current.fn;
    },
    useEffect(fn, deps) {
      const current = slot(() => ({}));cursor += 1;
      if (!same(current.deps, deps)) {
        current.deps = deps;
        effects.push(() => {current.cleanup?.();current.cleanup = fn();});
      }
    },
  };
  const context = vm.createContext({AbortController, AbortSignal,
    setTimeout(fn, delay) {const id = ++nextTimer;timers.set(id, {fn, delay});return id;},
    clearTimeout(id) {timers.delete(id);},
    async fetch(url, options) {
      requests.push({url, method: options?.method ?? 'GET', body: options?.body && JSON.parse(options.body)});
      assert.ok(responses.length, 'Unexpected request: ' + url);
      const value = responses.shift();
      return {ok: true, async json() {return value;}};
    },
  });
  const source = ts.transpileModule(fs.readFileSync(new URL('../src/usePreparation.ts', import.meta.url), 'utf8'), {
    compilerOptions: {target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS},
  }).outputText;
  const module = {exports: {}};
  vm.runInContext(`(function(require,module,exports){${source}\n})`, context)(name => {
    if (name === 'react') return react;
    if (name === './auth.ts') return {authFetch: context.fetch};
    if (name === './sessionAssets') return {forgetSessionAssets() {}, isSessionManifest() {
      if (!complete) throw new Error('Incomplete batch must not inspect a manifest');
      return true;
    }, prepareSessionAssets: async (_id, _manifest, _signal, onProgress) => {
      if (!complete) throw new Error('Incomplete batch must not enter browser warmup');
      onProgress(1, 1);
    }};
    if (name === './preparationErrors') return {preparationFailureMessage: (_, detail) => detail};
    if (name === './browserVideoCache') return {forgetBrowserVideoUrls() {}, prepareBrowserVideos() {
      throw new Error('This polling test does not request a browser video download');
    }};
    throw new Error('Unexpected hook import: ' + name);
  }, module, module.exports);
  const render = () => {
    cursor = 0;dirty = false;effects = [];
    result = module.exports.usePreparation('fixture-project', 7);
    for (const effect of effects) effect();
  };
  const settle = async () => {
    for (let attempt = 0; attempt < 30; attempt += 1) {
      await Promise.resolve();
      if (dirty) render();
    }
  };
  render();
  return {requests, timers, settle, get result() {return result;},
    async poll() {
      assert.equal(timers.size, 1);
      const [id, timer] = timers.entries().next().value;
      assert.equal(timer.delay, 1000);
      timers.delete(id);timer.fn();await settle();
    },
    close() {for (const current of slots) current?.cleanup?.();},
  };
}

test('a failed clip keeps polling active peers, then partial stops without exposing a manifest', async () => {
  const base = {project_id: 'fixture-project', state: 'running', stage: 'local', operation: 'download',
    total: 3, failed: 1, running: 1, items: [{video_id: 'failed', state: 'error', detail: 'Checksum mismatch'}]};
  const hook = mountPreparation([
    {...base, state: 'idle', ready: 0, queued: 3, running: 0, failed: 0, progress: 0},
    {...base, ready: 0, queued: 1, progress: 10},
    {...base, ready: 1, queued: 0, progress: 50},
    {...base, state: 'partial', ready: 2, queued: 0, running: 0, progress: 66.7},
    {...base, ready: 2, failed: 0, queued: 0, running: 1, progress: 66.7},
  ]);
  try {
    await hook.settle();
    assert.equal(hook.result.status.state, 'running');
    assert.equal(hook.result.status.failed, 1);
    assert.equal(hook.result.manifest, null);
    await hook.poll();
    assert.equal(hook.result.status.ready, 1);
    assert.equal(hook.result.status.failed, 1);
    await hook.poll();
    assert.equal(hook.result.status.state, 'partial');
    assert.equal(hook.result.status.ready, 2);
    assert.equal(hook.result.status.failed, 1);
    assert.equal(hook.result.status.progress, 66.7);
    assert.equal(hook.timers.size, 0);
    assert.equal(hook.result.manifest, null);
    assert.deepEqual(hook.requests.map(request => request.method), ['GET', 'POST', 'GET', 'GET']);
    assert.ok(hook.requests.every(request => !request.url.includes('/manifest')));
    await hook.result.start('fixture-project', true);
    await hook.settle();
    assert.equal(hook.requests.length, 5);
    assert.deepEqual(hook.requests[4].body, {retry_failed: true, cache_generation: 7});
    assert.equal(hook.result.status.ready, 2);
    assert.equal(hook.result.status.state, 'running');
  } finally {hook.close();}
});

test('refreshing an already ready project reads its session without restarting preparation', async () => {
  const status = {project_id: 'fixture-project', state: 'ready', total: 1, ready: 1,
    items: [{video_id: 'clip', state: 'ready'}]};
  const manifest = {version: 'ready-version', videos: [{id: 'clip'}]};
  const hook = mountPreparation([status, manifest], true);
  try {
    await hook.settle();
    assert.equal(hook.result.status.state, 'ready');
    assert.equal(hook.result.manifest.version, 'ready-version');
    assert.deepEqual(hook.requests.map(request => [request.method, request.url]), [
      ['GET', '/api/projects/fixture-project/session'],
      ['GET', '/api/projects/fixture-project/session/manifest'],
    ]);
  } finally {hook.close();}
});
