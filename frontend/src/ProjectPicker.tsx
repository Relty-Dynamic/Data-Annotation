import { useId, useLayoutEffect, useRef, useState } from 'react';
import type { KeyboardEvent } from 'react';
import { createPortal } from 'react-dom';
import { Check, ChevronDown, FolderOpen, Pencil, Plus, Trash2 } from 'lucide-react';
import './project-picker.css';

export interface ProjectPickerItem {
  id: string;
  name: string;
  storage_mode?: string;
}

export interface ProjectPickerProps {
  projects: ProjectPickerItem[];
  currentId: string | null;
  disabled?: boolean;
  canRename?: boolean;
  onChoose: (id: string) => void;
  onRename: (id: string) => void;
  onRemove: (id: string) => void;
  onCreate: () => void;
  showStorageMode?: boolean;
}

const storageName = (mode?: string) => mode === 's3-mock' ? 'S3 模拟' : mode === 'nas' ? 'NAS' : '本机';

export default function ProjectPicker({ projects, currentId, disabled = false, canRename = true, onChoose, onRename, onRemove, onCreate, showStorageMode = false }: ProjectPickerProps) {
  const [open, setOpen] = useState(false);
  const [position, setPosition] = useState({ left: 0, top: 0, width: 0, maxHeight: 360 });
  const trigger = useRef<HTMLButtonElement>(null);
  const menu = useRef<HTMLDivElement>(null);
  const id = useId();
  const menuId = `${id}-projects`;
  const current = projects.find(project => project.id === currentId);
  const expanded = open && !disabled;

  const close = (restoreFocus = false) => {
    setOpen(false);
    if (restoreFocus) trigger.current?.focus({ preventScroll: true });
  };

  const perform = (action: () => void) => {
    // A newly opened dialog can then restore focus to this persistent button.
    close(true);
    action();
  };

  useLayoutEffect(() => {
    if (disabled) setOpen(false);
  }, [disabled]);

  useLayoutEffect(() => {
    if (!expanded) return;
    const updatePosition = () => {
      const bounds = trigger.current?.getBoundingClientRect();
      if (!bounds) return;
      const margin = 8;
      const gap = 6;
      const width = Math.min(Math.max(bounds.width, 280), window.innerWidth - margin * 2);
      const availableBelow = window.innerHeight - bounds.bottom - gap - margin;
      const availableAbove = bounds.top - gap - margin;
      const above = availableBelow < 180 && availableAbove > availableBelow;
      const maxHeight = Math.max(80, Math.min(360, above ? availableAbove : availableBelow));
      const height = Math.min(menu.current?.scrollHeight ?? maxHeight, maxHeight);
      setPosition({
        left: Math.max(margin, Math.min(bounds.right - width, window.innerWidth - width - margin)),
        top: above ? Math.max(margin, bounds.top - height - gap) : bounds.bottom + gap,
        width,
        maxHeight,
      });
    };
    const contains = (target: EventTarget | null) => target instanceof Node && (trigger.current?.contains(target) || menu.current?.contains(target));
    const outsidePointer = (event: PointerEvent) => { if (!contains(event.target)) setOpen(false); };
    const outsideFocus = (event: FocusEvent) => { if (!contains(event.target)) setOpen(false); };
    const fullscreenChanged = () => close(true);
    updatePosition();
    const selected = menu.current?.querySelector<HTMLButtonElement>('[aria-checked="true"]');
    (selected ?? menu.current?.querySelector<HTMLButtonElement>('button'))?.focus({ preventScroll: true });
    selected?.scrollIntoView({ block: 'nearest' });
    document.addEventListener('pointerdown', outsidePointer, true);
    document.addEventListener('focusin', outsideFocus, true);
    document.addEventListener('fullscreenchange', fullscreenChanged);
    window.addEventListener('resize', updatePosition);
    window.addEventListener('scroll', updatePosition, true);
    return () => {
      document.removeEventListener('pointerdown', outsidePointer, true);
      document.removeEventListener('focusin', outsideFocus, true);
      document.removeEventListener('fullscreenchange', fullscreenChanged);
      window.removeEventListener('resize', updatePosition);
      window.removeEventListener('scroll', updatePosition, true);
    };
  }, [expanded]);

  const menuKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    event.stopPropagation();
    if (event.nativeEvent.isComposing || event.keyCode === 229) return;
    if (event.key === 'Escape') {
      event.preventDefault();
      close(true);
      return;
    }
    if (event.key === 'Tab') {
      // Restore the trigger before native Tab moves to the adjacent control.
      close(true);
      return;
    }
    if (!['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(event.key)) return;
    event.preventDefault();
    const controls = Array.from(menu.current?.querySelectorAll<HTMLButtonElement>('button:not(:disabled)') ?? []);
    if (!controls.length) return;
    const currentIndex = controls.indexOf(document.activeElement as HTMLButtonElement);
    const nextIndex = event.key === 'Home' ? 0 : event.key === 'End' ? controls.length - 1
      : (currentIndex + (event.key === 'ArrowUp' ? -1 : 1) + controls.length) % controls.length;
    controls[nextIndex].focus({ preventScroll: true });
    controls[nextIndex].scrollIntoView({ block: 'nearest' });
  };

  return <div className="project-menu-picker" data-workspace-menu>
    <button ref={trigger} className="project-menu-trigger" type="button" aria-label={current ? `选择项目，当前项目：${current.name}` : '选择项目'}
      aria-haspopup="menu" aria-expanded={expanded} aria-controls={expanded ? menuId : undefined}
      disabled={disabled} title={current?.name ?? '选择项目'} onClick={() => setOpen(value => !value)}
      onKeyDown={event => {
        event.stopPropagation();
        if (event.nativeEvent.isComposing || event.keyCode === 229) return;
        if (event.key === 'ArrowDown' || event.key === 'ArrowUp') { event.preventDefault(); setOpen(true); }
        if (event.key === 'Escape' && expanded) { event.preventDefault(); close(true); }
      }}>
      <FolderOpen size={15} aria-hidden="true" />
      <span>{current?.name ?? '选择项目'}</span>
      <ChevronDown size={14} aria-hidden="true" className={expanded ? 'is-open' : undefined} />
    </button>
    {expanded && createPortal(<div ref={menu} id={menuId} role="menu" aria-label="项目列表" data-workspace-menu
      className="project-menu-popover" style={position} onKeyDown={menuKeyDown}>
      <button type="button" role="menuitem" className="project-menu-create" onClick={() => perform(onCreate)}>
        <Plus size={16} aria-hidden="true" />新建项目
      </button>
      {projects.length === 0 && <p className="project-menu-empty" role="presentation">暂无本机项目</p>}
      {projects.map(project => <div key={project.id} className={`project-menu-row${project.id === currentId ? ' is-current' : ''}`} role="none">
        <button type="button" role="menuitemradio" aria-checked={project.id === currentId} title={project.name}
          className="project-menu-choice" onClick={() => perform(() => onChoose(project.id))}>
          <span className="project-menu-current-mark" aria-hidden="true">{project.id === currentId && <Check size={14} />}</span>
          <span className="project-menu-name">{project.name}{showStorageMode && <small>{storageName(project.storage_mode)}</small>}</span>
        </button>
        <button type="button" role="menuitem" className="project-menu-action" aria-label={`修改项目名称：${project.name}`} disabled={!canRename} title={canRename ? '修改项目名称' : '仅管理员可修改项目名称'}
          onClick={() => perform(() => onRename(project.id))}><Pencil size={14} aria-hidden="true" /></button>
        <button type="button" role="menuitem" className="project-menu-action project-menu-remove" aria-label={`删除本机项目：${project.name}`} title="清理本机预览并移除项目"
          onClick={() => perform(() => onRemove(project.id))}><Trash2 size={14} aria-hidden="true" /></button>
      </div>)}
    </div>, document.fullscreenElement ?? document.body)}
  </div>;
}
