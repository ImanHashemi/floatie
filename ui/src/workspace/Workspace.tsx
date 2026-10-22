import { useLayoutEffect, useRef, useState } from 'react';
import type { Dispatch, SetStateAction } from 'react';
import type { Filter, Intent } from '../api.js';
import type { AppSnapshot, Controller } from '../controller.js';
import type { WorkspaceViewState } from '../App.js';
import { agentsCurrent, canPerform, current, filterWorkers, identityMatches, inventoryCurrent, selectWorkers, vmCurrent } from '../model.js';
import type { WorkerView } from '../model.js';
import { Command } from '../components/Command.js';
import { Dialog } from '../components/Dialog.js';
import { Feedback } from '../components/Feedback.js';
import { RootPanel } from './RootPanel.js';
import { WorkerRow } from './WorkerRow.js';
import { resultText, WorkerDialogs } from './WorkerDialogs.js';

export function Workspace({ active, snapshot, controller, view, onViewChange }: {
  active: boolean; snapshot: AppSnapshot; controller: Controller; view: WorkspaceViewState;
  onViewChange: Dispatch<SetStateAction<WorkspaceViewState>>;
}) {
  const [createOpen, setCreateOpen] = useState(false);
  const [removeIntent, setRemoveIntent] = useState<Extract<Intent, {kind: 'remove'}> | null>(null);
  const [stopIntent, setStopIntent] = useState<Extract<Intent, {kind: 'vm'}> | null>(null);
  const [stopMessage, setStopMessage] = useState('');
  const [feedback, setFeedback] = useState({ message: '', output: '' });
  const [vmMessage, setVmMessage] = useState('');
  const [announcement, setAnnouncement] = useState('');
  const [focusTarget, setFocusTarget] = useState<string | null>(null);
  const focusedRow = useRef<string | null>(null);
  const focusedControl = useRef<HTMLElement | null>(null);
  const temporarilyDisabled = useRef(false);
  const content = useRef<HTMLDivElement>(null);
  const heading = useRef<HTMLHeadingElement>(null);
  const newWorker = useRef<HTMLButtonElement>(null);
  const rows = selectWorkers(snapshot, snapshot.now);
  const workers = rows.filter(row => row.key !== 'main');
  const filtered = filterWorkers(rows, view.search, view.filter);
  const busy = ['waiting', 'submitting', 'observing'].includes(snapshot.operation.phase);
  const state = snapshot.state;
  const freshVM = vmCurrent(snapshot, snapshot.now);
  const matching = identityMatches(snapshot);
  const absentVM = matching && freshVM && state?.vm.status === 'absent';
  const vmIntent = (action: 'start' | 'stop' | 'reload'): Extract<Intent, {kind: 'vm'}> => ({ kind: 'vm', expectedVm: snapshot.boundVm ?? '', action });
  const expand = (key: string, open: boolean) => onViewChange(previous => {
    const expanded = new Set(previous.expanded);
    if (open) expanded.add(key); else expanded.delete(key);
    return { ...previous, expanded };
  });
  useLayoutEffect(() => {
    if (!active) return;
    if (focusTarget) {
      const button = focusTarget === 'new' ? newWorker.current :
        [...(content.current?.querySelectorAll<HTMLElement>('[data-worker]') ?? [])].find(row => row.dataset.worker === focusTarget)?.querySelector<HTMLButtonElement>('.copy');
      if (button) { button.focus(); setFocusTarget(null); }
      return;
    }
    const key = focusedRow.current;
    if (key && !rows.some(row => row.key === key) && !(snapshot.operation.intent?.kind === 'remove' && snapshot.operation.intent.name === key)) {
      heading.current?.focus(); focusedRow.current = null;
      setAnnouncement(`${key} disappeared from the workspace. Focus moved to Workers.`);
    }
    const control = focusedControl.current;
    if (control?.isConnected && control.matches(':disabled')) temporarilyDisabled.current = true;
    else if (temporarilyDisabled.current) {
      temporarilyDisabled.current = false;
      if (control?.isConnected && control.checkVisibility() && document.activeElement === document.body && !document.querySelector('dialog[open]')) control.focus();
    }
  });
  const finish = (kind: 'create' | 'remove' | 'inspect', name: string, message: string) => {
    setCreateOpen(false); setRemoveIntent(null);
    setFeedback({ message, output: '' });
    if (kind !== 'remove') onViewChange(previous => ({ ...previous, search: '', filter: 'all', expanded: kind === 'inspect' ? new Set([...previous.expanded, name]) : previous.expanded }));
    setFocusTarget(kind === 'remove' ? 'new' : name);
  };
  const remove = (row: WorkerView) => {
    const intent: Extract<Intent, {kind: 'remove'}> = { kind: 'remove', name: row.key, expectedVm: snapshot.boundVm ?? '', repos: row.env?.repos.map(repo => repo.repo) ?? [] };
    if (canPerform(controller.getSnapshot(), intent, Date.now()).allowed) setRemoveIntent(intent);
  };
  const runVM = async (intent: Extract<Intent, {kind: 'vm'}>, confirmed = false) => {
    const permission = canPerform(controller.getSnapshot(), intent, Date.now());
    if (!permission.allowed) { if (confirmed) setStopMessage(permission.reason!); return; }
    if (intent.action === 'stop' && !confirmed) { setStopMessage(''); setStopIntent(intent); return; }
    const result = await controller.perform(intent);
    const message = resultText(result);
    setVmMessage(message);
    if (confirmed) { if (result.outcome === 'blocked') setStopMessage(message); else setStopIntent(null); }
  };
  const creationReady = matching && inventoryCurrent(snapshot, snapshot.now) && agentsCurrent(snapshot, snapshot.now) && !busy && !snapshot.refreshing;
  const setupCommand = absentVM && state?.workspace.dir ? "cd -- '" + state.workspace.dir.replace(/'/g, "'\"'\"'") + "' && floatie create" : null;
  const vmUnknown = snapshot.blockedUnknownKeys.some(key => key.startsWith('vm:'));
  const diskCurrent = !!state && !snapshot.stateError && current({ observedAt: state.vm.diskObservedAt ?? null, error: state.vm.diskError ?? null }, snapshot.now);
  const inventoryMessage = snapshot.stateError || state?.inventory.error || state?.inventory.repositoryError || state?.inventory.countsError || snapshot.agentError;
  return <div id="workspace-content" data-pane="workspace" ref={content} aria-busy={busy}
    onBlurCapture={event => {
      const control = event.target as HTMLElement;
      // A disabled or removed control can lose focus without a destination.
      // A still-enabled control's null blur instead releases focus ownership.
      const leftWorkspace = event.relatedTarget ? !event.currentTarget.contains(event.relatedTarget) :
        control.isConnected && !control.matches(':disabled');
      if (leftWorkspace) {
        focusedRow.current = null;
        focusedControl.current = null;
        temporarilyDisabled.current = false;
      }
    }}
    onFocusCapture={event => {
      focusedControl.current = event.target as HTMLElement;
      temporarilyDisabled.current = false;
      focusedRow.current = focusedControl.current.closest<HTMLElement>('[data-worker]')?.dataset.worker ?? null;
    }}>
    <section id="vm-setup" hidden={!absentVM} className="details-panel">
      <h2>Create the VM</h2><p>The VM is absent. Run this command from your host to set up the workspace.</p>
      <Command commandName="create" label="Copy create command" value={setupCommand} disabledReason={null} />
    </section>
    <p id="vmnote" className="vm-note">{vmUnknown ? 'Outcome unknown. Use Refresh to re-observe before retrying.' : !freshVM ? 'VM observation unavailable or stale. Refresh to check again.' : state?.vm.status === 'Stopped' ? 'VM stopped. Start it to observe workers; saved files remain on disk.' : ''}</p>
    {view.vmDetails && <section id="vm-details" className="details-panel" aria-labelledby="vm-details-title">
      <h2 id="vm-details-title">VM details</h2>
      <dl><dt>Workspace directory</dt><dd><code>{state?.workspace.dir || 'Unknown'}</code></dd>
        <dt>VM</dt><dd>{snapshot.boundVm ?? 'Unknown'}</dd>
        <dt>Observation</dt><dd>{freshVM ? state?.vm.status : 'Unknown; last known ' + (state?.vm.status ?? 'unavailable')}{state?.vm.error && ' · ' + state.vm.error}</dd>
        <dt>Allocation</dt><dd>{state?.vm.cpus ?? 'Unknown'} CPUs / {state?.vm.memoryGiB ?? 'Unknown'} GiB allocated{!freshVM && ' · stale'}</dd>
        <dt>Disk usage</dt><dd>{diskCurrent && state?.vm.diskPct != null ? `${state.vm.diskPct}% used` : 'Unknown'}{!diskCurrent && state?.vm.diskPct != null && ` · last known ${state.vm.diskPct}%`}. {state?.vm.diskError ?? ''} Observed: {state?.vm.diskObservedAt ?? 'unavailable'}</dd>
      </dl>
      <div className="vm-actions">{(['start', 'stop', 'reload'] as const).map(action => {
        const permission = canPerform(snapshot, vmIntent(action), snapshot.now);
        return <div key={action} hidden={action === 'start' ? state?.vm.status !== 'Stopped' : state?.vm.status !== 'Running'}>
          <button type="button" data-testid={`act-${action}`} disabled={!permission.allowed}
            onClick={() => void runVM(vmIntent(action))}>{action === 'start' ? 'Start VM' : action === 'stop' ? 'Stop VM' : 'Apply workspace files'}</button>
          {!permission.allowed && <p>{permission.reason}</p>}
        </div>;
      })}</div>
      {state?.vm.status === 'Running' && <p>Apply workspace files places context, tools, skills and stack files and attempts Claude plugin installation. It does not restart the VM or agents, copy new secrets, apply firewall policy, clone repositories or resize the VM.</p>}
    </section>}
    {!view.vmDetails && state?.vm.status === 'Stopped' && <button type="button" disabled={!canPerform(snapshot, vmIntent('start'), snapshot.now).allowed} onClick={() => void runVM(vmIntent('start'))}>Start VM</button>}
    <div id="log">{vmMessage && <Feedback scope="VM action" message={vmMessage} />}</div>
    {state && (!freshVM || !current(state.firewall, snapshot.now) || state.firewall.mode === 'unknown' || snapshot.stateError) && <p className="workspace-exception">Network observation unknown. <a href="#settings/network">Review Network settings</a>.</p>}
    {!!state?.secrets.some(secret => secret.state !== 'set') && <p className="workspace-exception">Credentials need setup. <a href="#settings/credentials">Review Credentials</a>.</p>}
    <RootPanel row={rows[0]!} snapshot={snapshot} controller={controller} expanded={view.expanded.has('main')}
      onExpand={open => expand('main', open)} onResult={(message, output = '') => setFeedback({ message, output })} />
    <section className="workers-shell" aria-labelledby="workers-heading">
      <div className="workers-heading"><h2 id="workers-heading" ref={heading} tabIndex={-1}>Workers</h2>
        <button ref={newWorker} id="new-worker" type="button" className="primary" disabled={!creationReady} onClick={() => { if (creationReady) setCreateOpen(true); }}>New worker</button></div>
      <div className="worker-toolbar">
        <label className="search-control" htmlFor="worker-search"><span className="visually-hidden">Search workers</span><input id="worker-search" type="search" placeholder="Find a worker or repository…" value={view.search} onChange={event => onViewChange(previous => ({ ...previous, search: event.target.value }))} /></label>
        <label htmlFor="worker-filter"><span className="visually-hidden">Worker filter</span><select id="worker-filter" value={view.filter} onChange={event => onViewChange(previous => ({ ...previous, filter: event.target.value as Filter }))}>
          <option value="all">All ({workers.length})</option><option value="running">Agent running ({workers.filter(row => row.running).length})</option>
          <option value="absent">Terminal absent ({workers.filter(row => row.absent).length})</option><option value="changes">Repository changes ({workers.filter(row => row.changes).length})</option>
        </select></label>
        <span id="worker-count">{filtered.length} of {workers.length} workers</span>
      </div>
      <div id="workers-observed">Repositories {inventoryCurrent(snapshot, snapshot.now) ? 'observed' : 'stale or unavailable'} · agents {agentsCurrent(snapshot, snapshot.now) ? 'observed' : 'unavailable'}</div>
      <div id="workers-refreshing" hidden={!snapshot.refreshing}>Refreshing observations…</div>
      <p id="workers-status">{!inventoryCurrent(snapshot, snapshot.now) && workers.length === 0 ? 'Worker inventory unavailable until the VM is running.' : inventoryMessage ? 'Observation unavailable: ' + inventoryMessage : ''}</p>
      <div id="envlog">{feedback.message && <Feedback scope="Workspace action" message={feedback.message} output={feedback.output} />}</div>
      <div className="visually-hidden" role="status" aria-label="Worker updates">{announcement}</div>
      <div className="worker-columns" aria-hidden="true"><span>Worker / repositories</span><span>Terminal</span><span>Repository changes</span><span>Actions</span></div>
      <div id="workers">{filtered.map(row => <WorkerRow key={row.key} row={row} snapshot={snapshot} expanded={view.expanded.has(row.key)}
        onExpand={open => expand(row.key, open)} onRemove={remove} />)}</div>
      {!filtered.length && <div id="empty-workers">{workers.length ? <>No workers match your search and filter. <button type="button" onClick={() => onViewChange(previous => ({ ...previous, search: '', filter: 'all' }))}>Clear filters</button></> : inventoryCurrent(snapshot, snapshot.now) ? 'No workers yet. Create your first worker.' : 'Worker inventory is unavailable.'}</div>}
    </section>
    <WorkerDialogs active={active} snapshot={snapshot} controller={controller} createOpen={createOpen} removeIntent={removeIntent}
      onCancelCreate={() => setCreateOpen(false)} onCancelRemove={() => setRemoveIntent(null)} onFinished={finish} />
    <Dialog id="stop-dialog" open={active && !!stopIntent} title={`Stop VM ${stopIntent?.expectedVm ?? ''}`} busy={busy} onCancel={() => setStopIntent(null)}>
      <p>Stop the whole VM <strong>{stopIntent?.expectedVm}</strong>? All terminals and agents stop. Services shut down with the VM. Files are retained on disk.</p>
      <div role="status" className="dialog-result">{stopMessage}</div>
      <div className="dialog-actions"><button type="button" disabled={busy} onClick={() => setStopIntent(null)}>Cancel</button>
        <button type="button" disabled={busy} onClick={() => { if (stopIntent) void runVM(stopIntent, true); }}>Stop VM</button></div>
    </Dialog>
  </div>;
}
