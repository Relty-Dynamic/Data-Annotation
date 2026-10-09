import { useEffect } from 'react';
import {apiUrl, separateApiOrigin} from './apiOrigin.ts';

/** Warm only the next prepared clip's metadata; never start playback or load a whole project. */
export function usePreloadNext(nextUrl: string | undefined) {
  useEffect(() => {
    if (!nextUrl) return;
    let video: HTMLVideoElement | undefined;
    // Let rapid navigation settle before creating another media request.
    const timer = setTimeout(() => {
      video = document.createElement('video');
      video.preload = 'metadata';
      if (separateApiOrigin && !nextUrl.startsWith('blob:')) video.crossOrigin = 'use-credentials';
      video.muted = true;
      video.playsInline = true;
      video.disableRemotePlayback = true;
      video.src = apiUrl(nextUrl);
      video.load();
    }, 300);
    return () => {
      clearTimeout(timer);
      if (!video) return;
      video.pause();
      video.removeAttribute('src');
      video.load();
      video = undefined;
    };
  }, [nextUrl]);
}
