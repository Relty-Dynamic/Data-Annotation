import test from 'node:test';
import assert from 'node:assert/strict';
import { resolvePreviewStatus } from '../src/usePreview.ts';

test('repreparing the same project and video immediately selects its new local version', () => {
  for (const suffix of ['', '?fast=true']) {
    const key = 'project/video' + suffix;
    const old = {key, status: {state: 'ready', progress: null, url: '/api/session-media/project/old/video' + suffix}};
    const next = '/api/session-media/project/new/video' + suffix;
    assert.equal(resolvePreviewStatus(key, next, old).url, next);
    assert.equal(resolvePreviewStatus(key, next, old).state, 'ready');
  }
});

test('a disabled or different video never receives another video result', () => {
  const old = {key: 'project/video', status: {state: 'ready', progress: null, url: '/api/session-media/project/old/video'}};
  assert.equal(resolvePreviewStatus('', undefined, old).state, 'idle');
  assert.equal(resolvePreviewStatus('project/other', undefined, old).url, null);
});

test('annotation mode cannot reuse a legacy remote result when the local asset is missing', () => {
  const old = {key:'project/video', status:{state:'ready', progress:null, url:'/api/media/project/video'}};
  const missing = resolvePreviewStatus('project/video', undefined, old, true);
  assert.equal(missing.state, 'error');
  assert.equal(missing.url, null);
  assert.match(missing.detail, /本机播放素材/);
  assert.equal(resolvePreviewStatus('', undefined, old, true).state, 'idle');
  const local = resolvePreviewStatus('project/video', '/api/session-media/project/new/video', old, true);
  assert.equal(local.state, 'ready');
  assert.match(local.url, /^\/api\/session-media\//);
});
