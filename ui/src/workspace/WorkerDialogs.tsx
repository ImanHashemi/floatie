import { useLayoutEffect, useRef, useState } from 'react';
import type { Intent, OperationResult } from '../api.js';
import type { AppSnapshot, Controller } from '../controller.js';
import { canPerform, inventoryCurrent, agentsCurrent, sameSet, selectWorkers, validWorkerName } from '../model.js';
import { Dialog } from '../components/Dialog.js';

export function resultText(result: OperationResult): string {
  if (result.outcome === 'blocked') return result.reason;
  const prefix = result.outcome === 'unknown' ? 'Outcome unknown. Refresh observations before retrying.' : result.outcome === 'failed' ? 'Operation failed.' : 'Operation completed.';
  return [prefix, result.response.error, result.response.output].filter(Boolean).join('\n');
}
const ownsOperation = (snapshot: AppSnapshot) => ['waiting', 'submitting', 'observing'].includes(snapshot.operation.phase);

export function WorkerDialogs({ active, snapshot, controller, createOpen, removeIntent, onCancelCreate, onCancelRemove, onFinished }: {
  active: boolean; snapshot: AppSnapshot; controller: Controller; createOpen: boolean; removeIntent: Extract<Intent, {kind: 'remove'}> | null;
  onCancelCreate(): void; onCancelRemove(): void; onFinished(kind: 'create' | 'remove' | 'inspect', name: string, message: string): void;
}) {
  const [name, setName] = useState('');
  const [repo, setRepo] = useState('');
  const [available, setAvailable] = useState<string[]>([]);
  const [createMessage, setCreateMessage] = useState('');
  const [removeMessage, setRemoveMessage] = useState('');
  const [partialName, setPartialName] = useState<string | null>(null);
  const [pending, setPending] = useState(false);
  const wasOpen = useRef(false);
  const focusName = useRef(false);
  const nameInput = useRef<HTMLInputElement>(null);
  useLayoutEffect(() => {
    if (createOpen && !wasOpen.current) {
      setAvailable([...(snapshot.state?.availableRepos ?? [])]);
    }
    wasOpen.current = createOpen;
  }, [createOpen, snapshot.state?.availableRepos]);
  useLayoutEffect(() => { setRemoveMessage(''); }, [removeIntent]);
  const busy = pending || ownsOperation(snapshot);
  useLayoutEffect(() => { if (active && focusName.current && !busy) { nameInput.current?.focus(); focusName.current = false; } }, [active, busy, createMessage]);
  const changed = !sameSet(available, snapshot.state?.availableRepos ?? []);
  const known = inventoryCurrent(snapshot, snapshot.now) && agentsCurrent(snapshot, snapshot.now);
  const unknownCreate = snapshot.blockedUnknownKeys.includes(`create:${name}`);
  const unknownRemove = !!removeIntent && snapshot.blockedUnknownKeys.includes(`remove:${removeIntent.name}`);
  const missing = snapshot.state?.config?.repos.map(url => url.split('/').pop()!.replace(/\.git$/, ''))
    .filter(item => !available.includes(item)) ?? [];
  const refresh = () => { void controller.refresh('explicit'); };
  async function create() {
    if (busy || snapshot.refreshing || unknownCreate) return;
    if (!validWorkerName(name)) { setCreateMessage('Name must be 1–63 lowercase letters, digits, underscores or hyphens, starting with a letter or digit. main, stack, workspace and all are reserved.'); return; }
    const before = controller.getSnapshot();
    if (selectWorkers(before, Date.now()).some(row => row.key === name)) {
      setCreateMessage('Already exists — attach to the saved worker. Inspect its saved scope; a new request cannot expand it.'); return;
    }
    const intent: Intent = { kind: 'create', expectedVm: before.boundVm ?? '', name, repos: repo ? [repo] : [...available], availableRepos: [...available] };
    setPending(true); setCreateMessage('Waiting for current observations…'); setPartialName(null);
    const result = await controller.perform(intent);
    setPending(false);
    if (result.outcome === 'completed') {
      setCreateMessage('');
      onFinished('create', intent.name, 'Worker terminal created. No agent or service stack was automatically started.\n' + resultText(result));
      return;
    }
    const after = controller.getSnapshot();
    const survivor = after.environments.find(env => env.kind === 'worker' && env.name === intent.name);
    const surviving = survivor?.repos.map(item => item.repo) ?? [];
    const missingRepos = intent.repos.filter(item => !surviving.includes(item));
    setCreateMessage(resultText(result) + (survivor ? `\nRequested: ${intent.repos.join(', ')}. Surviving scope: ${surviving.join(', ') || 'unknown'}. Missing: ${missingRepos.join(', ') || 'none'}. The original request cannot simply be resubmitted under this name. Inspect/recreate the reduced saved scope, or use guarded Remove before creating the original set again.` : ''));
    if (survivor) setPartialName(intent.name);
    focusName.current = true;
  }
  async function remove() {
    if (!removeIntent || busy || snapshot.refreshing || unknownRemove) return;
    setPending(true); setRemoveMessage('Waiting for fresh preservation observations…');
    const result = await controller.perform(removeIntent);
    setPending(false);
    if (result.outcome === 'completed') {
      onFinished('remove', removeIntent.name, resultText(result));
    } else {
      setRemoveMessage((result.outcome === 'blocked' ? 'Preservation or observation state changed. ' : '') + resultText(result));
    }
  }
  return <>
    <Dialog id="create-dialog" open={active && createOpen} title="New worker" busy={busy} onCancel={onCancelCreate}>
      <p>Create a terminal with saved worktrees. No agent or service stack starts automatically.</p>
      <label htmlFor="wname">Worker name</label>
      <input ref={nameInput} id="wname" data-testid="worker-name" autoFocus value={name} disabled={busy}
        autoComplete="off" onChange={event => { setName(event.target.value); setPartialName(null); }} />
      <label htmlFor="wrepo">Repositories</label>
      <select id="wrepo" data-testid="worker-repo" value={repo} disabled={busy} onChange={event => setRepo(event.target.value)}>
        <option value="">All available repositories</option>
        {available.map(item => <option key={item} value={item}>One repository: {item}</option>)}
        {repo && !available.includes(repo) && <option value={repo}>Unavailable: {repo}</option>}
      </select>
      <p>Available: {available.join(', ') || 'none'}</p>
      {!!missing.length && <p>Configured but missing: {missing.join(', ')}. Complete repository setup in Settings.</p>}
      {changed && <div className="availability-review"><p>Repository availability changed. Review the updated set: {snapshot.state?.availableRepos.join(', ') || 'none'}.</p>
        <button type="button" disabled={busy} onClick={() => { setAvailable([...(snapshot.state?.availableRepos ?? [])]); }}>Use reviewed availability</button></div>}
      {!known && <p>Repository or agent observations unavailable. Refresh before creating.</p>}
      <div role="status" aria-live="polite" className="dialog-result">{createMessage || (unknownCreate ? 'Outcome unknown. Refresh observations before retrying.' : '')}</div>
      <div className="dialog-actions">
        <button id="create-cancel" type="button" disabled={busy} onClick={onCancelCreate}>Cancel</button>
        <button id="wcreate" data-testid="worker-create" type="button" disabled={busy || snapshot.refreshing || unknownCreate || !known || changed || !available.length}
          onClick={() => void create()}>Create worker</button>
        {partialName && <button type="button" disabled={busy} onClick={() => onFinished('inspect', partialName, createMessage)}>Inspect worker</button>}
        {(unknownCreate || !known) && <button type="button" disabled={busy || snapshot.refreshing} onClick={refresh}>Refresh observations</button>}
      </div>
    </Dialog>
    <Dialog id="remove-dialog" open={active && !!removeIntent} title={`Remove worker ${removeIntent?.name ?? ''}`} busy={busy} onCancel={onCancelRemove}>
      <p>Remove <strong>{removeIntent?.name}</strong> and its saved repositories ({removeIntent?.repos.join(', ')})?</p>
      <p>This stops the terminal session and agent, runs the workspace stack cleanup hook, removes owned worktrees and branches, and may delete stack data through the hook. Preservation is checked again before removal. A partial failure can leave earlier phases completed.</p>
      <div role="status" aria-live="polite" className="dialog-result">{removeMessage || (unknownRemove ? 'Outcome unknown. Refresh observations before retrying.' : '')}</div>
      <div className="dialog-actions">
        <button id="remove-cancel" type="button" disabled={busy} onClick={onCancelRemove}>Cancel</button>
        <button id="remove-confirm" type="button" disabled={busy || snapshot.refreshing || unknownRemove} onClick={() => void remove()}>Remove worker</button>
        {unknownRemove && <button type="button" disabled={busy || snapshot.refreshing} onClick={refresh}>Refresh observations</button>}
      </div>
    </Dialog>
  </>;
}
