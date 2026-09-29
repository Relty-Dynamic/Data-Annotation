export type SeekSettings = {
  arrowMs: number;
  shiftArrowMs: number;
  ctrlArrowMs: number;
  wheelMs: number;
  shiftWheelMs: number;
};

export const DEFAULT_SEEK_SETTINGS: SeekSettings = Object.freeze({
  arrowMs: 100,
  shiftArrowMs: 1000,
  ctrlArrowMs: 30000,
  wheelMs: 5000,
  shiftWheelMs: 1000,
});

export const SEEK_SETTING_FIELDS = [
  { key: 'arrowMs', label: '← / →' },
  { key: 'shiftArrowMs', label: 'Shift + ← / →' },
  { key: 'ctrlArrowMs', label: 'Ctrl + ← / →' },
  { key: 'wheelMs', label: 'Ctrl + Alt + 滚轮' },
  { key: 'shiftWheelMs', label: 'Shift + 滚轮' },
] as const satisfies readonly { key: keyof SeekSettings; label: string }[];

const STORAGE_KEY = 'datamark-seek-settings';
const MAX_STEP_MS = 3_600_000;

function isSeekStepMs(value: unknown): value is number {
  return typeof value === 'number' && Number.isInteger(value) && value >= 1 && value <= MAX_STEP_MS;
}

/** Parse decimal seconds without allowing submillisecond or non-decimal values. */
export function parseSeekStepSeconds(value: string): number | null {
  const text = value.trim();
  if (!/^(?:\d+(?:\.\d{0,3})?|\.\d{1,3})$/.test(text)) return null;
  const milliseconds = Math.round(Number(text) * 1000);
  return isSeekStepMs(milliseconds) ? milliseconds : null;
}

/** One damaged preference must not discard the other valid shortcut settings. */
export function readSeekSettings(storage?: Pick<Storage, 'getItem'>): SeekSettings {
  const settings = { ...DEFAULT_SEEK_SETTINGS };
  try {
    const raw: unknown = JSON.parse((storage ?? globalThis.localStorage).getItem(STORAGE_KEY) ?? 'null');
    if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return settings;
    const values = raw as Record<string, unknown>;
    for (const { key } of SEEK_SETTING_FIELDS) {
      if (isSeekStepMs(values[key])) settings[key] = values[key];
    }
  } catch { /* Unavailable storage and invalid JSON leave usable defaults. */ }
  return settings;
}

export function writeSeekSettings(settings: SeekSettings, storage?: Pick<Storage, 'setItem'>): boolean {
  try {
    const values = { ...DEFAULT_SEEK_SETTINGS };
    for (const { key } of SEEK_SETTING_FIELDS) {
      const value = settings[key];
      if (!isSeekStepMs(value)) return false;
      values[key] = value;
    }
    (storage ?? globalThis.localStorage).setItem(STORAGE_KEY, JSON.stringify(values));
    return true;
  } catch { return false; }
}

/** Null leaves zoom/panning alone; zero consumes a seek gesture with no movement. */
export function wheelSeekDelta(event: {
  ctrlKey: boolean; altKey: boolean; shiftKey: boolean; metaKey: boolean;
  deltaX: number; deltaY: number;
}, settings: SeekSettings): number | null {
  if (event.metaKey) return null;
  const step = event.ctrlKey && event.altKey ? settings.wheelMs
    : event.shiftKey && !event.ctrlKey && !event.altKey ? settings.shiftWheelMs : null;
  if (step === null) return null;
  // Chromium may report Shift + wheel as horizontal delta even on a mouse.
  const delta = event.deltaY || event.deltaX;
  return Number.isFinite(delta) && delta !== 0 ? Math.sign(delta) * step : 0;
}
