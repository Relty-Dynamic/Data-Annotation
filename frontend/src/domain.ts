export type FixedTrack = 'scene' | 'posture' | 'category' | 'habit';
export type Track = string;
export interface CustomTrack { id: string; name: string; mode: 'state' | 'event'; labels: string[]; }

export interface Segment {
  id: string;
  label: string;
  start_ms: number;
  end_ms: number | null;
  kind?: 'point' | 'interval';
  mode?: 'state' | 'overlay';
  /** Main axis is 0; positive values identify reusable child axes. */
  lane?: number;
  transport_type?: string;
  needs_review?: boolean;
  review_reason?: string;
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
  collection_folder_name?: string;
  storage_mode?: 's3-mock' | 'nas';
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
  selected_tracks?: Track[];
  track_names?: Partial<Record<FixedTrack, string>>;
  track_labels?: Record<FixedTrack, string[]>;
  annotations: Record<string, Segment[]>;
  revision: number;
  updated_at: string;
  draft_dirty?: boolean;
  last_writeback?: {save_id: string; saved_at: string} | null;
  warnings?: string[];
}

export const TRACK_LABELS: Record<FixedTrack, string> = {
  scene: '场景',
  posture: '姿势',
  category: '大类',
  habit: '习惯',
};

export const TRACKS: FixedTrack[] = ['scene', 'posture', 'category', 'habit'];
export function projectAllTracks(project: Project): Track[] { return [...TRACKS.filter(track=>(project.fixed_tracks??TRACKS).includes(track)), ...(project.custom_tracks??[]).map(track=>track.id)]; }
export function projectTracks(project: Project): Track[] { const available=projectAllTracks(project);return project.selected_tracks?.filter(track=>available.includes(track))??available; }
export function customTrack(project: Project, track: Track): CustomTrack | undefined { return project.custom_tracks?.find(item=>item.id===track); }
export function trackName(project: Project, track: Track): string { return customTrack(project,track)?.name??project.track_names?.[track as FixedTrack]??TRACK_LABELS[track as FixedTrack]??'自定义轴'; }
export function isEventTrack(project: Project, track: Track): boolean { return track==='habit'||customTrack(project,track)?.mode==='event'; }
export const SCENE_LABELS = ['室内', '室外', '半开放', '隧道', '电梯', '交通工具内', 'Unknown'];
export const POSTURE_LABELS = ['坐', '站', '躺', '移动', '骑行', '交通工具坐', '交通工具站', '交通工具躺', 'Unknown'];
export const CATEGORY_LABELS = ['工作学习', '运动锻炼', '饮食', '出行', '社交', '休闲娱乐', '休息', '日常事务', 'Unknown'];
export const DEFAULT_TRACK_LABELS: Record<FixedTrack,string[]> = {
  scene: SCENE_LABELS, posture: POSTURE_LABELS, category: CATEGORY_LABELS, habit: [],
};
const LEGACY_TRACK_LABELS: Record<FixedTrack,string[]> = {
  scene: ['室内','室外'], posture: ['动','坐','站','躺'], category: ['专注','活动','用餐','通勤','社交','放松','休息','其他'], habit: [],
};
export function projectTrackLabels(project: Project, track: Track): string[] {
  return customTrack(project, track)?.labels ?? project.track_labels?.[track as FixedTrack] ?? LEGACY_TRACK_LABELS[track as FixedTrack] ?? [];
}

/** Recognize old labels when editing saved annotations without adding them to new-label buttons. */
export function projectAnnotationLabels(project: Project, track: Track): string[] {
  return [...new Set([...projectTrackLabels(project, track),
    ...(LEGACY_TRACK_LABELS[track as FixedTrack] ?? []), ...(track === 'scene' ? ['车内', '其他'] : [])])];
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

export function splitAnnotationLabel(label: string, options: string[]): {base: string; note: string} {
  if (options.includes(label)) return {base: label, note: ''};
  if (options.length === 0) {
    const match = /^(.*)（([^（）]+)）$/u.exec(label);
    return match ? {base: match[1], note: match[2]} : {base: label, note: ''};
  }
  const base = [...options].sort((left, right) => right.length - left.length)
    .find(option => label.startsWith(`${option}（`) && label.endsWith('）'));
  if (!base) return {base: label, note: ''};
  return {base, note: label.slice(base.length + 1, -1)};
}

export function annotationLabel(base: string, note: string): string {
  return note.trim() ? `${base}（${note.trim()}）` : base;
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
  const original = orderedCopies(segments);
  const assigned = assignSegmentLanes(segments, duration);
  const ordered = assigned.filter(segment => segment.lane === 0);
  const children = assigned.filter(segment => segment.lane !== 0);
  if (!label.trim() || !Number.isFinite(time) || !Number.isFinite(duration) || duration <= 0) return original;
  const boundary = Math.max(0, Math.min(Math.round(time), Math.round(duration)));
  if (boundary >= duration) return original;
  let coverageEnd = duration;
  if (recordingSpans) {
    const coverage = [...recordingSpans].sort((a, b) => a.start_ms - b.start_ms);
    const currentIndex = coverage.findIndex((video) => boundary >= video.start_ms && boundary < video.end_ms);
    if (currentIndex < 0) return original;
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
    ...atBoundary,
    id: atBoundary?.id ?? newId(),
    label: label.trim(),
    start_ms: boundary,
    end_ms: Math.min(later[0]?.start_ms ?? duration, duration, coverageEnd),
    lane: 0,
  };
  return orderedCopies([...mergeTouchingStates([...earlier, inserted, ...later]
    .filter(segment => (segment.end_ms ?? duration) > segment.start_ms)), ...children]);
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

function validLane(lane: number | undefined): lane is number {
  return Number.isSafeInteger(lane) && lane! >= 0;
}

function segmentsOverlap(left: Segment, right: Segment, duration: number): boolean {
  return left.start_ms < packingEnd(right, duration) && right.start_ms < packingEnd(left, duration);
}

/** Match Python's Unicode code-point ordering when migrating legacy row assignments. */
function compareIds(left: string, right: string): number {
  const leftPoints = Array.from(left, value => value.codePointAt(0)!);
  const rightPoints = Array.from(right, value => value.codePointAt(0)!);
  for (let index = 0; index < Math.min(leftPoints.length, rightPoints.length); index += 1) {
    if (leftPoints[index] !== rightPoints[index]) return leftPoints[index] - rightPoints[index];
  }
  return leftPoints.length - rightPoints.length;
}

/** Preserve saved rows; infer only legacy rows after reserving every saved row. */
export function assignSegmentLanes(segments: Segment[], duration: number): Segment[] {
  if (segments.some(segment => segment.lane !== undefined && !validLane(segment.lane))) {
    throw new Error('主轴或子轴编号须为非负安全整数。');
  }
  const ordered = [...segments].sort((a, b) =>
    a.start_ms - b.start_ms ||
    packingEnd(a, duration) - packingEnd(b, duration) ||
    compareIds(a.id, b.id),
  );
  const reserved = ordered.filter(segment => validLane(segment.lane)).map(segment => ({ ...segment }));
  return ordered.map(segment => {
    if (validLane(segment.lane)) return { ...segment };
    const occupied = new Set(reserved.filter(other => segmentsOverlap(segment, other, duration)).map(other => other.lane!));
    let lane = 0;
    while (occupied.has(lane)) lane += 1;
    const assigned = { ...segment, lane };
    reserved.push(assigned);
    return assigned;
  });
}

/** Display persistent main/child rows without moving existing annotations between them. */
export function arrangeLanes(segments: Segment[], duration: number): LaneSegment[] {
  return assignSegmentLanes(segments, duration).map(segment => ({ segment, lane: segment.lane! }));
}



/** Join touching intervals on each saved row; repair false file seams only for legacy states. */
export function normalizeStateSeams(
  annotations: Project['annotations'],
  bridges: ContinuityBridge[] = [],
  videos: Video[] = [],
  customTracks: CustomTrack[] = [],
  duration = Infinity,
): Project['annotations'] {
  const bridgeAtEnd = new Map(bridges.map((bridge) => [bridge.start_ms, bridge]));
  const videoEnds = new Map(videos.map((video) => [video.id, video.end_ms]));
  const legacyStateTracks = new Set(['scene', 'posture', ...customTracks.filter(track => track.mode === 'state').map(track => track.id)]);
  const normalize = (track: Track, segments: Segment[]): Segment[] => {
    const legacyIds = new Set(segments.filter(segment => !validLane(segment.lane)).map(segment => segment.id));
    return mergeTouchingStates(assignSegmentLanes(segments, duration), (previous, segment) => {
      const bridge = previous.end_ms == null ? undefined : bridgeAtEnd.get(previous.end_ms);
      return legacyStateTracks.has(track) && bridge !== undefined && legacyIds.has(previous.id) && legacyIds.has(segment.id) &&
        segment.start_ms >= bridge.end_ms &&
        segment.start_ms < (videoEnds.get(bridge.next_video_id) ?? bridge.end_ms);
    });
  };
  return Object.fromEntries([...new Set([...TRACKS, ...Object.keys(annotations), ...customTracks.map(track => track.id)])]
    .map(track => [track, normalize(track, annotations[track] ?? [])]));
}

/** Compare saved annotation meaning, allowing server normalization and tie ordering. */
export function annotationsEqual(left: Project['annotations'], right: Project['annotations'], duration = Infinity): boolean {
  const tracks = [...new Set([...TRACKS, ...Object.keys(left), ...Object.keys(right)])].sort();
  const normalize = (annotations: Project['annotations']) => tracks.map((track) => [track, mergeTouchingStates(assignSegmentLanes(annotations[track] ?? [], duration).map((segment) => ({
    id: segment.id,
    label: segment.label.trim(),
    kind: segment.kind ?? 'interval',
    mode: track === 'category' ? segment.mode ?? 'state' : segment.mode,
    start_ms: segment.start_ms,
    end_ms: segment.end_ms,
    lane: segment.lane,
    transport_type: segment.transport_type,
    needs_review: segment.needs_review,
    review_reason: segment.review_reason,
  }))).sort((a, b) => a.start_ms - b.start_ms || compareIds(a.id, b.id))]);
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
    .sort((a, b) => a.start_ms - b.start_ms || compareIds(a.id, b.id));
}

/** Keep the first interval's identity/creator and the latest known contributor. */
function mergeAttribution(previous: Segment, segment: Segment): void {
  const previousTime = previous.updated_at ?? previous.created_at ?? '';
  const segmentTime = segment.updated_at ?? segment.created_at ?? '';
  if (segmentTime <= previousTime) return;
  const actor = segment.updated_at !== undefined ? 'updated' : 'created';
  delete previous.updated_by;
  delete previous.updated_by_name;
  const by = segment[`${actor}_by`];
  const name = segment[`${actor}_by_name`];
  if (by !== undefined) previous.updated_by = by;
  if (name !== undefined) previous.updated_by_name = name;
  previous.updated_at = segmentTime;
}

function mergeTouchingStates(segments: Segment[], acrossLegacySeam?: (left: Segment, right: Segment) => boolean): Segment[] {
  const result: Segment[] = [];
  for (const segment of orderedCopies(segments).sort((a, b) => (a.lane ?? 0) - (b.lane ?? 0) || a.start_ms - b.start_ms || compareIds(a.id, b.id))) {
    const previous = result[result.length - 1];
    if (previous && previous.lane === segment.lane && previous.label === segment.label &&
        previous.transport_type === segment.transport_type && previous.needs_review === segment.needs_review &&
        previous.review_reason === segment.review_reason &&
        (previous.mode ?? 'state') === (segment.mode ?? 'state') && !isPointSegment(previous) && !isPointSegment(segment) &&
        (previous.end_ms === segment.start_ms || acrossLegacySeam?.(previous, segment))) {
      mergeAttribution(previous, segment);
      previous.end_ms = segment.end_ms;
    } else result.push(segment);
  }
  return orderedCopies(result);
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
  const assigned = assignSegmentLanes(original, duration);
  const states = assigned.filter(segment => segment.lane === 0);
  const children = assigned.filter(segment => segment.lane !== 0);
  const changed = states.length
    ? switchState(states, boundary, label, duration, runs)
    : runs.map(span => ({ id: newId(), label: label.trim(), ...span, lane: 0 }));
  return orderedCopies([
    ...changed.map(segment => category ? { ...segment, mode: 'state' as const } : segment),
    ...children,
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
  _exclusive = false,
  subaxis = false,
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
  const assigned = assignSegmentLanes(original, Infinity);
  const editing = existingId ? assigned.find(segment => segment.id === existingId) : undefined;
  let remaining = assigned.filter(segment => segment.id !== existingId);
  let lane = editing?.lane ?? 0;
  const occupied = new Set(remaining.filter(segment =>
    ranges.some(range => segmentsOverlap(segment, { ...range, id: '', label: '' }, Infinity))).map(segment => segment.lane!));
  if (editing && lane > 0 && occupied.has(lane)) throw new Error('该时间范围与同一子轴上的已有标注重叠，请调整起止时间。');
  if (!editing && subaxis) {
    while (occupied.has(lane)) lane += 1;
  }
  if ((!editing && !subaxis) || editing?.lane === 0) {
    remaining = remaining.flatMap(segment => {
      if (segment.lane !== 0) return [segment];
      let pieces = [{ ...segment }];
      for (const range of ranges) {
        pieces = pieces.flatMap(piece => {
          const pieceEnd = packingEnd(piece, Infinity);
          if (piece.start_ms >= range.end_ms || pieceEnd <= range.start_ms) return [piece];
          if (isPointSegment(piece)) return [];
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
    ...editing,
    id: index === 0 ? existingId ?? newId() : newId(),
    label: label.trim(),
    ...range,
    kind: 'interval',
    lane,
    ...(track === 'category' ? { mode: editing?.mode ?? 'state' as const } : {}),
  }));
  const result = [...remaining, ...added];
  return mergeTouchingStates(result);
}

/** Add a point on the main axis or edit it in place; overlap creation requires anchor ranges. */
export function applyAnnotationPoint(
  segments: Segment[],
  time: number,
  label: string,
  duration: number,
  existingId?: string,
): Segment[] {
  const original = orderedCopies(segments);
  if (!label.trim() || !Number.isFinite(time) || time < 0 || time > duration) return original;
  const assigned = assignSegmentLanes(segments, duration);
  const editing = existingId ? assigned.find(segment => segment.id === existingId) : undefined;
  const point: Segment = { ...editing, id: existingId ?? newId(), label: label.trim(),
    start_ms: Math.round(time), end_ms: Math.round(time), kind: 'point', lane: editing?.lane ?? 0 };
  const remaining = assigned.filter(segment => segment.id !== existingId &&
    (editing || segment.lane !== 0 || !isPointSegment(segment) || segment.start_ms !== point.start_ms));
  if (remaining.some(segment => segment.lane === point.lane && segmentsOverlap(segment, point, duration))) {
    throw new Error(editing ? '该时间与同一主轴或子轴上的已有标注重叠，请调整时间。' :
      '该时间主轴已有标注；新增重叠标注请先选择锚点区间并勾选“子轴”。');
  }
  return orderedCopies([...remaining, point]);
}

/** Delete only the selected annotation, retaining blank time and saved child row assignments. */
export function deleteAnnotation(
  segments: Segment[],
  id: string,
  _spans: VideoGap[],
  _track: Track,
  _eventTrack = false,
): Segment[] {
  return orderedCopies(assignSegmentLanes(segments, Infinity).filter(segment => segment.id !== id));
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
