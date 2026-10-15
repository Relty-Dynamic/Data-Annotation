export interface AnchorRange { start_ms: number; end_ms: number; }

/** Keep the last two playback positions; toggling an existing position removes it. */
export function toggleAnchor(anchors: number[], time: number): number[] {
  if (!Number.isFinite(time) || time < 0) return anchors;
  const point = Math.round(time);
  const current = anchors.slice(-2);
  return current.includes(point) ? current.filter(value => value !== point) : [...current, point].slice(-2);
}

/** Selection is explicit; these endpoints do not depend on insertion order. */
export function anchorRange(anchors: number[]): AnchorRange | null {
  if (anchors.length !== 2 || anchors.some(time => !Number.isFinite(time) || time < 0) || anchors[0] === anchors[1]) return null;
  return {start_ms: Math.min(...anchors), end_ms: Math.max(...anchors)};
}
