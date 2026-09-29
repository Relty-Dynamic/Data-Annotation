import { useEffect, useId, useRef, useState } from 'react';
import type { KeyboardEvent } from 'react';
import { createPortal } from 'react-dom';
import { AlertCircle, ArrowRight, Check, CheckCircle2, Clock3, Film, LoaderCircle, Pause, RotateCw, X } from 'lucide-react';
import './prepare.css';
import { canSkipFailed, measuredPercent, preparationPresentation, preparationItemLabel, preparationRetryAction } from './preparationPresentation';
import type { PreparationOperation, PreparationStage } from './preparationPresentation';

export type PreparationStatus = {
  project_id: string;
  requested?: boolean;
  checking?: boolean;
  stage?: PreparationStage;
  operation?: PreparationOperation;
  detail?: string;
  read_bytes_per_second?: number | null;
  bytes_ready?: number;
  bytes_total?: number;
  state: 'idle' | 'running' | 'ready' | 'partial' | 'paused';
  total: number;
  ready: number;
  failed: number;
  running: number;
  queued: number;
  progress: number;
  items: Array<{
    video_id: string;
    name: string;
    state: string;
    operation?: PreparationOperation;
    progress: number | null;
    detail?: string | null;
  }>;
};

export type PreparePanelProps = {
  projectName: string;
  status: PreparationStatus | null;
  error: string;
  onRetry: () => void;
  onEnter: () => void;
  onClose: () => void;
  busy: boolean;
  onSkipFailed?: (ids: string[]) => Promise<void>;
  onRestoreSkipped?: () => Promise<void>;
  skippedVideos?: Array<{id: string; name: string}>;
};

type ItemState = 'ready' | 'running' | 'checking' | 'failed' | 'queued' | 'paused' | 'idle' | 'unknown';

function itemState(state: string): ItemState {
  if (state === 'error' || state === 'failed') return 'failed';
  if (state === 'ready' || state === 'running' || state === 'checking' || state === 'queued' || state === 'paused' || state === 'idle') return state;
  return 'unknown';
}

function count(value: number): number {
  return Number.isFinite(value) ? Math.max(0, Math.floor(value)) : 0;
}

function StateIcon({ state }: { state: ItemState }) {
  if (state === 'ready') return <Check size={15} aria-hidden="true" />;
  if (state === 'running' || state === 'checking') return <LoaderCircle size={15} className="prep-spin" aria-hidden="true" />;
  if (state === 'failed') return <AlertCircle size={15} aria-hidden="true" />;
  if (state === 'paused') return <Pause size={14} aria-hidden="true" />;
  return <Clock3 size={14} aria-hidden="true" />;
}

const ITEM_LABELS: Record<Exclude<ItemState, 'unknown'>, string> = {
  ready: '已就绪', running: '处理中', checking: '检查缓存中', failed: '准备失败', queued: '等待中', paused: '已暂停', idle: '尚未准备',
};

export default function PreparePanel({ projectName, status, error, onRetry, onEnter, onClose, busy, onSkipFailed, onRestoreSkipped, skippedVideos = [] }: PreparePanelProps) {
  const [confirmation, setConfirmation] = useState<'skip' | 'restore' | null>(null);
  const [confirmed, setConfirmed] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [actionError, setActionError] = useState('');
  busy = busy || submitting;
  const canSkip = canSkipFailed(status, error) && !!onSkipFailed;
  const failedItems = status?.items.filter(item => item.state === 'error') ?? [];
  const confirmationKey = JSON.stringify([status?.state, failedItems.map(item => item.video_id), skippedVideos.map(item => item.id)]);
  useEffect(() => {setConfirmation(null);setConfirmed(false);setActionError('');}, [confirmationKey]);
  async function confirmSelection() {
    if (!confirmed || busy) return;
    setSubmitting(true);setActionError('');
    try {
      if (confirmation === 'skip' && canSkip) await onSkipFailed?.(failedItems.map(item => item.video_id));
      else if (confirmation === 'restore') await onRestoreSkipped?.();
      setConfirmation(null);setConfirmed(false);
    } catch (error) {setActionError((error as Error).message);}
    finally {setSubmitting(false);}
  }
  const titleId = useId();
  const descriptionId = useId();
  const dialogRef = useRef<HTMLDivElement>(null);
  const closeRef = useRef<HTMLButtonElement>(null);
  const isReady = status?.state === 'ready' && !error;
  const checking = Boolean(status?.checking);
  const paused = status?.state === 'paused' || status?.state === 'idle';
  const presentation = preparationPresentation(status, error);
  const progress = presentation.progress;
  const items = status?.items ?? [];
  const current = items.find(item => item.state === 'running');
  const next = items.find(item => item.state === 'queued');
  const failed = status ? count(status.failed) : 0;
  const retry = preparationRetryAction(status, error);
  const phase = presentation.phase;
  const phaseLabel = presentation.activity;
  const stageLabels = {prepare: '准备播放素材', browser: '载入预览图片', ready: '开始标注'};
  const title = presentation.title;
  const description = presentation.description;

  useEffect(() => {
    const previous = document.activeElement;
    closeRef.current?.focus({ preventScroll: true });
    return () => {
      if (previous instanceof HTMLElement && previous.isConnected) previous.focus({ preventScroll: true });
    };
  }, []);

  function handleKeyDown(event: KeyboardEvent<HTMLDivElement>) {
    // A preparation dialog owns its keys; playback shortcuts remain inactive underneath it.
    event.stopPropagation();
    if (event.key === 'Escape') {
      event.preventDefault();
      if (submitting) return;
      if (confirmation) {setConfirmation(null);setConfirmed(false);return;}
      onClose();
      return;
    }
    if (event.key !== 'Tab') return;
    const focusable = dialogRef.current?.querySelectorAll<HTMLElement>('button:not(:disabled), [href], input:not(:disabled), select:not(:disabled), textarea:not(:disabled), [tabindex="0"]');
    if (!focusable?.length) {
      event.preventDefault();
      dialogRef.current?.focus({ preventScroll: true });
      return;
    }
    const first = focusable[0];
    const last = focusable[focusable.length - 1];
    if (event.shiftKey && (document.activeElement === first || document.activeElement === dialogRef.current)) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
  }

  return createPortal(
    <div className="prep-backdrop">
      <div ref={dialogRef} className="prep-panel" role="dialog" aria-modal="true" aria-labelledby={titleId} aria-describedby={descriptionId} tabIndex={-1} onKeyDown={handleKeyDown}>
        <header className="prep-header">
          <div className={'prep-heading-icon' + (isReady ? ' is-ready' : '')}>
            {isReady ? <CheckCircle2 size={25} aria-hidden="true" /> : <Film size={25} aria-hidden="true" />}
          </div>
          <div className="prep-heading-copy">
            <span className="prep-eyebrow">标注前 · 素材准备</span>
            <h2 id={titleId}>{title}</h2>
            <p id={descriptionId}>{description}</p>
          </div>
          <button ref={closeRef} className="prep-close" type="button" disabled={submitting} aria-label="返回项目选择" title="返回项目选择；准备会继续，完成后再进入标注" onClick={onClose}><X size={20} aria-hidden="true" /></button>
        </header>

        <main className="prep-main">
          <section className="prep-overview" aria-label="整体准备进度">
            <div className="prep-project-row">
              <strong className="prep-project-name" title={projectName}>{projectName}</strong>
              <span className={'prep-phase' + (isReady ? ' is-ready' : '')}>{status?.state === 'partial' ? '部分失败' : phaseLabel}</span>
            </div>
            <div className="prep-progress-heading">
              <span>{status ? <>{presentation.progressLabel}<span> · 已就绪 {count(status.ready)} / {count(status.total)} 段</span></> : '正在获取准备进度'}</span>
              <strong className="prep-percent">{checking || progress === null ? '—' : `${Math.floor(progress)}%`}</strong>
            </div>
            <progress className="prep-total-progress" max={100} value={checking ? undefined : progress ?? undefined} aria-label={presentation.progressLabel} />
            <ol className="prep-stages" aria-label="素材准备步骤">{(['prepare', 'browser', 'ready'] as const).map((step, index) => <li key={step} className={isReady || ['prepare','browser','ready'].indexOf(phase) > index ? 'done' : phase === step ? 'active' : ''}><span>{index + 1}</span>{stageLabels[step]}</li>)}</ol>
            <div className="prep-counts">
              <div className="is-ready"><i /><span>已就绪</span><b>{status ? count(status.ready) : '—'}</b></div>
              <div className="is-running"><i /><span>处理中</span><b>{status ? count(status.running) : '—'}</b></div>
              <div><i /><span>等待中</span><b>{status ? count(status.queued) : '—'}</b></div>
              <div className={failed > 0 ? 'is-failed' : ''}><i /><span>失败</span><b>{status ? failed : '—'}</b></div>
            </div>
            {current&&<div className="prep-current-file"><span>当前片段</span><strong title={current.name}>{current.name}</strong></div>}
            <div className="prep-current" role="status" aria-live="polite" aria-atomic="true">
              {isReady ? <><CheckCircle2 size={14} aria-hidden="true" /><span>全部播放素材已准备完成，标注时直接读取本机缓存</span></>
                : status?.detail ? <>{paused ? <Pause size={14} aria-hidden="true"/> : status.state==='partial' ? <AlertCircle size={14} aria-hidden="true"/> : <LoaderCircle className="prep-spin" size={14} aria-hidden="true"/>}<span>{status.detail}</span></>
                  : current ? <><LoaderCircle className="prep-spin" size={14} aria-hidden="true" /><span>正在处理：<strong title={current.name}>{current.name}</strong></span></>
                    : paused ? <><Pause size={14} aria-hidden="true" /><span>准备已暂停，请继续准备后进入标注</span></>
                      : status?.state === 'partial' ? <><AlertCircle size={14} aria-hidden="true" /><span>部分素材准备失败，查看下方详情后重试</span></>
                        : next ? <><Clock3 size={14} aria-hidden="true" /><span>等待处理：<strong title={next.name}>{next.name}</strong></span></>
                          : <><Clock3 size={14} aria-hidden="true" /><span>正在检查全部播放素材</span></>}
            </div>
            {error && <p className="prep-error" role="alert"><AlertCircle size={15} aria-hidden="true" /><span>{error}</span></p>}
          </section>

          <section className="prep-items-section" aria-label="逐文件准备结果">
            <div className="prep-list-heading"><h3>视频列表</h3><span>{status ? `${count(status.total)} 段` : '读取中'}</span></div>
            <div className="prep-items" tabIndex={0} role="region" aria-label="视频准备详情，可上下滚动">
              {items.length > 0 ? <ul>{items.map((item, index) => {
                const state = itemState(item.state);
                const label = preparationItemLabel(item.state,item.operation) ?? (state === 'unknown' ? item.state || '等待状态' : ITEM_LABELS[state]);
                const itemProgress = measuredPercent(item.progress);
                return <li className={`prep-item is-${state}`} key={item.video_id}>
                  <span className="prep-item-index">{String(index + 1).padStart(2, '0')}</span>
                  <span className="prep-item-icon"><StateIcon state={state} /></span>
                  <div className="prep-item-body">
                    <div className="prep-item-top"><strong title={item.name}>{item.name}</strong><span className="prep-item-state">{label}{state === 'running' && itemProgress !== null && <b> {Math.floor(itemProgress)}%</b>}</span></div>
                    {state === 'running' && <progress className="prep-item-progress" max={100} value={itemProgress ?? undefined} aria-label={`${item.name} 准备进度`} />}
                    {item.detail && <p className="prep-item-detail">{item.detail}</p>}
                  </div>
                </li>;
              })}</ul> : <div className="prep-empty"><Film size={24} aria-hidden="true" /><p>{status ? '暂无视频准备详情' : '正在读取视频列表…'}</p></div>}
            </div>
          </section>
        </main>

        <footer className="prep-footer">
          {skippedVideos.length > 0 && <p className="prep-skipped-summary" title={skippedVideos.map(video => video.name).join('\n')}>本项目已跳过 {skippedVideos.length} 段，原时间位置保留为空档。</p>}
          {confirmation && <section className="prep-selection-confirm" aria-label={confirmation === 'skip' ? '确认跳过失败片段' : '确认恢复片段'}>
            <strong>{confirmation === 'skip' ? `跳过以下 ${failedItems.length} 段？` : `恢复已跳过的 ${skippedVideos.length} 段？`}</strong>
            <ul>{(confirmation === 'skip' ? failedItems.map(item => ({id: item.video_id, name: item.name})) : skippedVideos).map(item => <li key={item.id}>{item.name}</li>)}</ul>
            <label><input type="checkbox" checked={confirmed} disabled={busy} onChange={event => setConfirmed(event.target.checked)} /><span>{confirmation === 'skip' ? '确认不标注这些片段，保留时间空档；不删除原视频、NAS 缓存或已有标注。' : '恢复这些片段并重新准备；已有标注保持原位置。'}</span></label>
            <div className="prep-enter-actions"><button type="button" className="prep-button prep-secondary" disabled={busy} onClick={() => {setConfirmation(null);setConfirmed(false);}}>取消</button><button type="button" className="prep-button prep-primary" disabled={!confirmed || busy || (confirmation === 'skip' && !canSkip)} onClick={() => void confirmSelection()}>{submitting ? <LoaderCircle className="prep-spin" size={15}/> : <ArrowRight size={15}/>} {confirmation === 'skip' ? '确认跳过并进入标注' : '确认恢复并准备'}</button></div>
          </section>}
          {actionError && <p className="prep-error" role="alert">{actionError}</p>}
          {!confirmation && <><p>使用 270p 精简预览提高定位速度；原视频的画质、标注时间和导出结果保持不变。再次打开时复用有效的本机播放缓存。</p>
          <div className="prep-actions">
            {retry.visible && <button type="button" className="prep-button prep-retry" disabled={busy} onClick={onRetry}>{busy ? <LoaderCircle size={15} className="prep-spin" aria-hidden="true" /> : <RotateCw size={15} aria-hidden="true" />}{retry.label}</button>}
            {canSkip && !confirmation && <button type="button" className="prep-button prep-secondary" disabled={busy} onClick={() => {setConfirmation('skip');setConfirmed(false);setActionError('');}}><ArrowRight size={15}/>跳过失败片段并标注</button>}
            {!!skippedVideos.length && onRestoreSkipped && status?.state !== 'running' && !confirmation && <button type="button" className="prep-button prep-secondary" disabled={busy} onClick={() => {setConfirmation('restore');setConfirmed(false);setActionError('');}}><RotateCw size={15}/>恢复已跳过片段</button>}
            <div className="prep-enter-actions">
              <button type="button" className="prep-button prep-secondary" disabled={submitting} onClick={onClose}>返回项目选择</button>
              <button type="button" className="prep-button prep-primary" disabled={!isReady || busy || !!confirmation} onClick={onEnter}>开始标注<ArrowRight size={16} aria-hidden="true" /></button>
            </div>
          </div>
          </>}
        </footer>
      </div>
    </div>,
    document.body,
  );
}

