import { useCallback, useEffect, useRef, useState } from 'react';
import {authFetch} from './auth.ts';
import type { PreparationStatus } from './PreparePanel';
import { forgetSessionAssets, isSessionManifest, prepareSessionAssets } from './sessionAssets';
import type { SessionManifest } from './sessionAssets';
import { preparationFailureMessage } from './preparationErrors';

export function usePreparation(projectId: string | undefined, cacheGeneration = 0) {
  const projectRef = useRef(projectId);
  projectRef.current = projectId;
  const retryFailed = useRef(false);
  const [result, setResult] = useState<{id: string; status: PreparationStatus | null; manifest: SessionManifest | null; error: string}>({id: '', status: null, manifest: null, error: ''});
  const [startingId, setStartingId] = useState('');
  const [epoch, setEpoch] = useState(0);

  useEffect(() => {
    if (!projectId) return;
    const id = projectId;
    let stopped = false;
    let retryDelay = 5000;
    let timer: ReturnType<typeof setTimeout>;
    const abort = new AbortController();
    setResult({id, status: null, manifest: null, error: ''});
    const publish = (status: PreparationStatus, manifest: SessionManifest | null = null) => {
      if (!stopped) setResult({id, status, manifest, error: ''});
    };
    const read = async (start: boolean): Promise<void> => {
      try {
        if (start) setStartingId(id);
        const response = await authFetch('/api/projects/' + id + (start ? '/session/prepare' : '/session'), {
          ...(start ? {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({retry_failed: retryFailed.current, cache_generation: cacheGeneration})} : {}),
          cache: 'no-store', signal: AbortSignal.any([abort.signal, AbortSignal.timeout(60000)]),
        });
        const body = await response.json();
        if (!response.ok) {
          let capabilities: string[] | undefined;
          if (response.status === 404) {
            try {
              const healthResponse = await fetch('/api/health', {cache: 'no-store', signal: AbortSignal.any([abort.signal, AbortSignal.timeout(8000)])});
              const health = await healthResponse.json();
              if (healthResponse.ok && Array.isArray(health.capabilities)) capabilities = health.capabilities;
            } catch { /* Preserve the original failure when health cannot be read. */ }
          }
          throw new Error(preparationFailureMessage(response.status, body.detail, capabilities));
        }
        if (stopped) return;
        retryFailed.current = false;
        const status = body as PreparationStatus;
        if (status.state === 'ready') {
          publish({...status, state: 'running', checking: false, stage: 'browser', progress: 0, detail: '正在载入封面和悬停预览图片'});
          const manifestResponse = await fetch('/api/projects/' + id + '/session/manifest', {cache: 'no-store', signal: AbortSignal.any([abort.signal, AbortSignal.timeout(30000)])});
          const manifest = await manifestResponse.json();
          if (!manifestResponse.ok) throw new Error(typeof manifest.detail === 'string' ? manifest.detail : '无法读取播放素材列表');
          if (!isSessionManifest(manifest) || manifest.videos.length !== status.total ||
            (status.items.length > 0 && status.items.some(item => !manifest.videos.some(video => video.id === item.video_id)))) {
            throw new Error('播放素材列表不完整，请重新准备。');
          }
          await prepareSessionAssets(id, manifest, abort.signal, (ready, total) => {
            publish({...status, state: 'running', checking: false, stage: 'browser', progress: total ? ready / total * 100 : 100,
              detail: '正在载入预览图片 ' + ready + ' / ' + total});
          });
          if (!stopped) publish({...status, state: 'ready', checking: false, stage: 'ready', progress: 100}, manifest);
          return;
        }
        publish(status);
        retryDelay = 5000;
        if (status.state === 'running') timer = setTimeout(() => void read(false), 1000);
      } catch (error) {
        if (stopped) return;
        setResult(previous => ({id, status: previous.id === id ? previous.status : null, manifest: null, error: (error as Error).message}));
        timer = setTimeout(() => void read(start), retryDelay);
        retryDelay = Math.min(retryDelay * 2, 30000);
      } finally {
        if (start && !stopped) setStartingId(previous => previous === id ? '' : previous);
      }
    };
    void read(true);
    return () => {
      stopped = true;
      clearTimeout(timer);
      abort.abort();
      forgetSessionAssets(id);
    };
  }, [projectId, cacheGeneration, epoch]);

  const refresh = useCallback(() => {
    retryFailed.current = false;
    setResult({id: projectRef.current ?? '', status: null, manifest: null, error: ''});
    setEpoch(value => value + 1);
  }, []);

  const start = useCallback(async (id: string, retry = false) => {
    if (projectRef.current !== id) return;
    retryFailed.current = retry;
    setResult({id, status: null, manifest: null, error: ''});
    setEpoch(value => value + 1);
  }, []);

  return {refresh, manifest: result.id === projectId ? result.manifest : null,
    status: result.id === projectId ? result.status : null, error: result.id === projectId ? result.error : '',
    busy: startingId === projectId && !!projectId, start};
}
