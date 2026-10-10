import test from 'node:test';
import assert from 'node:assert/strict';
import {apiUrl, apiWebSocketUrl, validateApiOrigin} from '../src/apiOrigin.ts';

test('public API uses a separate exact HTTPS host for requests and browser connection', () => {
  const origin = validateApiOrigin('https://api-annotate.reltydynamic.com:10443');
  assert.equal(apiUrl('/api/projects?x=1', origin), 'https://api-annotate.reltydynamic.com:10443/api/projects?x=1');
  assert.equal(apiWebSocketUrl('https://annotate.reltydynamic.com', origin),
    'wss://api-annotate.reltydynamic.com:10443/api/browser/connection');
  assert.equal(apiUrl('blob:local-preview', origin), 'blob:local-preview');
  for (const invalid of ['http://api.example.com', 'https://api.example.com/path',
                         'https://api.example.com:invalid', 'https://user:pass@api.example.com']) {
    assert.throws(() => validateApiOrigin(invalid));
  }
});

test('local and intranet modes keep same-origin API and browser connection', () => {
  assert.equal(apiUrl('/api/projects', ''), '/api/projects');
  assert.equal(apiWebSocketUrl('http://127.0.0.1:8765', ''),
    'ws://127.0.0.1:8765/api/browser/connection');
});
