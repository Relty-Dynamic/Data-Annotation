const configuredOrigin = import.meta.env?.VITE_DATAMARK_API_ORIGIN?.trim() ?? '';

export function validateApiOrigin(origin: string): string {
  if (!origin) return '';
  const parsed = new URL(origin);
  if (parsed.protocol !== 'https:' || parsed.origin !== origin ||
      parsed.username || parsed.password) {
    throw new Error('VITE_DATAMARK_API_ORIGIN 必须是无路径的 HTTPS 域名，可指定端口。');
  }
  return origin;
}

export const apiOrigin = validateApiOrigin(configuredOrigin);
export const separateApiOrigin = !!apiOrigin;

export function apiUrl(path: string, origin = apiOrigin): string {
  return path.startsWith('/api/') ? `${origin}${path}` : path;
}

export function apiWebSocketUrl(pageOrigin: string, origin = apiOrigin): string {
  return `${(origin || pageOrigin).replace(/^http/, 'ws')}/api/browser/connection`;
}
