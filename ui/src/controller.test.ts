import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import { createController } from './controller.js';
import { ApiError, createApi } from './api.js';
import { canPerform, selectWorkers } from './model.js';
import type { Clock } from './controller.js';
import type { Api, StateResponse, AgentListResponse, ActionResponse, Intent } from './api.js';

const NOW = Date.parse('2026-10-14T12:00:00Z');
function fixture(): StateResponse {
  return JSON.parse(readFileSync('src/test/state.json', 'utf8'));
}
function agentFixture(): AgentListResponse {
  return { ok: true, identity: { matches: true, error: null }, agents: [{ target: 'main', state: 'shell', pane: '%0', harness: null }] };
}
function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (error: Error) => void;
  const promise = new Promise<T>((accept, refuse) => {
    resolve = accept;
    reject = refuse;
  });
  return { promise, resolve, reject };
}
class FakeClock implements Clock {
  time = NOW;
  next = 0;
  timers = new Map<number, { at: number; fn: () => void }>();
  now = () => this.time;
  setTimeout = (fn: () => void, ms: number) => {
    const id = ++this.next;
    this.timers.set(id, { at: this.time + ms, fn });
    return id;
  };
  clearTimeout = (handle: unknown) => { this.timers.delete(handle as number); };
  advance(ms: number) {
    this.time += ms;
    for (const [id, timer] of [...this.timers]) {
      if (timer.at <= this.time) {
        this.timers.delete(id);
        timer.fn();
      }
    }
  }
}
class FakeApi implements Api {
  next = fixture();
  agentNext = agentFixture();
  stateCalls = 0;
  agentCalls = 0;
  domainCalls = 0;
  mutationCalls: Intent[] = [];
  stateWait: ReturnType<typeof deferred<StateResponse>> | null = null;
  agentWait: ReturnType<typeof deferred<AgentListResponse>> | null = null;
  mutationWait: ReturnType<typeof deferred<ActionResponse>> | null = null;
  response: ActionResponse = { ok: true, outcome: 'completed', output: '[phase] completed' };
  state = async () => {
    this.stateCalls++;
    return this.stateWait ? this.stateWait.promise : structuredClone(this.next);
  };
  agents = async () => {
    this.agentCalls++;
    return this.agentWait ? this.agentWait.promise : structuredClone(this.agentNext);
  };
  baseDomains = async () => {
    this.domainCalls++;
    return { domains: ['example.org'] };
  };
  mutate = async (intent: Intent) => {
    this.mutationCalls.push(intent);
    return this.mutationWait ? this.mutationWait.promise : structuredClone(this.response);
  };
}
async function ready() {
  const api = new FakeApi(), clock = new FakeClock(), controller = createController(api, clock);
  controller.start();
  await controller.refresh('explicit');
  return { api, clock, controller };
}
async function turns() {
  for (let i = 0; i < 20; i++) await Promise.resolve();
}
const root: Intent = { kind: 'root', expectedVm: 'demo', harness: 'codex' };

test('one_timer_survives_start_dispose_start', async()=>{
  const {controller,clock,api}=await ready();
  assert.equal(clock.timers.size,1);
  controller.start();
  assert.equal(clock.timers.size,1);
  controller.dispose();
  assert.equal(clock.timers.size,0);
  controller.start();
  await controller.refresh('explicit');
  assert.equal(clock.timers.size,1);
  const n=api.agentCalls,states=api.stateCalls;
  clock.advance(3000);
  await turns();
  assert.equal(api.agentCalls,n+1);
  assert.equal(api.stateCalls,states);
  assert.equal(clock.timers.size,1);
  controller.dispose();
  assert.equal(clock.timers.size,0);
});

test('hidden_pauses_and_visible_observes',async()=>{
  const {controller,clock,api}=await ready();
  const before=api.stateCalls;
  controller.setContext('workspace',false);
  clock.advance(9000);
  await turns();
  assert.equal(api.stateCalls,before);
  assert.equal(clock.timers.size,0);
  controller.setContext('workspace',true);
  await controller.refresh('background');
  assert.equal(api.stateCalls,before+1);
  controller.setContext('settings/network',true);
  await controller.refresh('background');
  assert.equal(api.domainCalls,1);
  clock.advance(3000);
  await turns();
  assert.equal(api.domainCalls,1);
  await controller.refresh('explicit');
  assert.equal(api.domainCalls,2);
  controller.dispose();
});

test('intent_is_reserved_and_frozen_before_wait',async()=>{
  const {controller,api}=await ready();
  const pending=deferred<AgentListResponse>();
  api.agentWait=pending;
  const read=controller.refresh('background');
  await turns();
  const intent:Intent={kind:'create',expectedVm:'demo',name:'new',repos:['api'],availableRepos:['api','sdk']};
  const first=controller.perform(intent);
  intent.availableRepos.push('unexpected');
  intent.repos.push('sdk');
  assert.equal(controller.getSnapshot().operation.phase,'waiting');
  assert.equal((await controller.perform(root)).outcome,'blocked');
  assert.equal(api.mutationCalls.length,0);
  api.agentWait=null;
  pending.resolve(agentFixture());
  await read;
  await first;
  assert.deepEqual(api.mutationCalls[0],{kind:'create',expectedVm:'demo',name:'new',repos:['api'],availableRepos:['api','sdk']});
  assert.ok(Object.isFrozen(api.mutationCalls[0]));
  controller.dispose();
});

test('changed_read_blocks_dispatch',async()=>{
  for(const fail of ['identity','error','scope','availability','uncommitted','unpushed','not-clean','missing-count','no-repos','unknown-inventory']){
    const {controller,api}=await ready();
    const wait=deferred<StateResponse>();
    api.stateWait=wait;
    const intent:Intent=fail==='availability'?{kind:'create',expectedVm:'demo',name:'new',repos:['api'],availableRepos:['api','sdk']}:{kind:'remove',expectedVm:'demo',name:'alpha',repos:['api','sdk']};
    const reads=api.stateCalls;
    const action=controller.perform(intent);
    await turns();
    // Hold the operation's real preflight, not a public explicit Refresh that
    // would refuse the action before any of the changed evidence is checked.
    assert.equal(controller.getSnapshot().operation.phase,'waiting');
    assert.equal(api.stateCalls,reads+1);
    const changed=fixture();
    if(fail==='identity')changed.identity.matches=false;
    if(fail==='scope')changed.environments[0].repos.splice(1);
    if(fail==='availability')changed.availableRepos=['api'];
    if(fail==='uncommitted')Object.assign(changed.environments[0].repos[0],{uncommitted:1,clean:false});
    if(fail==='unpushed')Object.assign(changed.environments[0].repos[0],{unpushed:1,clean:false});
    if(fail==='not-clean')Object.assign(changed.environments[0].repos[0],{uncommitted:null,clean:false});
    if(fail==='missing-count')changed.environments[0].repos[0].unpushed=null;
    if(fail==='no-repos')changed.environments[0].repos=[];
    if(fail==='unknown-inventory')changed.inventory.inventoryKnown=false;
    api.next=changed;
    api.stateWait=null;
    if(fail==='error') wait.reject(Error('state unavailable'));
    else wait.resolve(changed);
    assert.equal((await action).outcome,'blocked',fail);
    assert.equal(api.mutationCalls.length,0);
    controller.dispose();
  }
});

test('direct_root_call_refuses_an_observed_running_agent',async()=>{
  const {controller,api}=await ready();
  api.agentNext.agents=[{target:'main',state:'running',pane:'%1',harness:'claude'}];
  await controller.refresh('background');
  assert.equal((await controller.perform(root)).outcome,'blocked');
  assert.equal(api.mutationCalls.length,0);
  controller.dispose();
});

test('late_read_cannot_restore_old_identity',async()=>{
  const {controller,api}=await ready();
  const wait=deferred<StateResponse>();
  api.stateWait=wait;
  const old=controller.refresh('explicit');
  await turns();
  controller.dispose();
  controller.start();
  api.next.identity.matches=false;
  api.stateWait=null;
  wait.resolve(fixture());
  await old;
  await controller.refresh('background');
  assert.equal(controller.getSnapshot().state?.identity.matches,false);
  assert.equal((await controller.perform(root)).outcome,'blocked');
  controller.dispose();
});

test('unknown_requires_matching_explicit_observation',async()=>{
  const {controller,api}=await ready();
  api.response={ok:false,outcome:'unknown',output:'phase started'};
  const save:Intent={kind:'save',expectedVm:'demo',section:'resources',expectedRevision:'revision-1',changes:{cpus:8}};
  assert.equal((await controller.perform(save)).outcome,'unknown');
  assert.equal(api.mutationCalls.length,1);
  await controller.refresh('background');
  assert.equal((await controller.perform({...save,section:'agents',changes:{plugins:[]}})).outcome,'blocked');
  api.next.configError='unreadable';
  api.next.configRevision=null;
  await controller.refresh('explicit');
  assert.equal((await controller.perform(save)).outcome,'blocked');
  api.next=fixture();
  await controller.refresh('explicit');
  api.response={ok:true,outcome:'completed'};
  assert.equal((await controller.perform(save)).outcome,'completed');
  assert.equal(api.mutationCalls.length,2);
  controller.dispose();
});

test('reservation_covers_followup_and_visibility_return',async()=>{
  const {controller,api,clock}=await ready();
  const mutation=deferred<ActionResponse>();
  api.mutationWait=mutation;
  const result=controller.perform(root);
  await turns();
  assert.equal(api.mutationCalls.length,1);
  assert.equal(clock.timers.size,0);
  controller.setContext('workspace',false);
  controller.setContext('workspace',true);
  const read=deferred<StateResponse>();
  api.stateWait=read;
  const n=api.stateCalls;
  mutation.resolve({ok:true,outcome:'completed'});
  await turns();
  assert.equal(api.stateCalls,n+1);
  assert.equal(controller.getSnapshot().operation.phase,'observing');
  assert.equal((await controller.perform(root)).outcome,'blocked');
  api.stateWait=null;
  read.resolve(fixture());
  await result;
  assert.equal(api.mutationCalls.length,1);
  assert.equal(clock.timers.size,1);
  controller.dispose();
});

test('immutable_snapshots_retain_counts_without_resurrecting_removed_repositories',async()=>{
  const {controller,api}=await ready();
  const initial=controller.getSnapshot();
  assert.equal(controller.getSnapshot(),initial);
  assert.throws(()=>{(initial.environments as unknown[]).push({});
  });
  api.next.inventory.countsError='counts unavailable';
  api.next.inventory.countsObservedAt=null;
  api.next.environments[0].repos[0].clean=null;
  api.next.environments[0].repos[0].uncommitted=null;
  api.next.environments[0].repos[0].error='counts unavailable';
  api.next.environments[0].repos.splice(1);
  await controller.refresh('explicit');
  const retained=controller.getSnapshot();
  assert.equal(retained.environments[0].repos[0].uncommitted,0);
  assert.equal(retained.environments[0].repos.length,1);
  assert.equal(retained.repositoryAt,NOW);
  assert.equal(initial.environments[0].repos.length,2);
  assert.equal(api.next.environments[0].repos[0].uncommitted,null);
  api.next.environments[0].repos[0].repo='renamed';
  await controller.refresh('explicit');
  assert.equal(controller.getSnapshot().environments[0].repos[0].uncommitted,null);
  api.next.inventory.inventoryKnown=false;
  api.next.environments=[];
  await controller.refresh('explicit');
  assert.equal(controller.getSnapshot().environments.length,1);
  controller.dispose();
});

test('dispose_cancels_waiting_intent_but_never_retries_dispatched_mutation', async () => {
  const {controller,api,clock}=await ready();
  const wait=deferred<AgentListResponse>();
  api.agentWait=wait;
  const read=controller.refresh('background');
  await turns();
  const action=controller.perform(root);
  controller.dispose();
  api.agentWait=null;
  wait.resolve(agentFixture());
  await read;
  assert.equal((await action).outcome,'blocked');
  assert.equal(api.mutationCalls.length,0);
  assert.equal(clock.timers.size,0);
  controller.start();
  await controller.refresh('background');
  const mutation=deferred<ActionResponse>();
  api.mutationWait=mutation;
  const dispatched=controller.perform(root);
  await turns();
  assert.equal(api.mutationCalls.length,1);
  controller.dispose();
  mutation.reject(Error('lost response'));
  assert.equal((await dispatched).outcome,'unknown');
  assert.equal(clock.timers.size,0);
  controller.start();
  await controller.refresh('background');
  assert.equal((await controller.perform(root)).outcome,'blocked');
  assert.equal(api.mutationCalls.length,1);
  await controller.refresh('explicit');
  assert.deepEqual(controller.getSnapshot().blockedUnknownKeys,[]);
  controller.dispose();
});

test('refreshes_serialize_and_context_discards_an_obsolete_response', async () => {
  const {controller,api,clock}=await ready();
  const wait=deferred<StateResponse>();
  api.stateWait=wait;
  const reading=controller.refresh('explicit');
  await turns();
  const count=api.stateCalls;
  const second=controller.refresh('explicit'),third=controller.refresh('background');
  controller.setContext('settings/network',true);
  await turns();
  assert.equal(api.stateCalls,count);
  assert.equal(clock.timers.size,0);
  const obsolete=fixture();
  obsolete.workspace.name='wrong';
  obsolete.identity.matches=false;
  api.stateWait=null;
  wait.resolve(obsolete);
  await Promise.all([reading,second,third]);
  assert.equal(api.stateCalls,count+1);
  assert.equal(api.domainCalls,1);
  assert.equal(controller.getSnapshot().state?.identity.matches,true);
  assert.equal(controller.getSnapshot().boundVm,'demo');
  assert.equal(clock.timers.size,1);
  controller.dispose();
});

test('unknown_blocks_require_their_own_sources_and_original_vm', async () => {
  const intents:Intent[]=[root,{kind:'vm',action:'stop',expectedVm:'demo'},
    {kind:'network',expectedVm:'demo',expectedRevision:'revision-1',policy:{permissive:false,egress:[]}},
    {kind:'create',expectedVm:'demo',name:'new',repos:['api'],availableRepos:['api','sdk']},
    {kind:'remove',expectedVm:'demo',name:'alpha',repos:['api','sdk']}];
  for (const intent of intents) {
    const {controller,api}=await ready();
    api.response={ok:false,outcome:'unknown',output:'[phase] started'};
    assert.equal((await controller.perform(intent)).outcome,'unknown',intent.kind);
    assert.equal(controller.getSnapshot().blockedUnknownKeys.length,1);
    // Direct action-boundary calls bypass disabled UI controls entirely.
    assert.equal((await controller.perform(intent)).outcome,'blocked',intent.kind);
    assert.equal(api.mutationCalls.length,1);
    await controller.refresh('background');
    assert.equal(controller.getSnapshot().blockedUnknownKeys.length,1);
    assert.equal((await controller.perform(intent)).outcome,'blocked',intent.kind);
    assert.equal(api.mutationCalls.length,1);
    if (intent.kind==='root') api.agentNext={...agentFixture(),observedAt:null};
    if (intent.kind==='vm') api.next.vm.observedAt=null;
    if (intent.kind==='network') api.next.firewall.observedAt=null;
    if (intent.kind==='create') api.next.inventory.inventoryKnown=false;
    if (intent.kind==='remove') api.next.environments[0].repos[0].observedAt=null;
    await controller.refresh('explicit');
    assert.equal(controller.getSnapshot().blockedUnknownKeys.length,1,intent.kind+' missing evidence');
    api.next=fixture();
    api.agentNext=agentFixture();
    api.next.workspace.name='another';
    api.next.config!.name='another';
    await controller.refresh('explicit');
    assert.equal(controller.getSnapshot().blockedUnknownKeys.length,1);
    api.next=fixture();
    await controller.refresh('explicit');
    assert.equal(controller.getSnapshot().blockedUnknownKeys.length,0);
    assert.equal(api.mutationCalls.length,1);
    controller.dispose();
  }
});

test('revision_and_policy_are_frozen_and_revalidated_without_mutation_retry', async () => {
  const {controller,api}=await ready();
  const wait=deferred<StateResponse>();
  api.stateWait=wait;
  const read=controller.refresh('explicit');
  await turns();
  const network:Intent={kind:'network',expectedVm:'demo',expectedRevision:'revision-1',policy:{permissive:false,egress:[]}};
  const applying=controller.perform(network);
  network.policy.permissive=true;
  network.policy.egress.push('unexpected.example');
  network.expectedRevision='revision-2';
  const changed=fixture();
  changed.configRevision='revision-2';
  changed.config!.permissive=true;
  api.next=changed;
  api.stateWait=null;
  wait.resolve(changed);
  await read;
  assert.equal((await applying).outcome,'blocked');
  assert.equal(api.mutationCalls.length,0);
  const conflict=new ApiError(409,{ok:false,outcome:'failed',error:'Revision changed',config:changed.config!,configRevision:'revision-2'});
  const saved:Intent[]=[];
  api.mutate=async intent=>{saved.push(intent);
  throw conflict;
  };
  const save:Intent={kind:'save',section:'resources',expectedVm:'demo',expectedRevision:'revision-1',changes:{cpus:8}};
  const saving=controller.perform(save);
  save.changes.cpus=16;
  save.expectedRevision='revision-2';
  const result=await saving;
  assert.equal(result.outcome,'failed');
  assert.deepEqual(saved,[{kind:'save',section:'resources',expectedVm:'demo',expectedRevision:'revision-1',changes:{cpus:8}}]);
  assert.equal(result.response.configRevision,'revision-2');
  assert.deepEqual(controller.getSnapshot().blockedUnknownKeys,[]);
  controller.dispose();
});

test('failed_creation_preserves_phase_output_and_surviving_scope', async () => {
  const {controller,api}=await ready();
  api.mutate=async intent=>{
    api.mutationCalls.push(intent);
    api.next.environments.push({...api.next.environments[0],name:'new',session:'new',repos:[api.next.environments[0].repos[0]]});
    return {ok:false,outcome:'failed',exitCode:1,output:'[api] created\n[sdk] failed; surviving scope retained'};
  };
  const create:Intent={kind:'create',expectedVm:'demo',name:'new',repos:['api','sdk'],availableRepos:['api','sdk']};
  const result=await controller.perform(create);
  assert.equal(result.outcome,'failed');
  assert.equal(result.response.output,'[api] created\n[sdk] failed; surviving scope retained');
  assert.deepEqual(controller.getSnapshot().environments.find(env=>env.name==='new')?.repos.map(repo=>repo.repo),['api']);
  assert.equal((await controller.perform(create)).outcome,'blocked');
  assert.equal(api.mutationCalls.length,1);
  controller.dispose();
});

test('observation_failures_retain_independent_sources_and_input_ownership', async () => {
  const {controller,api}=await ready();
  const input=fixture();
  api.state=async()=>input;
  await controller.refresh('explicit');
  input.environments[0].repos[0].unpushed=99;
  assert.equal(controller.getSnapshot().environments[0].repos[0].unpushed,0);
  const prior=controller.getSnapshot();
  let notifications=0;
  const unsubscribe=controller.subscribe(()=>notifications++);
  api.state=async()=>{throw Error('state unavailable');
  };
  await controller.refresh('explicit');
  assert.equal(controller.getSnapshot().agentError,null);
  assert.notEqual(controller.getSnapshot().stateError,null);
  assert.equal(controller.getSnapshot().repositoryAt,prior.repositoryAt);
  assert.ok(notifications>0);
  unsubscribe();
  notifications=0;
  await controller.refresh('explicit');
  assert.equal(notifications,0);
  assert.equal((await controller.perform(root)).outcome,'blocked');
  controller.dispose();
});

test('lightweight_poll_preserves_expired_sources_while_agent_discovery_stays_current', async () => {
  const { controller, api, clock } = await ready();
  const stateCalls = api.stateCalls;
  clock.time += 65_000;
  const currentTime = new Date(clock.time).toISOString();
  api.next.vm.observedAt = currentTime;
  api.next.inventory.observedAt = currentTime;
  api.next.inventory.repositoryObservedAt = currentTime;
  api.next.inventory.countsObservedAt = currentTime;
  api.next.environments[0].repos.forEach(repo => { repo.observedAt = currentTime; });
  api.agentNext.agents = [{ target: 'alpha', state: 'running', pane: '%1', harness: 'codex' }];
  await controller.refresh('background');
  assert.equal(api.stateCalls, stateCalls);
  assert.equal(controller.getSnapshot().repositoryAt, NOW);
  assert.equal(controller.getSnapshot().agentAt, clock.now());
  assert.equal(selectWorkers(controller.getSnapshot(), clock.now()).find(row => row.key === 'alpha')?.running, true);
  assert.equal((await controller.perform({ kind: 'remove', expectedVm: 'demo', name: 'alpha', repos: ['api', 'sdk'] })).outcome, 'blocked');
  assert.equal(api.mutationCalls.length, 0);
  controller.dispose();
});

test('invalidated_remove_preflight_blocks_dispatch_and_releases_reservation', async () => {
  const { controller, api, clock } = await ready();
  const wait = deferred<StateResponse>();
  api.stateWait = wait;
  const readsBefore = api.stateCalls;
  const removing = controller.perform({ kind: 'remove', expectedVm: 'demo', name: 'alpha', repos: ['api', 'sdk'] });
  await turns();
  assert.equal(api.stateCalls, readsBefore + 1);
  assert.equal(controller.getSnapshot().operation.phase, 'waiting');
  controller.setContext('workspace', false);
  controller.setContext('workspace', true);
  const changed = fixture();
  changed.environments[0].repos.splice(1);
  api.next = changed;
  api.stateWait = null;
  wait.resolve(changed);
  const result = await removing;
  assert.equal(result.outcome, 'blocked');
  assert.match(result.reason, /observation.*invalidated/i);
  assert.equal(api.mutationCalls.length, 0);
  assert.equal(controller.getSnapshot().operation.phase, 'idle');
  await turns();
  assert.deepEqual(controller.getSnapshot().environments[0].repos.map(repo => repo.repo), ['api']);
  assert.equal(clock.timers.size, 1);
  // A separate, explicit action can reserve the released slot after the queued read finishes.
  assert.equal((await controller.perform(root)).outcome, 'completed');
  assert.deepEqual(api.mutationCalls.map(intent => intent.kind), ['root']);
  controller.dispose();
});

test('invalidated_pending_read_blocks_dispatch_and_observes_changed_identity', async () => {
  const { controller, api, clock } = await ready();
  const wait = deferred<AgentListResponse>();
  api.agentWait = wait;
  const reading = controller.refresh('background');
  await turns();
  const stopping = controller.perform({ kind: 'vm', expectedVm: 'demo', action: 'stop' });
  assert.equal(controller.getSnapshot().operation.phase, 'waiting');
  controller.setContext('workspace', false);
  controller.setContext('workspace', true);
  const changed = fixture();
  changed.identity.matches = false;
  api.next = changed;
  api.agentWait = null;
  wait.resolve({ ...agentFixture(), identity: changed.identity });
  await reading;
  const result = await stopping;
  assert.equal(result.outcome, 'blocked');
  assert.match(result.reason, /observation.*invalidated/i);
  assert.equal(api.mutationCalls.length, 0);
  assert.equal(controller.getSnapshot().operation.phase, 'idle');
  await turns();
  assert.equal(controller.getSnapshot().state?.identity.matches, false);
  assert.equal(clock.timers.size, 1);
  api.next = fixture();
  await controller.refresh('explicit');
  assert.equal((await controller.perform(root)).outcome, 'completed');
  assert.deepEqual(api.mutationCalls.map(intent => intent.kind), ['root']);
  controller.dispose();
});

for (const { payload, status } of [
  { payload: { ok: true, outcome: ['failed'] }, status: 200 },
  { payload: { ok: false, outcome: ['unknown'] }, status: 200 },
  { payload: { ok: false, outcome: ['unknown'] }, status: 503 },
]) {
  test(`malformed_wire_outcome_${payload.outcome[0]}_http_${status}_retains_unknown_block`, async () => {
    const { controller, api } = await ready();
    let wireCalls = 0;
    const wire = createApi('synthetic-token', (async () => {
      wireCalls++;
      return Response.json(payload, { status });
    }) as typeof fetch);
    api.mutate = async intent => {
      api.mutationCalls.push(intent);
      return wire.mutate(intent);
    };
    const intent: Intent = { kind: 'vm', expectedVm: 'demo', action: 'stop' };
    const result = await controller.perform(intent);
    assert.equal(result.outcome, 'unknown');
    assert.equal(result.response.outcome, 'unknown');
    assert.equal(controller.getSnapshot().operation.phase, 'unknown');
    assert.deepEqual(controller.getSnapshot().blockedUnknownKeys, ['vm:stop']);
    await controller.refresh('background');
    assert.equal((await controller.perform(intent)).outcome, 'blocked');
    assert.equal(wireCalls, 1);
    assert.equal(api.mutationCalls.length, 1);
    controller.dispose();
  });
}


test('explicit_refresh_reserves_actions_synchronously_while_queued_behind_background_read', async () => {
  const { api, clock, controller } = await ready();
  const poll = deferred<AgentListResponse>();
  api.agentWait = poll;
  clock.advance(3000);
  await turns();
  assert.equal(controller.getSnapshot().refreshing, false);
  assert.equal(canPerform(controller.getSnapshot(), root, clock.now()).allowed, true);
  const refreshing = controller.refresh('explicit');
  assert.equal(controller.getSnapshot().refreshing, true);
  assert.equal(canPerform(controller.getSnapshot(), root, clock.now()).allowed, false);
  const refused = await controller.perform(root);
  assert.equal(refused.outcome, 'blocked');
  assert.equal(api.mutationCalls.length, 0);
  const stateRead = deferred<StateResponse>();
  api.stateWait = stateRead;
  api.agentWait = null;
  poll.resolve(agentFixture());
  await turns();
  assert.equal(controller.getSnapshot().refreshing, true);
  assert.equal(canPerform(controller.getSnapshot(), root, clock.now()).allowed, false);
  api.stateWait = null;
  stateRead.resolve(api.next);
  await refreshing;
  assert.equal(controller.getSnapshot().refreshing, false);
  assert.equal(canPerform(controller.getSnapshot(), root, clock.now()).allowed, true);
  controller.dispose();
});
