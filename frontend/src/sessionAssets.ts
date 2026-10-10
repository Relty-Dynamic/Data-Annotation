import { isStoryboardManifest } from './storyboard.ts';
import {apiUrl} from './apiOrigin.ts';
import {fetchAsset, validAssetUrl} from './objectAsset.ts';
import type { StoryboardManifest } from './storyboard.ts';

export interface SessionVideo {
  id: string;
  url: string;
  fast_url: string;
  thumbnail_url: string;
  storyboard: StoryboardManifest;
}
export interface SessionManifest { version: string; videos: SessionVideo[] }
type AssetCache = {version: string; urls: Map<string, string>; objects: string[]; manifests: Map<string, StoryboardManifest>; decoded: Map<string, HTMLImageElement>};
const caches = new Map<string, AssetCache>();

export function isSessionManifest(value: unknown): value is SessionManifest {
  if (!value || typeof value !== 'object') return false;
  const manifest = value as SessionManifest;
  if (typeof manifest.version !== 'string' || !manifest.version || !Array.isArray(manifest.videos) || !manifest.videos.length) return false;
  return new Set(manifest.videos.map(video => video?.id)).size === manifest.videos.length &&
    manifest.videos.every(video => video && typeof video.id === 'string' && video.id &&
      [video.url, video.fast_url, video.thumbnail_url].every(url => typeof url === 'string' && validAssetUrl(url)) &&
      isStoryboardManifest(video.storyboard) && video.storyboard.sheets.every(url => validAssetUrl(url)));
}

export function forgetSessionAssets(projectId: string) {
  const cache = caches.get(projectId);
  if (!cache) return;
  caches.delete(projectId);
  cache.decoded.clear();
  for (const url of cache.urls.values()) URL.revokeObjectURL(url);
}

export function sessionAssetUrl(projectId: string, url: string): string {
  return caches.get(projectId)?.urls.get(url) ?? apiUrl(url);
}

export function sessionDecodedImages(projectId: string): Map<string, HTMLImageElement> | undefined {
  return caches.get(projectId)?.decoded;
}

export function sessionStoryboard(projectId: string, videoId: string): StoryboardManifest | undefined {
  return caches.get(projectId)?.manifests.get(videoId);
}

/** Keep JPEGs compressed. HoverPreview only decodes its bounded active-sheet LRU. */
export async function prepareSessionAssets(
  projectId: string,
  manifest: SessionManifest,
  signal: AbortSignal,
  onProgress: (ready: number, total: number) => void,
): Promise<void> {
  const assets = [...new Set(manifest.videos.flatMap(video => [video.thumbnail_url, ...video.storyboard.sheets]))];
  const existing = caches.get(projectId);
  if (existing?.version === manifest.version && existing.objects.length === assets.length) {
    const urls = new Map(assets.map((url, index) => [url, existing.objects[index]]));
    const manifests = new Map(manifest.videos.map(video => [video.id, {
      ...video.storyboard, sheets: video.storyboard.sheets.map(url => urls.get(url)!),
    }]));
    caches.set(projectId, {...existing, urls, manifests});
    onProgress(assets.length, assets.length);
    return;
  }
  const urls = new Map<string, string>();
  let cursor = 0;
  let completed = 0;
  const controller = new AbortController();
  const combined = AbortSignal.any([signal, controller.signal]);
  const worker = async () => {
    while (cursor < assets.length) {
      combined.throwIfAborted();
      const url = assets[cursor++];
      const response = await fetchAsset(url, {cache: 'no-store', signal: AbortSignal.any([combined, AbortSignal.timeout(30000)])});
      if (!response.ok) throw new Error('预览图片读取失败，请重试准备素材。');
      const blob = await response.blob();
      if (!blob.size || !blob.type.startsWith('image/')) throw new Error('预览图片数据不完整，请重试准备素材。');
      combined.throwIfAborted();
      urls.set(url, URL.createObjectURL(blob));
      completed += 1;
      onProgress(completed, assets.length);
    }
  };
  onProgress(0, assets.length);
  try {
    await Promise.all(Array.from({length: Math.min(3, assets.length)}, async () => {
      try { await worker(); } catch (error) { controller.abort(); throw error; }
    }));
    combined.throwIfAborted();
    const manifests = new Map(manifest.videos.map(video => [video.id, {
      ...video.storyboard, sheets: video.storyboard.sheets.map(url => urls.get(url)!),
    }]));
    forgetSessionAssets(projectId);
    caches.set(projectId, {version: manifest.version, urls, objects: assets.map(url => urls.get(url)!), manifests, decoded: new Map()});
  } catch (error) {
    controller.abort();
    for (const url of urls.values()) URL.revokeObjectURL(url);
    throw error;
  }
}
