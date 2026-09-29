import { useId, useLayoutEffect, useRef, useState } from 'react';
import { Settings, X } from 'lucide-react';
import { DEFAULT_SEEK_SETTINGS, SEEK_SETTING_FIELDS, parseSeekStepSeconds } from './seekSettings';
import type { SeekSettings } from './seekSettings';

type Props = {
  value: SeekSettings;
  onCancel: () => void;
  onSave: (value: SeekSettings) => void;
};
const draftFor = (value: SeekSettings) => Object.fromEntries(
  SEEK_SETTING_FIELDS.map(({ key }) => [key, String(value[key] / 1000)]),
) as Record<keyof SeekSettings, string>;

export default function SeekSettingsDialog({ value, onCancel, onSave }: Props) {
  const [draft, setDraft] = useState(() => draftFor(value));
  const id = useId();
  const dialog = useRef<HTMLDivElement>(null);
  const firstInput = useRef<HTMLInputElement>(null);
  const composing = useRef(false);
  const previousFocus = useRef<HTMLElement | null>(document.activeElement instanceof HTMLElement ? document.activeElement : null);
  const cancel = useRef(onCancel);
  cancel.current = onCancel;
  const invalid = SEEK_SETTING_FIELDS.some(({ key }) => parseSeekStepSeconds(draft[key]) === null);

  useLayoutEffect(() => {
    const focusFirst = () => firstInput.current?.focus({ preventScroll: true });
    const containFocus = (event: FocusEvent) => {
      if (dialog.current && event.target instanceof Node && !dialog.current.contains(event.target)) focusFirst();
    };
    const key = (event: KeyboardEvent) => {
      if (composing.current || event.isComposing || event.keyCode === 229) {
        // Confirming an IME candidate must never submit the settings form.
        if (event.key === 'Enter') { event.preventDefault(); event.stopPropagation(); }
        return;
      }
      if (event.key === 'Escape') {
        event.preventDefault(); event.stopPropagation(); cancel.current(); return;
      }
      if (event.key !== 'Tab') return;
      const controls = dialog.current?.querySelectorAll<HTMLElement>('input:not(:disabled), button:not(:disabled)');
      if (!controls?.length) return;
      const first = controls[0], last = controls[controls.length - 1];
      const focused = document.activeElement;
      if (!dialog.current?.contains(focused) || focused === dialog.current) {
        event.preventDefault(); (event.shiftKey ? last : first).focus();
      } else if (event.shiftKey && focused === first) {
        event.preventDefault(); last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault(); first.focus();
      }
    };
    document.addEventListener('focusin', containFocus, true);
    document.addEventListener('keydown', key, true);
    focusFirst(); firstInput.current?.select();
    const restore = previousFocus.current;
    return () => {
      document.removeEventListener('focusin', containFocus, true);
      document.removeEventListener('keydown', key, true);
      queueMicrotask(() => { if (restore?.isConnected) restore.focus({ preventScroll: true }); });
    };
  }, []);

  return <div className="modal-backdrop" onClick={event => { if (event.target === event.currentTarget) onCancel(); }}>
    <div ref={dialog} className="import-modal seek-settings-dialog" role="dialog" aria-modal="true" aria-labelledby={`${id}-title`} aria-describedby={`${id}-description`} tabIndex={-1}>
      <form noValidate onSubmit={event => {
        event.preventDefault();
        if (invalid || composing.current) return;
        const next = { ...value };
        for (const { key } of SEEK_SETTING_FIELDS) {
          const step = parseSeekStepSeconds(draft[key]);
          if (step === null) return;
          next[key] = step;
        }
        onSave(next);
      }}>
        <div className="seek-settings-heading"><Settings size={21}/><h2 id={`${id}-title`}>进度快捷键设置</h2><button className="icon-button" type="button" aria-label="关闭进度设置" onClick={onCancel}><X size={18}/></button></div>
        <p id={`${id}-description`}>自定义快捷键每次移动的时长，应用于所有项目。</p>
        <div className="seek-settings-fields">
          {SEEK_SETTING_FIELDS.map(({ key, label }, index) => {
            const error = parseSeekStepSeconds(draft[key]) === null;
            return <div className="seek-setting-row" key={key}>
              <label htmlFor={`${id}-${key}`}>{label}</label>
              <div className="seek-setting-value"><input id={`${id}-${key}`} ref={index === 0 ? firstInput : undefined} type="text" inputMode="decimal" autoComplete="off" spellCheck={false} value={draft[key]} aria-invalid={error} aria-describedby={`${id}-limits${error ? ` ${id}-${key}-error` : ''}`}
                onChange={event => setDraft(previous => ({ ...previous, [key]: event.target.value }))}
                onCompositionStart={() => { composing.current = true; }} onCompositionEnd={() => { composing.current = false; }}/><span>秒</span></div>
              {error && <span className="seek-setting-error" id={`${id}-${key}-error`}>请输入 0.001–3600 秒，最多三位小数。</span>}
            </div>;
          })}
        </div>
        <p className="seek-settings-hint" id={`${id}-limits`}>支持 0.001–3600 秒，最多三位小数。滚轮向上后退、向下前进，调整进度后保持暂停。</p>
        <p className="seek-settings-storage">保存后立即生效，下次打开此浏览器时继续使用。</p>
        <div className="seek-settings-actions"><button className="text-button" type="button" onClick={() => setDraft(draftFor(DEFAULT_SEEK_SETTINGS))}>恢复默认</button><button className="secondary" type="button" onClick={onCancel}>取消</button><button className="primary" type="submit" disabled={invalid}>保存设置</button></div>
      </form>
    </div>
  </div>;
}
