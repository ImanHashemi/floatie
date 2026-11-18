"""Pinned UI identity and frozen policy through real dispatch."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import pytest
import ui_server as u
from test_agent_ui import post
ROOT = Path(__file__).resolve().parents[1]

@pytest.fixture
def workspace(tmp_path):
    (tmp_path / 'workspace.yml').write_text('name: reviewed-vm\npermissive: false\negress:\n  - reviewed.example\n')
    return tmp_path

@pytest.mark.parametrize('route,payload', [
    ('/api/action', {'action': action}) for action in
    ['start', 'stop', 'reload', 'firewall-on', 'firewall-off', 'worker-create', 'worker-rm']
] + [('/api/agents', {'action': 'start', 'target': 'main'}),
     ('/api/agents', {'action': 'send', 'target': 'Legacy', 'message': 'hello'}),
     ('/api/config', {'expectedRevision': '0' * 64, 'changes': {'cpus': 8}})])
@pytest.mark.parametrize('change', ['renamed', 'missing', 'wrong-client', 'missing-client'])
def test_all_mutations_reject_changed_identity_without_dispatch(workspace, monkeypatch, route, payload, change):
    handler = u.make_handler('test-token', str(ROOT), str(workspace), 'reviewed-vm')
    payload = {**payload, 'expectedVm': 'reviewed-vm'}
    if change == 'renamed':
        (workspace / 'workspace.yml').write_text('name: replacement\n')
    elif change == 'missing':
        (workspace / 'workspace.yml').unlink()
    elif change == 'wrong-client':
        payload['expectedVm'] = 'replacement'
    else:
        payload.pop('expectedVm')
    monkeypatch.setattr(u.subprocess, 'run', lambda *a, **k: pytest.fail('stale identity dispatched'))
    status, result = post(handler, payload, path=route)
    assert status == 409 and result['error']

@pytest.mark.parametrize('action', ['firewall-on', 'firewall-off'])
def test_stale_policy_revision_blocks_apply_and_refresh(workspace, monkeypatch, action):
    handler = u.make_handler('test-token', str(ROOT), str(workspace), 'reviewed-vm')
    monkeypatch.setattr(u.subprocess, 'run', lambda *a, **k: pytest.fail('stale policy dispatched'))
    status, result = post(handler, {'action': action, 'expectedVm': 'reviewed-vm',
                                  'expectedRevision': '0' * 64}, path='/api/action')
    assert status == 409 and result['configRevision'] == u.read_config(str(workspace))[1]

@pytest.fixture
def dispatch(workspace, tmp_path, monkeypatch, request):
    tool = tmp_path / ("tool 'quoted' $(touch injected-path)" if getattr(request, "param", False) else "tool")
    tool.mkdir()
    for source in ROOT.iterdir():
        if source.is_file() and (source.suffix in ('.sh', '.py', '.just', '.yaml') or source.name == 'justfile'):
            shutil.copy2(source, tool / source.name)
    bindir = tmp_path / 'bin'
    bindir.mkdir()
    log = tmp_path / 'calls.jsonl'
    stub = bindir / 'limactl'
    stub.write_text(f'#!{sys.executable}\n' + '''import json, os, sys
from pathlib import Path
args = sys.argv[1:]
data = sys.stdin.read()
log = Path(os.environ['CALLS'])
first = not log.exists()
with log.open('a') as out:
    out.write(json.dumps({'argv': args, 'stdin': data}) + '\\n')
if first:
    Path('workspace.yml').write_text('name: replacement\\npermissive: true\\negress:\\n  - replacement.example\\n')
if args[0] == 'list':
    print(os.environ.get('VM_STATUS', 'Running'))
elif 'agent_tools.py' in ' '.join(args):
    print('{"ok": true, "pane": "%99"}')
''')
    stub.chmod(0o755)
    monkeypatch.setenv('PATH', str(bindir) + os.pathsep + os.environ['PATH'])
    monkeypatch.setenv('CALLS', str(log))
    monkeypatch.setenv('FLOATIE_SKIP_DISK_CHECK', '1')
    monkeypatch.delenv('FLOATIE_UI_FIXTURES', raising=False)
    def run(recipe, *args, overrides=()):
        result = subprocess.run(['just', '--justfile', str(tool / 'justfile'), '-d', str(workspace),
                                 '--set', 'name', 'reviewed-vm', '--set', 'permissive', 'false',
                                 '--set', 'egress', 'reviewed.example', *overrides, recipe, *args],
                                input='', text=True, capture_output=True, timeout=30)
        calls = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
        return result, calls
    return run

@pytest.mark.parametrize('recipe,args', [('create', ()), ('reload', ()), ('start', ()),
    ('rebuild', ()), ('codex-setup', ()), ('worker', ('review', 'api')),
    ('orchestrate', ()), ('agent-start', ('Legacy',)), ('agent-send', ('Legacy',)),
    ('agent-list', ()), ('agent-read', ('Legacy',)), ('agent-wait', ('Legacy',))])
@pytest.mark.parametrize('race', ['before', 'during'])
def test_nested_just_dispatch_retains_target_and_policy(dispatch, workspace, monkeypatch, recipe, args, race):
    if race == 'before':
        (workspace / 'workspace.yml').write_text('name: replacement\npermissive: true\negress:\n  - replacement.example\n')
    monkeypatch.setenv('VM_STATUS', 'Stopped' if recipe == 'start' else 'Running')
    result, calls = dispatch(recipe, *args)
    assert result.returncode == 0, result.stderr
    assert calls
    for call in calls:
        argv = call['argv']
        assert ('reviewed-vm' in argv or '--name=reviewed-vm' in argv), argv
        assert 'replacement' not in argv
    firewalls = [c for c in calls if 'firewall.sh —' in c['stdin']]
    if recipe in ('create', 'start', 'rebuild', 'codex-setup'):
        assert len(firewalls) == 1 and firewalls[0]['argv'][-1] == 'reviewed.example'
    assert not list(Path(os.environ['CALLS']).parent.rglob('injected-path'))

@pytest.mark.parametrize('dispatch', [True], indirect=True)
def test_literal_identity_and_plugins_cannot_execute(dispatch, workspace):
    name = 'A "$(touch injected-identity)" O\'Brien'
    plugin = 'plug\'"$(touch injected-plugin)"@market'
    result, calls = dispatch('create', overrides=('--set', 'git_user_name', name, '--set', 'plugins', plugin))
    assert result.returncode == 0, result.stderr
    assert any('GIT_USER_NAME=' + name + '\n' in c['stdin'] for c in calls)
    assert any(c['argv'][-1] == plugin for c in calls)
    assert not (workspace / 'injected-identity').exists()
    assert not (workspace / 'injected-plugin').exists()

@pytest.mark.parametrize('mode', ['typo', 'OFF', '$(touch injected-mode)'])
def test_firewall_rejects_unknown_mode_before_lima(dispatch, mode):
    result, calls = dispatch('firewall', mode)
    assert result.returncode != 0 and calls == []

def test_base_domain_query_needs_no_external_commands(tmp_path):
    result = subprocess.run(['/bin/bash', str(ROOT / 'firewall.sh'), '--print-base-domains'],
                            env={'PATH': str(tmp_path)}, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert {'github.com', 'api.openai.com', 'api.anthropic.com'} <= set(result.stdout.splitlines())

def test_timeout_unknown_keeps_phase_output_without_retry(workspace, monkeypatch):
    attempts = []
    def timeout(*args, **kwargs):
        attempts.append(args)
        raise subprocess.TimeoutExpired('just', 600, output=b'phase 1 done\n', stderr=b'phase 2 pending\n')
    monkeypatch.setattr(u.subprocess, 'run', timeout)
    result = u.run_action(str(ROOT), str(workspace), 'start', {}, vm_name='reviewed-vm',
                          policy={'permissive': False, 'egress': ['reviewed.example']})
    assert result['outcome'] == 'unknown' and not result['ok'] and len(attempts) == 1
    assert 'phase 1 done' in result['output'] and 'phase 2 pending' in result['output']

@pytest.mark.parametrize('permissive', [False, True])
def test_http_capture_then_config_race_executes_original_policy(workspace, dispatch, monkeypatch, permissive):
    original = 'name: reviewed-vm\npermissive: ' + str(permissive).lower() + '\negress:\n  - reviewed.example\n'
    (workspace / 'workspace.yml').write_text(original)
    revision = u.read_config(str(workspace))[1]
    read = u.read_config
    reads = []
    def racing_read(ws):
        assert u.CONFIG_LOCK._is_owned()
        result = read(ws)
        reads.append(result)
        (workspace / 'workspace.yml').write_text('name: replacement\npermissive: false\negress:\n  - replacement.example\n')
        return result
    monkeypatch.setattr(u, 'read_config', racing_read)
    handler = u.make_handler('test-token', str(ROOT), str(workspace), 'reviewed-vm')
    status, body = post(handler, {'action': 'firewall-on', 'expectedVm': 'reviewed-vm',
                                'expectedRevision': revision}, path='/api/action')
    assert status == 200 and body['ok'] and len(reads) == 1
    calls = [json.loads(line) for line in Path(os.environ['CALLS']).read_text().splitlines()]
    assert len(calls) == 1
    assert calls[0]['argv'][1] == 'reviewed-vm'
    assert calls[0]['argv'][-1] == ('--off' if permissive else 'reviewed.example')

@pytest.mark.parametrize('displayed,repos,known,expected', [
    (['api','web'], ['api','web'], True, 200), (['api','web'], ['api'], True, 200),
    (['api'], ['api'], True, 409), (['api','web'], ['gone'], True, 409),
    (['api','web'], ['api'], False, 409), (['api','web'], [], True, 400),
])
def test_creation_checks_fresh_available_set_and_never_discovers_scope_later(workspace, dispatch, monkeypatch, displayed, repos, known, expected):
    monkeypatch.setattr(u.LiveProvider, 'inventory', lambda self: {
        'availableRepos': ['api', 'web'], 'inventoryKnown': known})
    handler = u.make_handler('test-token', str(ROOT), str(workspace), 'reviewed-vm')
    status, body = post(handler, {'action': 'worker-create', 'expectedVm': 'reviewed-vm',
        'params': {'name': 'review', 'repos': repos, 'availableRepos': displayed}}, path='/api/action')
    assert status == expected, body
    log = Path(os.environ['CALLS'])
    if expected != 200:
        assert not log.exists()
    else:
        calls = [json.loads(line) for line in log.read_text().splitlines()]
        assert len(calls) == 1 and calls[0]['argv'][-len(repos):] == repos

@pytest.mark.parametrize('action', ['list', 'read'])
def test_agent_reads_remain_pinned_and_report_mismatch(workspace, monkeypatch, action):
    (workspace / 'workspace.yml').write_text('name: replacement\n')
    calls = []
    def run(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, '{"ok":true}', '')
    monkeypatch.setattr(u.subprocess, 'run', run)
    handler = u.make_handler('test-token', str(ROOT), str(workspace), 'reviewed-vm')
    payload = {'action': action}
    if action == 'read': payload['target'] = 'A' * 64
    code, body = post(handler, payload)
    assert code == 200 and body['identity']['matches'] is False
    assert calls[0][2] == 'reviewed-vm'

@pytest.fixture
def host_git(tmp_path, monkeypatch):
    """Point git's global config at a temp file, so `git config --get` is synthetic."""
    config = tmp_path / 'host-gitconfig'
    config.write_text('')
    monkeypatch.setenv('GIT_CONFIG_GLOBAL', str(config))
    monkeypatch.setenv('GIT_CONFIG_NOSYSTEM', '1')
    def set_identity(name, email):
        subprocess.run(['git', 'config', '--file', str(config), 'user.name', name], check=True)
        subprocess.run(['git', 'config', '--file', str(config), 'user.email', email], check=True)
    return set_identity

def identity_files(calls):
    return [c['stdin'] for c in calls if c['stdin'].startswith('GIT_USER_NAME=')]

def test_workspace_identity_wins_over_host_git(dispatch, host_git):
    host_git('Host Person', 'host@example.com')
    result, calls = dispatch('create', overrides=('--set', 'git_user_name', 'Jane Smith',
                                                  '--set', 'git_user_email', 'jane@example.com'))
    assert result.returncode == 0, result.stderr
    assert identity_files(calls) == ['GIT_USER_NAME=Jane Smith\nGIT_USER_EMAIL=jane@example.com\n']
    assert 'from this Mac' not in result.stdout

def test_missing_workspace_identity_falls_back_to_host_git(dispatch, host_git):
    host_git('Host Person', 'host@example.com')
    result, calls = dispatch('create')
    assert result.returncode == 0, result.stderr
    assert identity_files(calls) == ['GIT_USER_NAME=Host Person\nGIT_USER_EMAIL=host@example.com\n']
    assert "==> git identity: from this Mac's git config (Host Person <host@example.com>)" in result.stdout

def test_each_key_falls_back_on_its_own(dispatch, host_git):
    host_git('Host Person', 'host@example.com')
    result, calls = dispatch('create', overrides=('--set', 'git_user_name', 'Jane Smith'))
    assert result.returncode == 0, result.stderr
    assert identity_files(calls) == ['GIT_USER_NAME=Jane Smith\nGIT_USER_EMAIL=host@example.com\n']

def test_both_empty_still_skips_identity_file_and_warns(dispatch, host_git):
    result, calls = dispatch('create')
    assert result.returncode == 0, result.stderr
    assert identity_files(calls) == []
    assert 'both git_user_name and git_user_email are blank' in result.stdout
    assert 'from this Mac' not in result.stdout
