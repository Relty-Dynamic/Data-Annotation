export type Account = {id:string; username:string; display_name:string; role:'admin'|'annotator'};

function csrfCookie(): string {
  const item = document.cookie.split('; ').find(part => part.startsWith('datamark_csrf='));
  return item ? decodeURIComponent(item.slice('datamark_csrf='.length)) : '';
}

export function authFetch(input: RequestInfo | URL, init: RequestInit = {}): Promise<Response> {
  const method = (init.method ?? 'GET').toUpperCase();
  const headers = new Headers(init.headers);
  if (!['GET', 'HEAD', 'OPTIONS'].includes(method)) {
    const token = csrfCookie();
    if (token) headers.set('X-CSRF-Token', token);
  }
  return fetch(input, {...init, headers, credentials: 'same-origin'});
}
