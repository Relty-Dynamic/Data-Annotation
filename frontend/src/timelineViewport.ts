export const DEFAULT_WINDOW_MS = 3_600_000;
export const MIN_WINDOW_MS = 300_000;

/** A short recording always fits; longer recordings can zoom down to five minutes. */
export function timelineWindow(duration: number, requested = DEFAULT_WINDOW_MS): number {
  const limit = Number.isFinite(duration) ? Math.max(1, duration) : 1;
  const minimum = Math.min(MIN_WINDOW_MS, limit);
  return Math.max(minimum, Math.min(limit, Number.isFinite(requested) ? requested : DEFAULT_WINDOW_MS));
}

/** Change scale around a viewport pixel, preserving its time unless an end clamps scrolling. */
export function scaleTimelineWindow(
  duration: number, previousWindow: number, nextWindow: number,
  visibleWidth: number, scrollLeft: number, pointerX: number,
): { windowMs: number; scrollLeft: number; axisWidth: number } {
  const limit = Number.isFinite(duration) ? Math.max(1, duration) : 1;
  const width = Number.isFinite(visibleWidth) ? Math.max(1, visibleWidth) : 1;
  const before = timelineWindow(limit, previousWindow);
  const windowMs = timelineWindow(limit, nextWindow);
  const pointer = Math.max(0, Math.min(width, Number.isFinite(pointerX) ? pointerX : width / 2));
  const oldWidth = width * limit / before;
  const oldScroll = Math.max(0, Math.min(oldWidth - width, Number.isFinite(scrollLeft) ? scrollLeft : 0));
  const time = (oldScroll + pointer) / oldWidth * limit;
  const axisWidth = width * limit / windowMs;
  const nextScroll = Math.max(0, Math.min(axisWidth - width, time / limit * axisWidth - pointer));
  return { windowMs, scrollLeft: nextScroll, axisWidth };
}

/** Only create visible ruler ticks plus one neighbor, even for very long projects. */
export function visibleTimelineTicks(start: number, end: number, step: number, duration: number): number[] {
  if (!Number.isFinite(step) || step <= 0 || !Number.isFinite(duration) || duration < 0) return [];
  const first = Math.max(0, Math.floor(Math.max(0, start) / step) - 1);
  const last = Math.min(Math.floor(duration / step), Math.ceil(Math.min(duration, end) / step) + 1);
  const count = Math.max(0, Math.min(1000, last - first + 1));
  return Array.from({ length: count }, (_, index) => (first + index) * step);
}
