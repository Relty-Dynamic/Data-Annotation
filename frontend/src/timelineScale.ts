import {timelineWindow} from './timelineViewport.ts';

type Span = {start_ms: number; end_ms: number};

/** Collapse only recording gaps; all callers still exchange real project times. */
export function timelineScale(duration: number, gaps: Span[], gapLimit: number) {
  const limit = Math.max(1, duration);
  const merged: Span[] = [];
  for (const gap of [...gaps].sort((a, b) => a.start_ms - b.start_ms)) {
    const start = Math.max(0, Math.min(limit, gap.start_ms));
    const end = Math.max(start, Math.min(limit, gap.end_ms));
    if (end <= start) continue;
    const previous = merged.at(-1);
    if (previous && start <= previous.end_ms) previous.end_ms = Math.max(previous.end_ms, end);
    else merged.push({start_ms: start, end_ms: end});
  }
  let removed = 0;
  const sections = merged.map(gap => {
    const length = gap.end_ms - gap.start_ms;
    const shown = Math.min(length, Math.max(1, gapLimit));
    const displayStart = gap.start_ms - removed;
    removed += length - shown;
    return {...gap, displayStart, displayEnd: displayStart + shown, ratio: shown / length};
  });
  const displayDuration = limit - removed;
  const toDisplay = (time: number) => {
    const bounded = Math.max(0, Math.min(limit, time));
    let offset = 0;
    for (const gap of sections) {
      if (bounded < gap.start_ms) break;
      if (bounded <= gap.end_ms) return gap.displayStart + (bounded - gap.start_ms) * gap.ratio;
      offset += (gap.end_ms - gap.start_ms) - (gap.displayEnd - gap.displayStart);
    }
    return bounded - offset;
  };
  const toTime = (display: number) => {
    const bounded = Math.max(0, Math.min(displayDuration, display));
    let offset = 0;
    for (const gap of sections) {
      if (bounded < gap.displayStart) break;
      if (bounded <= gap.displayEnd) return gap.start_ms + (bounded - gap.displayStart) / gap.ratio;
      offset += (gap.end_ms - gap.start_ms) - (gap.displayEnd - gap.displayStart);
    }
    return bounded + offset;
  };
  return {duration: displayDuration, recordedDuration: limit - merged.reduce((sum, gap) => sum + gap.end_ms - gap.start_ms, 0), toDisplay, toTime};
}

export function snapDisplayedTime(time: number, points: number[], scale: ReturnType<typeof timelineScale>, axisWidth: number, thresholdPx = 8) {
  let nearest = time, distance = Infinity;
  const display = scale.toDisplay(time);
  for (const point of points) {
    const delta = Math.abs(scale.toDisplay(point) - display) / scale.duration * axisWidth;
    if (delta <= thresholdPx && (delta < distance || delta === distance && point < nearest)) {
      nearest = point;
      distance = delta;
    }
  }
  return Math.round(nearest);
}

export function recordingScale(duration: number, gaps: Span[], windowMs: number, width: number) {
  const recorded = Math.max(1, timelineScale(duration, gaps, 1).recordedDuration);
  return timelineScale(duration, gaps, timelineWindow(recorded, windowMs) / Math.max(1, width) * 24);
}

/** Zoom around the same real timestamp, including when the gap width changes. */
export function zoomRecordingScale(duration: number, gaps: Span[], before: number, requested: number, width: number, scrollLeft: number, pointerX: number) {
  const visibleWidth = Math.max(1, width);
  const pointer = Math.max(0, Math.min(visibleWidth, pointerX));
  const oldScale = recordingScale(duration, gaps, before, visibleWidth);
  const oldWidth = visibleWidth * oldScale.duration / timelineWindow(oldScale.duration, before);
  const time = oldScale.toTime((Math.max(0, Math.min(oldWidth - visibleWidth, scrollLeft)) + pointer) / oldWidth * oldScale.duration);
  const windowMs = timelineWindow(duration, requested);
  const scale = recordingScale(duration, gaps, windowMs, visibleWidth);
  const axisWidth = visibleWidth * scale.duration / timelineWindow(scale.duration, windowMs);
  return {windowMs, axisWidth, scrollLeft: Math.max(0, Math.min(axisWidth - visibleWidth, scale.toDisplay(time) / scale.duration * axisWidth - pointer))};
}
