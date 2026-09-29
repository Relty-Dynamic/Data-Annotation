import { useEffect } from 'react';

export function useWorkspaceGestures() {
  useEffect(() => {
    // Cancel native horizontal overscroll even at the timeline boundary, where
    // Chromium can otherwise turn a touchpad swipe into history navigation.
    const wheel = (event: WheelEvent) => {
      // Ctrl + wheel belongs to timeline zoom. Elsewhere in this workspace it
      // must not resize the browser page; leave propagation intact for the axis.
      if (event.ctrlKey) {
        event.preventDefault();
        return;
      }
      if (event.defaultPrevented) return;
      const horizontal = event.deltaX !== 0 && Math.abs(event.deltaX) >= Math.abs(event.deltaY);
      if (!horizontal) return;
      event.preventDefault();
      const target = event.target instanceof Element ? event.target : null;
      const timeline = target?.closest<HTMLElement>('.tl-scroll');
      if (!timeline) return;
      const delta = horizontal ? event.deltaX : event.deltaY;
      const unit = event.deltaMode === 1 ? 16 : event.deltaMode === 2 ? timeline.clientWidth : 1;
      timeline.scrollLeft += delta * unit;
    };
    const keydown = (event: KeyboardEvent) => {
      if (!(event.ctrlKey || event.metaKey) || event.altKey) return;
      if (!['Equal', 'Minus', 'Digit0', 'NumpadAdd', 'NumpadSubtract', 'Numpad0'].includes(event.code)) return;
      event.preventDefault();
      event.stopImmediatePropagation();
    };
    // Bubble phase leaves the timeline's Ctrl/Alt capture handlers in charge.
    document.addEventListener('wheel', wheel, { passive: false });
    document.addEventListener('keydown', keydown, true);
    return () => {
      document.removeEventListener('wheel', wheel);
      document.removeEventListener('keydown', keydown, true);
    };
  }, []);
}
