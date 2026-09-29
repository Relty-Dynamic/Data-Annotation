import { useEffect, useState } from 'react';
import type { RefObject } from 'react';
import { mediaNeedsBuffering } from './mediaBuffering';

/** Inspect the active player, since media events can arrive late after a seek. */
export function useMediaBuffering(
  player: RefObject<HTMLVideoElement | null>,
  wantsPlay: RefObject<boolean>,
  enabled: boolean,
  identity: string,
) {
  const [visible, setVisible] = useState(false);
  useEffect(() => {
    setVisible(false);
    if (!enabled) return;
    let blockedSince: number | null = null;
    let previousPlayer: HTMLVideoElement | null = null;
    let previousTime = 0;
    const inspect = () => {
      const video = player.current;
      const progressed = video === previousPlayer && !!video && video.currentTime > previousTime;
      previousPlayer = video;
      previousTime = video?.currentTime ?? 0;
      const waiting = !!video && mediaNeedsBuffering({
        readyState: video.readyState, seeking: video.seeking,
        wantsPlay: wantsPlay.current, ended: video.ended, error: !!video.error,
      }, progressed);
      if (!waiting) {
        blockedSince = null;
        setVisible(false);
      } else {
        const now = performance.now();
        blockedSince ??= now;
        setVisible(now - blockedSince >= 350);
      }
    };
    inspect();
    const timer = setInterval(inspect, 150);
    return () => clearInterval(timer);
  }, [player, wantsPlay, enabled, identity]);
  return enabled && visible;
}
