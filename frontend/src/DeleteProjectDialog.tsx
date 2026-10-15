import { useEffect, useRef } from 'react';
import { AlertTriangle } from 'lucide-react';
import type { Project } from './domain';

export default function DeleteProjectDialog({ project, deleting, error, onCancel, onConfirm }: {
  project: Project; deleting: boolean; error: string; onCancel: () => void; onConfirm: () => void;
}) {
  const dialog = useRef<HTMLDivElement>(null);
  const cancel = useRef<HTMLButtonElement>(null);
  useEffect(() => { cancel.current?.focus(); }, []);
  const count = Object.values(project.annotations).reduce((total, items) => total + items.length, 0);
  return <div className="modal-backdrop" onClick={event => { if (event.target === event.currentTarget && !deleting) onCancel(); }}>
    <div ref={dialog} className="import-modal delete-project-dialog" role="dialog" aria-modal="true" aria-labelledby="delete-project-title"
      onKeyDown={event => {
        if (event.key === 'Escape' && !deleting) { event.preventDefault(); onCancel(); }
        if (event.key === 'Tab') {
          const buttons = dialog.current?.querySelectorAll<HTMLButtonElement>('button:not(:disabled)');
          if (!buttons?.length) { event.preventDefault(); return; }
          const first = buttons[0], last = buttons[buttons.length - 1];
          if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
          else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
        }
      }}>
      <div className="delete-project-icon"><AlertTriangle size={25}/></div>
      <h2 id="delete-project-title">移除本机项目？</h2>
      <div className="delete-project-summary"><strong>{project.name}</strong><span>{project.videos.length} 段视频 · {count} 条标注</span></div>
      <p>清理本机播放预览、倍速预览、封面和悬停图片，并将项目从当前浏览器的项目列表移除；刷新后也不再显示。</p>
      <p className="delete-project-preserved">平台标注草稿、原视频、{project.storage_mode==='s3-mock'?'模拟存储中的视频':'NAS 缓存'}和已输出 JSON 保留。需要继续时，可通过“新建项目”重新选择原素材。</p>
      {error && <p className="delete-project-error" role="alert">{error}</p>}
      <div className="delete-project-actions"><button ref={cancel} className="secondary" disabled={deleting} onClick={onCancel}>取消</button>
        <button className="delete-project-confirm" disabled={deleting} onClick={onConfirm}>{deleting?'正在清理并移除…':'清理预览并移除'}</button></div>
    </div>
  </div>;
}
