import assert from 'node:assert/strict';
import test from 'node:test';
import { createApi, parseState, parseAgents, ApiError } from './api.js';
import { readFileSync } from 'node:fs';
import type { StateResponse, AgentListResponse } from './api.js';
function fixture(): StateResponse {
  return JSON.parse(readFileSync('src/test/state.json', 'utf8'));
}
function agentFixture(): AgentListResponse {
  return { ok: true, identity: { matches: true, error: null }, agents: [{ target: 'main', state: 'shell', pane: '%0', harness: null }] };
}

test('malformed_observations_stay_unknown', () => {
  for (const bad of [null, {}, {...fixture(), inventory: {}}, {...fixture(), environments: [{}]},
    {...fixture(), availableRepos: ['api', 'api']}, {...fixture(), error: 'read failed'}]) {
    assert.throws(() => parseState(bad));
  }
  const s = fixture();
  s.environments[0].repos[0].clean = null;
  s.environments[0].repos[0].uncommitted = null;
  assert.equal(parseState(s).environments[0].repos[0].uncommitted, null);
  for (const agents of [[agentFixture().agents[0], agentFixture().agents[0]],
    [{target:'main',state:'shell',candidates:[{target:'other',state:'running'}]}],
    [{target:'main',state:'running',pane:'not-a-pane'}]]) {
    assert.throws(() => parseAgents({...agentFixture(), agents}));
  }
  assert.throws(() => parseAgents({...agentFixture(), ok:false, error:'unavailable'}));
});

test('api_preserves_routes_guards_and_saved_policy', async () => {
  const calls: { url: string; options: RequestInit }[] = [];
  const api = createApi('synthetic-token', (async (url, options) => {
    calls.push({url:String(url),options:options!});
    return Response.json(String(url)==='/api/state' ? fixture() : String(url)==='/api/base-domains' ? {domains:['example.org']} :
      JSON.parse(String(options?.body)).action==='list' ? agentFixture() : {ok:true,output:'phase one\nphase two'});
  }) as typeof fetch);
  const abort = new AbortController();
  await api.state(abort.signal);
  await api.agents();
  await api.baseDomains();
  await api.mutate({kind:'root',expectedVm:'demo',harness:'claude'});
  await api.mutate({kind:'create',expectedVm:'demo',name:'new',repos:['api'],availableRepos:['api','sdk']});
  await api.mutate({kind:'remove',expectedVm:'demo',name:'old',repos:['api']});
  await api.mutate({kind:'vm',expectedVm:'demo',action:'stop'});
  await api.mutate({kind:'network',expectedVm:'demo',expectedRevision:'rev',policy:{permissive:true,egress:['example.org']}});
  await api.mutate({kind:'save',expectedVm:'demo',expectedRevision:'rev',section:'resources',changes:{cpus:8}});
  assert.deepEqual(calls.map(c=>c.url), ['/api/state','/api/agents','/api/base-domains','/api/agents',...Array(4).fill('/api/action'),'/api/config']);
  assert.deepEqual(calls.slice(3).map(c=>JSON.parse(String(c.options.body))), [
    {action:'start',target:'main',harness:'claude',expectedVm:'demo'},
    {action:'worker-create',params:{name:'new',repos:['api'],availableRepos:['api','sdk']},expectedVm:'demo'},
    {action:'worker-rm',params:{name:'old'},expectedVm:'demo'},
    {action:'stop',params:{},expectedVm:'demo'},
    {action:'firewall-off',params:{},expectedRevision:'rev',expectedVm:'demo'},
    {expectedVm:'demo',expectedRevision:'rev',changes:{cpus:8}}
  ]);
  for (const c of calls) {
    assert.equal(new Headers(c.options.headers).get('X-Floatie-Token'),'synthetic-token');
    assert.equal(c.options.referrerPolicy,'no-referrer');
    assert.equal(c.options.mode,'same-origin');
  }
  assert.equal(calls[0].options.signal,abort.signal);
});

test('http_conflict_is_structured_and_transport_or_unreadable_mutations_are_unknown', async () => {
  const intent = {kind:'save' as const,expectedVm:'demo',section:'resources' as const,expectedRevision:'old',changes:{cpus:8}};
  const conflict = createApi('synthetic-token', (async()=>Response.json({ok:false,error:'Revision changed',config:fixture().config,configRevision:'new',secret:'must not survive'}, {status:409})) as typeof fetch);
  await assert.rejects(conflict.mutate(intent), (e:unknown) => {
    assert.ok(e instanceof ApiError);
    assert.equal(e.status,409);
    assert.equal(e.response?.configRevision,'new');
    assert.equal(e.response?.outcome,'failed');
    assert.equal('secret' in e.response!,false);
    return true;
  });
  for (const fetcher of [
    async () => { throw Error('private transport detail'); },
    async () => new Response('not json'),
    async () => Response.json({ok:'yes'}),
  ]) {
    const response=await createApi('synthetic-token',fetcher as typeof fetch).mutate(intent);
    assert.equal(response.outcome,'unknown');
    assert.equal(response.ok,false);
    assert.doesNotMatch(JSON.stringify(response),/private transport detail|not json/);
  }
  const failed=createApi('synthetic-token',(async()=>Response.json({ok:false,error:'refused',output:'[preserve] changes remain'})) as typeof fetch);
  assert.equal((await failed.mutate(intent)).outcome,'failed');
});

test('http_errors_preserve_status_without_inventing_a_readable_failure', async () => {
  const intent={kind:'vm' as const,action:'stop' as const,expectedVm:'demo'};
  for (const body of ['not json','{}','{"ok":"invalid"}']) {
    const api=createApi('synthetic-token',(async()=>new Response(body,{status:503})) as typeof fetch);
    await assert.rejects(api.mutate(intent),(error:unknown)=>{
      assert.ok(error instanceof ApiError);
      assert.equal(error.status,503);
      assert.equal(error.response,null);
      return true;
    });
  }
  const api=createApi('synthetic-token',(async()=>Response.json({error:'Refused'},{status:400})) as typeof fetch);
  await assert.rejects(api.mutate(intent),(error:unknown)=>{
    assert.ok(error instanceof ApiError);
    assert.equal(error.status,400);
    assert.equal(error.response?.outcome,'failed');
    return true;
  });
});

test('actual_server_fixture_shapes_preserve_known_unknown_and_credential_status', () => {
  for (const name of ['running', 'stopped', 'absent', 'worker_offline', 'fw_off', 'fw_diverged']) {
    const payload = JSON.parse(readFileSync(`../tests/fixtures/state_${name}.json`, 'utf8'), (_key, value) => {
      if (value && typeof value === 'object' && Object.keys(value).length === 1 && 'secondsAgo' in value) {
        return new Date(Date.parse('2026-10-14T12:00:00Z') - value.secondsAgo * 1000).toISOString();
      }
      return value;
    });
    // build_state adds these from the bound workspace and its backed configuration file.
    payload.identity = { matches: true, error: null };
    payload.config.name = payload.workspace.name;
    payload.configRevision = 'fixture-revision';
    payload.configError = null;
    payload.generatedAt = '2026-10-14T12:00:00Z';
    const state = parseState(payload);
    assert.equal(state.vm.status, payload.vm.status);
    assert.equal(state.inventory.inventoryKnown, payload.inventory.inventoryKnown);
    assert.deepEqual(state.environments.flatMap(env => env.repos.map(repo => repo.uncommitted)),
      payload.environments.flatMap((env: { repos: { uncommitted: number | null }[] }) => env.repos.map(repo => repo.uncommitted)));
    assert.deepEqual(state.secrets, payload.secrets);
  }
  const unavailable = { ...fixture(), config: {}, configError: 'Unreadable configuration', configRevision: null };
  assert.equal(parseState(unavailable).config, null);
});

test('live_provider_unavailable_inventory_keeps_vm_and_config_observations', () => {
  const payload = { ...fixture(), vm: { ...fixture().vm, status: 'Stopped' }, environments: [], availableRepos: [],
    // RealProvider.inventory returns this smaller shape before repository/session discovery can run.
    inventory: { inventoryKnown: false, observedAt: null, error: 'VM is stopped', countsObservedAt: null, countsError: null } };
  const state = parseState(payload);
  assert.equal(state.vm.status, 'Stopped');
  assert.equal(state.configRevision, 'revision-1');
  assert.equal(state.inventory.repositoryObservedAt, null);
  assert.equal(state.inventory.sessionObservedAt, null);
});

test('mutation_outcomes_require_primitive_strings_on_the_wire', async () => {
  const intent = { kind: 'vm' as const, action: 'stop' as const, expectedVm: 'demo' };
  const malformed = [
    { ok: true, outcome: ['failed'] },
    { ok: false, outcome: ['unknown'] },
    { ok: true, outcome: null },
    { ok: true, outcome: 1 },
    { ok: false, outcome: {} },
    { ok: true, outcome: 'unexpected' },
    { ok: true, outcome: 'failed' },
    { ok: false, outcome: 'completed' },
  ];
  for (const payload of malformed) {
    let calls = 0;
    const api = createApi('synthetic-token', (async () => {
      calls++;
      return Response.json(payload);
    }) as typeof fetch);
    assert.equal((await api.mutate(intent)).outcome, 'unknown', JSON.stringify(payload));
    assert.equal(calls, 1);
  }
  const httpError = createApi('synthetic-token', (async () =>
    Response.json({ ok: false, outcome: ['unknown'] }, { status: 503 })) as typeof fetch);
  await assert.rejects(httpError.mutate(intent), (error: unknown) => {
    assert.ok(error instanceof ApiError);
    assert.equal(error.status, 503);
    assert.equal(error.response, null);
    return true;
  });
  for (const ok of [true, false]) {
    const api = createApi('synthetic-token', (async () => Response.json({ ok })) as typeof fetch);
    assert.equal((await api.mutate(intent)).outcome, ok ? 'completed' : 'failed');
  }
});

test('live_provider_disk_source_metadata_survives_validation', () => {
  // LiveProvider.vm observes status and disk separately, including a disk-only failure.
  for (const disk of [
    { diskPct: 43, diskObservedAt: '2026-10-14T11:59:40Z', diskError: null },
    { diskPct: null, diskObservedAt: null, diskError: 'Disk observation unavailable' },
  ]) {
    const payload = fixture();
    const vm = { ...payload.vm, ...disk };
    assert.deepEqual(parseState({ ...payload, vm }).vm, vm);
  }
  for (const status of ['Stopped', 'Running']) {
    const payload = fixture();
    const vm = parseState({ ...payload, vm: { ...payload.vm, status, diskPct: null } }).vm;
    assert.equal('diskObservedAt' in vm, false);
    assert.equal('diskError' in vm, false);
  }
});

test('malformed_supplied_disk_metadata_rejects_the_observation', () => {
  for (const metadata of [
    { diskObservedAt: 123 }, { diskObservedAt: [] }, { diskObservedAt: {} },
    { diskError: false }, { diskError: ['unavailable'] }, { diskError: {} },
  ]) {
    const payload = fixture();
    assert.throws(() => parseState({ ...payload, vm: { ...payload.vm, ...metadata } }));
  }
});
