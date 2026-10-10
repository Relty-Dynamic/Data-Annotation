import assert from 'node:assert/strict';
import {test} from 'node:test';
import {fetchAsset, validAssetUrl} from '../src/objectAsset.ts';

test('mock object downloads use the API host without browser credentials', async () => {
  const previousWindow = globalThis.window;
  const previousFetch = globalThis.fetch;
  globalThis.window = {location: {origin: 'http://127.0.0.1:8765'}};
  let received;
  globalThis.fetch = async (url, init) => { received = {url, init}; return new Response('ok'); };
  try {
    const url = 'http://127.0.0.1:8765/mock-objects/media/project/version/video/normal.mp4?expires=1&signature=x';
    assert.equal(validAssetUrl(url), true);
    await fetchAsset(url);
    assert.equal(received.url, url);
    assert.equal(received.init.credentials, 'omit');
    assert.equal(validAssetUrl('https://attacker.example/mock-objects/media/video'), false);
    assert.throws(() => fetchAsset('https://attacker.example/mock-objects/media/video'), /不受信任/);
  } finally {
    globalThis.window = previousWindow;
    globalThis.fetch = previousFetch;
  }
});
