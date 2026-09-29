import { useId, useLayoutEffect, useRef, useState } from 'react';
import { projectNameError } from './projectNames';

export interface RenameProjectDialogProps {
  name: string;
  saving: boolean;
  error: string;
  onCancel: () => void;
  onConfirm: (name: string) => void;
}

export default function RenameProjectDialog({ name, saving, error, onCancel, onConfirm }: RenameProjectDialogProps) {
  const [value, setValue] = useState(name);
  const dialog = useRef<HTMLDivElement>(null);
  const input = useRef<HTMLInputElement>(null);
  const composing = useRef(false);
  const previousFocus = useRef<HTMLElement | null>(typeof document !== 'undefined' && document.activeElement instanceof HTMLElement ? document.activeElement : null);
  const state = useRef({ saving, onCancel });
  state.current = { saving, onCancel };
  const id = useId();
  const titleId = `${id}-title`;
  const inputId = `${id}-name`;
  const hintId = `${id}-hint`;
  const errorId = `${id}-error`;
  const validationError = projectNameError(value);
  const displayedError = validationError || error;

  useLayoutEffect(() => {
    const focusInput = () => { (input.current ?? dialog.current)?.focus({ preventScroll: true }); };
    const containFocus = (event: FocusEvent) => {
      if (dialog.current && event.target instanceof Node && !dialog.current.contains(event.target)) focusInput();
    };
    const key = (event: KeyboardEvent) => {
      if (event.key === 'Enter' && (composing.current || event.isComposing || event.keyCode === 229)) {
        event.preventDefault();
        event.stopPropagation();
        return;
      }
      if (event.key === 'Escape') {
        // Escape first dismisses IME candidates, never the dialog underneath them.
        if (composing.current || event.isComposing || event.keyCode === 229) return;
        event.preventDefault();
        event.stopPropagation();
        if (!state.current.saving) state.current.onCancel();
        return;
      }
      if (event.key !== 'Tab') return;
      const controls = dialog.current?.querySelectorAll<HTMLElement>('input:not(:disabled), button:not(:disabled), [tabindex]:not([tabindex="-1"])');
      if (!controls?.length) { event.preventDefault(); dialog.current?.focus(); return; }
      const first = controls[0];
      const last = controls[controls.length - 1];
      const focused = document.activeElement;
      if (!dialog.current?.contains(focused) || focused === dialog.current) {
        event.preventDefault();
        (event.shiftKey ? last : first).focus();
      } else if (event.shiftKey && focused === first) {
        event.preventDefault(); last.focus();
      } else if (!event.shiftKey && focused === last) {
        event.preventDefault(); first.focus();
      }
    };
    document.addEventListener('focusin', containFocus, true);
    document.addEventListener('keydown', key, true);
    focusInput();
    input.current?.select();
    const restore = previousFocus.current;
    return () => {
      document.removeEventListener('focusin', containFocus, true);
      document.removeEventListener('keydown', key, true);
      // Wait until the parent's inert state has been removed during this commit.
      queueMicrotask(() => { if (restore?.isConnected) restore.focus({ preventScroll: true }); });
    };
  }, []);

  useLayoutEffect(() => {
    const focused = document.activeElement;
    if (!dialog.current?.contains(focused) || focused instanceof HTMLButtonElement && focused.disabled) {
      input.current?.focus({ preventScroll: true });
    }
  }, [saving]);

  return <div className="modal-backdrop" onClick={event => {
    if (event.target === event.currentTarget && !saving) onCancel();
  }}>
    <div ref={dialog} className="import-modal rename-project-dialog" role="dialog" aria-modal="true" aria-labelledby={titleId} aria-busy={saving} tabIndex={-1}>
      <form onSubmit={event => {
        event.preventDefault();
        if (saving || composing.current || projectNameError(value)) return;
        onConfirm(value.trim());
      }}>
        <h2 id={titleId}>重命名项目</h2>
        <label className="field-label" htmlFor={inputId}>项目名称</label>
        <input ref={input} id={inputId} className="project-name-input" autoFocus autoComplete="off" readOnly={saving}
          value={value} aria-invalid={Boolean(validationError)} aria-describedby={`${hintId}${displayedError ? ` ${errorId}` : ''}`}
          onChange={event => setValue(event.target.value)}
          onCompositionStart={() => { composing.current = true; }}
          onCompositionEnd={() => { composing.current = false; }} />
        <p className="input-hint" id={hintId}>最多 80 个字符，名称可以重复。</p>
        {displayedError && <p className="project-name-error" id={errorId} role="alert">{displayedError}</p>}
        <div className="dialog-actions">
          <button className="secondary" type="button" disabled={saving} onClick={onCancel}>取消</button>
          <button className="primary" type="submit" disabled={saving || Boolean(validationError)}>{saving ? '正在保存…' : '保存名称'}</button>
        </div>
      </form>
    </div>
  </div>;
}
