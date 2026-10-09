import assert from 'node:assert/strict';
import {test} from 'node:test';
import {browserVideoUrl, clearBrowserVideoCache, forgetBrowserVideoUrls, prepareBrowserVideos} from '../src/browserVideoCache.ts';

function missing() { return new DOMException('Missing', 'NotFoundError'); }

class Directory {
  constructor() { this.directories = new Map(); this.files = new Map(); }
  async getDirectoryHandle(name, options = {}) {
    if (!this.directories.has(name)) {
      if (!options.create) throw missing();
      this.directories.set(name, new Directory());
    }
    return this.directories.get(name);
  }
  async getFileHandle(name, options = {}) {
    if (!this.files.has(name) && !options.create) throw missing();
    return {
      getFile: async () => new File([this.files.get(name) ?? new Uint8Array()], name),
      createWritable: async () => {
        const chunks = [];
        if (name.endsWith('.size')) return {
          write: async chunk => chunks.push(new TextEncoder().encode(chunk)),
          close: async () => this.files.set(name, Buffer.concat(chunks)),
        };
        return new WritableStream({
          write: chunk => chunks.push(Buffer.from(chunk)),
          close: () => this.files.set(name, Buffer.concat(chunks)),
        });
      },
    };
  }
  async removeEntry(name) {
    if (this.files.has(name)) this.files.delete(name);
    else if (this.directories.has(name)) this.directories.delete(name);
    else throw missing();
  }
}

test('downloads both complete previews, reuses them, then clears only the chosen project', async () => {
  const oldNavigator = globalThis.navigator;
  const oldFetch = globalThis.fetch;
  const root = new Directory();
  Object.defineProperty(globalThis, 'navigator', {configurable: true, value: {storage: {getDirectory: async () => root}}});
  let requests = 0;
  globalThis.fetch = async () => {
    requests++;
    const bytes = new Uint8Array([1, 2, 3, 4]);
    return new Response(bytes, {headers: {'content-type': 'video/mp4', 'content-length': String(bytes.length)}});
  };
  const manifest = {version: 'version_1', videos: [{id: 'video_1', url: '/api/session-media/p/normal', fast_url: '/api/session-media/p/fast'}]};
  try {
    const progress = [];
    await prepareBrowserVideos('project_1', manifest, new AbortController().signal, (ready, total) => progress.push([ready, total]));
    assert.equal(requests, 2);
    assert.deepEqual(progress.at(-1), [2, 2]);
    assert.match(browserVideoUrl('project_1', manifest.videos[0].url), /^blob:/);
    forgetBrowserVideoUrls('project_1');
    await prepareBrowserVideos('project_1', manifest, new AbortController().signal, () => {});
    assert.equal(requests, 2, 'persistent files should be reused without another network read');
    await clearBrowserVideoCache('project_1');
    assert.equal(browserVideoUrl('project_1', manifest.videos[0].url), manifest.videos[0].url);
    await prepareBrowserVideos('project_1', manifest, new AbortController().signal, () => {});
    assert.equal(requests, 4, 'cleared files must be downloaded again');
  } finally {
    forgetBrowserVideoUrls('project_1');
    globalThis.fetch = oldFetch;
    Object.defineProperty(globalThis, 'navigator', {configurable: true, value: oldNavigator});
  }
});
