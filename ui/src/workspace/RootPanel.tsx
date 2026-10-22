import { useState } from 'react';
import type { Harness, Intent } from '../api.js';
import type { AppSnapshot, Controller } from '../controller.js';
import { agentsCurrent, canPerform } from '../model.js';
import type { WorkerView } from '../model.js';
import { Command } from '../components/Command.js';
import { Buoy } from '../App.js';
import { copyReason, RepositoryDetails } from './WorkerRow.js';

export function RootPanel({ row, snapshot, controller, expanded, onExpand, onResult }: {
  row: WorkerView; snapshot: AppSnapshot; controller: Controller; expanded: boolean;
  onExpand(open: boolean): void; onResult(message: string, output?: string): void;
}) {
  const [harness, setHarness] = useState<Harness>('codex');
  const intent: Intent = { kind: 'root', expectedVm: snapshot.boundVm ?? '', harness };
  const eligible = canPerform(snapshot, intent, snapshot.now);
  const launchable = agentsCurrent(snapshot, snapshot.now) && (!row.agent || ['shell', 'dead', 'missing'].includes(row.agent.state));
  const busy = ['waiting', 'submitting', 'observing'].includes(snapshot.operation.phase);
  const start = async () => {
    const current = controller.getSnapshot();
    if (!canPerform(current, intent, Date.now()).allowed) return;
    const result = await controller.perform(intent);
    if (result.outcome === 'blocked') onResult(result.reason);
    else onResult(result.outcome === 'unknown' ? 'Outcome unknown. Refresh observations before trying again.' : result.response.ok
      ? `${result.response.harness === 'claude' ? 'Claude' : result.response.harness === 'codex' ? 'Codex' : 'Agent'} ${result.response.started === false ? 'already running; reused existing agent.' : 'started.'}`
      : result.response.error || 'Orchestrator start failed.',
      [result.response.error, result.response.output].filter(Boolean).join('\n'));
  };
  return <section id="orchestrator" aria-label="Orchestrator"><article className="root-strip" data-worker="main">
    <Buoy large /><div className="root-name"><h3>Orchestrator</h3><p>Root workspace · One conversation for the whole workspace.</p></div>
    <div className="process">{row.status}</div>
    <Command value={row.command} disabledReason={copyReason(row, snapshot)} hideButton={row.absent}
      expanded={expanded} onExpandedChange={onExpand}>
      <RepositoryDetails row={row} snapshot={snapshot} />
      <p>To switch: save a handoff, exit the old agent in its terminal, refresh, then start the other harness. Selecting a harness never launches or replaces an agent.</p>
    </Command>
    <div className="root-launch" hidden={!launchable}>
      <label htmlFor="root-harness">Start orchestrator with</label>
      <select id="root-harness" value={harness} disabled={busy || snapshot.refreshing} onChange={event => setHarness(event.target.value as Harness)}>
        <option value="codex">Codex</option><option value="claude">Claude</option>
      </select>
      <button id="root-start" type="button" disabled={!eligible.allowed} onClick={() => void start()}>
        {row.agent?.state === 'dead' && row.agent.pane ? 'Restart orchestrator' : 'Start orchestrator'}
      </button>
      {!eligible.allowed && <p>{eligible.reason}</p>}
    </div>
  </article></section>;
}
