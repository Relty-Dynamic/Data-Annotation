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


test('R2 accepts signed HTTPS objects and omits account cookies', async () => {
  const url = 'https://' + 'a'.repeat(32) + '.r2.cloudflarestorage.com/test-bucket/media/project/normal.mp4?X-Amz-Algorithm=AWS4-HMAC-SHA256&X-Amz-Signature=' + 'b'.repeat(64) + '&X-Amz-Expires=1800';
  assert.equal(validAssetUrl(url), true);
  assert.equal(validAssetUrl(url.replace('https:', 'http:')), false);
  assert.equal(validAssetUrl(url.replace('Expires=1800', 'Expires=7200')), false);
  assert.equal(validAssetUrl(url.split('?')[0]), false);
  assert.equal(validAssetUrl(url.replace('/media/', '/private/')), false);
  const original = globalThis.fetch;
  globalThis.fetch = async (value, init) => {
    assert.equal(value, url);
    assert.equal(init.credentials, 'omit');
    return new Response('fixture');
  };
  try { await fetchAsset(url, {credentials: 'include'}); }
  finally { globalThis.fetch = original; }
});
