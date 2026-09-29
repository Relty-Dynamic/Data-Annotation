import type { Project, Segment, Track } from './domain';

export interface ExternalTimeline {
  track: Track;
  name: string;
  segments: Segment[];
  alignment: string;
  warnings: string[];
}

function object(value: unknown): Record<string, unknown> | null {
  return value !== null && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : null;
}

function origin(value: unknown): number | null {
  if (typeof value !== 'string' || !/^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}/.test(value)) return null;
  const text = value.trim().replace(' ', 'T');
  const parsed = Date.parse(/(?:Z|[+-]\d{2}:?\d{2})$/i.test(text) ? text : `${text}+08:00`);
  return Number.isFinite(parsed) ? parsed : null;
}

/** Read-only import. Preserve labels and overlaps; never normalize into GT. */
export function parseExternalTimeline(text: string, track: Track, project: Pick<Project, 'duration_ms' | 'recording_start'>, name: string): ExternalTimeline {
  let data: unknown;
  try { data = JSON.parse(text.replace(/^\uFEFF/, '')); } catch { throw new Error('文件不是有效的 JSON，请检查后重新选择。'); }
  const root = object(data);
  const records = Array.isArray(data) ? data : root?.segments;
  if (!Array.isArray(records)) throw new Error('需要 timeline.json（包含 segments），或包含 label、start_ms、end_ms 的数组。');
  if (records.length > 50000) throw new Error('单次最多查看 50000 条时间段，请先拆分文件。');
  const timebase = object(root?.timebase);
  if (timebase?.unit !== undefined && timebase.unit !== 'ms') throw new Error('仅支持毫秒时间轴（timebase.unit 为 ms）。');
  const sourceStart = origin(timebase?.recording_start);
  const projectStart = origin(project.recording_start);
  if (timebase?.recording_start !== undefined && sourceStart === null) throw new Error('文件中的录制起点无效，请使用完整日期时间。');
  if (sourceStart !== null && projectStart === null) throw new Error('当前项目没有有效录制起点，无法按文件的录制时间对齐。');
  const offset = sourceStart !== null && projectStart !== null ? sourceStart - projectStart : 0;
  const warnings: string[] = [];
  if (root?.axis && root.axis !== track) warnings.push('文件声明的轴类型与所选类型不同，本次按所选类型查看。');
  let clipped = 0, outside = 0;
  const segments: Segment[] = [];
  for (const [index, raw] of records.entries()) {
    const row = object(raw);
    const fail = (message: string): never => { throw new Error(`第 ${index + 1} 条：${message}`); };
    if (!row || typeof row.label !== 'string' || !row.label.trim()) fail('缺少有效 label。');
    const item = row!;
    if (typeof item.start_ms !== 'number' || !Number.isSafeInteger(item.start_ms) || item.start_ms < 0) fail('start_ms 必须为非负整数毫秒。');
    if (item.kind !== undefined && item.kind !== 'point' && item.kind !== 'interval') fail('kind 仅支持 point 或 interval。');
    const start = item.start_ms as number;
    const isPoint = item.kind === 'point' || item.end_ms === start;
    const end = item.end_ms === undefined && item.kind === 'point' ? start : item.end_ms;
    if (end !== null && (typeof end !== 'number' || !Number.isSafeInteger(end) || end < start)) fail('end_ms 必须不早于 start_ms，未结束区间可用 null。');
    if (isPoint && end !== start) fail('瞬时事件的起止时间必须相同。');
    if (isPoint && track !== 'habit') fail('瞬时事件请选择习惯轴。');
    if (!isPoint && end === start) fail('区间时长必须大于零。');
    const alignedStart = start + offset;
    const alignedEnd = end === null ? project.duration_ms : (end as number) + offset;
    if (alignedStart > project.duration_ms || alignedEnd < 0 || (!isPoint && (alignedStart === project.duration_ms || alignedEnd === 0))) { outside++; continue; }
    if (alignedStart < 0 || alignedEnd > project.duration_ms) clipped++;
    segments.push({id: `external-${index}`, label: (item.label as string).trim(), start_ms: Math.max(0, alignedStart), end_ms: end === null ? null : Math.min(project.duration_ms, alignedEnd), kind: isPoint ? 'point' : 'interval'});
  }
  if (records.length && !segments.length) throw new Error('外部时间轴与当前项目的时间范围没有交集，请核对项目和录制起点。');
  if (outside) warnings.push(`${outside} 条位于项目时间范围外，未显示。`);
  if (clipped) warnings.push(`${clipped} 条跨出项目范围，仅显示范围内部分。`);
  if (!records.length) warnings.push('文件为空时间轴。');
  return {track, name, segments: segments.sort((a, b) => a.start_ms - b.start_ms), warnings,
    alignment: sourceStart !== null ? `按录制时间对齐 · 偏移 ${offset / 1000} 秒` : '按项目起点对齐 · 时间单位：毫秒'};
}
