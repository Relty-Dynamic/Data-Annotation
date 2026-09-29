import { useEffect, useMemo, useRef, useState } from 'react';
import { TRACK_LABELS, TRACKS, type Project, type Track } from './domain';
import { parseExternalTimeline, type ExternalTimeline } from './externalTimeline';

export default function TimelineImportDialog({project, initialTrack, onCancel, onOpen}: {
  project: Project; initialTrack: Track; onCancel: () => void; onOpen: (value: ExternalTimeline) => void;
}) {
  const dialog = useRef<HTMLDialogElement>(null);
  const [track, setTrack] = useState(initialTrack);
  const [source, setSource] = useState<{text: string; name: string} | null>(null);
  const [error, setError] = useState('');
  const [reading, setReading] = useState(false);
  const generation = useRef(0);
  const result = useMemo(() => {
    if (!source) return null;
    try { return {value: parseExternalTimeline(source.text, track, project, source.name), error: ''}; }
    catch (reason) { return {value: null, error: (reason as Error).message}; }
  }, [source, track, project]);
  useEffect(() => { dialog.current?.showModal(); return () => { generation.current++; }; }, []);
  return <dialog ref={dialog} className="timeline-import-dialog" aria-labelledby="timeline-import-title" onCancel={event => {event.preventDefault();onCancel();}}>
    <form onSubmit={event => {event.preventDefault();if (result?.value && !reading) onOpen(result.value);}}>
      <h2 id="timeline-import-title">打开外部时间轴</h2>
      <p>与「{project.name}」的视频及已有标注对照查看。</p>
      <label>轴的类型<select aria-label="轴的类型" value={track} onChange={event => setTrack(event.target.value as Track)}>{TRACKS.map(value => <option key={value} value={value}>{TRACK_LABELS[value]}</option>)}</select></label>
      <label>时间轴 JSON 文件<input aria-label="时间轴 JSON 文件" type="file" accept=".json,application/json" onChange={async event => {
        const file = event.target.files?.[0], request = ++generation.current;
        setSource(null);setError('');setReading(false);
        if (!file) return;
        if (file.size > 20 * 1024 * 1024) {setError('文件不能超过 20 MB。');return;}
        setReading(true);
        try {const text = await file.text();if (generation.current === request) setSource({text,name:file.name});}
        catch {if (generation.current === request) setError('文件读取失败，请重新选择。');}
        finally {if (generation.current === request) setReading(false);}
      }}/></label>
      <p className="muted-text">支持看台导出的 timeline.json，或 label / start_ms / end_ms 数组。有录制起点时按录制时间对齐，否则按当前项目起点对齐。</p>
      {reading && <p role="status">正在读取文件…</p>}
      {(error || result?.error) && <p role="alert" className="timeline-import-error">{error || result?.error}</p>}
      {result?.value && <div className="timeline-import-summary" role="status"><strong>{TRACK_LABELS[track]} · {result.value.segments.length} 条</strong><p>{result.value.alignment}</p>{result.value.warnings.map(warning => <p key={warning}>{warning}</p>)}</div>}
      <div className="timeline-import-actions"><button type="button" className="secondary" onClick={onCancel}>取消</button><button className="primary" type="submit" disabled={!result?.value || reading}>打开对照窗口</button></div>
    </form>
  </dialog>;
}
