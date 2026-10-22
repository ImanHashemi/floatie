import { useState } from 'react';
import type { Intent, OperationResult } from '../api.js';
import type { AppSnapshot, Controller } from '../controller.js';
import { configObservedAfter } from '../controller.js';
import { canPerform, current } from '../model.js';
import { Dialog } from '../components/Dialog.js';
import { Feedback } from '../components/Feedback.js';
import type { ConfigObservation } from './drafts.js';

type NetworkIntent = Extract<Intent, { kind: 'network' }>;
export function operationMessage(result: OperationResult): string {
  if (result.outcome === 'blocked') return result.reason;
  return [result.outcome === 'unknown' ? 'Outcome unknown. Explicitly Refresh before retrying.' :
    result.outcome === 'completed' ? 'Operation completed.' : 'Operation failed.',
  result.response.error, result.response.output].filter(Boolean).join('\n');
}

export function Network({ snapshot, controller, observation, onResult }: {
  snapshot: AppSnapshot; controller: Controller; observation: ConfigObservation;
  onResult(message: string, unknown: boolean, latest?: ConfigObservation): void;
}) {
  const [confirmation, setConfirmation] = useState<NetworkIntent | null>(null);
  const [dialogStatus, setDialogStatus] = useState('');
  const { config: saved, revision } = observation;
  const busy = ['waiting', 'submitting', 'observing'].includes(snapshot.operation.phase);
  const intent: NetworkIntent | null = saved && revision && snapshot.boundVm ? {
    kind: 'network', expectedVm: snapshot.boundVm, expectedRevision: revision,
    policy: { permissive: saved.permissive, egress: [...saved.egress] },
  } : null;
  const eligibility = intent ? canPerform(snapshot, intent, snapshot.now) : { allowed: false, reason: 'Saved policy unavailable.' };
  const effective = snapshot.state?.firewall;
  const observed = !snapshot.stateError && current(snapshot.state?.vm, snapshot.now) && current(effective, snapshot.now);
  const differs = observed && saved && effective && (effective.mode !== (saved.permissive ? 'off' : 'on') ||
    effective.permissiveConfigured !== saved.permissive || JSON.stringify(effective.egress) !== JSON.stringify(saved.egress));
  const apply = async (captured: NetworkIntent) => {
    // The controller checks this exact policy again after any active read.
    const result = await controller.perform(captured);
    const message = `${operationMessage(result)}\nReviewed saved policy: ${captured.policy.permissive ? 'Unrestricted' : 'Restricted'}; extra domains: ${captured.policy.egress.join(', ') || 'none'}.`;
    const observed = configObservedAfter(controller.getSnapshot(), result);
    const response = result.outcome === 'blocked' ? null : result.response;
    // A structured conflict is still reviewable if the following read fails.
    // A successful later read takes precedence over that earlier response.
    const latest = observed ?? (response?.config && response.configRevision
      ? { config: response.config, revision: response.configRevision } : undefined);
    onResult(message, result.outcome === 'unknown', latest);
    if (result.outcome === 'blocked') setDialogStatus(message);
    else setConfirmation(null);
  };
  const requestApply = () => {
    if (!intent) return;
    const guard = canPerform(controller.getSnapshot(), intent, controller.getSnapshot().now);
    if (!guard.allowed) { onResult(guard.reason!, false); return; }
    if (intent.policy.permissive) { setDialogStatus(''); setConfirmation(intent); }
    else void apply(intent);
  };
  return <div className="settings-content network-observations">
    {snapshot.blockedUnknownKeys.includes('network:apply') && <Feedback scope="Network recovery"
      message={`Outcome unknown. Explicitly Refresh the saved policy, VM and firewall observations before retrying. ${effective?.error ?? ''}`} />}
    <section aria-label="Saved network policy">
      <h3>Saved network policy</h3>
      <p>{saved ? saved.permissive ? 'Unrestricted — allow all outbound network access' : 'Restricted — allow configured domains' : 'Saved policy unknown'}</p>
      <p>Extra domains: {saved?.egress.join(', ') || 'none'}</p>
      <p className="revision">Revision: <code>{revision ?? 'unknown'}</code></p>
      <p>Apply uses these saved values. Unsaved draft edits are excluded.</p>
      <div className="settings-buttons">
        <button type="button" disabled={!eligibility.allowed} onClick={requestApply}>Apply saved network policy</button>
        <button type="button" disabled={!eligibility.allowed} onClick={requestApply}>Refresh DNS</button>
      </div>
      {!eligibility.allowed && <p>{eligibility.reason}</p>}
    </section>
    <section aria-label="Effective network policy">
      <h3>Effective network policy</h3>
      <p>{observed && effective?.mode === 'on' ? 'ON · enforcing restricted outbound access' :
        observed && effective?.mode === 'off' ? 'OFF · unrestricted outbound access' : 'Effective policy unknown. Refresh to observe the firewall.'}</p>
      {!observed && <p>Last known: {effective?.mode === 'on' ? 'ON' : effective?.mode === 'off' ? 'OFF' : 'unavailable'} · observation stale or unavailable.</p>}
      {effective?.error && <p>{effective.error}</p>}
      {observed && effective?.egress && <p>Observed extra domains: {effective.egress.join(', ') || 'none'}</p>}
      {differs && <p>Saved and effective policies differ. Saved changes have not been fully applied.</p>}
      <p>Saving does not change the firewall. Applying never restarts workers. Effective state is reported only from observations.</p>
    </section>
    <details><summary>Base allowed domains</summary>
      {snapshot.baseDomainsError ? <p>Base domain observation unavailable: {snapshot.baseDomainsError}</p> :
        snapshot.baseDomains ? <ul>{snapshot.baseDomains.map(domain => <li key={domain}>{domain}</li>)}</ul> : <p>Base domains unknown.</p>}
      <p>Read from the firewall source, including supported agent providers. Extra domains extend this list.</p>
    </details>
    <Dialog open={confirmation !== null} title="Allow all outbound network access" busy={busy} onCancel={() => setConfirmation(null)}>
      <p>This applies the reviewed saved Unrestricted policy to {confirmation?.expectedVm}.</p>
      <p>Saved extra domains: {confirmation?.policy.egress.join(', ') || 'none'}</p>
      <p className="revision">Saved revision: <code>{confirmation?.expectedRevision}</code></p>
      <p role="status">{dialogStatus}</p>
      <div className="dialog-actions">
        <button type="button" disabled={busy} onClick={() => setConfirmation(null)}>Cancel</button>
        <button type="button" disabled={busy} onClick={() => { if (confirmation) void apply(confirmation); }}>Apply saved policy</button>
      </div>
    </Dialog>
  </div>;
}
