const recordingClock = new Intl.DateTimeFormat('en-GB', {
  timeZone: 'Asia/Shanghai', numberingSystem: 'latn',
  year: 'numeric', month: '2-digit', day: '2-digit',
  hour: '2-digit', minute: '2-digit', second: '2-digit',
  fractionalSecondDigits: 3, hourCycle: 'h23',
});

export function parseRecordingOrigin(start: string | null | undefined): number | null {
  if (!start?.trim()) return null;
  const value = start.trim().replace(' ', 'T');
  const zoned = /(?:Z|[+-]\d{2}:?\d{2})$/i.test(value) ? value : `${value}+08:00`;
  const parsed = Date.parse(zoned);
  return Number.isFinite(parsed) ? parsed : null;
}

export function recordingDateTime(origin: number | null, offset: number): string | null {
  if (origin === null) return null;
  const parts = recordingClock.formatToParts(origin + offset);
  const part = (name: Intl.DateTimeFormatPartTypes) => parts.find((item) => item.type === name)?.value ?? '';
  return `${part('year')}-${part('month')}-${part('day')} ${part('hour')}:${part('minute')}:${part('second')}.${part('fractionalSecond')}`;
}
