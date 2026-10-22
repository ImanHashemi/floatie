import json
from pathlib import Path
import ui_server as u

FIX = Path(__file__).parent / "fixtures" / "state_running.json"


def test_fixture_provider_roundtrip():
    p = u.FixtureProvider(str(FIX))
    state = u.build_state(p)
    assert state["vm"]["status"] == "Running"
    assert state["firewall"]["mode"] == "on"
    assert "generatedAt" in state and state["generatedAt"]


def test_build_state_has_environments_not_work_workers():
    p = u.FixtureProvider(str(FIX))
    state = u.build_state(p)
    for k in ("workspace", "vm", "firewall", "environments", "secrets", "config", "generatedAt"):
        assert k in state, f"missing {k}"
    assert "work" not in state and "workers" not in state


def test_environments_shape():
    p = u.FixtureProvider(str(FIX))
    envs = u.build_state(p)["environments"]
    ws = next(e for e in envs if e["kind"] == "workspace")
    wk = next(e for e in envs if e["kind"] == "worker")
    assert ws["name"] == "workspace" and ws["session"] == "main"
    assert wk["name"] == "alpha" and wk["repos"][0]["branch"] == "worker/alpha"
    assert wk["repos"][0]["unpushed"] == 2
    assert {"repo", "branch", "clean", "uncommitted", "unpushed"} <= set(wk["repos"][0].keys())


import subprocess
import pytest

ROOT = Path(__file__).resolve().parents[1]


def reply(stdout='', rc=0, stderr=''):
    return subprocess.CompletedProcess([], rc, stdout, stderr)


@pytest.fixture
def live(tmp_path, monkeypatch):
    (tmp_path / 'workspace.yml').write_text('name: bound-vm\n')
    boundary = {'vm': reply('{"name":"bound-vm","status":"Running"}'),
                'firewall': reply('-P OUTPUT ACCEPT\n'), 'guard': reply('[]'),
                'inventory': reply(json.dumps({
                    'environments': [{'name': 'alpha', 'kind': 'worker', 'session': 'alpha',
                        'sessionLive': False, 'scopeKnown': True, 'managementReason': None,
                        'repos': [{'repo': 'api', 'path': '/vm/work/.worktrees/alpha/api', 'branch': 'custom',
                                   'clean': None, 'uncommitted': None, 'unpushed': None}]}],
                    'inventoryKnown': True, 'availableRepos': ['api'], 'observedAt': '2026-10-12T10:00:00+00:00',
                    'error': None, 'sessionObservedAt': '2026-10-12T10:00:01+00:00', 'sessionError': None})), 'calls': []}
    def run(argv, **kwargs):
        boundary['calls'].append((argv, kwargs))
        if argv[:2] == ['limactl', 'list']:
            assert argv == ['limactl', 'list', '--filter', '.name == "bound-vm"', '--json']
            value = boundary['vm']
        else:
            assert argv[:4] == ['limactl', 'shell', 'bound-vm', '--']
            if argv[4] == 'df': value = reply('Use%\n17%\n')
            elif argv[4:6] == ['sudo', 'iptables']: value = boundary['firewall']
            elif kwargs.get('input') == (ROOT / 'lifecycle-guard.sh').read_text(): value = boundary['guard']
            elif argv[4:6] == ['python3', '-']:
                assert kwargs['input'] == (ROOT / 'worker_inventory.py').read_text()
                value = boundary['inventory']
            else: pytest.fail(f'unexpected command {argv}')
        if isinstance(value, Exception): raise value
        return value
    monkeypatch.setattr(u.subprocess, 'run', run)
    return u.LiveProvider(str(ROOT), str(tmp_path), 'bound-vm'), boundary


@pytest.mark.parametrize('response,status', [(reply(), 'absent'), (reply('', 1, 'transport failed'), 'unknown'),
    (reply('not json'), 'unknown'), (reply('{}'), 'unknown'), (reply('[]'), 'unknown'),
    (reply('{"name":"other","status":"Running"}'), 'unknown'),
    (OSError('unavailable'), 'unknown'), (subprocess.TimeoutExpired('limactl', 1), 'unknown')])
def test_vm_empty_is_absent_only_on_confirmed_success(live, response, status):
    provider, boundary = live
    boundary['vm'] = response
    vm = provider.vm()
    assert vm['status'] == status
    assert bool(vm.get('error')) is (status == 'unknown')
    assert bool(vm.get('observedAt')) is (status != 'unknown')
    assert provider._status() == status


@pytest.mark.parametrize('response,mode', [(reply('', 1, 'sudo failed'), 'unknown'), (reply(), 'unknown'),
    (reply('bad output'), 'unknown'), (reply('-P OUTPUT ACCEPT\n'), 'off'),
    (reply('-P OUTPUT DROP\n-A OUTPUT -m set --match-set allowed dst -j ACCEPT\n'), 'on')])
def test_firewall_read_errors_never_become_off(live, response, mode):
    provider, boundary = live
    boundary['firewall'] = response
    fw = provider.firewall()
    assert fw['mode'] == mode
    assert bool(fw['error']) is (mode == 'unknown')
    assert bool(fw['observedAt']) is (mode != 'unknown')


@pytest.mark.parametrize('guard', [reply('[]'), reply('', 1, 'guard failed'), reply('{malformed'), reply('{}'),
    reply('[{"path":"/vm/work/.worktrees/alpha/api/","clean":true,"uncommitted":0,"unpushed":0}]'),
    reply('[{"path":"/vm/work/.worktrees/alpha/api","clean":false,"uncommitted":null,"unpushed":null,"error":"index flags"}]')])
def test_failed_counts_do_not_default_clean(live, guard):
    provider, boundary = live
    boundary['guard'] = guard
    state = u.build_state(provider)
    repo = state['environments'][0]['repos'][0]
    assert repo['clean'] is None and repo['uncommitted'] is None and repo['unpushed'] is None
    assert repo['error'] and repo['observedAt'] is None
    assert state['inventory']['countsError']


def test_exact_counts_join_and_source_times(live):
    provider, boundary = live
    boundary['guard'] = reply('[{"path":"/vm/work/.worktrees/alpha/api","clean":false,"uncommitted":2,"unpushed":3}]')
    state = u.build_state(provider)
    repo = state['environments'][0]['repos'][0]
    assert (repo['clean'], repo['uncommitted'], repo['unpushed']) == (False, 2, 3)
    assert repo['observedAt'] and not repo.get('error')
    assert state['availableRepos'] == ['api']
    assert state['inventory']['observedAt'] == '2026-10-12T10:00:00+00:00'
    assert state['inventory']['countsObservedAt']


@pytest.mark.parametrize('vm', [reply('{"name":"bound-vm","status":"Stopped"}'), reply(), reply('', 1)])
def test_cold_stopped_inventory_unavailable_no_shell(live, vm):
    provider, boundary = live
    boundary['vm'] = vm
    state = u.build_state(provider)
    assert state['environments'] == []
    assert not state['inventory']['inventoryKnown']
    assert state['inventory']['observedAt'] is None and state['inventory']['error']
    assert all(call[0][1] == 'list' for call in boundary['calls'])


@pytest.mark.parametrize('response', [reply('', 1, 'transport'), reply(), reply('[]'), reply('{"environments":[]}')])
def test_inventory_transport_and_malformed_payload_are_unknown(live, response):
    provider, boundary = live
    boundary['inventory'] = response
    state = u.build_state(provider)
    assert not state['inventory']['inventoryKnown'] and state['inventory']['error']
    assert state['inventory']['observedAt'] is None


def test_numeric_session_live_is_malformed_not_true(live):
    provider, boundary = live
    payload = json.loads(boundary['inventory'].stdout)
    payload['environments'][0]['sessionLive'] = 1
    boundary['inventory'] = reply(json.dumps(payload))
    state = u.build_state(provider)
    assert state['inventory']['inventoryKnown'] is False
    assert state['inventory']['error']


@pytest.mark.parametrize('patch', [{'clean': False, 'uncommitted': None, 'unpushed': None},
    {'clean': True, 'uncommitted': True}, {'clean': True, 'unpushed': -1},
    {'clean': True, 'uncommitted': 2}])
def test_malformed_preservation_counts_are_unknown(live, patch):
    provider, boundary = live
    counts = dict(path='/vm/work/.worktrees/alpha/api', clean=True, uncommitted=0, unpushed=0)
    counts.update(patch)
    boundary['guard'] = reply(json.dumps([counts]))
    repo = u.build_state(provider)['environments'][0]['repos'][0]
    assert repo['clean'] is None and repo['uncommitted'] is None and repo['unpushed'] is None
    assert repo['error']


def test_duplicate_count_paths_cannot_overwrite_error(live):
    provider, boundary = live
    count = dict(path='/vm/work/.worktrees/alpha/api', clean=True, uncommitted=0, unpushed=0)
    boundary['guard'] = reply(json.dumps([{**count, 'error': 'failed'}, count]))
    repo = u.build_state(provider)['environments'][0]['repos'][0]
    assert repo['clean'] is None and repo['error']


def test_lima_filter_is_a_literal_for_unusual_bound_vm_name(tmp_path, monkeypatch):
    name = 'vm" or true or .name == "other'
    calls = []
    def run(argv, **kwargs):
        calls.append(argv)
        return reply()
    monkeypatch.setattr(u.subprocess, 'run', run)
    provider = u.LiveProvider(str(ROOT), str(tmp_path), name)
    assert provider.vm()['status'] == 'absent'
    assert calls == [['limactl', 'list', '--filter', '.name == ' + json.dumps(name), '--json']]


from datetime import datetime, timezone


@pytest.mark.parametrize('name', [p.name for p in sorted(FIX.parent.glob('state_*.json'))])
def test_synthetic_fixtures_declare_metadata_and_use_runtime_relative_freshness(name, monkeypatch):
    path = FIX.parent / name
    raw = json.loads(path.read_text())
    assert {'availableRepos', 'inventory'} <= raw.keys()
    required = {'inventoryKnown', 'observedAt', 'error', 'repositoryObservedAt', 'repositoryError',
                'sessionObservedAt', 'sessionError', 'countsObservedAt', 'countsError'}
    assert required <= raw['inventory'].keys()
    for env in raw['environments']:
        assert {'scopeKnown', 'managementReason'} <= env.keys()
        for repo in env['repos']:
            assert {'path', 'observedAt'} <= repo.keys()

    def timestamps(value):
        if isinstance(value, dict):
            for key, child in value.items():
                if key.endswith('observedAt') or key.endswith('ObservedAt'):
                    yield child
                else:
                    yield from timestamps(child)
        elif isinstance(value, list):
            for child in value: yield from timestamps(child)

    assert all(value is None or (isinstance(value, dict) and set(value) == {'secondsAgo'}
               and type(value['secondsAgo']) is int and value['secondsAgo'] >= 0) for value in timestamps(raw))
    monkeypatch.setattr(u.subprocess, 'run', lambda *a, **k: pytest.fail('fixture accessed real process'))
    before = datetime.now(timezone.utc)
    state = u.build_state(u.FixtureProvider(str(path)))
    after = datetime.now(timezone.utc)
    for source, resolved in zip(timestamps(raw), timestamps({k: state[k] for k in raw})):
        if source is None:
            assert resolved is None
        else:
            value = datetime.fromisoformat(resolved)
            assert source['secondsAgo'] <= (after - value).total_seconds()
            assert (before - value).total_seconds() <= source['secondsAgo']
    # Loading must not write materialized dates into the reusable fixture.
    assert json.loads(path.read_text()) == raw


def test_saved_and_unknown_fixture_scope_is_explicit():
    state = u.build_state(u.FixtureProvider(str(FIX.parent / 'state_worker_offline.json')))
    rows = {row['name']: row for row in state['environments']}
    assert rows['beta']['scopeKnown'] is True and rows['beta']['sessionLive'] is False
    assert rows['empty']['scopeKnown'] is False and rows['empty']['repos'] == []
    assert rows['empty']['managementReason']
    assert rows['legacy']['kind'] == 'unknown' and rows['legacy']['managementReason']
    repo, = rows['damaged']['repos']
    assert repo['error'] and repo['observedAt'] is None
    assert all(repo[key] is None for key in ('clean', 'uncommitted', 'unpushed'))
    assert state['inventory']['countsObservedAt'] is None and state['inventory']['countsError']
