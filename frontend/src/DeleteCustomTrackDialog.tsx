import { useEffect, useRef } from 'react';
import type { CustomTrack } from './domain';

export default function DeleteCustomTrackDialog({track,count,deleting,error,onCancel,onConfirm}: {
  track: CustomTrack;
  count: number;
  deleting: boolean;
  error: string;
  onCancel: () => void;
  onConfirm: () => void;
}) {
  const dialog = useRef<HTMLDialogElement>(null);
  useEffect(() => { dialog.current?.showModal(); }, []);
  return <dialog ref={dialog} className="custom-track-dialog delete-custom-track-dialog"
    aria-labelledby="delete-custom-track-title"
    onCancel={event => { event.preventDefault(); if (!deleting) onCancel(); }}>
    <h2 id="delete-custom-track-title">删除「{track.name}」时间轴？</h2>
    <p>此项目中的 {count} 条标注会随时间轴删除。四条固定轴、原视频和其它项目不受影响。</p>
    <p>如已写回 NAS，对应 JSON 会在下次写回时删除，并保留一份备份。</p>
    {error && <p role="alert" className="import-error">{error}</p>}
    <div className="custom-track-actions">
      <button className="secondary" disabled={deleting} onClick={onCancel}>取消</button>
      <button className="delete-custom-track-confirm" disabled={deleting} onClick={onConfirm}>
        {deleting ? '删除中…' : '删除时间轴'}
      </button>
    </div>
  </dialog>;
}
