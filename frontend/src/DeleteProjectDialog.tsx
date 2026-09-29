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
      <h2 id="delete-project-title">清理本机预览？</h2>
      <div className="delete-project-summary"><strong>{project.name}</strong><span>{project.videos.length} 段视频 · {count} 条标注</span></div>
      <p>将停止本项目的准备任务，清理本机的播放预览、倍速预览、封面和悬停图片。</p>
      <p className="delete-project-preserved">项目记录、全部标注草稿、原视频、NAS 缓存包、旧上传副本和已保存结果均保留。</p>
      {error && <p className="delete-project-error" role="alert">{error}</p>}
      <div className="delete-project-actions"><button ref={cancel} className="secondary" disabled={deleting} onClick={onCancel}>取消</button>
        <button className="delete-project-confirm" disabled={deleting} onClick={onConfirm}>{deleting?'正在清理本机预览…':'清理本机预览'}</button></div>
    </div>
  </div>;
}
