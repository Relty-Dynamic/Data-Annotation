export type PreparationOperation = 'check' | 'generate' | 'reuse' | 'assets' | 'remote' | 'nas' | 'download' | 'reconnect';
export type PreparationStage = 'cache' | 'compact' | 'local' | 'browser' | 'ready';
export type PreparationPhase = 'prepare' | 'browser' | 'ready';

export function measuredPercent(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) ? Math.max(0, Math.min(100, value)) : null;
}

/** Describe actual work supplied by the server, never infer encoding from elapsed time. */
export function preparationPresentation(status: {
  state: string; stage?: PreparationStage; operation?: PreparationOperation;
  progress: number | null; checking?: boolean; failed?: number;
} | null, error = '') {
  const ready = status?.state === 'ready' && !error;
  const paused = status?.state === 'paused' || status?.state === 'idle';
  const phase: PreparationPhase = ready ? 'ready' : status?.stage === 'browser' ? 'browser' : 'prepare';
  let title = '正在准备播放缓存';
  let description = '首次导入需要生成播放素材；已有有效缓存会直接复用。全部准备完成后再进入标注。';
  let activity = '准备播放缓存';
  if (status?.operation === 'check') {
    title = '正在检查本机缓存';
    activity = '检查已有缓存';
    description = '正在确认已有缓存是否可直接使用。只有缺失或失效的素材需要重新生成。';
  } else if (status?.operation === 'generate') {
    title = '正在首次生成播放缓存';
    activity = '首次生成播放缓存';
    description = '当前片段尚无可用的本机播放缓存，正在生成精简预览。完成后再次打开会复用有效缓存。';
  } else if (status?.operation === 'reuse') {
    title = '正在复用已有缓存';
    activity = '复用已有缓存';
    description = '当前片段的播放素材已经生成，正在读取已有缓存。完成全部预览准备后即可标注。';
  } else if (status?.operation === 'assets') {
    title = '正在准备快进和缩略图';
    activity = '准备快进和缩略图';
    description = '正在准备高倍速预览、封面和悬停图片。已经完成的播放缓存会保留。';
  } else if (status?.operation === 'nas') {
    title = '检查 NAS 缓存';
    activity = '检查 NAS 缓存';
    description = '正在检查原视频目录中已保存的缓存包。';
  } else if (status?.operation === 'remote') {
    title = '服务器处理中';
    activity = '服务器处理中';
    description = '服务器正在准备播放素材，完成后保存到 NAS 并下载到本机。';
  } else if (status?.operation === 'download') {
    title = '下载到本机';
    activity = '下载到本机';
    description = '正在下载并校验播放素材。全部准备完成后即可开始标注。';
  } else if (status?.operation === 'reconnect') {
    title = '重新连接服务器';
    activity = '重新连接服务器';
    description = '连接暂时中断，正在自动重试。已完成的缓存和下载进度会保留。';
  }
  if (phase === 'browser') {
    title = '正在载入预览图片';
    activity = '载入预览图片';
    description = '视频缓存已经准备好，正在将封面和悬停图片载入当前页面。全部载入后可开始标注。';
  }
  if (ready) {
    title = '标注素材已就绪';
    activity = '本机播放就绪';
    description = '视频、封面和悬停预览均已准备好，可以开始标注。';
  } else if (error || status?.state === 'partial') {
    title = status?.state === 'partial' && !error ? '部分素材准备失败' : '素材准备需要重试';
    if (status?.state === 'partial') description = '部分素材准备失败，已完成的缓存保留。';
  } else if (paused) {
    title = '等待继续准备素材';
  } else if (status?.state === 'running' && Number.isFinite(status.failed) && status.failed! > 0) {
    description = `有 ${Math.floor(status.failed!)} 段准备失败，其余片段继续准备。`;
  }
  return {
    phase, title, description, activity,
    progressLabel: phase === 'browser' ? '当前阶段：预览图片载入' : phase === 'ready' ? '全部播放素材已就绪' : '当前阶段：全部视频缓存准备',
    progress: status && !status.checking ? measuredPercent(status.progress) : null,
  };
}

export function preparationRetryAction(status: {state: string; failed: number} | null, error = '') {
  const running = status?.state === 'running';
  const paused = status?.state === 'paused' || status?.state === 'idle';
  const failed = !!status && Number.isFinite(status.failed) && status.failed > 0;
  return {
    visible: Boolean(error) || (!running && (status?.state === 'partial' || failed || paused)),
    label: running ? '重新获取进度' : failed ? '重试失败项' : status?.state === 'partial' ? '重新准备' : paused ? '继续准备' : '重新获取进度',
  };
}

export function canSkipFailed(status: {state: string; ready: number; failed: number; running: number; queued: number; total: number; items: Array<{state: string}>} | null, error = '') {
  return !error && !!status && status.state === 'partial' && status.ready > 0 && status.failed > 0 &&
    status.running === 0 && status.queued === 0 && status.ready + status.failed === status.total &&
    status.items.length === status.total && status.items.every(item => item.state === 'ready' || item.state === 'error');
}

export function preparationItemLabel(state: string, operation?: PreparationOperation): string | null {
  if (state === 'ready' && operation === 'reuse') return '已复用';
  if (state !== 'running') return null;
  return ({check: '检查缓存', generate: '首次生成', reuse: '复用缓存', assets: '准备快进和图片', remote: '服务器处理中', nas: '检查 NAS 缓存', download: '下载到本机', reconnect: '重新连接服务器'} as const)[operation!] ?? null;
}

export function formatReadRate(value: unknown): string {
  if (typeof value !== 'number' || !Number.isFinite(value) || value < 0) return '采样中…';
  return `${(value / 1_000_000).toFixed(2)} MB/s`;
}
