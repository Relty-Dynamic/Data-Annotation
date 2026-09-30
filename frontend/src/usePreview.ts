import { useEffect, useState } from 'react';
import {authFetch} from './auth.ts';

export interface PreviewStatus {
  state: 'idle' | 'queued' | 'running' | 'ready' | 'error';
  progress: number | null;
  url: string | null;
  detail?: string | null;
}
const readyUrls = new Map<string, string>();
export function rememberPreparedPreviews(projectId: string, videos: Array<{id: string; url: string; fast_url?: string}>) {
  for (const video of videos) {
    readyUrls.set(`${projectId}/${video.id}`, video.url);
    readyUrls.set(`${projectId}/${video.id}?fast=true`, video.fast_url ?? `${video.url}?fast=true`);
  }
}
export function forgetProjectPreviews(projectId: string) {
  for (const key of readyUrls.keys()) if (key.startsWith(projectId + '/')) readyUrls.delete(key);
}
const idle: PreviewStatus = { state: 'idle', progress: null, url: null };

/** The newly prepared manifest wins before React effects flush an older result. */
export function resolvePreviewStatus(key: string, knownUrl: string | undefined, result: {key: string; status: PreviewStatus}, localOnly = false): PreviewStatus {
  if (knownUrl) return {...idle, state: 'ready', url: knownUrl};
  if (localOnly && key) return {...idle, state: 'error', detail: '本机播放素材尚未就绪，请重新准备。'};
  return result.key === key ? result.status : idle;
}

export function usePreview(projectId: string | undefined, videoId: string, version: number, enabled = true, fast = false, localOnly = false) {
  const key = enabled && projectId && videoId ? `${projectId}/${videoId}${fast ? "?fast=true" : ""}` : '';
  const [result, setResult] = useState<{key: string; status: PreviewStatus}>({key: '', status: idle});
  const cached = readyUrls.get(key);
  const status = resolvePreviewStatus(key, cached, result, localOnly);

  useEffect(() => {
    if (!key) return;
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout>;
    const controller = new AbortController();
    const known = readyUrls.get(key);
    setResult({key, status: known ? {...idle, state: 'ready', url: known} : idle});
    let failures = 0;
    async function check(start: boolean) {
      try {
        const response = await authFetch(`/api/previews/${key}`, {
          ...(start ? {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({prefetch: true, retry: true})} : {}),
          signal: AbortSignal.any([controller.signal, AbortSignal.timeout(10000)]),
        });
        const body = await response.json();
        if (!response.ok) throw new Error(typeof body.detail === 'string' ? body.detail : '预览状态暂时无法读取');
        if (cancelled) return;
        const next = body as PreviewStatus;
        failures = 0;
        if (next.state === 'ready' && next.url) readyUrls.set(key, next.url);
        if (next.state === 'error') readyUrls.delete(key);
        setResult({key, status: next});
        if (next.state !== 'ready' && next.state !== 'error') timer = setTimeout(() => void check(false), 700);
      } catch (error) {
        if (cancelled) return;
        if (++failures < 3) timer = setTimeout(() => void check(start), 1200);
        else {
          readyUrls.delete(key);
          setResult({key, status: {state: 'error', progress: null, url: null, detail: (error as Error).message}});
        }
      }
    }
    // Batch preparation already verified these files. Re-scanning this clip
    // and two neighbours on every click adds NAS round trips to playback.
    // A media error clears this cache before the explicit retry.
    if (!known && !localOnly) timer = setTimeout(() => void check(true), 160);
    return () => {cancelled = true; clearTimeout(timer); controller.abort();};
  }, [key, version, localOnly]);
  return status;
}
