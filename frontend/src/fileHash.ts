import {streamingSha256} from './fileHashCore.ts';
export {streamingSha256} from './fileHashCore.ts';

export async function fileSha256(blob: Blob, signal?: AbortSignal, onProgress?: (bytes: number) => void): Promise<string> {
  signal?.throwIfAborted();
  // Use the browser's native implementation for bounded files. Large videos
  // retain streaming verification instead of allocating an unbounded buffer.
  if (blob.size <= 64 * 1024 * 1024 && globalThis.crypto?.subtle) {
    const bytes = await blob.arrayBuffer();
    signal?.throwIfAborted();
    const hash = await crypto.subtle.digest('SHA-256', bytes);
    signal?.throwIfAborted();
    onProgress?.(blob.size);
    return Array.from(new Uint8Array(hash), byte => byte.toString(16).padStart(2, '0')).join('');
  }
  if (typeof Worker !== 'undefined') {
    return new Promise<string>((resolve, reject) => {
      const worker = new Worker(new URL('./fileHash.worker.ts', import.meta.url), {type: 'module'});
      const finish = () => { worker.terminate(); signal?.removeEventListener('abort', abort); };
      const abort = () => { finish(); reject(signal?.reason); };
      signal?.addEventListener('abort', abort, {once: true});
      worker.onmessage = event => {
        finish();
        if (event.data.error) reject(new Error(event.data.error));
        else { onProgress?.(blob.size); resolve(event.data.hash); }
      };
      worker.onerror = () => { finish(); reject(new Error('缓存校验线程失败，请重新准备。')); };
      try { signal?.throwIfAborted(); worker.postMessage(blob); }
      catch (error) { finish(); reject(error); }
    });
  }
  const hash = await streamingSha256(blob, signal);
  onProgress?.(blob.size);
  return hash;
}
