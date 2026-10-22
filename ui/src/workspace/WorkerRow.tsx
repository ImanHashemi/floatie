import type { AppSnapshot } from '../controller.js';
import type { WorkerView } from '../model.js';
import { countsCurrent, current, identityMatches, validWorkerName } from '../model.js';
import { Command } from '../components/Command.js';

export function RepositoryDetails({ row, snapshot }: { row: WorkerView; snapshot: AppSnapshot }) {
  return <>
    <div className="repos">{row.env?.repos.length ? row.env.repos.map(repo => <div className="repository" key={repo.repo}>
      <strong>{repo.repo}</strong> <code>{repo.branch ?? 'Branch unknown'}</code>
      <span>{repo.uncommitted === null ? 'Uncommitted unknown' : `${repo.uncommitted} uncommitted`} · {repo.unpushed === null ? 'Unpushed unknown' : `${repo.unpushed} unpushed`}</span>
      <span>{countsCurrent(snapshot, snapshot.now) && current(repo, snapshot.now) ? 'Repository observed' : 'Repository status stale or unknown'}: <time dateTime={repo.observedAt ?? undefined}>{repo.observedAt ?? 'unknown time'}</time></span>
      {repo.path && <span className="literal">{repo.path}</span>}{repo.error && <p>{repo.error}</p>}
    </div>) : <p>Repository details unavailable. Inspect the terminal manually.</p>}</div>
    <p>Terminal: {row.env?.session ?? row.agent?.target ?? 'unknown'}{row.agent?.pane && ` · pane ${row.agent.pane}`}</p>
    {row.agent?.candidates?.map(agent => <p key={`${agent.target}:${agent.pane}`}>{agent.harness ?? 'Unknown agent'} · {agent.pane ?? 'Unknown pane'}</p>)}
    <p>Agent observation: {snapshot.agentError ?? (snapshot.agentAt === null ? 'Unknown' : new Date(snapshot.agentAt).toISOString())}</p>
    {row.env?.managementReason && <p>{row.env.managementReason}</p>}
  </>;
}

export function copyReason(row: WorkerView, snapshot: AppSnapshot): string | null {
  if (!snapshot.state?.workspace.dir) return 'Host workspace directory unavailable. Refresh before copying.';
  if (snapshot.stateError) return 'Observation unavailable. Refresh before copying.';
  if (!identityMatches(snapshot)) return 'Workspace identity unavailable; reopen the intended workspace before copying.';
  return row.copy.reason;
}
export function WorkerRow({ row, snapshot, expanded, onExpand, onRemove }: {
  row: WorkerView; snapshot: AppSnapshot; expanded: boolean; onExpand(open: boolean): void; onRemove(row: WorkerView): void;
}) {
  return <article className="worker-row" data-worker={row.key}>
    <div className="worker-name"><h3>{row.key}</h3><p>{row.env?.repos.map(repo => repo.repo).join(', ') || 'Repository details unavailable'}</p></div>
    <div className="process">{row.status}</div>
    <div className="changes">{row.repositoryStatus}</div>
    <Command value={row.command} disabledReason={copyReason(row, snapshot)} label={`Copy ${row.recreate ? 'recreate' : 'attach'} command`}
      expanded={expanded} onExpandedChange={onExpand} actions={<button type="button" className="remove" aria-label="Remove worker"
        title={row.remove.reason ?? undefined} disabled={!row.remove.allowed} onClick={() => onRemove(row)}>Remove</button>}>
      <RepositoryDetails row={row} snapshot={snapshot} />
      {row.recreate && <p className="recreate-note">Recreates the terminal for the saved repository set. Launch or resume the agent separately; conversation history is not resumed.</p>}
    </Command>
    {!row.remove.allowed && <p className="remove-note">{snapshot.stateError ? 'Observations unavailable. Refresh before Remove.' : row.remove.reason} {(!row.env?.scopeKnown || !row.env.repos.length || !validWorkerName(row.key) || row.env.managementReason) && 'Please inspect the saved worktrees manually.'}</p>}
  </article>;
}
