import { assignSegmentLanes, projectAnnotationLabels, projectTracks, splitAnnotationLabel } from './domain.ts';
import type { Project, Segment, VideoGap } from './domain.ts';
import { parseExternalTimeline } from './externalTimeline.ts';

export interface PostureImport {
  annotations: Project['annotations'];
  sourceCount: number;
  importedCount: number;
  importedDurationMs: number;
  discardedDurationMs: number;
  replacedCount: number;
}

function newId(): string {
  return globalThis.crypto.randomUUID();
}

/** Use actual video coverage, including skipped-video gaps, rather than bridged recording runs. */
function videoCoverage(project: Project): VideoGap[] {
  const spans = project.videos.map(video => ({
    start_ms: Math.max(0, video.start_ms), end_ms: Math.min(project.duration_ms, video.end_ms),
  })).filter(span => span.end_ms > span.start_ms).sort((a, b) => a.start_ms - b.start_ms);
  const result: VideoGap[] = [];
  for (const span of spans) {
    const previous = result.at(-1);
    if (previous && span.start_ms <= previous.end_ms) previous.end_ms = Math.max(previous.end_ms, span.end_ms);
    else result.push({...span});
  }
  return result;
}

/** Import timestamped predictions into the main posture row, replacing only covered intervals. */
export function importPostureTimeline(text: string, project: Project): PostureImport {
  if (!projectTracks(project).includes('posture')) throw new Error('请先在“选择时间轴”中勾选姿势轴。');
  let root: Record<string, unknown>;
  try { root = JSON.parse(text.replace(/^\uFEFF/, '')); }
  catch { throw new Error('文件不是有效的 JSON，请检查后重新选择。'); }
  if (!root || Array.isArray(root) || root.axis !== 'posture' || !Array.isArray(root.segments)) {
    throw new Error('请选择包含 axis: "posture" 和 segments 的姿势时间轴 JSON。');
  }
  const timebase = root.timebase as Record<string, unknown> | undefined;
  if (!timebase || typeof timebase.recording_start !== 'string') {
    throw new Error('姿势 JSON 必须包含 timebase.recording_start，以便按实际时间写入。');
  }
  if (!root.segments.length) throw new Error('文件没有姿势标注，未修改项目。');
  if (root.segments.length > 50000) throw new Error('单次最多导入 50000 条姿势标注。');
  const allowed = projectAnnotationLabels(project, 'posture');
  let sourceDuration = 0;
  for (const [index, raw] of root.segments.entries()) {
    const row = raw as Partial<Segment> | null;
    const fail = (message: string): never => { throw new Error(`第 ${index + 1} 条：${message}`); };
    if (!row || typeof row.label !== 'string' || !row.label.trim() || row.label.length > 500) fail('姿势标签不能为空，且不能超过 500 字。');
    if (!allowed.includes(splitAnnotationLabel(row!.label!.trim(), allowed).base)) fail('姿势标签不在允许范围内。');
    if (row!.kind === 'point' || row!.end_ms === null) fail('姿势标注必须是已结束的持续区间。');
    if (row!.lane !== undefined && row!.lane !== 0) fail('本入口只导入姿势主轴，请选择不含子轴的预测文件。');
    sourceDuration += typeof row!.start_ms === 'number' && typeof row!.end_ms === 'number' ? row!.end_ms - row!.start_ms : 0;
  }
  // The shared reader converts absolute timestamps to project coordinates; camera offsets are never applied.
  const source = parseExternalTimeline(text, 'posture', project, '姿势 JSON').segments;
  for (let index = 1; index < source.length; index++) {
    if (source[index].start_ms < source[index - 1].end_ms!) {
      throw new Error('JSON 的姿势主轴存在重叠区间，请检查文件后重新导入。');
    }
  }
  const coverage = videoCoverage(project);
  const imported: Segment[] = [];
  let videoIndex = 0;
  for (const segment of source) {
    while (videoIndex < coverage.length && coverage[videoIndex].end_ms <= segment.start_ms) videoIndex++;
    for (let index = videoIndex; index < coverage.length && coverage[index].start_ms < segment.end_ms!; index++) {
      const start_ms = Math.max(segment.start_ms, coverage[index].start_ms);
      const end_ms = Math.min(segment.end_ms!, coverage[index].end_ms);
      if (start_ms < end_ms) imported.push({id: newId(), label: segment.label, start_ms, end_ms, kind: 'interval', lane: 0});
    }
  }
  if (!imported.length) throw new Error('JSON 标注与实际视频没有重叠，未修改项目。');
  const preserved: Segment[] = [];
  let importIndex = 0, replacedCount = 0;
  for (const segment of assignSegmentLanes(project.annotations.posture ?? [], project.duration_ms)) {
    if (segment.lane !== 0) { preserved.push(segment); continue; }
    const end = segment.end_ms ?? project.duration_ms;
    while (importIndex < imported.length && imported[importIndex].end_ms! <= segment.start_ms) importIndex++;
    let cursor = segment.start_ms, pieces = 0, replaced = false;
    const keep = (start_ms: number, end_ms: number) => {
      if (start_ms < end_ms) preserved.push({...segment, id: pieces++ ? newId() : segment.id, start_ms, end_ms});
    };
    for (let index = importIndex; index < imported.length && imported[index].start_ms < end; index++) {
      const item = imported[index];
      keep(cursor, Math.min(end, item.start_ms));
      cursor = Math.max(cursor, Math.min(end, item.end_ms!));
      replaced = true;
      if (cursor >= end) break;
    }
    if (!replaced) preserved.push(segment);
    else { replacedCount++; keep(cursor, end); }
  }
  const importedDurationMs = imported.reduce((total, item) => total + item.end_ms! - item.start_ms, 0);
  return {
    annotations: {...project.annotations, posture: [...preserved, ...imported].sort((a, b) => a.start_ms - b.start_ms)},
    sourceCount: root.segments.length, importedCount: imported.length, importedDurationMs,
    discardedDurationMs: sourceDuration - importedDurationMs, replacedCount,
  };
}
