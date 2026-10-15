import { assignSegmentLanes, isPointSegment } from './domain.ts';
import type { Project, Segment, Track, VideoGap } from './domain.ts';

export type AnnotationBoundary = 'start' | 'end';

/** One gesture always reads this snapshot, so dragging back restores swallowed rows. */
export interface BoundaryEdit {
  track: Track;
  id: string;
  edge: AnnotationBoundary;
  originalTime: number;
  annotations: Project['annotations'];
  segment: Segment;
  lane: number;
  lanes: ReadonlyMap<string, number>;
  minTime: number;
  maxTime: number;
  duration: number;
}

function recordingSpans(project: Project): VideoGap[] {
  const coverage = project.recording_runs?.length ? project.recording_runs : project.videos;
  const spans = [...coverage, ...(project.continuity_bridges ?? [])]
    .filter(span => Number.isFinite(span.start_ms) && Number.isFinite(span.end_ms))
    .map(span => ({start_ms: Math.max(0, Math.round(span.start_ms)), end_ms: Math.min(project.duration_ms, Math.round(span.end_ms))}))
    .filter(span => span.end_ms > span.start_ms)
    .sort((left, right) => left.start_ms - right.start_ms);
  const runs: VideoGap[] = [];
  for (const span of spans) {
    const previous = runs[runs.length - 1];
    if (previous && span.start_ms <= previous.end_ms) previous.end_ms = Math.max(previous.end_ms, span.end_ms);
    else runs.push({...span});
  }
  return runs;
}

export function createBoundaryEdit(project: Project, track: Track, id: string, edge: AnnotationBoundary): BoundaryEdit | null {
  const source = project.annotations[track]?.find(segment => segment.id === id);
  if (!source || !Number.isFinite(source.start_ms) || !Number.isFinite(project.duration_ms)) return null;
  const point = isPointSegment(source);
  if (!point && (source.end_ms === null && edge === 'end' || source.end_ms !== null &&
      (!Number.isFinite(source.end_ms) || source.end_ms <= source.start_ms))) return null;
  // A legacy interval spanning a real break cannot be resized without inventing footage.
  const run = recordingSpans(project).find(span => source.start_ms >= span.start_ms &&
    (point ? source.start_ms <= span.end_ms : source.start_ms < span.end_ms && (source.end_ms === null || source.end_ms <= span.end_ms)));
  if (!run) return null;
  const annotations = Object.fromEntries(Object.entries(project.annotations)
    .map(([key, segments]) => [key, segments.map(segment => ({...segment}))]));
  const lanes = new Map(assignSegmentLanes(annotations[track], project.duration_ms).map(item => [item.id, item.lane!]));
  // Persist inferred legacy rows before changing geometry. Otherwise consuming or
  // shortening a row could make the next render repack another row into its place.
  annotations[track] = annotations[track].map(item => ({...item, lane: lanes.get(item.id)!}));
  const segment = annotations[track].find(item => item.id === id)!;
  const lane = lanes.get(id)!;
  let minTime = run.start_ms;
  let maxTime = run.end_ms;
  if (point) {
    // Historical zero-width records remain readable/movable without creating overlaps
    // or stretching a point into an interval. New editing UI does not offer this kind.
    for (const other of annotations[track]) {
      if (other.id === id || lanes.get(other.id) !== lane) continue;
      const pointOther = isPointSegment(other);
      const end = pointOther ? other.start_ms + 1 : other.end_ms ?? project.duration_ms;
      if (end <= source.start_ms) minTime = Math.max(minTime, end);
      else if (other.start_ms > source.start_ms) maxTime = Math.min(maxTime, other.start_ms - 1);
      else return null;
    }
  } else if (edge === 'start') maxTime = Math.min(maxTime, (source.end_ms ?? run.end_ms) - 1);
  else minTime = Math.max(minTime, source.start_ms + 1);
  if (minTime > maxTime) return null;
  return {track, id, edge, originalTime: point || edge === 'start' ? source.start_ms : source.end_ms!,
    annotations, segment, lane, lanes, minTime, maxTime, duration: project.duration_ms};
}

/** Move one edge, preserving touching neighbors and consuming any rows it passes. */
export function moveAnnotationBoundary(session: BoundaryEdit, targetMs: number): {annotations: Project['annotations']; time: number} {
  const {track, id, edge, originalTime, annotations, segment, lane, lanes, duration} = session;
  const time = Math.max(session.minTime, Math.min(session.maxTime,
    Number.isFinite(targetMs) ? Math.round(targetMs) : originalTime));
  const point = isPointSegment(segment);
  const changed: Segment[] = [];
  for (const source of annotations[track]) {
    const item = {...source};
    if (item.id === id) {
      if (point) { item.start_ms = time; item.end_ms = time; }
      else if (edge === 'start') item.start_ms = time;
      else item.end_ms = time;
    } else if (!point && lanes.get(item.id) === lane && time !== originalTime) {
      const otherPoint = isPointSegment(item);
      const end = item.end_ms ?? duration;
      if (edge === 'end') {
        if (time < originalTime && !otherPoint && item.start_ms === originalTime) item.start_ms = time;
        else if (time > originalTime) {
          if (otherPoint) {
            if (item.start_ms >= originalTime && item.start_ms < time) continue;
          } else if (item.start_ms >= originalTime && item.start_ms < time) {
            if (end <= time) continue;
            item.start_ms = time;
          }
        }
      } else {
        if (time > originalTime && !otherPoint && item.end_ms === originalTime) item.end_ms = time;
        else if (time < originalTime) {
          if (otherPoint) {
            if (item.start_ms >= time && item.start_ms < originalTime) continue;
          } else if (end <= originalTime && end > time) {
            if (item.start_ms >= time) continue;
            item.end_ms = time;
          }
        }
      }
    }
    changed.push(item);
  }
  return {annotations: {...annotations, [track]: changed}, time};
}
