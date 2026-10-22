import { useLayoutEffect, useRef, useState } from 'react';
import type { ReactNode } from 'react';
import { createCopyHandler } from './commandCopy.js';
import type { CopyResult } from './commandCopy.js';

// The latest value/eligibility is checked again after the asynchronous OS clipboard call.
export function Command({ value, disabledReason, label = 'Copy attach command', hideButton = false,
  expanded = false, onExpandedChange, children, actions, detailLabel = 'Details', commandName }: {
  value: string | null; disabledReason: string | null; label?: string; hideButton?: boolean;
  expanded?: boolean; onExpandedChange?(open: boolean): void; children?: ReactNode; actions?: ReactNode;
  detailLabel?: string; commandName?: string;
}) {
  const [result, setResult] = useState<CopyResult | null>(null);
  const latest = useRef({ value, disabledReason });
  const code = useRef<HTMLElement>(null);
  const mounted = useRef(false);
  useLayoutEffect(() => {
    latest.current = { value, disabledReason };
  });
  useLayoutEffect(() => { mounted.current = true; return () => { mounted.current = false; }; }, []);
  const activeResult = result?.value === value && !disabledReason ? result : null;
  useLayoutEffect(() => {
    if (!activeResult?.failed || !code.current) return;
    code.current.focus();
    const range = document.createRange();
    range.selectNodeContents(code.current);
    const selection = window.getSelection();
    selection?.removeAllRanges(); selection?.addRange(range);
  }, [activeResult]);
  const copy = createCopyHandler(() => mounted.current ? latest.current : null,
    value => navigator.clipboard.writeText(value), result => {
      setResult(result);
      if (result.failed) onExpandedChange?.(true);
    });
  return <>
    <div className="row-actions">
      <button type="button" className="copy" aria-label={label} data-command={commandName}
        hidden={hideButton} disabled={!value || !!disabledReason} onClick={copy}>
        {label.replace(' command', '')}
      </button>
      {actions}
    </div>
    <div className="copy-status" role="status" aria-live="polite">{activeResult ? activeResult.failed ? 'Copy failed. Select and copy the command below.' : 'Copied' : ''}</div>
    {disabledReason && <p className="copy-note">{disabledReason}</p>}
    <details className="row-disclosure" open={expanded || !!activeResult?.failed}
      onToggle={event => { if (event.currentTarget.open !== expanded) onExpandedChange?.(event.currentTarget.open); }}>
      <summary>{detailLabel}</summary>
      <div className="expanded-details">
        {children}
        <code className="command-value" ref={code} tabIndex={-1}>{value ?? ''}</code>
        {value && <p className="command-note">Run in a host terminal. The command uses workspace configuration at execution.</p>}
      </div>
    </details>
  </>;
}
