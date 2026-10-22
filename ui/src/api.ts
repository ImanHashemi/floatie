export type Section = 'resources' | 'repositories' | 'agents' | 'network' | 'credentials';
export type EditableSection = Exclude<Section, 'credentials'>;
export type Route = 'workspace' | `settings/${Section}`;
export type Filter = 'all' | 'running' | 'absent' | 'changes';
export type Harness = 'codex' | 'claude';

export interface Config {
  name: string | null;
  cpus: number | null;
  memoryGiB: number | null;
  permissive: boolean;
  repos: string[];
  plugins: string[];
  egress: string[];
  gitUserName: string | null;
  gitUserEmail: string | null;
}
export type ConfigChanges = Partial<Omit<Config, 'name'>>;
export type Intent = { expectedVm: string } & (
  | { kind: 'root'; harness: Harness }
  | { kind: 'create'; name: string; repos: string[]; availableRepos: string[] }
  | { kind: 'remove'; name: string; repos: string[] }
  | { kind: 'vm'; action: 'start' | 'stop' | 'reload' }
  | { kind: 'network'; expectedRevision: string; policy: { permissive: boolean; egress: string[] } }
  | { kind: 'save'; section: EditableSection; expectedRevision: string; changes: ConfigChanges }
);
export type OperationResult =
  | { outcome: 'blocked'; reason: string }
  | { outcome: 'completed' | 'failed' | 'unknown'; response: ActionResponse };
export interface Observation {
  observedAt: string | null;
  error: string | null;
}
export interface Identity {
  matches: boolean;
  error: string | null;
}
export interface Repository extends Observation {
  repo: string;
  path: string | null;
  branch: string | null;
  clean: boolean | null;
  uncommitted: number | null;
  unpushed: number | null;
}
export interface Environment {
  name: string;
  kind: 'workspace' | 'worker' | 'unknown';
  session: string;
  sessionLive: boolean | null;
  scopeKnown: boolean;
  managementReason: string | null;
  repos: Repository[];
}
export interface Inventory extends Observation {
  inventoryKnown: boolean;
  repositoryObservedAt: string | null;
  repositoryError: string | null;
  sessionObservedAt: string | null;
  sessionError: string | null;
  countsObservedAt: string | null;
  countsError: string | null;
}
export interface StateResponse {
  workspace: { name: string; dir: string };
  identity: Identity;
  vm: Observation & {
    status: string;
    cpus?: number | null;
    memoryGiB?: number | null;
    diskPct?: number | null;
    diskObservedAt?: string | null;
    diskError?: string | null;
  };
  firewall: Observation & { mode: 'on' | 'off' | 'unknown'; permissiveConfigured: boolean | null; egress: string[] | null };
  environments: Environment[];
  availableRepos: string[];
  inventory: Inventory;
  config: Config | null;
  configRevision: string | null;
  configError: string | null;
  secrets: { key: string; state: 'set' | 'unset' | 'placeholder' }[];
  generatedAt: string;
}
export interface Agent {
  target: string;
  state: 'running' | 'shell' | 'dead' | 'missing' | 'ambiguous' | 'unknown';
  pane?: string | null;
  harness?: string | null;
  cwd?: string | null;
  candidates?: Agent[];
}
export interface AgentListResponse {
  ok: true;
  agents: Agent[];
  identity: Identity;
  observedAt?: string | null;
}
export interface ActionResponse {
  ok: boolean;
  outcome?: 'completed' | 'failed' | 'unknown';
  error?: string;
  output?: string;
  exitCode?: number | null;
  fixture?: boolean;
  started?: boolean;
  target?: string;
  pane?: string | null;
  harness?: string | null;
  state?: Agent['state'];
  config?: Config;
  configRevision?: string;
}
export interface Api {
  state(signal?: AbortSignal): Promise<StateResponse>;
  agents(signal?: AbortSignal): Promise<AgentListResponse>;
  baseDomains(signal?: AbortSignal): Promise<{ domains: string[] }>;
  mutate(intent: Intent): Promise<ActionResponse>;
}
export class ApiError extends Error {
  constructor(public readonly status: number, public readonly response: ActionResponse | null) {
    super(`Request failed (HTTP ${status})`);
  }
}

// Project only known wire fields. Errors never include raw JSON, URLs or fetch exceptions.
function invalid(): never {
  throw new Error('Invalid observation response');
}
function object(value: unknown): Record<string, unknown> {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return invalid();
  return value as Record<string, unknown>;
}
function string(value: unknown): string {
  return typeof value === 'string' ? value : invalid();
}
function boolean(value: unknown): boolean {
  return typeof value === 'boolean' ? value : invalid();
}
function nullableString(value: unknown): string | null {
  return value === null ? null : string(value);
}
function nullableBoolean(value: unknown): boolean | null {
  return value === null ? null : boolean(value);
}
function nullableNumber(value: unknown, integer = false): number | null {
  if (value === null) return null;
  if (typeof value !== 'number' || !Number.isFinite(value) || value < 0 || (integer && !Number.isInteger(value))) return invalid();
  return value;
}
function array<T>(value: unknown, parse: (item: unknown) => T): T[] {
  return Array.isArray(value) ? value.map(parse) : invalid();
}
function strings(value: unknown): string[] {
  return array(value, string);
}
function unique<T>(values: T[], key: (value: T) => string): T[] {
  return new Set(values.map(key)).size === values.length ? values : invalid();
}
function observation(value: unknown): Observation {
  const item = object(value);
  return { observedAt: nullableString(item.observedAt), error: nullableString(item.error) };
}
function identity(value: unknown): Identity {
  const item = object(value);
  return { matches: boolean(item.matches), error: nullableString(item.error) };
}
export function parseConfig(value: unknown): Config {
  const item = object(value);
  return {
    name: nullableString(item.name), cpus: nullableNumber(item.cpus, true), memoryGiB: nullableNumber(item.memoryGiB),
    permissive: boolean(item.permissive), repos: strings(item.repos), plugins: strings(item.plugins), egress: strings(item.egress),
    gitUserName: nullableString(item.gitUserName), gitUserEmail: nullableString(item.gitUserEmail),
  };
}
function repository(value: unknown): Repository {
  const item = object(value);
  const repo: Repository = {
    repo: string(item.repo), path: nullableString(item.path ?? null), branch: nullableString(item.branch ?? null),
    clean: nullableBoolean(item.clean), uncommitted: nullableNumber(item.uncommitted, true), unpushed: nullableNumber(item.unpushed, true),
    observedAt: nullableString(item.observedAt), error: nullableString(item.error ?? null),
  };
  if (!repo.repo || (repo.clean !== null && repo.uncommitted !== null && repo.unpushed !== null &&
    repo.clean !== (repo.uncommitted === 0 && repo.unpushed === 0))) invalid();
  return repo;
}
function environment(value: unknown): Environment {
  const item = object(value);
  const kind = item.kind;
  if (kind !== 'worker' && kind !== 'workspace' && kind !== 'unknown') return invalid();
  return {
    name: string(item.name), kind, session: string(item.session), sessionLive: nullableBoolean(item.sessionLive),
    scopeKnown: boolean(item.scopeKnown), managementReason: nullableString(item.managementReason),
    repos: unique(array(item.repos, repository), repo => repo.repo),
  };
}
export function parseState(value: unknown): StateResponse {
  const item = object(value);
  if (item.error) invalid();
  const workspace = object(item.workspace);
  const vm = object(item.vm);
  const firewall = object(item.firewall);
  const inventory = object(item.inventory);
  const mode = firewall.mode;
  if (mode !== 'on' && mode !== 'off' && mode !== 'unknown') return invalid();
  const configError = nullableString(item.configError);
  const config = configError ? null : parseConfig(item.config);
  return {
    workspace: { name: string(workspace.name), dir: string(workspace.dir) }, identity: identity(item.identity),
    vm: {
      ...observation(vm), status: string(vm.status),
      ...Object.fromEntries(['cpus', 'memoryGiB', 'diskPct'].filter(key => key in vm).map(key => [key, nullableNumber(vm[key])])),
      ...('diskObservedAt' in vm ? { diskObservedAt: nullableString(vm.diskObservedAt) } : {}),
      ...('diskError' in vm ? { diskError: nullableString(vm.diskError) } : {}),
    },
    firewall: {
      ...observation(firewall), mode, permissiveConfigured: nullableBoolean(firewall.permissiveConfigured),
      egress: firewall.egress === null ? null : strings(firewall.egress),
    },
    inventory: {
      ...observation(inventory), inventoryKnown: boolean(inventory.inventoryKnown),
      // These sources are absent when the live provider cannot reach the VM.
      // Missing timestamps remain unknown; they never borrow the state receipt time.
      repositoryObservedAt: nullableString(inventory.repositoryObservedAt ?? null), repositoryError: nullableString(inventory.repositoryError ?? null),
      sessionObservedAt: nullableString(inventory.sessionObservedAt ?? null), sessionError: nullableString(inventory.sessionError ?? null),
      countsObservedAt: nullableString(inventory.countsObservedAt), countsError: nullableString(inventory.countsError),
    },
    environments: unique(array(item.environments, environment), env => env.kind === 'workspace' ? 'main' : env.name),
    availableRepos: unique(strings(item.availableRepos), name => name),
    config, configRevision: nullableString(item.configRevision), configError,
    secrets: array(item.secrets, value => {
      const secret = object(value);
      const state = secret.state;
      if (state !== 'set' && state !== 'unset' && state !== 'placeholder') return invalid();
      return { key: string(secret.key), state };
    }),
    generatedAt: string(item.generatedAt),
  };
}
function agent(value: unknown, candidate = false): Agent {
  const item = object(value);
  const target = string(item.target);
  const state = string(item.state) as Agent['state'];
  if (!/^[a-zA-Z0-9_-]+$/.test(target) || !['running', 'shell', 'dead', 'missing', 'ambiguous', 'unknown'].includes(state)) invalid();
  const result: Agent = { target, state };
  for (const key of ['pane', 'harness', 'cwd'] as const) {
    if (key in item) result[key] = nullableString(item[key]);
  }
  if (result.pane != null && !/^%\d+$/.test(result.pane)) invalid();
  if ('candidates' in item) {
    if (candidate) invalid();
    result.candidates = array(item.candidates, child => agent(child, true));
    if (result.candidates.some(child => child.target !== target)) invalid();
  }
  return result;
}
export function parseAgents(value: unknown): AgentListResponse {
  const item = object(value);
  if (item.ok !== true || item.error) return invalid();
  return {
    ok: true, agents: unique(array(item.agents, value => agent(value)), agent => agent.target), identity: identity(item.identity),
    ...('observedAt' in item ? { observedAt: nullableString(item.observedAt) } : {}),
  };
}
export function parseAction(value: unknown): ActionResponse {
  const item = object(value);
  const ok = boolean(item.ok);
  const outcome = 'outcome' in item ? string(item.outcome) : (ok ? 'completed' : 'failed');
  if (outcome !== 'completed' && outcome !== 'failed' && outcome !== 'unknown') invalid();
  if ((ok && outcome === 'failed') || (!ok && outcome === 'completed')) invalid();
  const result: ActionResponse = { ok, outcome };
  for (const key of ['error', 'output', 'target', 'configRevision'] as const) {
    if (key in item) result[key] = string(item[key]);
  }
  for (const key of ['pane', 'harness'] as const) {
    if (key in item) result[key] = nullableString(item[key]);
  }
  for (const key of ['started', 'fixture'] as const) {
    if (key in item) result[key] = boolean(item[key]);
  }
  if ('exitCode' in item) {
    if (item.exitCode !== null && (typeof item.exitCode !== 'number' || !Number.isInteger(item.exitCode))) invalid();
    result.exitCode = item.exitCode as number | null;
  }
  if ('state' in item) result.state = agent({ target: item.target, state: item.state, pane: item.pane, harness: item.harness }).state;
  if ('config' in item) result.config = parseConfig(item.config);
  return result;
}
export function unknownResponse(): ActionResponse {
  return { ok: false, outcome: 'unknown', error: 'Response unavailable. Outcome unknown; explicitly refresh before retrying.' };
}

export function createApi(token: string, fetcher: typeof fetch): Api {
  async function request(path: string, body: unknown | undefined, signal?: AbortSignal): Promise<unknown> {
    const response = await fetcher(path, {
      method: body === undefined ? 'GET' : 'POST',
      headers: { 'X-Floatie-Token': token, 'Content-Type': 'application/json' },
      ...(body === undefined ? {} : { body: JSON.stringify(body) }), signal,
      mode: 'same-origin', credentials: 'same-origin', cache: 'no-store', referrerPolicy: 'no-referrer', redirect: 'error',
    });
    let value: unknown;
    try {
      value = await response.json();
    } catch {
      throw new ApiError(response.status, null);
    }
    if (!response.ok) {
      let failure: ActionResponse | null = null;
      try {
        const error = object(value);
        if (error.ok !== false && !(error.ok === undefined && typeof error.error === 'string')) invalid();
        failure = parseAction({ ...error, ok: false });
      } catch {
        // Unreadable is not a known failure.
      }
      throw new ApiError(response.status, failure);
    }
    return value;
  }
  async function read<T>(path: string, body: unknown | undefined, parse: (value: unknown) => T, signal?: AbortSignal): Promise<T> {
    try {
      return parse(await request(path, body, signal));
    } catch (error) {
      if (error instanceof ApiError) throw error;
      throw new Error('Observation unavailable or malformed');
    }
  }
  return {
    state: signal => read('/api/state', undefined, parseState, signal),
    agents: signal => read('/api/agents', { action: 'list' }, parseAgents, signal),
    baseDomains: signal => read('/api/base-domains', undefined, value => ({ domains: strings(object(value).domains) }), signal),
    async mutate(intent) {
      const expectedVm = intent.expectedVm;
      let path = '/api/action';
      let body: unknown;
      switch (intent.kind) {
        case 'root':
          path = '/api/agents';
          body = { action: 'start', target: 'main', harness: intent.harness, expectedVm };
          break;
        case 'save':
          path = '/api/config';
          body = { expectedVm, expectedRevision: intent.expectedRevision, changes: intent.changes };
          break;
        case 'create':
          body = { action: 'worker-create', params: { name: intent.name, repos: intent.repos, availableRepos: intent.availableRepos }, expectedVm };
          break;
        case 'remove':
          body = { action: 'worker-rm', params: { name: intent.name }, expectedVm };
          break;
        case 'network':
          body = { action: intent.policy.permissive ? 'firewall-off' : 'firewall-on', params: {}, expectedRevision: intent.expectedRevision, expectedVm };
          break;
        case 'vm':
          body = { action: intent.action, params: {}, expectedVm };
          break;
      }
      try {
        return parseAction(await request(path, body));
      } catch (error) {
        if (error instanceof ApiError && (error.response || error.status >= 400)) throw error;
        return unknownResponse();
      }
    },
  };
}
