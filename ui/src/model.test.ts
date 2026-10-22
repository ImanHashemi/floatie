import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import { canPerform, commandFor, selectWorkers, filterWorkers } from './model.js';
import { createController } from './controller.js';
import type { StateResponse, AgentListResponse } from './api.js';

const NOW = Date.parse('2026-10-14T12:00:00Z');
function fixture(): StateResponse {
  return JSON.parse(readFileSync('src/test/state.json', 'utf8'));
}
async function ready() {
  const clock = {
    time: NOW,
    now() { return this.time; },
    setTimeout() { return 0; },
    clearTimeout() {},
  };
  const api = {
    next: fixture(),
    agentNext: { ok: true, identity: { matches: true, error: null }, agents: [] } as AgentListResponse,
    async state() { return structuredClone(this.next); },
    async agents() { return structuredClone(this.agentNext); },
    async baseDomains() { return { domains: [] }; },
    async mutate() { return { ok: true }; },
  };
  const controller = createController(api, clock);
  controller.start();
  await controller.refresh('explicit');
  return { api, clock, controller };
}

test('source_freshness_is_independent', async () => {
  const {controller,api,clock}=await ready();
  const remove={kind:'remove' as const,expectedVm:'demo',name:'alpha',repos:['api','sdk']};
  assert.equal(canPerform(controller.getSnapshot(),remove,NOW).allowed,true);
  for (const time of [new Date(NOW-60_001).toISOString(),new Date(NOW+1).toISOString(),'invalid',null]) {
    api.next=fixture();
    api.next.environments[0].repos[0].observedAt=time;
    await controller.refresh('explicit');
    assert.equal(canPerform(controller.getSnapshot(),remove,NOW).allowed,false);
    assert.equal(filterWorkers(selectWorkers(controller.getSnapshot(),NOW),'','changes').length,0);
  }
  api.next=fixture();
  await controller.refresh('explicit');
  clock.time+=60_001;
  await controller.refresh('background');
  assert.equal(canPerform(controller.getSnapshot(),remove,clock.now()).allowed,false);
  assert.equal(controller.getSnapshot().agentAt,clock.now());
  assert.equal(controller.getSnapshot().repositoryAt,NOW);
  controller.dispose();
});

test('commands_quote_literals_and_recreate_exact_saved_scope', async () => {
  const {controller,api}=await ready();
  api.next.workspace.dir="/work spaces/it's $cash; & `literal`";
  await controller.refresh('explicit');
  const prefix="cd -- '/work spaces/it'\"'\"'s $cash; & `literal`' && floatie ";
  assert.equal(commandFor(controller.getSnapshot(),'main'),prefix+'attach');
  assert.equal(commandFor(controller.getSnapshot(),'alpha'),prefix+"worker 'alpha'");
  api.next.environments[0].repos.splice(1);
  await controller.refresh('explicit');
  assert.equal(commandFor(controller.getSnapshot(),'alpha'),prefix+"worker 'alpha' 'api'");
  assert.equal(commandFor(controller.getSnapshot(),'bad; echo unsafe'),null);
  api.next.identity.matches=false;
  await controller.refresh('explicit');
  assert.equal(commandFor(controller.getSnapshot(),'alpha'),null);
  controller.dispose();
});

test('positive_filters_exclude_unknown_and_discovery_only_rows', async () => {
  const {controller,api}=await ready();
  api.agentNext.agents=[{target:'orphan',state:'running',pane:'%3',harness:'other'}];
  await controller.refresh('background');
  let rows=selectWorkers(controller.getSnapshot(),NOW);
  assert.deepEqual(rows.map(r=>r.key),['main','alpha','orphan']);
  assert.deepEqual(filterWorkers(rows,'','absent').map(r=>r.key),['alpha']);
  assert.deepEqual(filterWorkers(rows,'','running').map(r=>r.key),['orphan']);
  assert.deepEqual(filterWorkers(rows,'sdk','all').map(r=>r.key),['alpha']);
  assert.equal(rows.find(r=>r.key==='orphan')!.remove.allowed,false);
  api.next.inventory.inventoryKnown=false;
  api.next.inventory.error='partial';
  await controller.refresh('explicit');
  rows=selectWorkers(controller.getSnapshot(),NOW);
  assert.deepEqual(filterWorkers(rows,'','absent'),[]);
  assert.deepEqual(filterWorkers(rows,'','changes'),[]);
  controller.dispose();
});

test('network_accepts_the_exact_saved_policy_even_with_repeated_domains', async () => {
  const {controller,api}=await ready();
  api.next.config!.egress=['example.org','example.org'];
  await controller.refresh('explicit');
  assert.equal(canPerform(controller.getSnapshot(),{kind:'network',expectedVm:'demo',expectedRevision:'revision-1',policy:{permissive:false,egress:['example.org','example.org']}},NOW).allowed,true);
  controller.dispose();
});

test('vm_and_root_eligibility_use_positive_current_evidence', async () => {
  const {controller,api}=await ready();
  for (const state of ['running','ambiguous','unknown','shell','dead','missing'] as const) {
    api.agentNext.agents=[{target:'main',state}];
    await controller.refresh('background');
    assert.equal(canPerform(controller.getSnapshot(),{kind:'root',expectedVm:'demo',harness:'codex'},NOW).allowed,['shell','dead','missing'].includes(state));
  }
  for (const status of ['Stopped','Running','unknown','absent']) {
    api.next.vm.status=status;
    await controller.refresh('explicit');
    for (const action of ['start','stop','reload'] as const) assert.equal(canPerform(controller.getSnapshot(),{kind:'vm',expectedVm:'demo',action},NOW).allowed,action==='start'?status==='Stopped':status==='Running');
  }
  controller.dispose();
});

test('timestamps_reject_calendar_rollover_and_require_explicit_timezone', async () => {
  const {controller,api}=await ready();
  const invalidDates=['2026-02-30T12:00:00Z','2026-10-14T12:00:00'];
  for (const observedAt of invalidDates) {
    api.next.vm.observedAt=observedAt;
    await controller.refresh('explicit');
    const interpreted=Date.parse(observedAt);
    assert.equal(canPerform(controller.getSnapshot(),{kind:'vm',expectedVm:'demo',action:'stop'},interpreted).allowed,false);
  }
  controller.dispose();
});

test('eligibility_results_cannot_corrupt_future_preflights', async () => {
  const { controller } = await ready();
  const intent = { kind: 'vm' as const, action: 'stop' as const, expectedVm: 'demo' };
  const result = canPerform(controller.getSnapshot(), intent, NOW);
  try { result.allowed = false; } catch { /* A frozen result is also safe. */ }
  assert.equal(canPerform(controller.getSnapshot(), intent, NOW).allowed, true);
  controller.dispose();
});
