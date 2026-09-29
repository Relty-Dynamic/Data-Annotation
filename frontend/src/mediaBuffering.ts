export interface MediaBufferingState {
  readyState: number;
  seeking: boolean;
  wantsPlay: boolean;
  ended: boolean;
  error: unknown;
}

/** Derive the visible wait from the current player, not the last media event. */
export function mediaNeedsBuffering(state: MediaBufferingState, progressed = false): boolean {
  if (state.error || state.ended) return false;
  // A seek can move currentTime before the destination frame is decoded.
  if (state.seeking || state.readyState < 2) return true;
  // A paused annotation view only needs its current frame (HAVE_CURRENT_DATA).
  if (!state.wantsPlay) return false;
  return state.readyState < 3 && !progressed;
}
