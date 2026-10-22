import type { Agent, Environment, Filter, Intent, Observation } from './api.js';
import type { AppSnapshot, Immutable } from './controller.js';

export interface Eligibility {
  allowed: boolean;
  reason: string | null;
}
export interface WorkerView {
  key: string;
  env?: Immutable<Environment>;
  agent?: Immutable<Agent>;
  status: string;
  repositoryStatus: string;
  uncommitted: number | null;
  unpushed: number | null;
  running: boolean;
  absent: boolean;
  changes: boolean;
  copy: Eligibility;
  remove: Eligibility;
  command: string | null;
  recreate: boolean;
}
const allowed: Eligibility = Object.freeze({ allowed: true, reason: null });
const denied = (reason: string): Eligibility => ({ allowed: false, reason });

export function timestamp(value: string | null | undefined): number | null {
  // Date.parse accepts local times and rolls impossible dates into the next month.
  // Server timestamps are ISO instants; neither interpretation supplies valid evidence.
  const date = value?.match(/^(\d{4})-(\d{2})-(\d{2})T(?:[01]\d|2[0-3]):[0-5]\d:[0-5]\d(?:\.\d{1,9})?(?:Z|[+-](?:[01]\d|2[0-3]):[0-5]\d)$/);
  if (!date) return null;
  const year = Number(date[1]), month = Number(date[2]), day = Number(date[3]);
  if (month < 1 || month > 12 || day < 1 || day > new Date(Date.UTC(year, month, 0)).getUTCDate()) return null;
  const result = value ? Date.parse(value) : NaN;
  return Number.isFinite(result) ? result : null;
}
export function fresh(at: number | null, now: number): boolean {
  return at !== null && now - at >= 0 && now - at < 60_000;
}
export function current(source: Immutable<Observation> | undefined, now: number): boolean {
  return !!source && !source.error && fresh(timestamp(source.observedAt), now);
}
export function identityMatches(snapshot: AppSnapshot): boolean {
  const state = snapshot.state;
  return !!state && !snapshot.stateError && state.identity.matches && !state.identity.error &&
    !!snapshot.boundVm && state.workspace.name === snapshot.boundVm &&
    state.config?.name === snapshot.boundVm && !state.configError;
}
export function vmCurrent(snapshot: AppSnapshot, now: number): boolean {
  return !!snapshot.state && !snapshot.stateError && current(snapshot.state.vm, now);
}
export function agentsCurrent(snapshot: AppSnapshot, now: number): boolean {
  return !!snapshot.agents && !snapshot.agentError && fresh(snapshot.agentAt, now) &&
    snapshot.agents.identity.matches && !snapshot.agents.identity.error &&
    snapshot.state?.vm.status === 'Running';
}
export function inventoryCurrent(snapshot: AppSnapshot, now: number): boolean {
  const inventory = snapshot.state?.inventory;
  return vmCurrent(snapshot, now) && snapshot.state?.vm.status === 'Running' &&
    !!inventory?.inventoryKnown && current(inventory, now) && !inventory.repositoryError &&
    fresh(timestamp(inventory.repositoryObservedAt), now);
}
export function countsCurrent(snapshot: AppSnapshot, now: number): boolean {
  const inventory = snapshot.state?.inventory;
  return inventoryCurrent(snapshot, now) && !!inventory && !inventory.countsError &&
    fresh(timestamp(inventory.countsObservedAt), now);
}
function sessionsCurrent(snapshot: AppSnapshot, now: number): boolean {
  const inventory = snapshot.state?.inventory;
  return inventoryCurrent(snapshot, now) && !!inventory && !inventory.sessionError &&
    fresh(timestamp(inventory.sessionObservedAt), now);
}
export function sameSet(a: readonly string[], b: readonly string[]): boolean {
  return a.length === b.length && new Set(a).size === a.length &&
    new Set(b).size === b.length && a.every(value => b.includes(value));
}
export function operationKey(intent: Immutable<Intent>): string {
  switch (intent.kind) {
    case 'save': return 'config:save';
    case 'network': return 'network:apply';
    case 'root': return 'root:main';
    case 'vm': return `vm:${intent.action}`;
    case 'create': return `create:${intent.name}`;
    case 'remove': return `remove:${intent.name}`;
  }
}
export function validWorkerName(name: string): boolean {
  return /^[a-z0-9][a-z0-9_-]{0,62}$/.test(name) && !['main', 'stack', 'workspace', 'all'].includes(name);
}
function validRepoName(name: string): boolean {
  return /^[A-Za-z0-9._-]+$/.test(name) && name !== '.' && name !== '..';
}
function hasPane(agent?: Immutable<Agent>): boolean {
  return !!agent?.pane || !!agent?.candidates?.some(candidate => candidate.pane);
}
function merged(snapshot: AppSnapshot) {
  const rows = new Map<string, { key: string; env?: Immutable<Environment>; agent?: Immutable<Agent> }>();
  for (const env of snapshot.environments) {
    const key = env.kind === 'workspace' ? 'main' : env.name;
    rows.set(key, { key, env });
  }
  for (const agent of snapshot.agents?.agents ?? []) {
    const row = rows.get(agent.target) ?? { key: agent.target };
    rows.set(agent.target, { ...row, agent });
  }
  if (!rows.has('main')) rows.set('main', { key: 'main' });
  return [...rows.values()].sort((a, b) => a.key === 'main' ? -1 : b.key === 'main' ? 1 : a.key.localeCompare(b.key));
}
type Row = ReturnType<typeof merged>[number];
function knownScope(row: Row): boolean {
  return row.env?.kind === 'worker' && row.env.scopeKnown && !row.env.managementReason &&
    validWorkerName(row.key) && !!row.env.repos.length && row.env.repos.every(repo => validRepoName(repo.repo));
}
function absent(snapshot: AppSnapshot, row: Row, now: number): boolean {
  return agentsCurrent(snapshot, now) && sessionsCurrent(snapshot, now) && row.env?.sessionLive === false &&
    !hasPane(row.agent) && !['running', 'ambiguous', 'unknown'].includes(row.agent?.state ?? '');
}
function recreate(snapshot: AppSnapshot, row: Row, now: number): boolean {
  return knownScope(row) && countsCurrent(snapshot, now) && absent(snapshot, row, now);
}
function removable(snapshot: AppSnapshot, row: Row, now: number): Eligibility {
  if (!knownScope(row)) return denied(row.env?.managementReason || 'Known saved worker scope is required.');
  if (!countsCurrent(snapshot, now) || !agentsCurrent(snapshot, now)) return denied('Repository or agent observations are stale or unknown. Refresh before Remove.');
  if (!row.env!.repos.every(repo => current(repo, now) && repo.clean === true && repo.uncommitted === 0 && repo.unpushed === 0)) {
    return denied('Save and push changes, then refresh preservation observations before Remove.');
  }
  return allowed;
}
function busy(snapshot: AppSnapshot): boolean {
  return ['waiting', 'submitting', 'observing'].includes(snapshot.operation.phase);
}
export function canPerform(snapshot: AppSnapshot, intent: Intent, now: number): Eligibility {
  if (snapshot.refreshing) return denied('Explicit Refresh is still observing. Wait before acting.');
  if (busy(snapshot)) return denied('Another operation owns the request slot.');
  if (!snapshot.visible) return denied('Return to the visible workspace before acting.');
  if (!identityMatches(snapshot) || intent.expectedVm !== snapshot.boundVm) return denied('Workspace identity changed or is unavailable. Reopen the intended workspace.');
  if (snapshot.blockedUnknownKeys.includes(operationKey(intent))) return denied('Outcome unknown. Explicitly refresh the required observations before retrying.');
  if (intent.kind === 'save') {
    if (!intent.expectedRevision || !snapshot.state?.configRevision || snapshot.state.configError) return denied('Saved configuration is unavailable.');
    // Keep the draft revision: the server returns a structured conflict for an external edit.
    return Object.keys(intent.changes).length ? allowed : denied('No changed values to save.');
  }
  if (!vmCurrent(snapshot, now)) return denied('VM observation is stale or unknown. Refresh first.');
  if (intent.kind === 'vm') {
    const required = intent.action === 'start' ? 'Stopped' : 'Running';
    return snapshot.state?.vm.status === required ? allowed : denied(`VM must be ${required} for this action.`);
  }
  if (snapshot.state?.vm.status !== 'Running') return denied('The VM must be running.');
  if (intent.kind === 'network') {
    const state = snapshot.state;
    return !!state.configRevision && intent.expectedRevision === state.configRevision &&
      !!state.config && intent.policy.permissive === state.config.permissive &&
      JSON.stringify(intent.policy.egress) === JSON.stringify(state.config.egress) ? allowed : denied('Saved network policy changed. Review it before applying.');
  }
  if (snapshot.route !== 'workspace' || !agentsCurrent(snapshot, now)) return denied('Current Workspace agent discovery is required.');
  const rows = merged(snapshot);
  if (intent.kind === 'root') {
    const root = rows.find(row => row.key === 'main')?.agent;
    return !root || ['shell', 'dead', 'missing'].includes(root.state) ? allowed : denied('The orchestrator is running, ambiguous or unavailable.');
  }
  if (!inventoryCurrent(snapshot, now)) return denied('Repository inventory is stale or unknown.');
  if (!validWorkerName(intent.name)) return denied('Unsupported or reserved worker name.');
  const row = rows.find(row => row.key === intent.name);
  if (intent.kind === 'create') {
    if (row) return denied('Worker already exists. Inspect its saved scope before creating again.');
    if (!sameSet(intent.availableRepos, snapshot.state!.availableRepos)) return denied('Repository availability changed. Review the selection.');
    if (!intent.repos.length || new Set(intent.repos).size !== intent.repos.length ||
      intent.repos.some(repo => !validRepoName(repo) || !intent.availableRepos.includes(repo))) return denied('Selected repositories are unavailable or invalid.');
    return allowed;
  }
  if (!row || !sameSet(intent.repos, row.env?.repos.map(repo => repo.repo) ?? [])) return denied('Saved worker scope changed. Review it before removal.');
  return removable(snapshot, row, now);
}
function shellQuote(value: string): string {
  return "'" + value.replace(/'/g, "'\"'\"'") + "'";
}
function command(snapshot: AppSnapshot, row: Row, now: number): string | null {
  const dir = snapshot.state?.workspace.dir;
  if (!identityMatches(snapshot) || !dir || !/^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$/.test(row.key)) return null;
  let suffix = 'attach' + (row.key === 'main' ? '' : ' ' + shellQuote(row.key));
  if (recreate(snapshot, row, now)) {
    // The CLI reconnects the saved set when no single repository is specified.
    const repos = row.env!.repos;
    suffix = 'worker ' + shellQuote(row.key) + (repos.length === 1 ? ' ' + shellQuote(repos[0].repo) : '');
  }
  return 'cd -- ' + shellQuote(dir) + ' && floatie ' + suffix;
}
export function commandFor(snapshot: AppSnapshot, target: string): string | null {
  const row = merged(snapshot).find(row => row.key === target);
  return row ? command(snapshot, row, snapshot.now) : null;
}
function status(snapshot: AppSnapshot, row: Row, now: number): string {
  if (!agentsCurrent(snapshot, now)) return 'Status unavailable · last observation stale';
  const agent = row.agent;
  if (agent?.state === 'ambiguous') return 'Multiple agents · terminal present';
  if (agent?.state === 'running') return (agent.harness === 'codex' ? 'Codex' : agent.harness === 'claude' ? 'Claude' : agent.harness || 'Agent') + ' running · terminal present';
  if (agent?.state === 'unknown') return 'Agent status unknown' + (hasPane(agent) ? ' · terminal present' : '');
  if (agent?.state === 'dead') return hasPane(agent) ? 'Agent exited — terminal retained' : row.key === 'main' ? 'Not started' : 'Agent exited · terminal absent';
  if (hasPane(agent)) return 'Terminal present — no agent observed';
  if (sessionsCurrent(snapshot, now) && row.env?.sessionLive === true) return 'Terminal present · agent not observed';
  if (absent(snapshot, row, now)) return row.key === 'main' ? 'Not started' : 'Terminal absent · no agent observed';
  return 'Terminal status unavailable';
}
export function selectWorkers(snapshot: AppSnapshot, now: number): WorkerView[] {
  return merged(snapshot).map(row => {
    const repos = row.env?.repos ?? [];
    const known = countsCurrent(snapshot, now) && !!repos.length && repos.every(repo =>
      current(repo, now) && typeof repo.clean === 'boolean' && repo.uncommitted !== null && repo.unpushed !== null);
    const uncommitted = known ? repos.reduce((sum, repo) => sum + repo.uncommitted!, 0) : null;
    const unpushed = known ? repos.reduce((sum, repo) => sum + repo.unpushed!, 0) : null;
    const changes = known && (uncommitted! > 0 || unpushed! > 0);
    const literal = command(snapshot, row, now);
    const intent: Intent = { kind: 'remove', expectedVm: snapshot.boundVm ?? '', name: row.key, repos: repos.map(repo => repo.repo) };
    return {
      ...row, status: status(snapshot, row, now), uncommitted, unpushed,
      repositoryStatus: !known ? 'Repository status stale or unknown' : changes ?
        [uncommitted ? `${uncommitted} uncommitted` : '', unpushed ? `${unpushed} unpushed` : ''].filter(Boolean).join(' · ') : 'Clean',
      running: agentsCurrent(snapshot, now) && row.agent?.state === 'running',
      absent: absent(snapshot, row, now), changes,
      copy: literal ? allowed : denied('Workspace identity or target is unavailable.'),
      remove: canPerform(snapshot, intent, now), command: literal, recreate: recreate(snapshot, row, now),
    };
  });
}
export function filterWorkers(rows: WorkerView[], search: string, filter: Filter): WorkerView[] {
  const query = search.trim().toLowerCase();
  return rows.filter(row => row.key !== 'main' &&
    (!query || [row.key, ...(row.env?.repos.map(repo => repo.repo) ?? [])].some(value => value.toLowerCase().includes(query))) &&
    (filter === 'all' || row[filter]));
}
