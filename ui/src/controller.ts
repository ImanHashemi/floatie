import { ApiError, parseAction, parseAgents, parseState, unknownResponse } from './api.js';
import type { Api, StateResponse, AgentListResponse, Environment, Intent, OperationResult, Route } from './api.js';
import { agentsCurrent, canPerform, countsCurrent, current, fresh, identityMatches, inventoryCurrent,
  operationKey, timestamp, vmCurrent } from './model.js';

export type Immutable<T> = T extends object ? { readonly [K in keyof T]: Immutable<T[K]> } : T;
export interface Clock {
  now(): number;
  setTimeout(fn: () => void, ms: number): unknown;
  clearTimeout(handle: unknown): void;
}
export interface AppSnapshot {
  readonly now: number;
  readonly state: Immutable<StateResponse> | null;
  readonly agents: Immutable<AgentListResponse> | null;
  readonly environments: readonly Immutable<Environment>[];
  readonly boundVm: string | null;
  readonly stateAt: number | null;
  readonly configReadSequence: number;
  readonly agentAt: number | null;
  readonly repositoryAt: number | null;
  readonly stateError: string | null;
  readonly agentError: string | null;
  readonly baseDomains: readonly string[] | null;
  readonly baseDomainsError: string | null;
  readonly route: Route;
  readonly visible: boolean;
  readonly refreshing: boolean;
  readonly operation: Immutable<{
    phase: 'idle' | 'waiting' | 'submitting' | 'observing' | 'completed' | 'failed' | 'unknown';
    intent: Intent | null;
    result: OperationResult | null;
  }>;
  readonly blockedUnknownKeys: readonly string[];
}
export interface Controller {
  start(): void;
  dispose(): void;
  subscribe(listener: () => void): () => void;
  getSnapshot(): AppSnapshot;
  setContext(route: Route, visible: boolean): void;
  refresh(mode: 'explicit' | 'background'): Promise<void>;
  perform(intent: Intent): Promise<PerformedOperation>;
}
export type PerformedOperation = Extract<OperationResult, { outcome: 'blocked' }> |
  (Exclude<OperationResult, { outcome: 'blocked' }> & { readonly configReadAtDispatch: number });

export function configObservedAfter(snapshot: AppSnapshot, result: PerformedOperation) {
  const state = snapshot.state;
  return result.outcome !== 'blocked' && snapshot.configReadSequence > result.configReadAtDispatch &&
    identityMatches(snapshot) && state?.config && state.configRevision
    ? { config: state.config, revision: state.configRevision } : undefined;
}
function freeze<T>(value: T): T {
  if (value && typeof value === 'object' && !Object.isFrozen(value)) {
    Object.values(value).forEach(freeze);
    Object.freeze(value);
  }
  return value;
}
interface ReadRequest {
  full: boolean;
  explicit: boolean;
  domains: boolean;
}

export function createController(api: Api, clock: Clock): Controller {
  let snapshot: AppSnapshot = freeze({
    now: clock.now(), state: null, agents: null, environments: [], boundVm: null,
    stateAt: null, configReadSequence: 0, agentAt: null, repositoryAt: null,
    stateError: null, agentError: null, baseDomains: null, baseDomainsError: null,
    route: 'workspace', visible: true, refreshing: false,
    operation: { phase: 'idle', intent: null, result: null }, blockedUnknownKeys: [],
  });
  const listeners = new Set<() => void>();
  const unknown = new Map<string, Intent>();
  let active = false;
  let lifetime = 0;
  let generation = 0;
  let timer: unknown = null;
  let abortRead: AbortController | null = null;
  let pending: ReadRequest | null = null;
  let draining: Promise<boolean> | null = null;
  let reservation: Promise<void> | null = null;

  function publish(patch: Partial<AppSnapshot>): void {
    const next = { ...snapshot, ...patch, now: clock.now() };
    if (JSON.stringify(next) === JSON.stringify(snapshot)) return;
    snapshot = freeze(next);
    for (const listener of listeners) listener();
  }
  function clearTimer(): void {
    if (timer !== null) clock.clearTimeout(timer);
    timer = null;
  }
  function schedule(): void {
    clearTimer();
    if (!active || !snapshot.visible || reservation || draining || pending) return;
    timer = clock.setTimeout(() => {
      timer = null;
      void refresh('background');
    }, 3000);
  }
  function enqueue(request: ReadRequest): void {
    pending = {
      full: request.full || !!pending?.full,
      explicit: request.explicit || !!pending?.explicit,
      domains: request.domains || !!pending?.domains,
    };
  }
  function invalidateRead(): void {
    generation++;
    abortRead?.abort();
  }
  function adoptState(state: StateResponse): void {
    const previous = snapshot.environments;
    let environments = previous;
    if (state.vm.status === 'Running') {
      if (state.inventory.inventoryKnown && !state.inventory.error) {
        environments = state.environments.map(env => {
          const old = previous.find(item => item.kind === env.kind && item.name === env.name);
          return { ...env, repos: env.repos.map(repo => {
            const retained = old?.repos.find(item => item.repo === repo.repo && item.path === repo.path);
            if (!retained || (!state.inventory.countsError && !repo.error)) return repo;
            // Retain counts only for the same membership and exact path, with the failure attached.
            return { ...repo, clean: retained.clean, uncommitted: retained.uncommitted,
              unpushed: retained.unpushed, observedAt: retained.observedAt,
              error: repo.error || state.inventory.countsError };
          }) };
        });
      } else {
        // A partial inventory can add a discovered row; it cannot prove an old row disappeared.
        const combined = new Map(previous.map(env => [`${env.kind}:${env.name}`, env]));
        for (const env of state.environments) {
          const key = `${env.kind}:${env.name}`;
          if (!combined.has(key)) combined.set(key, env);
        }
        environments = [...combined.values()];
      }
    }
    const boundVm = snapshot.boundVm ?? state.workspace.name;
    if (state.workspace.name !== boundVm || state.config?.name !== boundVm) {
      state = { ...state, identity: { matches: false, error: 'Workspace identity changed or is unavailable.' } };
    }
    const countAt = timestamp(state.inventory.countsObservedAt);
    // Count accepted config reads, never timestamps or opaque revision strings.
    const configAccepted = identityMatches({ ...snapshot, state, boundVm, stateError: null }) && !!state.configRevision;
    publish({ state, environments, boundVm, stateAt: clock.now(), stateError: null,
      configReadSequence: snapshot.configReadSequence + (configAccepted ? 1 : 0),
      repositoryAt: !state.inventory.countsError && !state.inventory.error && fresh(countAt, clock.now()) ? countAt : snapshot.repositoryAt,
      ...(state.vm.status === 'Running' ? {} : { agentError: 'Agent discovery unavailable until the VM is running.' }),
    });
  }
  function clearObservedUnknowns(stateRead: boolean, agentsRead: boolean): void {
    const now = clock.now();
    if (!stateRead || !identityMatches(snapshot)) return;
    for (const [key, intent] of unknown) {
      if (intent.expectedVm !== snapshot.boundVm) continue;
      let observed = false;
      switch (intent.kind) {
        case 'save':
          observed = !!snapshot.state?.configRevision && !snapshot.state.configError;
          break;
        case 'vm':
          observed = vmCurrent(snapshot, now);
          break;
        case 'root':
          observed = agentsRead && agentsCurrent(snapshot, now);
          break;
        case 'create':
          observed = inventoryCurrent(snapshot, now) && agentsRead && agentsCurrent(snapshot, now);
          break;
        case 'remove': {
          const target = snapshot.state?.environments.find(env => env.kind === 'worker' && env.name === intent.name);
          const targetObserved = !target || (target.scopeKnown && target.repos.length > 0 &&
            target.repos.every(repo => current(repo, now) && repo.clean !== null &&
              repo.uncommitted !== null && repo.unpushed !== null));
          observed = countsCurrent(snapshot, now) && targetObserved && agentsRead && agentsCurrent(snapshot, now);
          break;
        }
        case 'network':
          observed = snapshot.state?.vm.status === 'Running' && vmCurrent(snapshot, now) &&
            current(snapshot.state.firewall, now) && ['on', 'off'].includes(snapshot.state.firewall.mode) &&
            !!snapshot.state.configRevision && !snapshot.state.configError;
          break;
      }
      if (observed) unknown.delete(key);
    }
    publish({ blockedUnknownKeys: [...unknown.keys()] });
  }
  // Report generation acceptance separately from source failures recorded on the snapshot.
  async function read(request: ReadRequest): Promise<boolean> {
    const readGeneration = generation;
    const route = snapshot.route;
    const valid = () => active && snapshot.visible && generation === readGeneration;
    const abort = new AbortController();
    abortRead = abort;
    let stateRead = false;
    let agentsRead = false;
    const full = request.full || route !== 'workspace' || snapshot.stateError !== null ||
      !snapshot.state || snapshot.state.vm.status !== 'Running' ||
      !snapshot.agents || snapshot.agentError !== null;
    if (request.explicit) publish({ refreshing: true });
    try {
      if (full) {
        try {
          const state = parseState(await api.state(abort.signal));
          if (!valid()) return false;
          adoptState(state);
          stateRead = true;
        } catch {
          if (!valid()) return false;
          publish({ stateError: 'State observation unavailable or malformed.' });
        }
      }
      // Discovery is independent of preservation counts and can still be useful after a failed state read.
      if (valid() && route === 'workspace' && snapshot.state?.vm.status === 'Running') {
        try {
          const agents = parseAgents(await api.agents(abort.signal));
          if (!valid()) return false;
          const agentAt = 'observedAt' in agents ? timestamp(agents.observedAt) : clock.now();
          publish({ agents, agentAt, agentError: fresh(agentAt, clock.now()) ? null : 'Agent observation is stale or unknown.',
            ...(!agents.identity.matches && snapshot.state ? { state: { ...snapshot.state, identity: agents.identity } } : {}),
          });
          agentsRead = true;
        } catch {
          if (!valid()) return false;
          publish({ agentError: 'Agent discovery unavailable or malformed.' });
        }
      }
      if (valid() && request.domains && route === 'settings/network') {
        try {
          const result = await api.baseDomains(abort.signal);
          if (!valid()) return false;
          if (!Array.isArray(result.domains) || result.domains.some(domain => typeof domain !== 'string')) throw new Error('Invalid domains');
          publish({ baseDomains: [...result.domains], baseDomainsError: null });
        } catch {
          if (!valid()) return false;
          publish({ baseDomainsError: 'Base domains unavailable.' });
        }
      }
      if (valid() && request.explicit) clearObservedUnknowns(stateRead, agentsRead);
      return valid();
    } finally {
      if (abortRead === abort) abortRead = null;
      if (generation === readGeneration) publish({ refreshing: !!pending?.explicit });
    }
  }
  function drain(): Promise<boolean> {
    if (draining) return draining;
    if (!active || !snapshot.visible || reservation || !pending) return Promise.resolve(true);
    clearTimer();
    // Set the shared promise before any listener can enqueue work or reserve an operation.
    draining = Promise.resolve().then(async () => {
      let accepted = true;
      while (active && snapshot.visible && !reservation && pending) {
        const request = pending;
        pending = null;
        accepted = await read(request);
      }
      return accepted;
    }).finally(() => {
      draining = null;
      schedule();
    });
    return draining;
  }
  async function refresh(mode: 'explicit' | 'background'): Promise<void> {
    clearTimer();
    if (!active || !snapshot.visible) return;
    if (mode === 'explicit') publish({ refreshing: true });
    enqueue({ full: mode === 'explicit', explicit: mode === 'explicit', domains: mode === 'explicit' && snapshot.route === 'settings/network' });
    if (reservation) await reservation;
    await drain();
  }
  function eligible(intent: Intent) {
    // Only the operation owner bypasses its own reservation, never freshness or identity guards.
    return canPerform({ ...snapshot, refreshing: false, operation: { phase: 'idle', intent: null, result: null } }, intent, clock.now());
  }
  async function perform(input: Intent): Promise<PerformedOperation> {
    if (snapshot.refreshing) return { outcome: 'blocked', reason: 'Explicit Refresh is still observing. Wait before acting.' };
    if (reservation) return { outcome: 'blocked', reason: 'Another operation owns the request slot.' };
    if (!active) return { outcome: 'blocked', reason: 'The controller is not active.' };
    const intent = freeze(structuredClone(input));
    const ownerLifetime = lifetime;
    const ownerGeneration = generation;
    const invalidatedReason = 'Observation was invalidated by a context change. Review the refreshed state before trying again.';
    let release!: () => void;
    reservation = new Promise<void>(resolve => { release = resolve; });
    clearTimer();
    publish({ operation: { phase: 'waiting', intent, result: null } });
    let result: PerformedOperation | null = null;
    try {
      const accepted = await draining;
      if (!active || ownerLifetime !== lifetime) {
        result = { outcome: 'blocked', reason: 'The operation was disposed before submission.' };
        return result;
      }
      if (accepted === false || ownerGeneration !== generation) {
        result = { outcome: 'blocked', reason: invalidatedReason };
        return result;
      }
      let permission = eligible(intent);
      if (!permission.allowed) {
        result = { outcome: 'blocked', reason: permission.reason! };
        return result;
      }
      if (intent.kind === 'create' || intent.kind === 'remove') {
        const preflightAccepted = await read({ full: true, explicit: false, domains: false });
        if (!preflightAccepted || ownerGeneration !== generation) {
          result = { outcome: 'blocked', reason: invalidatedReason };
          return result;
        }
        permission = eligible(intent);
        if (!active || ownerLifetime !== lifetime || !permission.allowed) {
          result = { outcome: 'blocked', reason: permission.reason || 'Observation changed before submission.' };
          return result;
        }
      }
      const configReadAtDispatch = snapshot.configReadSequence;
      publish({ operation: { phase: 'submitting', intent, result: null } });
      let response;
      try {
        response = parseAction(await api.mutate(intent));
      } catch (error) {
        response = error instanceof ApiError && error.response ? parseAction(error.response) : unknownResponse();
      }
      const outcome = response.outcome === 'unknown' ? 'unknown' : response.ok ? 'completed' : 'failed';
      result = { outcome, response, configReadAtDispatch };
      if (outcome === 'unknown') unknown.set(operationKey(intent), intent);
      publish({ blockedUnknownKeys: [...unknown.keys()], operation: { phase: 'observing', intent, result } });
      if (active && snapshot.visible) {
        // Consume a queued visibility/route read with the owned follow-up. Explicit recovery stays separate.
        const domains = !!pending?.domains;
        if (!pending?.explicit) pending = null;
        await read({ full: true, explicit: false, domains });
      } else {
        enqueue({ full: true, explicit: false, domains: false });
      }
      return result;
    } finally {
      publish({ operation: { phase: result && result.outcome !== 'blocked' ? result.outcome : 'idle', intent, result } });
      reservation = null;
      release();
      // The follow-up used the private read path, so it never waits on its own reservation.
      void drain();
      schedule();
    }
  }
  return {
    start() {
      if (active) return;
      active = true;
      enqueue({ full: true, explicit: false, domains: snapshot.route === 'settings/network' });
      void drain();
    },
    dispose() {
      active = false;
      lifetime++;
      invalidateRead();
      clearTimer();
      pending = null;
      listeners.clear();
      publish({ refreshing: false });
    },
    subscribe(listener) {
      listeners.add(listener);
      return () => { listeners.delete(listener); };
    },
    getSnapshot: () => snapshot,
    setContext(route, visible) {
      if (route === snapshot.route && visible === snapshot.visible) return;
      const enteredNetwork = route === 'settings/network' && snapshot.route !== route;
      clearTimer();
      invalidateRead();
      publish({ route, visible, refreshing: false });
      if (visible) {
        enqueue({ full: true, explicit: false, domains: enteredNetwork });
        void drain();
      }
    },
    refresh,
    perform,
  };
}
