import { useEffect } from 'react';

/** Warm only the next prepared clip's metadata; never start playback or load a whole project. */
export function usePreloadNext(nextUrl: string | undefined) {
  useEffect(() => {
    if (!nextUrl) return;
    let video: HTMLVideoElement | undefined;
    // Let rapid navigation settle before creating another media request.
    const timer = setTimeout(() => {
      video = document.createElement('video');
      video.preload = 'metadata';
      video.muted = true;
      video.playsInline = true;
      video.disableRemotePlayback = true;
      video.src = nextUrl;
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
