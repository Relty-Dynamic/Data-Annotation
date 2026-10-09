import {authFetch} from './auth.ts';
import {apiUrl} from './apiOrigin.ts';
import type {SessionManifest} from './sessionAssets.ts';

// Origin-private files live on the annotator's computer. Only disposable
// compact MP4 previews are stored here; annotations remain on the platform.
const ROOT = 'datamark-playback-v1';
const live = new Map<string, {version: string; urls: Map<string, string>}>();

function validPart(value: string): boolean {
  return /^[a-zA-Z0-9_-]{1,80}$/.test(value);
}

async function projectDirectory(projectId: string, create: boolean): Promise<FileSystemDirectoryHandle> {
  if (!validPart(projectId) || !navigator.storage?.getDirectory) {
    throw new Error('此浏览器不支持本机播放缓存；请使用支持本机存储的最新版浏览器。');
  }
  const root = await navigator.storage.getDirectory();
  const cache = await root.getDirectoryHandle(ROOT, {create});
  return cache.getDirectoryHandle(projectId, {create});
}

export function browserVideoUrl(projectId: string, url: string): string {
  return live.get(projectId)?.urls.get(url) ?? apiUrl(url);
}

export function forgetBrowserVideoUrls(projectId: string): void {
  const current = live.get(projectId);
  if (!current) return;
  live.delete(projectId);
  for (const url of current.urls.values()) URL.revokeObjectURL(url);
}

export async function clearBrowserVideoCache(projectId: string): Promise<void> {
  forgetBrowserVideoUrls(projectId);
  if (!validPart(projectId) || !navigator.storage?.getDirectory) return;
  const root = await navigator.storage.getDirectory();
  try {
    const cache = await root.getDirectoryHandle(ROOT);
    await cache.removeEntry(projectId, {recursive: true});
  } catch (error) {
    if (!(error instanceof DOMException && error.name === 'NotFoundError')) throw error;
  }
}

export async function prepareBrowserVideos(
  projectId: string,
  manifest: SessionManifest,
  signal: AbortSignal,
  onProgress: (ready: number, total: number) => void,
): Promise<void> {
  if (!validPart(manifest.version) || manifest.videos.some(video => !validPart(video.id))) {
    throw new Error('播放素材版本或视频编号无效，无法保存到本机。');
  }
  const files = manifest.videos.flatMap(video => [
    {name: `${video.id}-normal.mp4`, url: video.url},
    {name: `${video.id}-fast.mp4`, url: video.fast_url},
  ]);
  const previous = live.get(projectId);
  if (previous?.version === manifest.version && previous.urls.size === files.length) {
    onProgress(files.length, files.length);
    return;
  }
  const parent = await projectDirectory(projectId, true);
  const directory = await parent.getDirectoryHandle(manifest.version, {create: true});
  const urls = new Map<string, string>();
  onProgress(0, files.length);
  try {
    for (const [index, item] of files.entries()) {
      signal.throwIfAborted();
      let file: File | null = null;
      try {
        file = await (await directory.getFileHandle(item.name)).getFile();
        const sizeFile = await (await directory.getFileHandle(item.name + '.size')).getFile();
        if (!file.size || file.size !== Number(await sizeFile.text())) file = null;
      } catch (error) {
        if (!(error instanceof DOMException && error.name === 'NotFoundError')) throw error;
      }
      if (!file) {
        const response = await authFetch(item.url, {cache: 'no-store', signal});
        if (!response.ok || !response.body || !response.headers.get('content-type')?.startsWith('video/mp4')) {
          throw new Error('精简视频下载失败，请检查网络后重试。');
        }
        const expected = Number(response.headers.get('content-length'));
        if (!Number.isSafeInteger(expected) || expected <= 0) throw new Error('服务器未提供完整视频大小，无法核对本机缓存。');
        const handle = await directory.getFileHandle(item.name, {create: true});
        const writable = await handle.createWritable();
        try {
          await response.body.pipeTo(writable, {signal});
        } catch (error) {
          await directory.removeEntry(item.name).catch(() => {});
          if (error instanceof DOMException && error.name === 'QuotaExceededError') throw new Error('此电脑的浏览器存储空间不足，无法保存播放缓存。');
          throw error;
        }
        file = await handle.getFile();
        if (file.size !== expected) {
          await directory.removeEntry(item.name);
          throw new Error('本机预览下载不完整，请重新准备。');
        }
        const marker = await (await directory.getFileHandle(item.name + '.size', {create: true})).createWritable();
        await marker.write(String(expected));
        await marker.close();
      }
      urls.set(item.url, URL.createObjectURL(file));
      onProgress(index + 1, files.length);
    }
    signal.throwIfAborted();
    forgetBrowserVideoUrls(projectId);
    live.set(projectId, {version: manifest.version, urls});
  } catch (error) {
    for (const url of urls.values()) URL.revokeObjectURL(url);
    throw error;
  }
}
