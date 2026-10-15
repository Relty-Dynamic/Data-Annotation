import {useEffect, useRef} from 'react';

export type ClaimInspection = {
  path: string;
  project_name: string | null;
  owner_name: string | null;
  has_json: boolean;
  requires_takeover: boolean;
  requires_choice: boolean;
  has_platform_draft: boolean;
  claim_token: string;
};

export type ClaimMode = 'keep' | 'continue' | 'restart';

export default function ClaimProjectDialog({claim, mode, step, busy, error, storageName='NAS', onMode, onNext, onBack, onCancel}: {
  claim: ClaimInspection;
  storageName?: string;
  mode: ClaimMode;
  step: 1 | 2;
  busy: boolean;
  error: string;
  onMode: (mode: ClaimMode) => void;
  onNext: () => void;
  onBack: () => void;
  onCancel: () => void;
}) {
  const dialog = useRef<HTMLElement>(null);
  useEffect(() => {
    dialog.current?.focus();
    const key = (event: KeyboardEvent) => {
      if (event.key === 'Escape' && !busy) {event.preventDefault(); onCancel(); return;}
      if (event.key !== 'Tab' || !dialog.current) return;
      const controls = Array.from(dialog.current.querySelectorAll<HTMLElement>('button:not(:disabled), input:not(:disabled)'));
      const first = controls[0], last = controls.at(-1);
      if (!first || !last) return;
      if (event.shiftKey && (document.activeElement === first || document.activeElement === dialog.current)) {event.preventDefault(); last.focus();}
      else if (!event.shiftKey && (document.activeElement === last || document.activeElement === dialog.current)) {event.preventDefault(); first.focus();}
    };
    window.addEventListener('keydown', key, true);
    return () => window.removeEventListener('keydown', key, true);
  }, [step, busy]);

  return <div className="modal-backdrop claim-backdrop" onClick={() => {if (!busy) onCancel();}}>
    <section ref={dialog} tabIndex={-1} className="submit-project-dialog claim-project-dialog" role="dialog" aria-modal="true" aria-labelledby="claim-project-title" onClick={event => event.stopPropagation()}>
      {step === 1 ? <>
        <h2 id="claim-project-title">确认采集目录的标注来源</h2>
        <p className="claim-project-path" title={claim.path}>{claim.path}</p>
        {claim.project_name && <p>此目录已有项目「{claim.project_name}」。{claim.owner_name && <>当前认领人：{claim.owner_name}。</>}</p>}
        {claim.has_json ? <fieldset className="claim-options"><legend>本次领取后如何开始？</legend>
          <label><input type="radio" name="claim-mode" checked={mode === 'continue'} disabled={busy} onChange={() => onMode('continue')}/><span><strong>沿用 {storageName} 的旧 JSON</strong><small>载入现有时间轴，继续修改标注。</small></span></label>
          <label><input type="radio" name="claim-mode" checked={mode === 'restart'} disabled={busy} onChange={() => onMode('restart')}/><span><strong>备份旧 JSON，从空白开始</strong><small>保留旧文件版本和草稿恢复副本；首次写回时才提交新版本。</small></span></label>
        </fieldset> : <p>{storageName} 中没有时间轴 JSON；接管后保留平台已保存的项目草稿。</p>}
        {claim.has_platform_draft && mode !== 'keep' && <p>平台中另有旧草稿，切换标注来源前也会保存恢复副本。</p>}
        {claim.requires_takeover && <p>此项目已被其他账号认领，继续操作还需要再次确认接管。</p>}
        {error && <p className="import-error" role="alert">{error}</p>}
        <div className="submit-project-actions"><button className="secondary" disabled={busy} onClick={onCancel}>取消</button><button className="primary" disabled={busy} onClick={onNext}>{claim.requires_takeover ? '继续确认接管' : '确认并打开'}</button></div>
      </> : <>
        <h2 id="claim-project-title">再次确认接管项目</h2>
        <p>你将接管「{claim.project_name || claim.path}」。原认领人{claim.owner_name ? `「${claim.owner_name}」` : ''}将失去此项目的访问权；项目编号、原视频和现有 {storageName} 文件保持不变。</p>
        <p>{mode === 'restart' ? '旧 JSON 会先备份，打开后从空白标注。' : claim.has_json ? `打开后沿用 ${storageName} 中的时间轴 JSON。` : '打开后保留平台当前草稿。'}</p>
        {error && <p className="import-error" role="alert">{error}</p>}
        <div className="submit-project-actions"><button className="secondary" disabled={busy} onClick={onBack}>返回</button><button className="primary" disabled={busy} onClick={onNext}>{busy ? '正在接管…' : '第二次确认：接管并打开'}</button></div>
      </>}
    </section>
  </div>;
}
