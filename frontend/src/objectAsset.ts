import {authFetch} from './auth.ts';
import {apiOrigin} from './apiOrigin.ts';

export function validAssetUrl(value: string): boolean {
  if (value.startsWith('/api/session-') || /^\/api\/tasks\/items\/[0-9a-f]{32}\/workspace\/session-(media|thumbnails|storyboards)\//.test(value)) return true;
  try {
    const url = new URL(value);
    if (url.protocol === 'https:' && !url.username && !url.password && !url.port && !url.hash && /^[a-f0-9]{32}\.r2\.cloudflarestorage\.com$/.test(url.hostname) &&
        /^\/[a-z0-9][a-z0-9.-]+\/(media|daily)\//.test(url.pathname) &&
        url.searchParams.get('X-Amz-Algorithm') === 'AWS4-HMAC-SHA256' &&
        /^[a-f0-9]{64}$/.test(url.searchParams.get('X-Amz-Signature') ?? '') &&
        Number(url.searchParams.get('X-Amz-Expires')) > 0 && Number(url.searchParams.get('X-Amz-Expires')) <= 3600) return true;
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
