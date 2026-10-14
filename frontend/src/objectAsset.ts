import {authFetch} from './auth.ts';
import {apiOrigin} from './apiOrigin.ts';

export function validAssetUrl(value: string): boolean {
  if (value.startsWith('/api/session-') || /^\/api\/tasks\/items\/[0-9a-f]{32}\/workspace\/session-(media|thumbnails|storyboards)\//.test(value)) return true;
  try {
    const url = new URL(value);
    const expected = apiOrigin || (typeof window === 'undefined' ? '' : window.location.origin);
    return !!expected && url.origin === expected && (url.pathname.startsWith('/mock-objects/media/')||(url.pathname.startsWith('/mock-objects/daily/')&&url.pathname.endsWith('.mp4'))) &&
      (url.protocol === 'https:' || (url.protocol === 'http:' && ['localhost', '127.0.0.1'].includes(url.hostname)));
  } catch { return false; }
}

export function fetchAsset(url: string, init: RequestInit = {}): Promise<Response> {
  if (!validAssetUrl(url)) throw new Error('播放素材地址不受信任。');
  if (url.startsWith('/api/')) return authFetch(url, init);
  return fetch(url, {...init, credentials: 'omit'});
}
