import { useEffect, useLayoutEffect, useMemo, useRef, useState, type ReactNode, type CSSProperties, type MouseEvent, type PointerEvent as ReactPointerEvent } from 'react';
import { createPortal } from 'react-dom';
import { arrangeLanes, formatTime, isPointSegment, snapTimelineTime, timelineSnapPoints, TRACK_LABELS, TRACKS, type Project, type Segment, type Track, type VideoGap } from './domain';
import HoverPreview from './HoverPreview';
import type { ExternalTimeline } from './externalTimeline';
import { sessionAssetUrl } from './sessionAssets';
import { DEFAULT_WINDOW_MS, scaleTimelineWindow, timelineWindow, visibleTimelineTicks } from './timelineViewport';
import './timeline.css';

export interface TimelineProps {
  project: Project;
  comparison?: ExternalTimeline | null;
  currentTime: number;
  activeTrack: Track;
  trackRevealVersion?: number;
  selectedId: string | null;
  anchorControls?: ReactNode;
  anchors?: number[];
  selectedRange?: { start_ms: number; end_ms: number } | null;
  onRangeDismiss?: () => void;
  onSeek: (ms: number) => void;
  onScrubStart?: () => void;
  onScrubEnd?: (cancelled: boolean, targetMs: number) => void;
  onTrackSelect: (track: Track) => void;
  onSegmentSelect: (track: Track, segment: Segment, seekTime?: number) => void;
}

const HOUR_MS = DEFAULT_WINDOW_MS;
const LANE_PITCH = 24;
const SEGMENT_HEIGHT = 22;
const OVERLAP_VIEWPORT_MAX = 58;
const NO_ANCHORS: number[] = [];
const HOVER_WIDTH = 280;
const HOVER_HEIGHT = 226;
const recordingClock = new Intl.DateTimeFormat('en-GB', {
  timeZone: 'Asia/Shanghai', numberingSystem: 'latn',
  year: 'numeric', month: '2-digit', day: '2-digit',
  hour: '2-digit', minute: '2-digit', second: '2-digit',
  fractionalSecondDigits: 3, hourCycle: 'h23',
});

interface ScrubSession {
  pointerId: number;
  owner: HTMLElement;
  projectId: string;
  duration: number;
  clientX: number;
  startX: number;
  startY: number;
  grabOffset: number;
  hasMoved: boolean;
  latestTime: number;
  lastSeekTime: number | null;
  lastSeekAt: number;
  lastFrameAt: number;
  frame: number;
}

interface HoverTime {
  time: number;
  left: number;
  top: number;
  width: number;
  height: number;
  noVideo: boolean;
  snapped: boolean;
}

function recordingDateTime(origin: number | null, offset: number): string | null {
  if (origin === null) return null;
  const parts = recordingClock.formatToParts(origin + offset);
  const part = (name: Intl.DateTimeFormatPartTypes) => parts.find((item) => item.type === name)?.value ?? '';
  return `${part('year')}-${part('month')}-${part('day')} ${part('hour')}:${part('minute')}:${part('second')}.${part('fractionalSecond')}`;
}

function rulerTime(ms: number): string {
  const text = formatTime(ms).split('.')[0];
  return ms >= 3_600_000 ? text : text.slice(3);
}

function tickInterval(duration: number, axisWidth: number): number {
  const desired = duration / Math.max(1, axisWidth / 90);
  const intervals = [100, 250, 500, 1000, 2000, 5000, 10000, 15000, 30000, 60000, 120000, 300000, 600000, 900000, 1800000, 3600000];
  return intervals.find((interval) => interval >= desired) ?? Math.ceil(desired / 3600000) * 3600000;
}

function positionStyle(start: number, end: number, duration: number): CSSProperties {
  const safeStart = Math.max(0, Math.min(start, duration));
  const safeEnd = Math.max(safeStart, Math.min(end, duration));
  return { left: `${safeStart / duration * 100}%`, width: `${(safeEnd - safeStart) / duration * 100}%` };
}

export default function Timeline({ project, comparison, currentTime, activeTrack, trackRevealVersion = 0, selectedId, anchorControls, anchors = NO_ANCHORS, selectedRange = null, onRangeDismiss, onSeek, onScrubStart, onScrubEnd, onTrackSelect, onSegmentSelect }: TimelineProps) {
  const GUTTER = comparison ? 138 : 88;
  const [scrollX, setScrollX] = useState(0);
  const [viewportWidth, setViewportWidth] = useState(720);
  const [plotViewportWidth, setPlotViewportWidth] = useState(710);
  const [windowMs, setWindowMs] = useState(DEFAULT_WINDOW_MS);
  const rootRef = useRef<HTMLElement>(null);
  const tracksViewportRef = useRef<HTMLDivElement>(null);
  const overlapViewportRefs = useRef<Partial<Record<Track, HTMLDivElement | null>>>({});
  const pendingViewRef = useRef<{ projectId: string; windowMs: number; scrollLeft: number } | null>(null);
  const wheelHandlerRef = useRef<(event: WheelEvent) => void>(() => {});
  const [hoverTime, setHoverTime] = useState<HoverTime | null>(null);
  const [scrubTime, setScrubTime] = useState<number | null>(null);
  const scrubRef = useRef<ScrubSession | null>(null);
  const callbacksRef = useRef({ onSeek, onScrubStart, onScrubEnd });
  callbacksRef.current = { onSeek, onScrubStart, onScrubEnd };
  const projectIdRef = useRef(project.id);
  projectIdRef.current = project.id;
  const recordingOrigin = useMemo(() => {
    if (!project.recording_start) return null;
    const value = project.recording_start.trim().replace(' ', 'T');
    const zoned = /(?:Z|[+-]\d{2}:?\d{2})$/i.test(value) ? value : `${value}+08:00`;
    const parsed = Date.parse(zoned);
    return Number.isFinite(parsed) ? parsed : null;
  }, [project.recording_start]);
  const scrollRef = useRef<HTMLDivElement>(null);
  const rulerRef = useRef<HTMLDivElement>(null);
  const duration = Math.max(project.duration_ms, 1);
  const displayedTime = scrubTime ?? currentTime;
  const progress = Math.max(0, Math.min(displayedTime / duration, 1));
  const overlapSegments = useMemo(() => ({
    category: arrangeLanes(project.annotations.category ?? [], duration),
    habit: arrangeLanes(project.annotations.habit, duration),
  }), [project.annotations.category, project.annotations.habit, duration]);
  const rows = useMemo(() => [
    ...(comparison ? [{key: 'external', track: comparison.track, external: true, segments: arrangeLanes(comparison.segments, duration)}] : []),
    ...(comparison ? [comparison.track] : TRACKS).map(track => ({key: track, track, external: false, segments: track === 'category' || track === 'habit' ? overlapSegments[track] : project.annotations[track].map(segment => ({segment, lane: 0}))})),
  ], [comparison, project.annotations, overlapSegments, duration]);
  const visibleAnchors = useMemo(() => anchors.slice(-3).filter((time) => Number.isFinite(time) && time >= 0 && time <= project.duration_ms), [anchors, project.duration_ms]);
  const snapPoints = useMemo(() => timelineSnapPoints(comparison ? {...project, annotations: {scene: [], posture: [], category: [], habit: [], [comparison.track]: [...project.annotations[comparison.track], ...comparison.segments]}} : project, visibleAnchors), [project, visibleAnchors, comparison]);
  const rangeStart = Math.max(0, Math.min(selectedRange?.start_ms ?? 0, project.duration_ms));
  const rangeEnd = Math.max(rangeStart, Math.min(selectedRange?.end_ms ?? 0, project.duration_ms));
  const snapPointsRef = useRef(snapPoints);
  snapPointsRef.current = snapPoints;
  const gaps = useMemo(() => {
    if (project.gaps) return project.gaps;
    const found: VideoGap[] = [];
    let coverageEnd = 0;
    for (const video of [...project.videos].sort((a, b) => a.start_ms - b.start_ms)) {
      if (video.start_ms > coverageEnd) found.push({ start_ms: coverageEnd, end_ms: video.start_ms });
      coverageEnd = Math.max(coverageEnd, video.end_ms);
    }
    if (coverageEnd < project.duration_ms) found.push({ start_ms: coverageEnd, end_ms: project.duration_ms });
    return found;
  }, [project.gaps, project.videos, project.duration_ms]);
  const visibleAxisWidth = Math.max(1, plotViewportWidth - GUTTER);
  const visibleDuration = timelineWindow(duration, windowMs);
  const axisWidth = visibleAxisWidth * duration / visibleDuration;
  const contentWidth = axisWidth + GUTTER;
  const scrollbarWidth = Math.max(0, viewportWidth - plotViewportWidth);
  const maxScroll = Math.max(0, axisWidth - visibleAxisWidth);
  const visibleStart = Math.min(duration, Math.max(0, scrollX / axisWidth * duration));
  const visibleEnd = Math.min(duration, Math.max(0, (scrollX + visibleAxisWidth) / axisWidth * duration));
  const tickStep = tickInterval(duration, axisWidth);
  const ticks = useMemo(() => visibleTimelineTicks(visibleStart, visibleEnd, tickStep, duration), [visibleStart, visibleEnd, duration, tickStep]);

  useEffect(() => {
    const element = scrollRef.current;
    if (!element) return;
    const measure = () => { if (scrubRef.current) finishScrub(true); setViewportWidth(element.clientWidth); setPlotViewportWidth(tracksViewportRef.current?.clientWidth ?? element.clientWidth); setScrollX(element.scrollLeft); setHoverTime(null); };
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(element);
    if (tracksViewportRef.current) observer.observe(tracksViewportRef.current);
    return () => observer.disconnect();
  }, []);

  useLayoutEffect(() => {
    setHoverTime(null);
    finishScrub(true);
    pendingViewRef.current = null;
    setWindowMs(DEFAULT_WINDOW_MS);
    setScrollX(0);
    if (scrollRef.current) scrollRef.current.scrollLeft = 0;
    if (tracksViewportRef.current) tracksViewportRef.current.scrollLeft = 0;
    for (const viewport of Object.values(overlapViewportRefs.current)) if (viewport) viewport.scrollTop = 0;
  }, [project.id]);

  useLayoutEffect(() => {
    const scroller = scrollRef.current;
    if (!scroller) return;
    const pending = pendingViewRef.current;
    if (pending?.projectId === project.id) scroller.scrollLeft = pending.scrollLeft;
    pendingViewRef.current = null;
    if (tracksViewportRef.current) tracksViewportRef.current.scrollLeft = scroller.scrollLeft;
    setScrollX(scroller.scrollLeft);
  }, [axisWidth, viewportWidth, project.id]);

  useLayoutEffect(() => {
    if (activeTrack !== 'category' && activeTrack !== 'habit') return;
    const viewport = overlapViewportRefs.current[activeTrack];
    if (!viewport) return;
    const selected = overlapSegments[activeTrack].find(({ segment }) => segment.id === selectedId);
    const top = selected ? 6 + selected.lane * LANE_PITCH : 0;
    const bottom = selected ? top + SEGMENT_HEIGHT : Math.min(34, viewport.clientHeight);
    if (top < viewport.scrollTop) viewport.scrollTop = Math.max(0, top - 6);
    else if (bottom > viewport.scrollTop + viewport.clientHeight) viewport.scrollTop = bottom + 6 - viewport.clientHeight;
  }, [activeTrack, trackRevealVersion, selectedId, overlapSegments, project.id]);

  useEffect(() => {
    const hide = () => setHoverTime(null);
    window.addEventListener('scroll', hide, true);
    window.addEventListener('resize', hide);
    const blur = () => { hide(); finishScrub(true); };
    window.addEventListener('blur', blur);
    return () => {
      window.removeEventListener('scroll', hide, true);
      window.removeEventListener('resize', hide);
      window.removeEventListener('blur', blur);
    };
  }, []);

  useEffect(() => { setHoverTime(null); finishScrub(true); }, [project.duration_ms, recordingOrigin]);

  useEffect(() => {
    const escape = (event: KeyboardEvent) => {
      if (event.key !== 'Escape' || !scrubRef.current) return;
      event.preventDefault();
      event.stopPropagation();
      finishScrub(true);
    };
    window.addEventListener('keydown', escape, true);
    return () => {
      window.removeEventListener('keydown', escape, true);
      finishScrub(true, undefined, false);
    };
  }, []);

  function changeWindow(nextWindow: number, clientX?: number) {
    const scroller = scrollRef.current;
    if (!scroller || scrubRef.current) return;
    const rect = scroller.getBoundingClientRect();
    const pending = pendingViewRef.current;
    const before = pending?.projectId === project.id ? pending.windowMs : visibleDuration;
    const scrollLeft = pending?.projectId === project.id ? pending.scrollLeft : scroller.scrollLeft;
    const pointer = clientX === undefined ? visibleAxisWidth / 2 : clientX - rect.left - scroller.clientLeft - GUTTER;
    const next = scaleTimelineWindow(duration, before, nextWindow, visibleAxisWidth, scrollLeft, pointer);
    if (next.windowMs === before) return;
    pendingViewRef.current = { projectId: project.id, windowMs: next.windowMs, scrollLeft: next.scrollLeft };
    setHoverTime(null);
    setWindowMs(next.windowMs);
  }

  wheelHandlerRef.current = (event: WheelEvent) => {
    const scroller = scrollRef.current;
    if (!scroller) return;
    if (event.ctrlKey) {
      // The workspace also cancels browser zoom; this still runs when it has already done so.
      event.preventDefault();
      event.stopPropagation();
      const pending = pendingViewRef.current;
      const before = pending?.projectId === project.id ? pending.windowMs : visibleDuration;
      const unit = event.deltaMode === 1 ? 16 : event.deltaMode === 2 ? 100 : 1;
      const delta = Math.max(-400, Math.min(400, event.deltaY * unit));
      if (delta) changeWindow(before * Math.pow(1.25, delta / 100), event.clientX);
      return;
    }
    if (!event.altKey) {
      const toolbar = event.target instanceof Element ? event.target.closest<HTMLElement>('.tl-toolbar') : null;
      if (toolbar && toolbar.scrollWidth > toolbar.clientWidth) {
        event.preventDefault(); event.stopPropagation();
        const movement = Math.abs(event.deltaX) > Math.abs(event.deltaY) ? event.deltaX : event.deltaY;
        toolbar.scrollLeft += movement * (event.deltaMode === 1 ? 16 : event.deltaMode === 2 ? toolbar.clientWidth : 1);
      }
      return;
    }
    event.preventDefault();
    event.stopPropagation();
    setHoverTime(null);
    const movement = Math.abs(event.deltaY) >= Math.abs(event.deltaX) ? event.deltaY : event.deltaX;
    const unit = event.deltaMode === 1 ? 16 : event.deltaMode === 2 ? visibleAxisWidth : 1;
    scroller.scrollLeft += movement * unit;
    syncHorizontalScroll(scroller.scrollLeft);
  };

  useEffect(() => {
    const root = rootRef.current;
    if (!root) return;
    const wheel = (event: WheelEvent) => wheelHandlerRef.current(event);
    root.addEventListener('wheel', wheel, { passive: false, capture: true });
    return () => root.removeEventListener('wheel', wheel, true);
  }, []);

  function syncHorizontalScroll(left: number) {
    const viewport = tracksViewportRef.current;
    if (viewport && Math.abs(viewport.scrollLeft - left) > 0.5) viewport.scrollLeft = left;
    setScrollX(left);
    setHoverTime(null);
  }

  function pointerTime(clientX: number, session: ScrubSession): number | null {
    const scroller = scrollRef.current;
    const ruler = rulerRef.current;
    if (!scroller || !ruler) return null;
    const viewport = scroller.getBoundingClientRect();
    const plot = ruler.getBoundingClientRect();
    if (plot.width <= 0) return null;
    const left = viewport.left + scroller.clientLeft + GUTTER;
    const right = viewport.left + scroller.clientLeft + (tracksViewportRef.current?.clientWidth ?? scroller.clientWidth);
    const pointer = Math.max(left, Math.min(clientX - session.grabOffset, right));
    return snapTimelineTime((pointer - plot.left) / plot.width * session.duration, snapPointsRef.current, session.duration, plot.width);
  }

  function updateScrub(now: number) {
    const session = scrubRef.current;
    if (!session) return;
    if (session.projectId !== projectIdRef.current) { finishScrub(true); return; }
    const target = pointerTime(session.clientX, session);
    if (target === null) { finishScrub(true); return; }
    if (session.latestTime !== target) { session.latestTime = target; setScrubTime(target); }
    if (session.lastSeekTime !== target && now - session.lastSeekAt >= 100) {
      session.lastSeekAt = now;
      session.lastSeekTime = target;
      callbacksRef.current.onSeek(target);
    }
  }

  function scrubFrame(now: number) {
    const session = scrubRef.current;
    if (!session) return;
    const scroller = scrollRef.current;
    const elapsed = Math.max(0, Math.min(32, now - session.lastFrameAt));
    session.lastFrameAt = now;
    if (scroller && session.hasMoved) {
      const viewport = scroller.getBoundingClientRect();
      const left = viewport.left + scroller.clientLeft + GUTTER;
      const right = viewport.left + scroller.clientLeft + (tracksViewportRef.current?.clientWidth ?? scroller.clientWidth);
      const pointer = session.clientX - session.grabOffset;
      const edge = Math.min(32, Math.max(8, (right - left) / 4));
      const velocity = pointer < left + edge ? -Math.min(1, (left + edge - pointer) / edge)
        : pointer > right - edge ? Math.min(1, (pointer - right + edge) / edge) : 0;
      if (velocity !== 0) {
        const previous = scroller.scrollLeft;
        scroller.scrollLeft += velocity * 480 * elapsed / 1000;
        if (scroller.scrollLeft !== previous) syncHorizontalScroll(scroller.scrollLeft);
      }
    }
    updateScrub(now);
    if (scrubRef.current === session) session.frame = requestAnimationFrame(scrubFrame);
  }

  function beginScrub(event: ReactPointerEvent<HTMLElement>, fromHandle = false) {
    if (event.button !== 0 || !event.isPrimary || scrubRef.current) return;
    const plot = rulerRef.current?.getBoundingClientRect();
    if (!plot || plot.width <= 0) return;
    event.preventDefault();
    event.stopPropagation();
    const now = performance.now();
    const session: ScrubSession = {
      pointerId: event.pointerId, owner: event.currentTarget, projectId: project.id, duration: project.duration_ms,
      clientX: event.clientX, startX: event.clientX, startY: event.clientY,
      grabOffset: fromHandle ? event.clientX - (plot.left + progress * plot.width) : 0,
      hasMoved: false, latestTime: displayedTime, lastSeekTime: null,
      lastSeekAt: -Infinity, lastFrameAt: now, frame: 0,
    };
    try { event.currentTarget.setPointerCapture(event.pointerId); } catch { return; }
    scrubRef.current = session;
    event.currentTarget.focus({ preventScroll: true });
    setHoverTime(null);
    setScrubTime(displayedTime);
    callbacksRef.current.onScrubStart?.();
    updateScrub(now);
    if (scrubRef.current === session) session.frame = requestAnimationFrame(scrubFrame);
  }

  function moveScrub(event: ReactPointerEvent<HTMLElement>) {
    const session = scrubRef.current;
    if (!session || event.pointerId !== session.pointerId) return;
    event.preventDefault();
    event.stopPropagation();
    session.clientX = event.clientX;
    if (Math.abs(event.clientX - session.startX) > 3 || Math.abs(event.clientY - session.startY) > 3) session.hasMoved = true;
    updateScrub(performance.now());
  }

  function finishScrub(cancelled: boolean, clientX?: number, updateView = true) {
    const session = scrubRef.current;
    if (!session) return;
    scrubRef.current = null;
    cancelAnimationFrame(session.frame);
    const aborted = cancelled || session.projectId !== projectIdRef.current;
    const target = aborted ? session.latestTime : pointerTime(clientX ?? session.clientX, session) ?? session.latestTime;
    try { if (session.owner.hasPointerCapture(session.pointerId)) session.owner.releasePointerCapture(session.pointerId); } catch { /* Detached capture owner. */ }
    if (updateView) { setScrubTime(null); setHoverTime(null); }
    // The parent commits this exact release position once, after leaving scrub mode.
    if (callbacksRef.current.onScrubEnd) callbacksRef.current.onScrubEnd(aborted, target);
    else if (!aborted) callbacksRef.current.onSeek(target);
  }

  const scrubPointerHandlers = {
    onPointerMove: moveScrub,
    onPointerUp: (event: ReactPointerEvent<HTMLElement>) => {
      if (event.pointerId !== scrubRef.current?.pointerId) return;
      event.preventDefault(); event.stopPropagation(); finishScrub(false, event.clientX);
    },
    onPointerCancel: (event: ReactPointerEvent<HTMLElement>) => {
      if (event.pointerId !== scrubRef.current?.pointerId) return;
      event.preventDefault(); event.stopPropagation(); finishScrub(true);
    },
    onLostPointerCapture: (event: ReactPointerEvent<HTMLElement>) => {
      if (event.pointerId === scrubRef.current?.pointerId) finishScrub(true);
    },
    onClick: (event: MouseEvent<HTMLElement>) => { event.preventDefault(); event.stopPropagation(); },
  };

  function hoverVideoAt(time: number) {
    const exact = project.videos.find((video) => time >= video.start_ms && time < video.end_ms);
    if (exact) return exact;
    const bridge = project.continuity_bridges?.find((item) => time >= item.start_ms && time < item.end_ms);
    if (bridge) return project.videos.find((video) => video.end_ms === bridge.start_ms);
    const last = project.videos[project.videos.length - 1];
    return last && time === last.end_ms ? last : undefined;
  }

  function joinsNext(index: number) {
    const video = project.videos[index];
    const next = project.videos[index + 1];
    return !!video && !!next && (video.end_ms === next.start_ms || !!project.continuity_bridges?.some((bridge) => bridge.start_ms === video.end_ms && bridge.end_ms === next.start_ms));
  }

  function timeAtClientX(clientX: number): { time: number; snapped: boolean } | null {
    const plot = rulerRef.current?.getBoundingClientRect();
    if (!plot || plot.width <= 0) return null;
    const rawTime = Math.max(0, Math.min((clientX - plot.left) / plot.width, 1)) * project.duration_ms;
    const time = snapTimelineTime(rawTime, snapPointsRef.current, project.duration_ms, plot.width);
    return { time, snapped: snapPointsRef.current.includes(time) };
  }

  function showHoverTime(event: MouseEvent<HTMLElement>) {
    if (scrubRef.current) return;
    const scroller = scrollRef.current;
    const viewport = scroller?.getBoundingClientRect();
    if (!scroller || !viewport || event.clientX < viewport.left + scroller.clientLeft + GUTTER || event.clientX > viewport.left + scroller.clientLeft + (tracksViewportRef.current?.clientWidth ?? scroller.clientWidth)) {
      setHoverTime(null);
      return;
    }
    const pointed = timeAtClientX(event.clientX);
    if (!pointed) { setHoverTime(null); return; }
    const width = Math.min(HOVER_WIDTH, Math.max(0, window.innerWidth - 16));
    const left = Math.max(8, Math.min(event.clientX - width / 2, window.innerWidth - width - 8));
    const height = Math.min(HOVER_HEIGHT, Math.max(0, window.innerHeight - 16));
    const above = event.clientY - height - 12;
    const top = above >= 8 ? above : Math.max(8, Math.min(event.clientY + 18, window.innerHeight - height - 8));
    setHoverTime({ ...pointed, left, top, width, height, noVideo: !hoverVideoAt(pointed.time) });
  }

  function centerOnCursor() {
    const scroller = scrollRef.current;
    if (!scroller) return;
    setHoverTime(null);
    scroller.scrollLeft = Math.max(0, progress * axisWidth - visibleAxisWidth / 2);
    syncHorizontalScroll(scroller.scrollLeft);
  }

  function panHalfScreen(direction: -1 | 1) {
    const scroller = scrollRef.current;
    if (!scroller) return;
    setHoverTime(null);
    scroller.scrollLeft += direction * visibleAxisWidth / 2;
    syncHorizontalScroll(scroller.scrollLeft);
  }
  function renderGaps(variant: 'ruler' | 'video' | 'track') {
    return gaps.map((gap) => {
      const skipped = project.skipped_videos?.filter(video => video.start_ms < gap.end_ms && video.end_ms > gap.start_ms) ?? [];
      return <span
      key={`${gap.start_ms}-${gap.end_ms}`}
      className={`tl-gap tl-gap-${variant}`}
      style={positionStyle(gap.start_ms, gap.end_ms, duration)}
      title={`${skipped.length ? '已跳过：' + skipped.map(video => video.name).join('、') : '无视频'} · ${formatTime(gap.start_ms)} → ${formatTime(gap.end_ms)}`}
      aria-hidden="true"
    ><span className="tl-gap-break">//</span>{variant === 'video' && (gap.end_ms - gap.start_ms) / duration * axisWidth > 65 && <span className="tl-gap-label">{skipped.length ? '已跳过' : '无视频'}</span>}</span>;
    });
  }

  function renderSegment(track: Track, segment: Segment, lane = 0, external = false) {
    const end = segment.end_ms ?? project.duration_ms;
    const selected = !comparison && segment.id === selectedId && activeTrack === track;
    const point = track === 'habit' && isPointSegment(segment);
    return (
      <button
        key={segment.id}
        type="button"
        className={`tl-segment tl-segment-${track}${selected ? ' tl-segment-selected' : ''}${segment.end_ms === null ? ' tl-segment-open' : ''}${point ? ' tl-segment-point' : ''}`}
        style={{ ...positionStyle(segment.start_ms, point ? segment.start_ms : end, duration), top: 6 + lane * LANE_PITCH }}
        title={point ? `${segment.label} · 时点 ${formatTime(segment.start_ms)}` : `${segment.label} · ${formatTime(segment.start_ms)} → ${segment.end_ms === null ? '标注中' : formatTime(segment.end_ms)}`}
        aria-label={point ? `${comparison ? (external ? '外部' : '项目') : ''}${TRACK_LABELS[track]}：${segment.label}，时点 ${formatTime(segment.start_ms)}` : `${comparison ? (external ? '外部' : '项目') : ''}${TRACK_LABELS[track]}：${segment.label}，从 ${formatTime(segment.start_ms)}${segment.end_ms === null ? '，标注中' : ` 到 ${formatTime(segment.end_ms)}`}`}
        aria-pressed={selected}
        onClick={(event) => {
          event.stopPropagation();
          // The parent accepts the selection and commits one seek, or keeps its pending edit.
          const seekTime = event.detail === 0 ? segment.start_ms : timeAtClientX(event.clientX)?.time ?? segment.start_ms;
          if (comparison) onSeek(seekTime); else onSegmentSelect(track, segment, seekTime);
        }}
      >
        <span className="tl-segment-label">{segment.label}</span>
        {!point && segment.end_ms === null && <span className="tl-open-dot" aria-hidden="true" />}
      </button>
    );
  }

  return (
    <section ref={rootRef} className={`tl-root${comparison ? ' tl-comparison' : ''}${scrubTime !== null ? ' tl-is-scrubbing' : ''}`} aria-label="视频与标注时间轴">
      <header className="tl-toolbar">
        <div className="tl-heading"><h2>时间轴</h2></div>
        {anchorControls}
        <div className="tl-browse-controls" aria-label="浏览时间轴">
          <button type="button" aria-label="向左浏览半屏" title="向左浏览半屏" disabled={scrollX <= 1} onClick={() => panHalfScreen(-1)}>‹</button>
          <button type="button" aria-label="向右浏览半屏" title="向右浏览半屏" disabled={scrollX >= maxScroll - 1} onClick={() => panHalfScreen(1)}>›</button>
          <button type="button" className="tl-return-cursor" aria-label="回到光标" title={`回到播放光标 ${formatTime(currentTime)}`} onClick={centerOnCursor}>回到光标</button>
          <button type="button" className="tl-reset-zoom" aria-label="重置时间轴为每屏1小时" title="重置为每屏1小时；不足1小时则显示全程" onClick={() => changeWindow(DEFAULT_WINDOW_MS)}>1小时</button>
        </div>
        <span className="tl-screen-duration" title={`每屏 ${formatTime(visibleDuration)}；Ctrl + 滚轮缩放，Alt + 滚轮横移`}>每屏 {Math.abs(visibleDuration - HOUR_MS) < 1 ? '1小时' : rulerTime(visibleDuration)}</span>
      </header>
      <div className="tl-scroll" ref={scrollRef} onPointerDownCapture={(event) => {
        // Capture runs before scrub/segment handlers; toolbar buttons live outside this surface.
        const axis = event.target instanceof Element && event.target.closest('.tl-row, .tl-playhead-handle');
        if (selectedRange && event.button === 0 && event.isPrimary && axis) onRangeDismiss?.();
      }} onScroll={(event) => syncHorizontalScroll(event.currentTarget.scrollLeft)}>
        <div className="tl-content" onMouseMove={showHoverTime} onMouseLeave={() => setHoverTime(null)} style={{ width: contentWidth + scrollbarWidth, '--tl-gutter': `${GUTTER}px` } as CSSProperties}>
          <div className="tl-fixed-tracks" style={{ width: contentWidth }}>
          <div className="tl-row tl-ruler-row">
            <div className="tl-gutter tl-ruler-gutter">全局时间</div>
            <div className="tl-ruler tl-seek-surface" ref={rulerRef} onPointerDown={(event) => beginScrub(event)} {...scrubPointerHandlers} role="slider" aria-label="跳转全局视频时间" aria-valuemin={0} aria-valuemax={project.duration_ms} aria-valuenow={Math.round(displayedTime)} aria-valuetext={formatTime(displayedTime)} tabIndex={0} onKeyDown={(event) => {
              let value: number | undefined;
              if (event.key === 'Home') value = 0;
              if (event.key === 'End') value = project.duration_ms;
              if (value !== undefined) { event.preventDefault(); event.stopPropagation(); onSeek(Math.max(0, Math.min(value, project.duration_ms))); }
            }}>
              {renderGaps('ruler')}
              {ticks.map((tick) => <span key={tick} className={`tl-tick${tick === 0 ? ' tl-tick-first' : ''}`} style={{ left: `${tick / duration * 100}%` }}><span>{tickStep < 1000 ? formatTime(tick).slice(6) : rulerTime(tick)}</span></span>)}
            </div>
          </div>
          <div className="tl-row tl-video-row">
            <div className="tl-gutter"><span className="tl-row-symbol tl-video-symbol" aria-hidden="true">▸</span><span>视频</span><span className="tl-count">{project.videos.length}</span></div>
            <div className="tl-video-strip tl-seek-surface" onPointerDown={(event) => beginScrub(event)} {...scrubPointerHandlers}>
              {project.videos.map((video, index) => <div key={video.id} className={`tl-video-clip${joinsNext(index - 1) ? ' tl-video-joined-left' : ''}${joinsNext(index) ? ' tl-video-joined-right' : ''}`} style={positionStyle(video.start_ms, joinsNext(index) ? project.videos[index + 1].start_ms : video.end_ms, duration)}>
                {video.thumbnail_url && <div className="tl-video-image" style={{ backgroundImage: `url(${JSON.stringify(sessionAssetUrl(project.id, video.thumbnail_url))})` }} />}
                <span className="tl-video-index">{String(index + 1).padStart(2, '0')}</span>
                <span className="tl-video-name">{video.name}</span>
              </div>)}
              {renderGaps('video')}
              {!project.videos.length && <span className="tl-empty-track">导入视频后显示连续预览轴</span>}
            </div>
          </div>
          </div>
          <div className="tl-tracks-viewport" ref={tracksViewportRef} style={{ width: viewportWidth }} role="region" aria-label={comparison ? `外部与项目${TRACK_LABELS[comparison.track]}对照轴` : "场景、姿势、大类、习惯四维标注轴"} onScroll={(event) => {
            const left = event.currentTarget.scrollLeft;
            if (scrollRef.current && Math.abs(scrollRef.current.scrollLeft - left) > 0.5) scrollRef.current.scrollLeft = left;
            setHoverTime(null);
          }}>
            <div className="tl-tracks-content" style={{ width: contentWidth }}>
              {rows.map(({key, track, external, segments}) => {
                const active = !comparison && activeTrack === track;
                const overlapping = external || track === 'habit' || track === 'category';
                const laneCount = Math.max(1, ...segments.map(({ lane }) => lane + 1));
                const contentHeight = overlapping ? laneCount * LANE_PITCH + 10 : 34;
                return <div className={`tl-row tl-track-row tl-track-${track}${external ? ' tl-external-row' : ''}${active ? ' tl-track-active' : ''}`} key={key}>
                  <button type="button" className="tl-gutter tl-track-select" aria-pressed={active} onClick={() => { if (!comparison) onTrackSelect(track); }} aria-label={`${comparison ? (external ? '外部' : '项目') : '选择'}${TRACK_LABELS[track]}标注轴`}>
                    <span className="tl-track-title"><span className={`tl-row-dot tl-dot-${track}`} /><span>{comparison ? (external ? '外部 · ' : '项目 · ') : ''}{TRACK_LABELS[track]}</span><span className="tl-count">{segments.length}</span>
                    {overlapping && laneCount > 1 && <span className="tl-lane-hint">{laneCount}层</span>}</span>
                  </button>
                  <div className={overlapping ? 'tl-overlap-viewport' : 'tl-state-viewport'}
                    ref={(element) => { if (overlapping && !external) overlapViewportRefs.current[track] = element; }}
                    style={{ height: overlapping ? Math.min(contentHeight, OVERLAP_VIEWPORT_MAX) : contentHeight }}
                    role={overlapping ? 'region' : undefined}
                    aria-label={overlapping ? `${TRACK_LABELS[track]}标注，共 ${laneCount} 层，可上下滚动查看` : undefined}
                    tabIndex={overlapping ? 0 : undefined}
                    onScroll={() => setHoverTime(null)}
                    onKeyDown={(event) => {
                      if (!overlapping) return;
                      const viewport = event.currentTarget;
                      let next: number | undefined;
                      if (event.key === 'ArrowUp') next = viewport.scrollTop - LANE_PITCH;
                      if (event.key === 'ArrowDown') next = viewport.scrollTop + LANE_PITCH;
                      if (event.key === 'PageUp') next = viewport.scrollTop - viewport.clientHeight;
                      if (event.key === 'PageDown') next = viewport.scrollTop + viewport.clientHeight;
                      if (event.key === 'Home') next = 0;
                      if (event.key === 'End') next = viewport.scrollHeight;
                      if (next !== undefined) { event.preventDefault(); event.stopPropagation(); viewport.scrollTop = next; }
                    }}>
                  <div className="tl-track-surface tl-seek-surface" style={{ height: contentHeight }} onPointerDown={(event) => {
                    if ((event.target as HTMLElement).closest('button')) return;
                    beginScrub(event);
                  }} {...scrubPointerHandlers}>
                    {!segments.length && <span className="tl-empty-track">{comparison ? '暂无记录' : track === 'scene' ? '点击右侧按钮切换室内 / 室外' : track === 'posture' ? 'Z 动 / X 坐 / C 站 / V 躺' : track === 'category' ? '切换大类，或添加可重叠的时间段' : '记录习惯，同一时段可叠加'}</span>}
                    {segments.map(({ segment, lane }) => renderSegment(track, segment, lane, external))}
                    {renderGaps('track')}
                  </div>
                  </div>
                </div>;
              })}
            </div>
          </div>
          <div className="tl-overlays" style={{ left: GUTTER + scrollX, width: visibleAxisWidth }}>
          {rangeEnd > rangeStart && <div className="tl-range-overlay" aria-label={`锚点选区 ${formatTime(rangeStart)} 到 ${formatTime(rangeEnd)}`} style={{
            left: rangeStart / duration * axisWidth - scrollX,
            width: (rangeEnd - rangeStart) / duration * axisWidth,
          }} />}
          {visibleAnchors.map((time, index) => <div key={`${index}-${time}`} className="tl-anchor" style={{ left: time / duration * axisWidth - scrollX }} aria-label={`锚点 ${index + 1}，${formatTime(time)}`}>
            <span className={`tl-anchor-label${time / duration * axisWidth - scrollX > visibleAxisWidth - 30 ? ' tl-anchor-label-left' : ''}`}>{index + 1}</span>
          </div>)}
          {hoverTime && <div className={`tl-hover-line${hoverTime.snapped ? ' tl-hover-line-snapped' : ''}`} style={{ left: hoverTime.time / duration * axisWidth - scrollX }} aria-hidden="true" />}
          <div className="tl-playhead" style={{ left: progress * axisWidth - scrollX }}>
            <div className="tl-playhead-handle" role="slider" tabIndex={0} aria-label="拖动播放光标" aria-valuemin={0} aria-valuemax={project.duration_ms} aria-valuenow={Math.round(displayedTime)} aria-valuetext={formatTime(displayedTime)} onPointerDown={(event) => beginScrub(event, true)} {...scrubPointerHandlers} onKeyDown={(event) => {
              const target = event.key === 'Home' ? 0 : event.key === 'End' ? project.duration_ms : null;
              if (target !== null) { event.preventDefault(); event.stopPropagation(); if (!scrubRef.current) onSeek(target); }
            }} />
          </div>
          </div>
        </div>
      </div>
      {hoverTime && createPortal(<div
        className="tl-hover-tooltip"
        role="tooltip"
        style={{ left: hoverTime.left, top: hoverTime.top, width: hoverTime.width, height: hoverTime.height }}
      >
        <HoverPreview projectId={project.id} video={hoverVideoAt(hoverTime.time)} time={hoverTime.time} />
        <div className="tl-hover-main"><span>{hoverTime.snapped ? '已吸附' : '时间轴'}</span><strong>{formatTime(hoverTime.time)}</strong>{hoverTime.noVideo && <em>无视频</em>}</div>
        <div className="tl-hover-clock">{recordingDateTime(recordingOrigin, hoverTime.time) ?? '录制起点未设置'}{recordingOrigin !== null && <span>北京时间</span>}</div>
      </div>, document.body)}
    </section>
  );
}







