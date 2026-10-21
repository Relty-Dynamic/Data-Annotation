import { Fragment, useEffect, useLayoutEffect, useMemo, useRef, useState, type ReactNode, type CSSProperties, type MouseEvent, type PointerEvent as ReactPointerEvent } from 'react';
import { createPortal } from 'react-dom';
import { arrangeLanes, customTrack, formatTime, isPointSegment, isEventTrack, projectTracks, timelineSnapPoints, trackName, type Project, type Segment, type Track, type VideoGap } from './domain';
import HoverPreview from './HoverPreview';
import { sessionAssetUrl } from './sessionAssets';
import { DEFAULT_WINDOW_MS, timelineWheelAction, timelineWindow, visibleTimelineTicks } from './timelineViewport';
import { recordingScale, zoomRecordingScale, snapDisplayedTime } from './timelineScale';
import { parseRecordingOrigin, recordingDateTime } from './recordingTime';
import { durationReferenceLabels } from './durationReference';
import './timeline.css';

export interface TimelineProps {
  project: Project;
  currentTime: number;
  activeTrack: Track;
  trackRevealVersion?: number;
  selectedId: string | null;
  selectedBoundary?: 'start' | 'end' | null;
  anchorControls?: ReactNode;
  anchors?: number[];
  selectedRange?: { start_ms: number; end_ms: number } | null;
  onRangeDismiss?: () => void;
  onSeek: (ms: number) => void;
  onScrubStart?: () => void;
  onScrubEnd?: (cancelled: boolean, targetMs: number) => void;
  onTrackSelect: (track: Track) => void;
  onSegmentSelect: (track: Track, segment: Segment, seekTime?: number) => void;
  onBoundarySelect?: (track: Track, segment: Segment, edge: 'start' | 'end') => boolean;
  onBoundaryDismiss?: () => void;
}

const LANE_HEIGHT = 34;
const NO_ANCHORS: number[] = [];
const HOVER_WIDTH = 280;
const HOVER_HEIGHT = 226;

interface ScrubSession {
  pointerId: number;
  owner: HTMLElement;
  projectId: string;
  clientX: number;
  startX: number;
  startY: number;
  grabOffset: number;
  hasMoved: boolean;
  exactStart: number | null;
  snapPoints: number[];
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

export default function Timeline({ project, currentTime, activeTrack, trackRevealVersion = 0, selectedId, selectedBoundary = null, anchorControls, anchors = NO_ANCHORS, selectedRange = null, onRangeDismiss, onSeek, onScrubStart, onScrubEnd, onTrackSelect, onSegmentSelect, onBoundarySelect, onBoundaryDismiss }: TimelineProps) {
  const GUTTER = 112;
  const [scrollX, setScrollX] = useState(0);
  const [viewportWidth, setViewportWidth] = useState(720);
  const [plotViewportWidth, setPlotViewportWidth] = useState(710);
  const [windowMs, setWindowMs] = useState(DEFAULT_WINDOW_MS);
  const rootRef = useRef<HTMLElement>(null);
  const tracksViewportRef = useRef<HTMLDivElement>(null);
  const overlaysRef = useRef<HTMLDivElement>(null);
  const overlayContentRef = useRef<HTMLDivElement>(null);
  const trackGroupRefs = useRef<Partial<Record<Track, HTMLDivElement | null>>>({});
  const selectedSegmentRef = useRef<HTMLButtonElement | null>(null);
  const pendingViewRef = useRef<{ projectId: string; windowMs: number; scrollLeft: number } | null>(null);
  const wheelHandlerRef = useRef<(event: WheelEvent) => void>(() => {});
  const [hoverTime, setHoverTime] = useState<HoverTime | null>(null);
  const [hoveringBoundary, setHoveringBoundary] = useState(false);
  const dismissedPointerClick = useRef(false);
  const [scrubTime, setScrubTime] = useState<number | null>(null);
  const scrubRef = useRef<ScrubSession | null>(null);
  const callbacksRef = useRef({ onSeek, onScrubStart, onScrubEnd });
  callbacksRef.current = { onSeek, onScrubStart, onScrubEnd };
  const projectIdRef = useRef(project.id);
  projectIdRef.current = project.id;
  const recordingOrigin = useMemo(() => parseRecordingOrigin(project.recording_start), [project.recording_start]);
  const annotationTime = (ms: number) => recordingDateTime(recordingOrigin, ms)?.slice(11) ?? formatTime(ms);
  const scrollRef = useRef<HTMLDivElement>(null);
  const rulerRef = useRef<HTMLDivElement>(null);
  const duration = Math.max(project.duration_ms, 1);
  // Boundary edits can stop at a neighbor or recording gap before the pointer does.
  const displayedTime = selectedBoundary ? currentTime : scrubTime ?? currentTime;
  const rows = useMemo(() => projectTracks(project).map(track => ({track, segments: arrangeLanes(project.annotations[track] ?? [], duration)})), [project, duration]);
  const visibleAnchors = useMemo(() => anchors.slice(-2).filter((time) => Number.isFinite(time) && time >= 0 && time <= project.duration_ms), [anchors, project.duration_ms]);
  const snapPoints = useMemo(() => timelineSnapPoints(project, visibleAnchors), [project, visibleAnchors]);
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
  const scale = useMemo(() => recordingScale(duration, gaps, windowMs, visibleAxisWidth), [duration, gaps, windowMs, visibleAxisWidth]);
  const visibleDuration = timelineWindow(scale.duration, windowMs);
  const axisWidth = visibleAxisWidth * scale.duration / visibleDuration;
  // Recorded spans use this slope; compressed gaps must not shorten the reference.
  const minuteWidth = 60_000 * axisWidth / scale.duration;
  const minuteReferenceRef = useRef<HTMLDivElement>(null);
  const [referenceWidth, setReferenceWidth] = useState(0);
  useLayoutEffect(() => {
    const element = minuteReferenceRef.current;
    if (!element) return;
    const measure = () => setReferenceWidth(element.clientWidth);
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(element);
    return () => observer.disconnect();
  }, []);
  const referenceClipped = minuteWidth > referenceWidth;
  const referenceLabels = durationReferenceLabels(minuteWidth, referenceWidth);
  const progress = scale.toDisplay(displayedTime) / scale.duration;
  const pixelAt = (time: number) => scale.toDisplay(time) / scale.duration * axisWidth;
  const position = (start: number, end: number) => positionStyle(scale.toDisplay(start), scale.toDisplay(end), scale.duration);
  const contentWidth = axisWidth + GUTTER;
  const scrollbarWidth = Math.max(0, viewportWidth - plotViewportWidth);
  const visibleStart = scale.toTime(Math.max(0, scrollX / axisWidth * scale.duration));
  const visibleEnd = scale.toTime((scrollX + visibleAxisWidth) / axisWidth * scale.duration);
  const tickStep = tickInterval(scale.duration, axisWidth);
  const ticks = useMemo(() => {
    // Generate ticks only over recorded stretches, even across multi-day gaps.
    const result: number[] = [];
    let cursor = 0;
    for (const gap of [...gaps, {start_ms: duration, end_ms: duration}]) {
      const start = Math.max(cursor, visibleStart), end = Math.min(gap.start_ms, visibleEnd);
      if (end >= start) result.push(...visibleTimelineTicks(start, end, tickStep, duration).filter(tick => tick >= cursor && tick <= gap.start_ms));
      cursor = gap.end_ms;
    }
    return [...new Set(result)].sort((a, b) => a - b);
  }, [visibleStart, visibleEnd, gaps, duration, tickStep]);
  const labeledTicks = useMemo(() => {
    let previous = -Infinity;
    return new Set(ticks.filter(tick => {
      const x = scale.toDisplay(tick) / scale.duration * axisWidth - scrollX;
      if (!(tick === 0 ? scrollX < 1 : x >= 40 && x <= visibleAxisWidth - 40) || x - previous < 90) return false;
      previous = x;
      return true;
    }));
  }, [ticks, scale, axisWidth, scrollX, visibleAxisWidth]);


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
    const root = rootRef.current;
    const grid = root?.closest<HTMLElement>('.editor-grid');
    const toolbar = root?.querySelector<HTMLElement>('.tl-toolbar');
    const fixed = root?.querySelector<HTMLElement>('.tl-fixed-tracks');
    const tracks = root?.querySelector<HTMLElement>('.tl-tracks-content');
    const scroll = scrollRef.current;
    if (!root || !grid || !toolbar || !fixed || !tracks || !scroll) return;
    const previous = grid.style.getPropertyValue('--timeline-natural-height');
    let applied = '';
    const measure = () => {
      const style = getComputedStyle(root);
      const border = parseFloat(style.borderTopWidth) + parseFloat(style.borderBottomWidth);
      const scrollbar = scroll.offsetHeight - scroll.clientHeight;
      const height = Math.ceil(toolbar.getBoundingClientRect().height + fixed.getBoundingClientRect().height
        + tracks.getBoundingClientRect().height + scrollbar + border);
      const value = `${height}px`;
      if (value !== applied) grid.style.setProperty('--timeline-natural-height', applied = value);
    };
    measure();
    const observer = new ResizeObserver(measure);
    for (const element of [toolbar, fixed, tracks, scroll]) observer.observe(element);
    return () => {
      observer.disconnect();
      if (grid.style.getPropertyValue('--timeline-natural-height') === applied) {
        if (previous) grid.style.setProperty('--timeline-natural-height', previous);
        else grid.style.removeProperty('--timeline-natural-height');
      }
    };
  }, []);

  useLayoutEffect(() => {
    setHoverTime(null);
    finishScrub(true);
    pendingViewRef.current = null;
    setWindowMs(DEFAULT_WINDOW_MS);
    setScrollX(0);
    if (scrollRef.current) scrollRef.current.scrollLeft = 0;
    if (tracksViewportRef.current) tracksViewportRef.current.scrollLeft = 0;
    if (tracksViewportRef.current) tracksViewportRef.current.scrollTop = 0;
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
    const viewport = tracksViewportRef.current;
    const target = selectedSegmentRef.current ?? trackGroupRefs.current[activeTrack]?.firstElementChild;
    if (!viewport || !(target instanceof HTMLElement)) return;
    const top = target.getBoundingClientRect().top - viewport.getBoundingClientRect().top + viewport.scrollTop;
    const bottom = top + target.offsetHeight;
    if (top < viewport.scrollTop) viewport.scrollTop = Math.max(0, top - 6);
    else if (bottom > viewport.scrollTop + viewport.clientHeight) viewport.scrollTop = bottom + 6 - viewport.clientHeight;
  }, [activeTrack, trackRevealVersion, selectedId, rows]);

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
    const before = pending?.projectId === project.id ? pending.windowMs : windowMs;
    const scrollLeft = pending?.projectId === project.id ? pending.scrollLeft : scroller.scrollLeft;
    const pointer = Math.max(0, Math.min(visibleAxisWidth, clientX === undefined ? visibleAxisWidth / 2 : clientX - rect.left - scroller.clientLeft - GUTTER));
    const next = zoomRecordingScale(duration, gaps, before, nextWindow, visibleAxisWidth, scrollLeft, pointer);
    if (next.windowMs === before) return;
    pendingViewRef.current = { projectId: project.id, windowMs: next.windowMs, scrollLeft: next.scrollLeft };
    setHoverTime(null);
    setWindowMs(next.windowMs);
  }

  wheelHandlerRef.current = (event: WheelEvent) => {
    const scroller = scrollRef.current;
    if (!scroller) return;
    const target = event.target instanceof Element ? event.target : null;
    const action = timelineWheelAction(event.ctrlKey, event.altKey);
    if (action === 'zoom') {
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
    if (action === 'native') {
      const toolbar = target?.closest<HTMLElement>('.tl-toolbar') ?? null;
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
    // Move and clip overlays together before React paints the new scroll state.
    if (overlaysRef.current) overlaysRef.current.style.left = `${GUTTER}px`;
    if (overlayContentRef.current) overlayContentRef.current.style.transform = `translateX(${-left}px)`;
    setScrollX(left);
    setHoverTime(null);
  }

  function pointerTime(clientX: number, session: ScrubSession): number | null {
    // A second click selects the exact edge, even when its handle is wider than the block.
    if (session.exactStart !== null && !session.hasMoved) return session.exactStart;
    const scroller = scrollRef.current;
    const ruler = rulerRef.current;
    if (!scroller || !ruler) return null;
    const viewport = scroller.getBoundingClientRect();
    const plot = ruler.getBoundingClientRect();
    if (plot.width <= 0) return null;
    const left = viewport.left + scroller.clientLeft + GUTTER;
    const right = viewport.left + scroller.clientLeft + (tracksViewportRef.current?.clientWidth ?? scroller.clientWidth);
    const pointer = Math.max(left, Math.min(clientX - session.grabOffset, right));
    return snapDisplayedTime(scale.toTime((pointer - plot.left) / plot.width * scale.duration), session.snapPoints, scale, plot.width);
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

  function beginScrub(event: ReactPointerEvent<HTMLElement>, fromHandle = false, exactStart: number | null = null) {
    if (event.button !== 0 || !event.isPrimary || scrubRef.current) return;
    const plot = rulerRef.current?.getBoundingClientRect();
    if (!plot || plot.width <= 0) return;
    event.preventDefault();
    event.stopPropagation();
    const now = performance.now();
    const initialTime = exactStart ?? displayedTime;
    const session: ScrubSession = {
      pointerId: event.pointerId, owner: event.currentTarget, projectId: project.id,
      clientX: event.clientX, startX: event.clientX, startY: event.clientY,
      grabOffset: fromHandle ? event.clientX - (plot.left + scale.toDisplay(initialTime) / scale.duration * plot.width) : 0,
      hasMoved: false, exactStart, snapPoints: [...snapPointsRef.current], latestTime: initialTime, lastSeekTime: null,
      lastSeekAt: -Infinity, lastFrameAt: now, frame: 0,
    };
    try { event.currentTarget.setPointerCapture(event.pointerId); } catch { return; }
    scrubRef.current = session;
    event.currentTarget.focus({ preventScroll: true });
    setHoverTime(null);
    setScrubTime(initialTime);
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

  function nearSelectedBoundary(clientX: number, clientY: number) {
    const plot = rulerRef.current?.getBoundingClientRect();
    const scroller = scrollRef.current;
    if (!selectedBoundary || !plot || plot.width <= 0 || !scroller) return false;
    const viewport = scroller.getBoundingClientRect();
    const left = viewport.left + scroller.clientLeft + GUTTER;
    const right = viewport.left + scroller.clientLeft + (tracksViewportRef.current?.clientWidth ?? scroller.clientWidth);
    if (clientX < left || clientX > right || clientY < viewport.top || clientY >= viewport.top + scroller.clientHeight) return false;
    const edgeX = plot.left + scale.toDisplay(currentTime) / scale.duration * plot.width;
    return edgeX >= left && edgeX <= right && Math.abs(clientX - edgeX) <= 7;
  }

  function captureBoundaryPointer(event: ReactPointerEvent<HTMLElement>) {
    if (event.button !== 0 || !event.isPrimary) return;
    dismissedPointerClick.current = false;
    if (!selectedBoundary || scrubRef.current) return;
    const target = event.target instanceof Element ? event.target : null;
    // Existing handles retain their own exact-edge selection and pointer capture.
    if (target?.closest('.tl-boundary-handle, .tl-playhead-handle')) return;
    if (target?.closest('.tl-body') && nearSelectedBoundary(event.clientX, event.clientY)) {
      beginScrub(event, true, currentTime);
      return;
    }
    // Consume the entire click, even after the parent has cleared selectedBoundary.
    // Otherwise a block's click or a surface's pointerdown would immediately seek.
    dismissedPointerClick.current = true;
    event.preventDefault(); event.stopPropagation();
    setHoverTime(null); setHoveringBoundary(false);
    onBoundaryDismiss?.();
  }

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
    const rawTime = scale.toTime(Math.max(0, Math.min((clientX - plot.left) / plot.width, 1)) * scale.duration);
    const time = snapDisplayedTime(rawTime, snapPointsRef.current, scale, plot.width);
    return { time, snapped: snapPointsRef.current.includes(time) };
  }

  function showHoverTime(event: MouseEvent<HTMLElement>) {
    if (scrubRef.current) return;
    const nearBoundary = nearSelectedBoundary(event.clientX, event.clientY);
    setHoveringBoundary(nearBoundary);
    if (nearBoundary) { setHoverTime(null); return; }
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

  function renderGaps(variant: 'ruler' | 'video' | 'track') {
    return gaps.map((gap) => {
      const skipped = project.skipped_videos?.filter(video => video.start_ms < gap.end_ms && video.end_ms > gap.start_ms) ?? [];
      return <span
      key={`${gap.start_ms}-${gap.end_ms}`}
      className={`tl-gap tl-gap-${variant}`}
      style={position(gap.start_ms, gap.end_ms)}
      title={`${skipped.length ? '已跳过：' + skipped.map(video => video.name).join('、') : '无视频'} · ${annotationTime(gap.start_ms)} → ${annotationTime(gap.end_ms)}`}
      aria-hidden="true"
    ><span className="tl-gap-break">//</span>{variant === 'video' && pixelAt(gap.end_ms) - pixelAt(gap.start_ms) > 65 && <span className="tl-gap-label">{skipped.length ? '已跳过' : '无视频'}</span>}</span>;
    });
  }

  function renderSegment(track: Track, segment: Segment, lane = 0) {
    const end = segment.end_ms ?? project.duration_ms;
    const selected = segment.id === selectedId && activeTrack === track;
    const point = isPointSegment(segment);
    const top = lane === 0 ? 11 : 6;
    const narrow = !point && pixelAt(end) - pixelAt(segment.start_ms) < 24;
    const edges: Array<'start' | 'end'> = point || segment.end_ms === null ? ['start'] : ['start', 'end'];
    return (
      <Fragment key={segment.id}>
      <button
        ref={selected ? selectedSegmentRef : undefined}
        type="button"
        className={`tl-segment tl-segment-${customTrack(project,track)?'custom':track}${selected ? ' tl-segment-selected' : ''}${segment.end_ms === null ? ' tl-segment-open' : ''}${point ? ' tl-segment-point' : ''}`}
        style={{ ...position(segment.start_ms, point ? segment.start_ms : end), top }}
        title={`${lane === 0 ? '主轴' : `子轴 ${lane}`} · ${point ? `${segment.label} · ${annotationTime(segment.start_ms)}` : `${segment.label} · ${annotationTime(segment.start_ms)} → ${segment.end_ms === null ? '标注中' : annotationTime(segment.end_ms)}`}`}
        aria-label={point ? `${trackName(project,track)}${lane === 0 ? '主轴' : `子轴 ${lane}`}：${segment.label}，${annotationTime(segment.start_ms)}` : `${trackName(project,track)}${lane === 0 ? '主轴' : `子轴 ${lane}`}：${segment.label}，从 ${annotationTime(segment.start_ms)}${segment.end_ms === null ? '，标注中' : ` 到 ${annotationTime(segment.end_ms)}`}`}
        aria-pressed={selected}
        onClick={(event) => {
          event.stopPropagation();
          // The parent accepts the selection and commits one seek, or keeps its pending edit.
          const seekTime = event.detail === 0 ? segment.start_ms : timeAtClientX(event.clientX)?.time ?? segment.start_ms;
          onSegmentSelect(track, segment, seekTime);
        }}
      >
        <span className="tl-segment-label">{segment.label}</span>
        {!point && segment.end_ms === null && <span className="tl-open-dot" aria-hidden="true" />}
      </button>
      {selected && onBoundarySelect && edges.map(edge => {
        const edgeTime = edge === 'start' ? segment.start_ms : end;
        const edgeName = point ? '标注位置' : edge === 'start' ? '开始边界' : '结束边界';
        const boundarySelected = selectedBoundary === edge || (point && selectedBoundary !== null);
        return <button
          key={edge}
          type="button"
          className={`tl-boundary-handle tl-boundary-${edge}${narrow ? ' tl-boundary-narrow' : ''}${point ? ' tl-boundary-point' : ''}${boundarySelected ? ' tl-boundary-selected' : ''}`}
          style={{left: `${scale.toDisplay(edgeTime) / scale.duration * 100}%`, top: narrow ? top + (edge === 'start' ? -6 : 12) : top - 3}}
          data-boundary={edge}
          aria-label={`${segment.label}：${edgeName}，${annotationTime(edgeTime)}`}
          aria-pressed={boundarySelected}
          title={`${edgeName} ${annotationTime(edgeTime)} · 拖动或使用进度快捷键调整`}
          onPointerDown={event => {
            if (event.button !== 0 || !event.isPrimary || scrubRef.current) return;
            event.preventDefault(); event.stopPropagation();
            if (onBoundarySelect(track, segment, edge)) beginScrub(event, true, edgeTime);
          }}
          {...scrubPointerHandlers}
          onClick={event => {
            event.preventDefault(); event.stopPropagation();
            // Keyboard activation has no pointerdown; pointer clicks were handled above.
            if (event.detail === 0) onBoundarySelect(track, segment, edge);
          }}
        ><span aria-hidden="true" /></button>;
      })}
      </Fragment>
    );
  }

  return (
    <section ref={rootRef} data-boundary-keep className={`tl-root${scrubTime !== null ? ' tl-is-scrubbing' : ''}${selectedBoundary ? ' tl-is-boundary-editing' : ''}${selectedBoundary && hoveringBoundary ? ' tl-is-boundary-hovering' : ''}`} aria-label="视频与标注时间轴"
      onPointerDownCapture={captureBoundaryPointer}
      onPointerMove={scrubPointerHandlers.onPointerMove}
      onPointerUp={scrubPointerHandlers.onPointerUp}
      onPointerCancel={event => { dismissedPointerClick.current = false; scrubPointerHandlers.onPointerCancel(event); }}
      onLostPointerCapture={scrubPointerHandlers.onLostPointerCapture}
      onClickCapture={event => {
        if (dismissedPointerClick.current && event.detail > 0) {
          dismissedPointerClick.current = false;
          event.preventDefault(); event.stopPropagation();
        } else if (selectedBoundary && event.detail === 0 && !(event.target instanceof Element && event.target.closest('.tl-boundary-handle, .tl-playhead-handle'))) {
          event.preventDefault(); event.stopPropagation(); onBoundaryDismiss?.();
        }
      }}>
      <header className="tl-toolbar">
        <div className="tl-heading"><h2>时间轴</h2></div>
        {anchorControls}
        <div ref={minuteReferenceRef} className="tl-minute-reference" role="img"
          aria-label={`时长参考：从右向左累计，完整条带为1分钟，每格1秒，每5秒一条分隔线，30秒加粗并区分底色${referenceClipped ? '，空间不足，左侧部分未显示' : ''}`}
          title="从右向左累计：每格1秒，每5秒一条分隔线，30秒加粗，完整条带60秒。与下方有视频区间的比例一致；空间不足时只裁切左侧，不压缩比例。跨无视频断档的区间不适用。">
          {referenceLabels.map(({seconds, right}) => <span key={seconds} className={`tl-duration-label${seconds % 30 === 0 ? ' tl-duration-label-major' : ''}`} style={{right}} aria-hidden="true">{seconds}s</span>)}
          <div className="tl-minute-strip" style={{ width: minuteWidth }} aria-hidden="true">
            {Array.from({ length: 60 }, (_, second) => <span key={second} />)}
            {Array.from({ length: 13 }, (_, index) => <i key={index} className={index % 6 === 0 ? 'tl-reference-mark tl-reference-mark-major' : 'tl-reference-mark'} style={{right: `${index / 12 * 100}%`}} />)}
          </div>
        </div>
      </header>
      <div className="tl-body" onMouseMove={showHoverTime} onMouseLeave={() => { setHoverTime(null); setHoveringBoundary(false); }} onPointerDownCapture={(event) => {
        // Capture runs before scrub/segment handlers; toolbar buttons live outside this surface.
        const axis = event.target instanceof Element && event.target.closest('.tl-row, .tl-playhead-handle');
        if (selectedRange && event.button === 0 && event.isPrimary && axis) onRangeDismiss?.();
      }}>
      <div className="tl-scroll" ref={scrollRef} onScroll={(event) => syncHorizontalScroll(event.currentTarget.scrollLeft)}>
        <div className="tl-content" style={{ width: contentWidth + scrollbarWidth, '--tl-gutter': `${GUTTER}px` } as CSSProperties}>
          <div className="tl-fixed-tracks" style={{ width: contentWidth }}>
          <div className="tl-row tl-ruler-row">
            <div className="tl-gutter tl-ruler-gutter" title={recordingOrigin === null ? "录制起点未设置，显示相对时间" : "视频真实时间（北京时间）"}>{recordingOrigin === null ? "相对时间" : "视频时间"}</div>
            <div className="tl-ruler tl-seek-surface" ref={rulerRef} onPointerDown={(event) => beginScrub(event)} {...scrubPointerHandlers} role="slider" aria-label="跳转全局视频时间" aria-valuemin={0} aria-valuemax={project.duration_ms} aria-valuenow={Math.round(displayedTime)} aria-valuetext={recordingDateTime(recordingOrigin, displayedTime) ?? formatTime(displayedTime)} tabIndex={0} onKeyDown={(event) => {
              let value: number | undefined;
              if (event.key === 'Home') value = 0;
              if (event.key === 'End') value = project.duration_ms;
              if (value !== undefined) { event.preventDefault(); event.stopPropagation(); onSeek(Math.max(0, Math.min(value, project.duration_ms))); }
            }}>
              {renderGaps('ruler')}
              {ticks.map((tick) => {
                const labelVisible = labeledTicks.has(tick);
                const realTime = recordingDateTime(recordingOrigin, tick);
                return <span key={tick} className={`tl-tick${tick === 0 ? ' tl-tick-first' : ''}`} style={{ left: `${scale.toDisplay(tick) / scale.duration * 100}%` }}>
                  {labelVisible && <span title={realTime ? `${realTime} 北京时间` : undefined}>{(realTime?.slice(11) ?? formatTime(tick)).slice(0, tickStep < 1000 ? 12 : 8)}</span>}
                </span>;
              })}
            </div>
          </div>
          <div className="tl-row tl-video-row">
            <div className="tl-gutter"><span className="tl-row-symbol tl-video-symbol" aria-hidden="true">▸</span><span>视频</span><span className="tl-count">{project.videos.length}</span></div>
            <div className="tl-video-strip tl-seek-surface" onPointerDown={(event) => beginScrub(event)} {...scrubPointerHandlers}>
              {project.videos.map((video, index) => <div key={video.id} className={`tl-video-clip${joinsNext(index - 1) ? ' tl-video-joined-left' : ''}${joinsNext(index) ? ' tl-video-joined-right' : ''}`} style={position(video.start_ms, joinsNext(index) ? project.videos[index + 1].start_ms : video.end_ms)}>
                {video.thumbnail_url && <div className="tl-video-image" style={{ backgroundImage: `url(${JSON.stringify(sessionAssetUrl(project.id, video.thumbnail_url))})` }} />}
                <span className="tl-video-index" style={{ left: Math.max(6, scrollX - pixelAt(video.start_ms) + 6) }}>{String(index + 1).padStart(2, '0')}</span>
                <span className="tl-video-name" style={{ left: Math.max(7, scrollX - pixelAt(video.start_ms) + 7) }}>{video.name}</span>
              </div>)}
              {renderGaps('video')}
              {!project.videos.length && <span className="tl-empty-track">导入视频后显示连续预览轴</span>}
            </div>
          </div>
          </div>
          <div className="tl-tracks-viewport" ref={tracksViewportRef} style={{ width: viewportWidth }} role="region" tabIndex={0} aria-label="项目标注时间轴，可上下滚动查看主轴和子轴" onScroll={(event) => {
            const left = event.currentTarget.scrollLeft;
            if (scrollRef.current && Math.abs(scrollRef.current.scrollLeft - left) > 0.5) scrollRef.current.scrollLeft = left;
            setHoverTime(null);
          }}>
            <div className="tl-tracks-content" style={{ width: contentWidth }}>
              {rows.map(({track, segments}) => {
                const active = activeTrack === track;
                const groupName = trackName(project,track);
                const laneSegments = new Map<number, typeof segments>([[0, []]]);
                for (const item of segments) {
                  if (!laneSegments.has(item.lane)) laneSegments.set(item.lane, []);
                  laneSegments.get(item.lane)!.push(item);
                }
                const lanes = [...laneSegments.entries()].sort(([left], [right]) => left - right);
                const laneCount = lanes.length;
                return <div className={`tl-track-group tl-track-${customTrack(project,track)?'custom':track}${active ? ' tl-track-active' : ''}`} key={track}
                  ref={(element) => { trackGroupRefs.current[track] = element; }}
                  role="group" aria-label={`${groupName}：主轴${laneCount > 1 ? `及 ${laneCount - 1} 条子轴` : ''}`}>
                  {lanes.map(([lane, items], index) => <div className={`tl-row tl-track-row ${lane === 0 ? 'tl-main-row' : 'tl-child-row'}${index === laneCount - 1 ? ' tl-last-lane' : ''}`} key={lane} data-lane={lane}>
                    <button type="button" className="tl-gutter tl-track-select" aria-pressed={active} onClick={() => onTrackSelect(track)} aria-label={`选择${groupName}${lane === 0 ? '主轴' : `子轴 ${lane}`}`} title={`${groupName} · ${lane === 0 ? '主轴' : `子轴 ${lane}`} · ${items.length} 条标注`}>
                      {lane === 0 ? <>
                        <span className="tl-track-title"><span className={`tl-row-dot tl-dot-${customTrack(project,track)?'custom':track}`} /><span className="tl-track-name">{groupName}</span><span className="tl-count">{segments.length}</span></span>
                        <span className="tl-lane-name">主轴{laneCount > 1 && <span className="tl-child-count"> · {laneCount - 1} 条子轴</span>}</span>
                      </> : <span className="tl-child-title"><span className="tl-branch" aria-hidden="true" /><span>子轴 {lane}</span><span className="tl-count">{items.length}</span></span>}
                    </button>
                    <div className="tl-track-surface tl-seek-surface" style={{ height: lane === 0 ? 44 : LANE_HEIGHT }} onPointerDown={(event) => {
                      if ((event.target as HTMLElement).closest('button')) return;
                      beginScrub(event);
                    }} {...scrubPointerHandlers}>
                      {!items.length && <span className="tl-empty-track">{!segments.length ? isEventTrack(project,track) ? '记录事件，或用锚点添加子轴标注' : '点击右侧标签，或用锚点选择区间' : '主轴留空'}</span>}
                      {items.map(({segment}) => renderSegment(track, segment, lane))}
                      {renderGaps('track')}
                    </div>
                  </div>)}
                </div>;
              })}
            </div>
          </div>
        </div>
      </div>
          <div className="tl-overlays" ref={overlaysRef} style={{ left: GUTTER, width: visibleAxisWidth }}>
          <div className="tl-overlays-content" ref={overlayContentRef} style={{ transform: `translateX(${-scrollX}px)` }}>
          {rangeEnd > rangeStart && <div className="tl-range-overlay" aria-label={`锚点选区 ${annotationTime(rangeStart)} 到 ${annotationTime(rangeEnd)}`} style={{
            left: pixelAt(rangeStart),
            width: pixelAt(rangeEnd) - pixelAt(rangeStart),
          }} />}
          {visibleAnchors.map(time => <div key={time} className="tl-anchor" style={{ left: pixelAt(time) }} aria-label={`锚点，${annotationTime(time)}`}>
            <span className={`tl-anchor-marker${pixelAt(time) - scrollX > visibleAxisWidth - 12 ? ' tl-anchor-marker-right' : pixelAt(time) - scrollX < 12 ? ' tl-anchor-marker-left' : ''}`} aria-hidden="true" />
          </div>)}
          {hoverTime && <div className={`tl-hover-line${hoverTime.snapped ? ' tl-hover-line-snapped' : ''}`} style={{ left: pixelAt(hoverTime.time) }} aria-hidden="true" />}
          <div className="tl-playhead" style={{ left: progress * axisWidth }}>
            <div className="tl-playhead-handle" role="slider" tabIndex={0} aria-label="拖动播放光标" aria-valuemin={0} aria-valuemax={project.duration_ms} aria-valuenow={Math.round(displayedTime)} aria-valuetext={recordingDateTime(recordingOrigin, displayedTime) ?? formatTime(displayedTime)} onPointerDown={(event) => beginScrub(event, true, selectedBoundary ? currentTime : null)} {...scrubPointerHandlers} onKeyDown={(event) => {
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
        <div className="tl-hover-main"><span>{hoverTime.snapped ? '已吸附' : '时间轴'}</span><strong>{annotationTime(hoverTime.time)}</strong>{hoverTime.noVideo && <em>无视频</em>}</div>
        <div className="tl-hover-clock">{recordingDateTime(recordingOrigin, hoverTime.time) ?? '录制起点未设置'}{recordingOrigin !== null && <span>北京时间</span>}</div>
      </div>, document.body)}
    </section>
  );
}

