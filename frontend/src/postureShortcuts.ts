export const POSTURE_SHORTCUTS = [
  { key: 'Z', code: 'KeyZ', label: '动' },
  { key: 'X', code: 'KeyX', label: '坐' },
  { key: 'C', code: 'KeyC', label: '站' },
  { key: 'V', code: 'KeyV', label: '躺' },
] as const;

/** Plain posture keys must never consume text entry, IME, or clipboard/undo chords. */
export function postureShortcutLabel(event: {
  code: string; ctrlKey?: boolean; metaKey?: boolean; altKey?: boolean; shiftKey?: boolean;
  repeat?: boolean; isComposing?: boolean; keyCode?: number;
}, editing: boolean): string | null {
  if (editing || event.ctrlKey || event.metaKey || event.altKey || event.shiftKey ||
      event.repeat || event.isComposing || event.keyCode === 229) return null;
  return POSTURE_SHORTCUTS.find(shortcut => shortcut.code === event.code)?.label ?? null;
}
