import type { Video } from './domain';

/**
 * Move by recorded-video milliseconds, skipping unrecorded gaps.
 * The returned cursor remains on the original, global recording timeline.
 */
export function advanceVideoTime(videos: Video[], time: number, delta: number): number {
  const ordered = videos
    .filter((video) => Number.isFinite(video.start_ms) && Number.isFinite(video.end_ms) && video.end_ms > video.start_ms)
    .map((video) => ({ start: video.start_ms, end: video.end_ms }))
    .sort((left, right) => left.start - right.start || left.end - right.end);
  if (!ordered.length) return 0;

  // Merge touching/overlapping coverage without mutating the project videos.
  const spans: { start: number; end: number }[] = [];
  for (const span of ordered) {
    const previous = spans[spans.length - 1];
    if (previous && span.start <= previous.end) previous.end = Math.max(previous.end, span.end);
    else spans.push(span);
  }

  const first = spans[0].start;
  const last = spans[spans.length - 1].end;
  const cursor = Number.isNaN(time) ? first : Math.max(first, Math.min(time, last));
  const movement = Number.isNaN(delta) ? 0 : delta;
  if (movement === 0) return cursor;

  let recordedCursor = 0;
  let recordedDuration = 0;
  for (const span of spans) {
    const length = span.end - span.start;
    recordedDuration += length;
    if (cursor >= span.end) recordedCursor += length;
    else if (cursor > span.start) recordedCursor += cursor - span.start;
  }

  let remaining = Math.max(0, Math.min(recordedCursor + movement, recordedDuration));
  if (remaining <= 0) return first;
  if (remaining >= recordedDuration) return last;

  for (let index = 0; index < spans.length; index += 1) {
    const span = spans[index];
    const length = span.end - span.start;
    if (remaining < length) return span.start + remaining;
    if (remaining === length) {
      // A forward boundary belongs to the following recording; reverse owns
      // the preceding recording end, avoiding movement through the time gap.
      return movement > 0 ? (spans[index + 1]?.start ?? span.end) : span.end;
    }
    remaining -= length;
  }
  return last;
}

/** Compressed 20x media plays continuously; paused viewing always uses full media. */
export function playbackProfile(rate: number, playing: boolean) {
  const fast = playing && rate > 10;
  const scale = fast ? 20 : 1;
  return { fast, scale, nativeRate: playing ? rate / scale : 1 };
}

export function sourceTime(mediaSeconds: number, scale: number, video: Pick<Video, 'start_ms' | 'end_ms'>) {
  return Math.min(video.end_ms, Math.max(video.start_ms, video.start_ms + Math.round(mediaSeconds * scale * 1000)));
}

/** Avoid restarting an in-flight seek when the requested source timestamp is unchanged. */
export function seekMediaTime(player: { currentTime: number }, sourceSeconds: number, scale = 1): boolean {
  // Timeline positions use integer milliseconds; compare in source time so
  // compressed previews retain the same annotation precision as normal media.
  if (Math.abs(player.currentTime * scale - sourceSeconds) < 0.0005) return false;
  player.currentTime = sourceSeconds / scale;
  return true;
}
