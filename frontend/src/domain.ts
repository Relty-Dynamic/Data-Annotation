export type FixedTrack = 'scene' | 'posture' | 'category' | 'habit';
export type Track = FixedTrack | `custom_${string}`;
export interface CustomTrack { id: `custom_${string}`; name: string; mode: 'state' | 'event'; labels: string[]; }

export interface Segment {
  id: string;
  label: string;
  start_ms: number;
  end_ms: number | null;
  kind?: 'point' | 'interval';
  mode?: 'state' | 'overlay';
  created_by?: string;
  created_by_name?: string;
  created_at?: string;
  updated_by?: string;
  updated_by_name?: string;
  updated_at?: string;
}

export interface Video {
  id: string;
  name: string;
  relative_path: string;
  duration_ms: number;
  start_ms: number;
  end_ms: number;
  url: string;
  thumbnail_url: string | null;
}

export interface VideoGap {
  start_ms: number;
  end_ms: number;
}

export interface RecordingRun extends VideoGap {
  video_ids: string[];
}

export interface ContinuityBridge extends VideoGap {
  previous_video_id: string;
  next_video_id: string;
  raw_boundary_error_ms: number;
  reason: 'adjacent_camera_second_precision';
}

export interface Project {
  storage_mode?: 's3-mock';
  playback_generation?: number;
  id: string;
  name: string;
  source_dir: string | null;
  cache_mode?: 'source' | 'legacy' | 'mixed';
  needs_source_relink?: boolean;
  cache_directories?: string[];
  imported_at?: string;
  import_time_estimated?: boolean;
  deletion_pending?: boolean;
  recording_start?: string;
  gaps?: VideoGap[];
  recording_runs?: RecordingRun[];
  continuity_bridges?: ContinuityBridge[];
  duration_ms: number;
  videos: Video[];
  skipped_videos?: Array<{id: string; name: string; start_ms: number; end_ms: number; reason: string}>;
  custom_tracks?: CustomTrack[];
  fixed_tracks?: FixedTrack[];
  track_labels?: Record<FixedTrack, string[]>;
  annotations: Record<string, Segment[]>;
  revision: number;
  updated_at: string;
  draft_dirty?: boolean;
  last_writeback?: {save_id: string; saved_at: string} | null;
}

export const TRACK_LABELS: Record<FixedTrack, string> = {
  scene: '场景',
  posture: '姿势',
  category: '大类',
  habit: '习惯',
};

export const TRACKS: FixedTrack[] = ['scene', 'posture', 'category', 'habit'];
export function projectTracks(project: Project): Track[] { return [...TRACKS.filter(track=>(project.fixed_tracks??TRACKS).includes(track)), ...(project.custom_tracks??[]).map(track=>track.id)]; }
export function customTrack(project: Project, track: Track): CustomTrack | undefined { return project.custom_tracks?.find(item=>item.id===track); }
export function trackName(project: Project, track: Track): string { return track.startsWith('custom_') ? customTrack(project,track)?.name??'自定义轴' : TRACK_LABELS[track as FixedTrack]; }
export function isEventTrack(project: Project, track: Track): boolean { return track==='habit'||customTrack(project,track)?.mode==='event'; }
export function isExclusiveTrack(project: Project, track: Track): boolean { return track==='scene'||track==='posture'||customTrack(project,track)?.mode==='state'; }
export const SCENE_LABELS = ['室内', '室外'];
export const POSTURE_LABELS = ['动', '坐', '站', '躺'];
export const CATEGORY_LABELS = ['专注', '活动', '用餐', '通勤', '社交', '放松', '休息', '其他'];
export const DEFAULT_TRACK_LABELS: Record<FixedTrack,string[]> = {
  scene: SCENE_LABELS, posture: POSTURE_LABELS, category: CATEGORY_LABELS, habit: [],
};
export function projectTrackLabels(project: Project, track: Track): string[] {
  return track.startsWith('custom_') ? customTrack(project, track)?.labels ?? []
    : project.track_labels?.[track as FixedTrack] ?? DEFAULT_TRACK_LABELS[track as FixedTrack];
}

export function formatTime(ms: number): string {
  const value = Math.max(0, Math.round(Number.isFinite(ms) ? ms : 0));
  const hours = Math.floor(value / 3_600_000);
  const minutes = Math.floor((value % 3_600_000) / 60_000);
  const seconds = Math.floor((value % 60_000) / 1000);
  return `${String(hours).padStart(2, '0')}:${String(minutes).padStart(2, '0')}:${String(seconds).padStart(2, '0')}.${String(value % 1000).padStart(3, '0')}`;
}

export function parseTime(input: string): number | null {
  const value = input.trim();
  if (!value) return null;
  if (/^\d+(?:\.\d{1,3})?$/.test(value)) {
    const milliseconds = Math.round(Number(value) * 1000);
    return Number.isSafeInteger(milliseconds) ? milliseconds : null;
  }
  const parts = value.split(':');
  if (parts.length !== 2 && parts.length !== 3) return null;
  if (!parts.slice(0, -1).every((part) => /^\d+$/.test(part))) return null;
  if (!/^\d{1,2}(?:\.\d{1,3})?$/.test(parts[parts.length - 1])) return null;
  const seconds = Number(parts[parts.length - 1]);
  const minutes = Number(parts[parts.length - 2]);
  const hours = parts.length === 3 ? Number(parts[0]) : 0;
  if (seconds >= 60 || (parts.length === 3 && minutes >= 60)) return null;
  const milliseconds = Math.round((hours * 3600 + minutes * 60 + seconds) * 1000);
  return Number.isSafeInteger(milliseconds) ? milliseconds : null;
}

/** Compare an edit form semantically, so alternate valid time formats are not lost edits. */
export function isSegmentDraftDirty(segment: Segment | undefined, draft: {
  label: string; start: string; end: string; kind: 'point' | 'interval';
}): boolean {
  if (!segment) return Boolean(draft.label.trim() || draft.start.trim() || draft.end.trim());
  const kind = segment.kind ?? 'interval';
  const end = draft.end.trim() ? parseTime(draft.end) : null;
  return draft.label.trim() !== segment.label || draft.kind !== kind ||
    parseTime(draft.start) !== segment.start_ms ||
    (draft.kind !== 'point' && (Boolean(draft.end.trim() && end === null) || end !== segment.end_ms));
}

export function locateVideo(videos: Video[], time: number): Video | undefined {
  if (!videos.length || !Number.isFinite(time)) return undefined;
  const last = videos[videos.length - 1];
  if (time >= last.end_ms) return last;
  return videos.find((video) => time >= video.start_ms && time < video.end_ms) ??
    videos.find((video) => video.start_ms > time);
}

function newId(): string {
  return typeof globalThis.crypto?.randomUUID === 'function'
    ? globalThis.crypto.randomUUID()
    : `segment-${Date.now()}-${Math.random().toString(36).slice(2)}`;
}

/** Apply a state change at a global timestamp, keeping later state changes intact. */
export function switchState(
  segments: Segment[],
  time: number,
  label: string,
  duration: number,
  recordingSpans?: VideoGap[],
): Segment[] {
  const ordered = segments.map((segment) => ({ ...segment })).sort((a, b) => a.start_ms - b.start_ms);
  if (!label.trim() || !Number.isFinite(time) || !Number.isFinite(duration) || duration <= 0) return ordered;
  const boundary = Math.max(0, Math.min(Math.round(time), Math.round(duration)));
  if (boundary >= duration) return ordered;
  let coverageEnd = duration;
  if (recordingSpans) {
    const coverage = [...recordingSpans].sort((a, b) => a.start_ms - b.start_ms);
    const currentIndex = coverage.findIndex((video) => boundary >= video.start_ms && boundary < video.end_ms);
    if (currentIndex < 0) return ordered;
    coverageEnd = coverage[currentIndex].end_ms;
    for (let index = currentIndex + 1; index < coverage.length; index += 1) {
      if (coverage[index].start_ms > coverageEnd) break;
      coverageEnd = Math.max(coverageEnd, coverage[index].end_ms);
    }
  }
  const later = ordered.filter((segment) => segment.start_ms > boundary);
  const earlier = ordered.filter((segment) => segment.start_ms < boundary).map((segment) => ({
    ...segment,
    end_ms: Math.min(segment.end_ms ?? duration, boundary),
  }));
  const atBoundary = ordered.find((segment) => segment.start_ms === boundary);
  const inserted: Segment = {
    id: atBoundary?.id ?? newId(),
    label: label.trim(),
    start_ms: boundary,
    end_ms: Math.min(later[0]?.start_ms ?? duration, duration, coverageEnd),
  };
  const result: Segment[] = [];
  for (const segment of [...earlier, inserted, ...later]) {
    const end = segment.end_ms ?? duration;
    if (end <= segment.start_ms) continue;
    const previous = result[result.length - 1];
    if (previous && previous.label === segment.label && previous.end_ms === segment.start_ms) {
      previous.end_ms = segment.end_ms;
    } else {
      result.push({ ...segment });
    }
  }
  return result;
}

export interface LaneSegment {
  segment: Segment;
  lane: number;
}

export function isPointSegment(segment: Segment): boolean {
  return segment.kind === 'point' || (segment.kind !== 'interval' && segment.end_ms === segment.start_ms);
}

function packingEnd(segment: Segment, duration: number): number {
  return isPointSegment(segment) ? segment.start_ms + 1 : Math.max(segment.start_ms + 1, segment.end_ms ?? duration);
}

/** Pack overlapping behavior intervals and simultaneous points without permanent label rows. */
export function arrangeLanes(segments: Segment[], duration: number): LaneSegment[] {
  const ordered = [...segments].sort((a, b) =>
    a.start_ms - b.start_ms ||
    packingEnd(a, duration) - packingEnd(b, duration) ||
    a.id.localeCompare(b.id),
  );
  const laneEnds: number[] = [];
  return ordered.map((segment) => {
    const end = packingEnd(segment, duration);
    let lane = laneEnds.findIndex((laneEnd) => laneEnd <= segment.start_ms);
    if (lane < 0) lane = laneEnds.length;
    laneEnds[lane] = end;
    return { segment, lane };
  });
}



/** Join uninterrupted states and legacy false file seams without changing behavior events. */
export function normalizeStateSeams(
  annotations: Project['annotations'],
  bridges: ContinuityBridge[] = [],
  videos: Video[] = [],
  customTracks: CustomTrack[] = [],
): Project['annotations'] {
  const bridgeAtEnd = new Map(bridges.map((bridge) => [bridge.start_ms, bridge]));
  const videoEnds = new Map(videos.map((video) => [video.id, video.end_ms]));
  const normalize = (segments: Segment[]): Segment[] => {
    const ordered = [...segments].sort((a, b) => a.start_ms - b.start_ms || a.id.localeCompare(b.id));
    const result: Segment[] = [];
    for (const segment of ordered) {
      const previous = result[result.length - 1];
      const bridge = previous?.end_ms == null ? undefined : bridgeAtEnd.get(previous.end_ms);
      const acrossSeam = bridge !== undefined && segment.start_ms >= bridge.end_ms &&
        segment.start_ms < (videoEnds.get(bridge.next_video_id) ?? bridge.end_ms);
      if (previous && previous.label === segment.label && previous.created_by === segment.created_by &&
          previous.created_at === segment.created_at && previous.updated_by === segment.updated_by &&
          previous.updated_at === segment.updated_at &&
          (previous.end_ms === segment.start_ms || acrossSeam)) {
        previous.end_ms = segment.end_ms;
      } else {
        result.push({ ...segment });
      }
    }
    return result;
  };
  return {
    ...annotations,
    scene: normalize(annotations.scene ?? []),
    posture: normalize(annotations.posture ?? []),
    ...Object.fromEntries(customTracks.filter(track => track.mode === 'state').map(track => [track.id, normalize(annotations[track.id] ?? [])])),
    // Category intervals may overlap; normalizing them as states can erase annotations.
    category: annotations.category ?? [],
    habit: annotations.habit ?? [],
  };
}

/** Compare saved annotation meaning, allowing server normalization and tie ordering. */
export function annotationsEqual(left: Project['annotations'], right: Project['annotations']): boolean {
  const tracks = [...new Set([...TRACKS, ...Object.keys(left), ...Object.keys(right)])].sort();
  const normalize = (annotations: Project['annotations']) => tracks.map((track) => [track, (annotations[track] ?? []).map((segment) => ({
    id: segment.id,
    label: segment.label.trim(),
    kind: segment.kind ?? 'interval',
    mode: track === 'category' ? segment.mode ?? 'state' : segment.mode,
    start_ms: segment.start_ms,
    end_ms: segment.end_ms,
  })).sort((a, b) => a.start_ms - b.start_ms || a.id.localeCompare(b.id))]);
  return JSON.stringify(normalize(left)) === JSON.stringify(normalize(right));
}

/** Capture unsaved input against a stable video, before supplementing its timeline. */
export interface VideoTimeAnchor { videoId: string; offsetMs: number; }
export function anchorVideoTime(videos: Video[], time: number, edge: 'start' | 'end' = 'start'): VideoTimeAnchor | null {
  if (!Number.isFinite(time)) return null;
  const video = edge === 'end'
    ? videos.find(item => time > item.start_ms && time <= item.end_ms) ?? videos.find(item => item.start_ms === time)
    : videos.find(item => time >= item.start_ms && time < item.end_ms) ?? videos.find(item => item.end_ms === time);
  return video ? {videoId: video.id, offsetMs: time - video.start_ms} : null;
}
export function restoreVideoTime(videos: Video[], anchor: VideoTimeAnchor): number | null {
  const video = videos.find(item => item.id === anchor.videoId);
  return video ? video.start_ms + Math.max(0, Math.min(anchor.offsetMs, video.duration_ms)) : null;
}



/** Merge touching files into recording runs, retaining real recording breaks. */
function joinedSpans(spans: VideoGap[]): VideoGap[] {
  const ordered = spans.filter(span => Number.isFinite(span.start_ms) && Number.isFinite(span.end_ms))
    .map(span => ({ start_ms: Math.max(0, Math.round(span.start_ms)), end_ms: Math.round(span.end_ms) }))
    .filter(span => span.end_ms > span.start_ms)
    .sort((a, b) => a.start_ms - b.start_ms || a.end_ms - b.end_ms);
  const result: VideoGap[] = [];
  for (const span of ordered) {
    const previous = result[result.length - 1];
    if (previous && span.start_ms <= previous.end_ms) previous.end_ms = Math.max(previous.end_ms, span.end_ms);
    else result.push({ ...span });
  }
  return result;
}

function orderedCopies(segments: Segment[]): Segment[] {
  return segments.map(segment => ({ ...segment }))
    .sort((a, b) => a.start_ms - b.start_ms || a.id.localeCompare(b.id));
}

function mergeTouchingStates(segments: Segment[]): Segment[] {
  const result: Segment[] = [];
  for (const segment of orderedCopies(segments)) {
    const previous = result[result.length - 1];
    if (previous && previous.label === segment.label && previous.created_by === segment.created_by &&
        previous.created_at === segment.created_at && previous.updated_by === segment.updated_by &&
        previous.updated_at === segment.updated_at && previous.end_ms === segment.start_ms &&
        previous.mode === segment.mode && !isPointSegment(previous) && !isPointSegment(segment)) {
      previous.end_ms = segment.end_ms;
    } else result.push(segment);
  }
  return result;
}

/** Find uncovered recorded time using the union of intervals, including overlapping categories. */
export function uncoveredSpans(segments: Segment[], spans: VideoGap[]): VideoGap[] {
  const result: VideoGap[] = [];
  const intervals = orderedCopies(segments).filter(segment =>
    !isPointSegment(segment) && Number.isFinite(segment.start_ms) &&
    (segment.end_ms === null || Number.isFinite(segment.end_ms) && segment.end_ms > segment.start_ms),
  );
  for (const span of joinedSpans(spans)) {
    let cursor = span.start_ms;
    for (const segment of intervals) {
      const start = Math.max(span.start_ms, segment.start_ms);
      const end = Math.min(span.end_ms, segment.end_ms ?? span.end_ms);
      if (end <= cursor || start >= span.end_ms) continue;
      if (start > cursor) result.push({ start_ms: cursor, end_ms: start });
      cursor = Math.max(cursor, end);
      if (cursor >= span.end_ms) break;
    }
    if (cursor < span.end_ms) result.push({ start_ms: cursor, end_ms: span.end_ms });
  }
  return result;
}

/** Initialize a new state track across recordings, then preserve later explicit state changes. */
export function switchCoveredState(
  segments: Segment[],
  time: number,
  label: string,
  duration: number,
  spans: VideoGap[],
  category = false,
): Segment[] {
  const original = orderedCopies(segments);
  if (!label.trim() || !Number.isFinite(time) || !Number.isFinite(duration) || duration <= 0) return original;
  const boundary = Math.max(0, Math.min(Math.round(time), Math.round(duration)));
  const runs = joinedSpans(spans.map(span => ({ ...span, end_ms: Math.min(span.end_ms, duration) })));
  if (!runs.some(span => boundary >= span.start_ms && boundary < span.end_ms)) return original;
  const states = category ? original.filter(segment => segment.mode !== 'overlay') : original;
  const overlays = category ? original.filter(segment => segment.mode === 'overlay') : [];
  const changed = states.length
    ? switchState(states, boundary, label, duration, runs)
    : runs.map(span => ({ id: newId(), label: label.trim(), ...span }));
  return orderedCopies([
    ...changed.map(segment => category ? { ...segment, mode: 'state' as const } : segment),
    ...overlays,
  ]);
}

/** Apply a selected range, preserving outside states and splitting at real recording gaps. */
export function applyAnnotationRange(
  segments: Segment[],
  start: number,
  end: number,
  label: string,
  spans: VideoGap[],
  track: Track,
  existingId?: string,
  exclusive = false,
): Segment[] {
  const original = orderedCopies(segments);
  if (!label.trim() || !Number.isFinite(start) || !Number.isFinite(end)) return original;
  const from = Math.round(start);
  const to = Math.round(end);
  if (from < 0 || to <= from) return original;
  const ranges = joinedSpans(spans).map(span => ({
    start_ms: Math.max(from, span.start_ms),
    end_ms: Math.min(to, span.end_ms),
  })).filter(span => span.end_ms > span.start_ms);
  // An invalid edit must never remove the existing annotation.
  if (!ranges.length) return original;
  const editing = existingId ? original.find(segment => segment.id === existingId) : undefined;
  let remaining = original.filter(segment => segment.id !== existingId);
  if (track === 'scene' || track === 'posture' || exclusive) {
    remaining = remaining.flatMap(segment => {
      let pieces = [{ ...segment }];
      for (const range of ranges) {
        pieces = pieces.flatMap(piece => {
          const pieceEnd = piece.end_ms ?? Infinity;
          if (piece.start_ms >= range.end_ms || pieceEnd <= range.start_ms) return [piece];
          const outside: Segment[] = [];
          if (piece.start_ms < range.start_ms) outside.push({ ...piece, end_ms: range.start_ms });
          if (pieceEnd > range.end_ms) outside.push({
            ...piece,
            id: outside.length ? newId() : piece.id,
            start_ms: range.end_ms,
          });
          return outside;
        });
      }
      return pieces;
    });
  }
  const added: Segment[] = ranges.map((range, index) => ({
    id: index === 0 ? existingId ?? newId() : newId(),
    label: label.trim(),
    ...range,
    kind: 'interval',
    ...(track === 'category' ? { mode: editing ? editing.mode ?? 'state' : 'overlay' } as const : {}),
  }));
  const result = [...remaining, ...added];
  return track === 'scene' || track === 'posture' || exclusive ? mergeTouchingStates(result) : orderedCopies(result);
}

/** Delete a segment and extend only an adjacent predecessor into newly uncovered recorded time. */
export function deleteAnnotation(
  segments: Segment[],
  id: string,
  spans: VideoGap[],
  track: Track,
  eventTrack = false,
): Segment[] {
  const removed = segments.find(segment => segment.id === id);
  const remaining = orderedCopies(segments.filter(segment => segment.id !== id));
  if (!removed || track === 'habit' || eventTrack || isPointSegment(removed)) return remaining;
  const runs = joinedSpans(spans);
  const holes = uncoveredSpans(remaining, runs).map(hole => ({
    start_ms: Math.max(hole.start_ms, removed.start_ms),
    end_ms: Math.min(hole.end_ms, removed.end_ms ?? hole.end_ms),
  })).filter(hole => hole.end_ms > hole.start_ms);
  for (const hole of holes) {
    const run = runs.find(span => hole.start_ms >= span.start_ms && hole.start_ms < span.end_ms);
    if (!run) continue;
    // An overlay inside the deleted first segment is not its original predecessor.
    // Retain blank time after deleting a first/sole state, even beyond such overlays.
    const removedStart = Math.max(removed.start_ms, run.start_ms);
    const hadPredecessor = segments.some(segment =>
      segment.id !== id && !isPointSegment(segment) && segment.start_ms >= run.start_ms &&
      segment.start_ms < removedStart && (segment.end_ms ?? run.end_ms) >= removedStart,
    );
    if (!hadPredecessor) continue;
    const predecessor = remaining.filter(segment =>
      !isPointSegment(segment) && segment.start_ms >= run.start_ms && segment.start_ms < hole.start_ms &&
      segment.end_ms === hole.start_ms,
    ).sort((a, b) => b.start_ms - a.start_ms || a.id.localeCompare(b.id))[0];
    if (predecessor) predecessor.end_ms = hole.end_ms;
  }
  return track === 'scene' || track === 'posture' ? mergeTouchingStates(remaining) : orderedCopies(remaining);
}

/** Snap by rendered pixel distance, so zoom does not change the mouse tolerance. */
export function snapTimelineTime(
  time: number,
  points: number[],
  duration: number,
  axisWidth: number,
  thresholdPx = 8,
): number {
  const limit = Number.isFinite(duration) ? Math.max(0, Math.round(duration)) : 0;
  const bounded = Math.max(0, Math.min(limit, Number.isFinite(time) ? Math.round(time) : 0));
  if (!limit || !Number.isFinite(axisWidth) || axisWidth <= 0 || !Number.isFinite(thresholdPx) || thresholdPx < 0) return bounded;
  const tolerance = thresholdPx * limit / axisWidth;
  let nearest = bounded;
  let distance = Infinity;
  for (const point of points) {
    if (!Number.isFinite(point) || point < 0 || point > limit) continue;
    const candidate = Math.round(point);
    const delta = Math.abs(candidate - bounded);
    if (delta <= tolerance && (delta < distance || delta === distance && candidate < nearest)) {
      nearest = candidate;
      distance = delta;
    }
  }
  return nearest;
}

/** All visible cut/state boundaries and user anchors participate in timeline snapping. */
export function timelineSnapPoints(project: Project, anchors: number[] = []): number[] {
  const points = [0, project.duration_ms, ...anchors];
  for (const video of project.videos) points.push(video.start_ms, video.end_ms);
  for (const span of project.recording_runs ?? []) points.push(span.start_ms, span.end_ms);
  for (const track of projectTracks(project)) {
    for (const segment of project.annotations[track] ?? []) {
      points.push(segment.start_ms, segment.end_ms ?? project.duration_ms);
    }
  }
  return [...new Set(points.filter(point => Number.isFinite(point) && point >= 0 && point <= project.duration_ms)
    .map(point => Math.round(point)))].sort((a, b) => a - b);
}
