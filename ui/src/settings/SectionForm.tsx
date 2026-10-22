import { useLayoutEffect, useRef } from 'react';
import type { ReactNode } from 'react';
import type { AppSnapshot } from '../controller.js';
import { identityMatches } from '../model.js';
import { Command } from '../components/Command.js';
import type { ListEntry } from './drafts.js';

export function Field({ id, label, value, locked, onChange, hint }: {
  id: string; label: string; value: string; locked: boolean; onChange(value: string): void; hint?: string;
}) {
  return <div className="settings-field">
    <label htmlFor={id}>{label}</label>
    <input id={id} value={value} readOnly={locked} onChange={event => { if (!locked) onChange(event.target.value); }}
      aria-describedby={hint ? `${id}-hint` : undefined} autoComplete="off" />
    {hint && <p id={`${id}-hint`}>{hint}</p>}
  </div>;
}

export function DraftList({ id, label, inputLabel, entries, pending, locked, onPending, onAdd, onRemove }: {
  id: string; label: string; inputLabel: string; entries: readonly ListEntry[]; pending: string; locked: boolean;
  onPending(value: string): void; onAdd(): void; onRemove(id: string): void;
}) {
  const list = useRef<HTMLUListElement>(null);
  const input = useRef<HTMLInputElement>(null);
  const focused = useRef<HTMLElement | null>(null);
  useLayoutEffect(() => {
    const control = focused.current;
    if (control?.isConnected && document.activeElement === document.body) control.focus();
  });
  return <div className="draft-list" onFocusCapture={event => { focused.current = event.target; }}
    onBlurCapture={event => {
      if (event.relatedTarget || event.target.isConnected) focused.current = null;
    }}>
    <h3>{label === 'Repositories' ? 'Repository list' : label}</h3>
    <ul ref={list} aria-label={label}>{entries.map((entry, index) => <li key={entry.id}>
      <span>{entry.value}</span><button type="button" disabled={locked} aria-label={`Remove ${entry.value}`} onClick={() => {
        if (locked) return;
        const buttons = list.current?.querySelectorAll('button');
        const next = buttons?.[index + 1] ?? buttons?.[index - 1] ?? input.current;
        onRemove(entry.id);
        next?.focus();
      }}>Remove</button>
    </li>)}</ul>
    {!entries.length && <p>No entries configured.</p>}
    <div className="list-add"><label htmlFor={id}>{inputLabel}</label>
      <input id={id} ref={input} value={pending} readOnly={locked} autoComplete="off" placeholder={inputLabel}
        onChange={event => { if (!locked) onPending(event.target.value); }}
        onKeyDown={event => { if (event.key === 'Enter') { event.preventDefault(); if (!locked) onAdd(); } }} />
      <button type="button" disabled={locked} onClick={() => { if (!locked) onAdd(); }}>Add to draft</button>
    </div>
  </div>;
}

export function TerminalCommand({ snapshot, recipe }: { snapshot: AppSnapshot; recipe: 'rebuild' | 'create' | 'reload' | 'codex-setup' | 'attach' }) {
  const dir = snapshot.state?.workspace.dir;
  const valid = identityMatches(snapshot) && !!dir;
  const value = valid ? `cd -- '${dir.replace(/'/g, `'"'"'`)}' && floatie ${recipe}` : null;
  return <section className="settings-command" aria-label={`floatie ${recipe} command`}>
    <Command value={value} disabledReason={valid ? null : 'Workspace identity is unavailable. Reopen the intended workspace.'}
      label={`Copy floatie ${recipe}`} detailLabel={`View floatie ${recipe} command`} />
  </section>;
}

export function SectionForm({ children, locked, canSave, canDiscard, onSave, onDiscard, status, label }: {
  children: ReactNode; locked: boolean; canSave: boolean; canDiscard: boolean;
  onSave(): void; onDiscard(): void; status: string; label: string;
}) {
  return <form className="section-form" aria-busy={locked} onSubmit={event => { event.preventDefault(); if (canSave) onSave(); }}>
    {children}
    <div className="settings-save">
      <div className="settings-buttons"><button className="primary" type="submit" disabled={!canSave}>Save changes</button>
        <button type="button" disabled={!canDiscard} onClick={onDiscard}>Discard</button></div>
      <p>Save updates workspace.yml. Apply saved changes separately.</p>
      <div role="status" aria-label={`${label} status`} aria-live="polite" className="settings-status">{status}</div>
    </div>
  </form>;
}
