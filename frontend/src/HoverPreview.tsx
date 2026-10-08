import { useEffect, useState } from 'react';
import {authFetch} from './auth.ts';
import {apiUrl, separateApiOrigin} from './apiOrigin.ts';
import { sessionAssetUrl, sessionStoryboard, sessionDecodedImages } from './sessionAssets';
import { formatTime, type Video } from './domain';
import { isStoryboardManifest, storyboardFrame, spritePosition } from './storyboard';
import type { StoryboardFrame, StoryboardManifest, StoryboardStatus } from './storyboard';

const manifests = new Map<string, StoryboardManifest>();
// Eight 1600 × 900 sheets bound decoded image memory to roughly 44 MB.
const legacyImages = new Map<string, HTMLImageElement>();
const IMAGE_LIMIT = 8;
const MANIFEST_LIMIT = 64;

function remember<T>(cache: Map<string, T>, key: string, value: T, limit: number, dispose?: (value: T) => void) {
  cache.delete(key);
  cache.set(key, value);
  while (cache.size > limit) {
    const oldest = cache.keys().next().value!;
    const removed = cache.get(oldest)!;
    cache.delete(oldest);
    dispose?.(removed);
  }
}

function releaseLegacyImage(image: HTMLImageElement) {
  if (image.src.startsWith('blob:')) URL.revokeObjectURL(image.src);
}

/** Hover only reads prepared images; it never seeks or creates a video decoder. */
export default function HoverPreview({ projectId, video, time }: { projectId: string; video?: Video; time: number }) {
  const endpoint = video ? `/api/storyboards/${projectId}/${video.id}` : '';
  const sourceKey = video ? `${endpoint}|${video.url}|${video.duration_ms}` : '';
  const [result, setResult] = useState<{key: string; status: StoryboardStatus}>({key: '', status: {state: 'idle'}});
  const [loaded, setLoaded] = useState('');
  const [imageFailure, setImageFailure] = useState('');
  const [imageAttempt, setImageAttempt] = useState(0);
  const [cover, setCover] = useState('');
  const [lastFrame, setLastFrame] = useState<{key: string; frame: StoryboardFrame} | null>(null);
  const seeded = video ? sessionStoryboard(projectId, video.id) : undefined;
  const manifest = seeded ?? manifests.get(sourceKey) ?? (result.key === sourceKey && result.status.state === 'ready' ? result.status : undefined);
  const status = manifest ?? (result.key === sourceKey ? result.status : {state: 'idle'});
  const images = sessionDecodedImages(projectId) ?? legacyImages;
  const target = video && manifest ? storyboardFrame(manifest, time - video.start_ms) : undefined;
  const targetReady = !!target && (loaded === target.url || images.has(target.url));
  const displayed = targetReady ? target : lastFrame?.key === sourceKey ? lastFrame.frame : undefined;
  const coverReady = !!video?.thumbnail_url && cover === `${sourceKey}|${video.thumbnail_url}`;
  const isOldFrame = !!displayed && !targetReady;

  useEffect(() => {
    if (!sourceKey || seeded || video?.url.startsWith('/api/session-') || manifests.has(sourceKey)) return;
    let cancelled = false;
    let failures = 0;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const controller = new AbortController();
    const read = async () => {
      let delay = 1500;
      try {
        const response = await authFetch(endpoint, {
          signal: AbortSignal.any([controller.signal, AbortSignal.timeout(8000)]),
        });
        if (!response.ok) throw new Error('Storyboard status unavailable');
        const data = await response.json() as StoryboardStatus;
        if (cancelled) return;
        if (isStoryboardManifest(data)) {
          remember(manifests, sourceKey, data, MANIFEST_LIMIT);
          setResult({key: sourceKey, status: data});
          return;
        }
        if (!['idle', 'queued', 'running', 'error'].includes(data.state)) throw new Error('Invalid storyboard manifest');
        setResult({key: sourceKey, status: data});
        failures = data.state === 'error' ? failures + 1 : 0;
        if (failures) delay = Math.min(30000, 5000 * 2 ** (failures - 1));
      } catch {
        if (cancelled) return;
        failures += 1;
        setResult({key: sourceKey, status: {state: 'error'}});
        delay = Math.min(30000, 5000 * 2 ** (failures - 1));
      }
      if (!cancelled) timer = setTimeout(read, delay);
    };
    void read();
    return () => { cancelled = true; controller.abort(); clearTimeout(timer); };
  }, [sourceKey, endpoint, seeded]);

  useEffect(() => {
    if (!target?.url) return;
    const url = target.url;
    const cached = images.get(url);
    if (cached) {
      remember(images, url, cached, IMAGE_LIMIT, images === legacyImages ? releaseLegacyImage : undefined);
      setLoaded(url);
      return;
    }
    let cancelled = false;
    let retry: ReturnType<typeof setTimeout> | undefined;
    let blobUrl = '';
    let remembered = false;
    const controller = new AbortController();
    const image = new Image();
    image.decoding = 'async';
    const failed = () => {
      if (cancelled) return;
      clearTimeout(timeout);
      image.removeEventListener('load', ready);
      image.removeEventListener('error', failed);
      image.removeAttribute('src');
      controller.abort();
      if (blobUrl) { URL.revokeObjectURL(blobUrl); blobUrl = ''; }
      setImageFailure(url);
      retry = setTimeout(() => setImageAttempt((attempt) => attempt + 1), 5000);
    };
    const ready = () => {
      if (cancelled) return;
      clearTimeout(timeout);
      remember(images, url, image, IMAGE_LIMIT, images === legacyImages ? releaseLegacyImage : undefined);
      remembered = true;
      setImageFailure('');
      setLoaded(url);
    };
    const timeout = setTimeout(failed, 8000);
    image.addEventListener('load', ready);
    image.addEventListener('error', failed);
    if (separateApiOrigin && url.startsWith('/api/')) {
      void authFetch(url, {signal: controller.signal}).then(async response => {
        if (!response.ok) throw new Error('Storyboard image unavailable');
        const blob = await response.blob();
        if (cancelled) return;
        blobUrl = URL.createObjectURL(blob);
        image.src = blobUrl;
      }).catch(() => { if (!cancelled) failed(); });
    } else image.src = url;
    return () => {
      cancelled = true;
      clearTimeout(timeout);
      clearTimeout(retry);
      controller.abort();
      image.removeEventListener('load', ready);
      image.removeEventListener('error', failed);
      if (!image.complete) image.removeAttribute('src');
      if (blobUrl && !remembered) URL.revokeObjectURL(blobUrl);
    };
  }, [target?.url, sourceKey, imageAttempt, images]);

  useEffect(() => {
    if (target && targetReady) setLastFrame({key: sourceKey, frame: target});
  }, [sourceKey, target?.url, target?.index, targetReady]);

  let notice = '';
  if (video && !targetReady) {
    notice = target ? imageFailure === target.url ? '缩略图读取失败，正在重试' : '加载缩略图…'
      : status.state === 'error' ? '悬停预览暂不可用'
      : status.state === 'idle' ? '请先准备全部预览'
      : '悬停预览准备中';
  }
  const state = !video ? 'gap' : targetReady ? 'ready' : displayed ? 'previous' : coverReady ? 'cover' : 'loading';

  return <>
    <div className="tl-hover-picture" data-preview-state={state} data-video-id={video?.id ?? ''} data-frame-index={displayed?.index}>
      {displayed ? <div className="tl-hover-sprite" role="img" aria-label={`预览帧 ${formatTime((video?.start_ms ?? 0) + displayed.localTime)}`} style={{
        aspectRatio: displayed.aspectRatio,
        backgroundImage: `url(${JSON.stringify(images.get(displayed.url)?.src ?? apiUrl(displayed.url))})`,
        backgroundSize: `${displayed.columns * 100}% ${displayed.rows * 100}%`,
        backgroundPosition: spritePosition(displayed),
      }} /> : video?.thumbnail_url && <img key={sourceKey} className="tl-hover-cover" crossOrigin={separateApiOrigin?'use-credentials':undefined} src={sessionAssetUrl(projectId, video.thumbnail_url)} alt="片段封面" onLoad={() => setCover(`${sourceKey}|${video.thumbnail_url}`)} />}
      {!video && <div className="tl-hover-placeholder"><span>此处为断录间隔</span></div>}
      {video && !displayed && !coverReady && <div className="tl-hover-placeholder"><span>{notice}</span></div>}
      {notice && (displayed || coverReady) && <div className="tl-hover-notice">{notice}</div>}
      {(displayed || coverReady) && <div className="tl-hover-frame-label">{displayed
        ? `${isOldFrame ? '上次' : ''}预览帧 ${formatTime((video?.start_ms ?? 0) + displayed.localTime)} · 每秒采样`
        : '片段封面 · 非当前位置画面'}</div>}
    </div>
    <div className="tl-hover-file">{video?.name ?? '无录制视频'}</div>
  </>;
}
