import {apiUrl, separateApiOrigin} from './apiOrigin.ts';

export type Account = {id:string; username:string; display_name:string; role:'admin'|'annotator'};
let csrfToken = '';
export function setCsrfToken(value: string) { csrfToken = value; }

function csrfCookie(): string {
  const item = document.cookie.split('; ').find(part => part.startsWith('datamark_csrf='));
  return item ? decodeURIComponent(item.slice('datamark_csrf='.length)) : '';
}

export function authFetch(input: RequestInfo | URL, init: RequestInit = {}): Promise<Response> {
  const method = (init.method ?? 'GET').toUpperCase();
  const headers = new Headers(init.headers);
  if (!['GET', 'HEAD', 'OPTIONS'].includes(method)) {
    const token = csrfToken || (!separateApiOrigin ? csrfCookie() : '');
    if (token) headers.set('X-CSRF-Token', token);
  }
  return fetch(typeof input === 'string' ? apiUrl(input) : input,
    {...init, headers, credentials: separateApiOrigin ? 'include' : 'same-origin'});
}
