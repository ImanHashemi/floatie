import { useId, useLayoutEffect, useRef } from 'react';
import type { ReactNode } from 'react';

export function Dialog({ open, title, busy, onCancel, children, id }: {
  open: boolean; title: string; busy: boolean; onCancel(): void; children: ReactNode; id?: string;
}) {
  const dialog = useRef<HTMLDialogElement>(null);
  const label = useId();
  useLayoutEffect(() => {
    const element = dialog.current!;
    if (open && !element.open) element.showModal();
    if (!open && element.open) element.close();
    return () => { if (element.open) element.close(); };
  }, [open]);
  return <dialog ref={dialog} id={id} aria-labelledby={label} aria-busy={busy} tabIndex={-1}
    onKeyDown={event => {
      if (event.key !== 'Tab') return;
      const element = dialog.current!;
      const controls = [...element.querySelectorAll<HTMLElement>('button:not(:disabled),input:not(:disabled),select:not(:disabled),textarea:not(:disabled),a[href],[tabindex="0"]')]
        .filter(control => control.checkVisibility());
      const first = controls[0], last = controls[controls.length - 1];
      if (!first) { event.preventDefault(); element.focus(); }
      else if (event.shiftKey && (document.activeElement === first || document.activeElement === element)) {
        event.preventDefault(); last?.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault(); first.focus();
      }
    }}
    onCancel={event => { event.preventDefault(); if (!busy) onCancel(); }}>
    <h2 id={label}>{title}</h2>
    {children}
  </dialog>;
}
