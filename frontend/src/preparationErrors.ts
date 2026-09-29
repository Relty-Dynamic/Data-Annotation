export const OLD_PLAYBACK_BACKEND = '后台仍是旧版本，尚不支持精简播放缓存。请关闭所有平台网页，重新双击「启动标注平台」快捷方式，再刷新网页。';

/** Only a confirmed missing capability identifies an old backend; project 404s stay intact. */
export function preparationFailureMessage(status: number, detail: unknown, capabilities?: string[]): string {
  if (status === 404 && capabilities && !capabilities.includes('compact-local-playback')) return OLD_PLAYBACK_BACKEND;
  return typeof detail === 'string' && detail ? detail : '无法读取播放准备进度，请稍后重试。';
}
