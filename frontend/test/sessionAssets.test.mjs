import test from 'node:test';
import assert from 'node:assert/strict';
import { isSessionManifest, prepareSessionAssets, forgetSessionAssets, sessionAssetUrl, sessionStoryboard, sessionDecodedImages } from '../src/sessionAssets.ts';

const story = {state: 'ready', interval_ms: 1000, tile_width: 160, tile_height: 90, columns: 10, rows: 10, frame_count: 101, sheets: ['/api/session-storyboards/p/v/a/0', '/api/session-storyboards/p/v/a/1']};
const manifest = {version: 'v', videos: [{id: 'a', url: '/api/session-media/p/v/a', fast_url: '/api/session-media/p/v/a?fast=true', thumbnail_url: '/api/session-thumbnails/p/v/a', storyboard: story}]};

test('manifest requires complete, unique, dedicated local playback assets', () => {
  assert.equal(isSessionManifest(manifest), true);
  assert.equal(isSessionManifest({...manifest, version: ''}), false);
  assert.equal(isSessionManifest({...manifest, videos: [...manifest.videos, ...manifest.videos]}), false);
  assert.equal(isSessionManifest({...manifest, videos: [{...manifest.videos[0], url: '/api/media/p/a'}]}), false);
  assert.equal(isSessionManifest({...manifest, videos: [{...manifest.videos[0], storyboard: {...story, sheets: [story.sheets[0]]}}]}), false);
  assert.equal(isSessionManifest(null), false);
});

test('all compressed images are fetched before a manifest becomes visible and reuse is immediate', async (t) => {
  const requests = [];
  const releases = [];
  t.mock.method(globalThis, 'fetch', async (url, options) => {
    assert.equal(options.cache, 'no-store');
    requests.push(url);
    await new Promise(resolve => releases.push(resolve));
    return new Response(new Blob(['jpg'], {type: 'image/jpeg'}));
  });
  const progress = [];
  const promise = prepareSessionAssets('p', manifest, new AbortController().signal, (...args) => progress.push(args));
  assert.equal(requests.length, 3);
  assert.equal(sessionStoryboard('p', 'a'), undefined);
  releases.forEach(release => release());
  await promise;
  assert.equal(sessionStoryboard('p', 'a').sheets.length, 2);
  assert.ok(sessionStoryboard('p', 'a').sheets.every(url => url.startsWith('blob:')));
  assert.ok(sessionAssetUrl('p', manifest.videos[0].thumbnail_url).startsWith('blob:'));
  assert.deepEqual(progress.at(-1), [3, 3]);
  await prepareSessionAssets('p', manifest, new AbortController().signal, () => {});
  assert.equal(requests.length, 3, 'same version reuses compressed assets');
  forgetSessionAssets('p');
  assert.equal(sessionStoryboard('p', 'a'), undefined);
  assert.equal(sessionAssetUrl('p', manifest.videos[0].thumbnail_url), manifest.videos[0].thumbnail_url);
});

test('asset workers are bounded and shared sheets are loaded once', async (t) => {
  const videos = Array.from({length: 5}, (_, index) => ({...manifest.videos[0], id: String(index), thumbnail_url: '/api/session-thumbnails/p/v/' + index}));
  let active = 0, peak = 0;
  const requests = [];
  t.mock.method(globalThis, 'fetch', async url => {
    requests.push(url);
    peak = Math.max(peak, ++active);
    await new Promise(resolve => setImmediate(resolve));
    active--;
    return new Response(new Blob(['jpg'], {type: 'image/jpeg'}));
  });
  await prepareSessionAssets('bounded', {...manifest, videos}, new AbortController().signal, () => {});
  assert.equal(peak, 3);
  assert.equal(requests.length, 7, 'five covers and two shared sprite sheets');
  forgetSessionAssets('bounded');
});

test('switching project during load never publishes partial assets and releases blobs', async (t) => {
  const signal = new AbortController();
  const created = [], revoked = [];
  const create = URL.createObjectURL.bind(URL);
  const revoke = URL.revokeObjectURL.bind(URL);
  t.mock.method(URL, 'createObjectURL', blob => { const url = create(blob); created.push(url); return url; });
  t.mock.method(URL, 'revokeObjectURL', url => { revoked.push(url); revoke(url); });
  t.mock.method(globalThis, 'fetch', async () => new Response(new Blob(['jpg'], {type: 'image/jpeg'})));
  await assert.rejects(prepareSessionAssets('cancelled', manifest, signal.signal, ready => {if (ready === 1) signal.abort();}), {name: 'AbortError'});
  assert.equal(sessionStoryboard('cancelled', 'a'), undefined);
  assert.deepEqual(revoked.sort(), created.sort());
});

test('an invalid image fails the complete warmup instead of entering annotation', async (t) => {
  t.mock.method(globalThis, 'fetch', async () => new Response('not a JPEG', {headers: {'Content-Type': 'application/json'}}));
  await assert.rejects(prepareSessionAssets('invalid', manifest, new AbortController().signal, () => {}), /数据不完整/);
  assert.equal(sessionStoryboard('invalid', 'a'), undefined);
});

test('deleting or switching a project releases its decoded images together with compressed blobs', async (t) => {
  t.mock.method(globalThis, 'fetch', async () => new Response(new Blob(['jpg'], {type:'image/jpeg'})));
  await prepareSessionAssets('released', manifest, new AbortController().signal, () => {});
  const decoded = sessionDecodedImages('released');
  decoded.set('sheet', {src:'decoded-image'});
  assert.equal(decoded.size, 1);
  forgetSessionAssets('released');
  assert.equal(decoded.size, 0, 'held LRU references are cleared as part of project cleanup');
  assert.equal(sessionDecodedImages('released'), undefined);
});
