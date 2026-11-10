"""Exercise real just recipes with only the VM boundary replaced."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def recipe(tmp_path):
    bindir = tmp_path / 'bin'
    bindir.mkdir()
    workspace = tmp_path / "workspace 'quoted' $(touch injected-workspace)"
    workspace.mkdir()
    (workspace / 'workspace.yml').write_text('name: example-vm\n')
    log = tmp_path / 'calls.jsonl'
    lima = bindir / 'limactl'
    lima.write_text(f'#!{sys.executable}\n' + '''
import json, os, sys
with open(os.environ['TEST_AGENT_LOG'], 'a') as f:
    f.write(json.dumps({'argv': sys.argv[1:], 'stdin': sys.stdin.read()}) + '\\n')
if sys.argv[1] == 'list':
    print(os.environ.get('TEST_VM_STATUS', 'Running'))
    sys.exit(int(os.environ.get('TEST_VM_EXIT', '0')))
if 'has-session' in sys.argv and 'TEST_LIVE_SESSIONS' in os.environ:
    target = sys.argv[sys.argv.index('-t') + 1]
    sessions = json.loads(os.environ['TEST_LIVE_SESSIONS'])
    matched = target[1:] in sessions if target.startswith('=') else any(s.startswith(target) for s in sessions)
    sys.exit(0 if matched else 1)
print(json.dumps({'ok': True, 'pane': '%99', 'started': False}))
''')
    lima.chmod(0o755)

    df = bindir / 'df'
    df.write_text('#!/bin/sh\nprintf "Filesystem 1024-blocks Used Available Capacity Mounted on\\n"\n'
                  'printf "disk 100000000 0 ${TEST_FREE_KB:-50000000} 1%% /\\n"\n')
    df.chmod(0o755)

    def run(*args, stdin='', env=None):
        result = subprocess.run(
            [shutil.which('just'), '--justfile', str(ROOT / 'justfile'),
             '-d', str(workspace), *args], input=stdin, text=True,
            capture_output=True, timeout=20,
            env={**os.environ, 'PATH': f'{bindir}{os.pathsep}{os.environ["PATH"]}',
                 'TEST_AGENT_LOG': str(log), 'FLOATIE_SKIP_DISK_CHECK': '', **(env or {})},
        )
        calls = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
        return result, calls
    return run


def test_send_keeps_message_on_stdin_and_target_in_argv(recipe):
    message = 'literal $(touch /tmp/must-not-run)\nsecond line "quoted"'
    result, calls = recipe('agent-send', 'worker-a', '--pane', '%42', stdin=message)
    assert result.returncode == 0, result.stderr
    assert calls[-1]['argv'][-4:] == ['send', 'worker-a', '--pane', '%42']
    assert calls[-1]['stdin'] == message
    assert message not in str(calls[-1]['argv'])


def test_wait_forwards_cursor_and_explicit_targets(recipe):
    result, calls = recipe('agent-wait', 'worker-a', '--target', 'worker-b',
                          '--cursor', 'abc', '--seconds', '1')
    assert result.returncode == 0, result.stderr
    assert calls[-1]['argv'][-8:] == ['wait', 'worker-a', '--target', 'worker-b',
                                     '--cursor', 'abc', '--seconds', '1']


def test_install_streams_exact_files_to_atomic_guest_writer(recipe):
    result, calls = recipe('_place-agent-tools')
    assert result.returncode == 0, result.stderr
    expected = {'worker_inventory.py', 'agent_tools.py', 'agents.just', 'worker-init.sh', 'worker-rm.sh'}
    assert len(calls) == len(expected)
    assert {call['stdin'] for call in calls} == {(ROOT / name).read_text() for name in expected}
    for call in calls:
        remote_command = call['argv'][5]
        assert 'mktemp' in remote_command and 'mv ' in remote_command
        assert 'set -eu' in remote_command


def test_vm_justfile_parses_without_host_lima():
    result = subprocess.run([shutil.which('just'), '--justfile', str(ROOT / 'agents.just'),
                             '--summary'], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert 'agent-send' in result.stdout and 'worker-create' in result.stdout


def test_host_reconnect_focuses_exact_agent_pane_before_attach(recipe):
    result, calls = recipe('orchestrate', 'codex')
    assert result.returncode == 0, result.stderr
    commands = [call['argv'][3:] for call in calls[1:]]
    assert commands == [
        ['tmux', 'select-window', '-t', '%99'],
        ['tmux', 'select-pane', '-t', '%99'],
        ['tmux', 'attach-session', '-t', '=main'],
    ]


def test_vm_reconnect_focuses_exact_agent_pane_before_attach(tmp_path):
    home = tmp_path / 'home'
    share = home / '.local/share/floatie'
    share.mkdir(parents=True)
    (share / 'agent_tools.py').write_text('print(\'{"pane": "%99"}\')\n')
    bindir = tmp_path / 'bin'
    bindir.mkdir()
    log = tmp_path / 'tmux.log'
    stub = bindir / 'tmux'
    stub.write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> "$TEST_TMUX_LOG"\n')
    stub.chmod(0o755)
    result = subprocess.run([shutil.which('just'), '--justfile', str(ROOT / 'agents.just'),
                             'orchestrate'], capture_output=True, text=True,
                            env={**os.environ, 'HOME': str(home), 'TEST_TMUX_LOG': str(log),
                                 'PATH': f'{bindir}{os.pathsep}{os.environ["PATH"]}'})
    assert result.returncode == 0, result.stderr
    assert log.read_text().splitlines() == [
        'select-window -t %99', 'select-pane -t %99', 'attach-session -t =main',
    ]


@pytest.mark.parametrize('repos', [(), ('api',), ('api', 'web')])
def test_private_create_passes_frozen_scope_without_host_expansion(recipe, repos):
    result, calls = recipe('_worker-create', 'review', *repos)
    assert result.returncode == 0, result.stderr
    assert len(calls) == 1
    assert calls[0]['argv'] == ['shell', 'example-vm', '--', 'bash', '-s', '--', 'review', *repos]
    assert calls[0]['stdin'] == (ROOT / 'worker-init.sh').read_text()


@pytest.mark.parametrize('repos', [(), ('api',)])
def test_public_worker_attaches_exact_session_after_create(recipe, repos):
    result, calls = recipe('worker', 'review', *repos)
    assert result.returncode == 0, result.stderr
    assert len(calls) == 2
    assert calls[0]['argv'][-(len(repos)+1):] == ['review', *repos]
    assert calls[-1]['argv'][3:] == ['tmux', 'attach-session', '-t', '=review']


def test_public_worker_rejects_more_than_one_repo(recipe):
    result, calls = recipe('worker', 'review', 'api', 'web')
    assert result.returncode != 0 and calls == []


@pytest.mark.parametrize('name', ['workspace', 'all', 'Upper', 'a' * 64, 'ok\nbad', '$(touch injected-worker)'])
def test_host_creation_rejects_invalid_names_literally(recipe, name):
    result, calls = recipe('_worker-create', name, 'api')
    assert result.returncode != 0
    assert 'worker name' in result.stderr
    assert calls == []


@pytest.mark.parametrize('repo', ['.', '..', 'api\nweb', '$(touch injected-repo)'])
def test_host_creation_rejects_invalid_repo_components_literally(recipe, repo):
    result, calls = recipe('_worker-create', 'review', repo)
    assert result.returncode != 0
    assert 'repo name' in result.stderr
    assert calls == []


@pytest.mark.parametrize('recipe_name', ['_worker-create', 'worker'])
def test_worker_disk_preflight_refuses_before_vm_boundary(recipe, recipe_name):
    result, calls = recipe(recipe_name, 'review', env={'TEST_FREE_KB': '1'})
    assert result.returncode != 0
    assert 'free' in result.stderr and calls == []


def test_host_attach_uses_exact_target_and_warn_only_disk_preflight(recipe):
    result, calls = recipe('attach', 'review', env={'TEST_FREE_KB': '1'})
    assert result.returncode == 0, result.stderr
    assert 'WARN' in result.stdout
    assert calls[0]['argv'] == ['list', 'example-vm', '--format', '{{.Status}}']
    assert [c['argv'][3:] for c in calls[1:]] == [
        ['tmux', 'has-session', '-t', '=review'],
        ['tmux', 'attach-session', '-t', '=review'],
    ]


def test_attach_keeps_target_literal(recipe):
    target = '$(touch injected-attach)'
    result, calls = recipe('attach', target)
    assert result.returncode == 0, result.stderr
    assert calls[-1]['argv'][-1] == '=' + target


def test_prefix_collision_does_not_reuse_or_attach_other_worker(recipe):
    result, calls = recipe('attach', 'review', env={'TEST_LIVE_SESSIONS': '["review-other"]'})
    assert result.returncode != 0
    assert calls[0]['argv'] == ['list', 'example-vm', '--format', '{{.Status}}']
    assert [c['argv'][3:] for c in calls[1:]] == [['tmux', 'has-session', '-t', '=review']]
    assert 'floatie worker review' in result.stderr


def test_default_attach_does_not_reuse_prefixed_root_session(recipe, tmp_path):
    result, calls = recipe('attach')
    assert result.returncode == 0, result.stderr
    # Execute the guest shell command with a prefix-sensitive terminal stub.
    command = calls[-1]['argv'][-1]
    bindir = tmp_path / 'root-bin'
    bindir.mkdir()
    home = tmp_path / 'root-home'
    home.mkdir()
    log = tmp_path / 'root-tmux.log'
    tmux = bindir / 'tmux'
    tmux.write_text(f'#!{sys.executable}\n' + '''
import json, os, sys
args = sys.argv[1:]
with open(os.environ['ROOT_TMUX_LOG'], 'a') as out:
    out.write(json.dumps(args) + '\\n')
if args[0] == 'has-session':
    target = args[args.index('-t') + 1]
    sys.exit(1 if target == '=main' else 0)
if args[0] == 'new-session' and '-A' not in args:
    sys.exit(0)
sys.exit('would attach main-other')
''')
    tmux.chmod(0o755)
    executed = subprocess.run(['bash', '-c', command], capture_output=True, text=True,
                              env={**os.environ, 'HOME': str(home), 'ROOT_TMUX_LOG': str(log),
                                   'PATH': f'{bindir}:{os.environ["PATH"]}'})
    assert executed.returncode == 0, executed.stderr
    assert [json.loads(line) for line in log.read_text().splitlines()] == [
        ['has-session', '-t', '=main'], ['new-session', '-s', 'main', '-c', str(home / 'work')],
    ]


@pytest.mark.parametrize('status', ['', 'Stopped'])
def test_attach_refuses_before_vm_boundary_when_vm_is_not_running(recipe, status):
    result, calls = recipe('attach', env={'TEST_VM_STATUS': status})
    assert result.returncode != 0
    assert "'floatie create'" in result.stderr and "'floatie start'" in result.stderr
    assert [c['argv'][0] for c in calls] == ['list']


@pytest.mark.parametrize('recipe_name', ['start', 'stop'])
def test_missing_vm_points_to_the_floatie_command(recipe, recipe_name):
    result, calls = recipe(recipe_name, env={'TEST_VM_STATUS': ''})
    assert result.returncode != 0
    assert "run 'floatie create' first" in result.stderr
    assert [c['argv'][0] for c in calls] == ['list']


@pytest.mark.parametrize('status,code', [('Stopped', '0'), ('', '0'), ('', '1'), ('broken', '0')])
def test_stopped_workers_does_not_boot_vm(recipe, status, code):
    result, calls = recipe('workers', env={'TEST_VM_STATUS': status, 'TEST_VM_EXIT': code})
    assert 'unavailable' in (result.stdout + result.stderr).lower()
    assert [c['argv'] for c in calls] == [['list', '--filter', '.name == "example-vm"', '--format', '{{.Status}}']]


def test_workers_streams_shared_read_only_inventory(recipe):
    result, calls = recipe('workers', env={'TEST_VM_STATUS': 'Running'})
    assert result.returncode == 0, result.stderr
    assert calls[-1]['argv'] == ['shell', 'example-vm', '--', 'python3', '-', '--human']
    assert calls[-1]['stdin'] == (ROOT / 'worker_inventory.py').read_text()


def test_guest_default_uses_literal_installed_justfile_path(tmp_path):
    directory = tmp_path / "guest 'quoted' $(touch injected-guest)"
    directory.mkdir()
    justfile = directory / 'agents.just'
    justfile.write_bytes((ROOT / 'agents.just').read_bytes())
    result = subprocess.run(['just', '--justfile', str(justfile), 'default'],
                            cwd=tmp_path, input='', capture_output=True, text=True)
    assert not (directory / 'injected-guest').exists()
    assert not (tmp_path / 'injected-guest').exists()
    assert result.returncode == 0, result.stderr
    assert 'agent-send' in result.stdout


def test_installed_worker_entrypoints_keep_names_content_and_executable_mode(recipe, tmp_path):
    result, calls = recipe('_place-agent-tools')
    assert result.returncode == 0, result.stderr
    home = tmp_path / "installed 'quoted' home"
    home.mkdir()
    installed = {}
    for call in calls:
        argv = call['argv']
        destination = argv[-1]
        if not destination.startswith('.local/bin/'):
            continue
        # Execute the actual atomic guest writer locally, with a disposable HOME.
        written = subprocess.run(['bash', '-c', argv[5], '--', destination],
                                 input=call['stdin'], text=True, capture_output=True,
                                 env={**os.environ, 'HOME': str(home)})
        assert written.returncode == 0, written.stderr
        path = home / destination
        assert path.stat().st_mode & 0o777 == 0o755
        installed[path.name] = path.read_text()
    assert installed == {'floatie-worker-init': (ROOT / 'worker-init.sh').read_text(),
                         'floatie-worker-rm': (ROOT / 'worker-rm.sh').read_text()}


def test_guest_bare_and_named_default_still_list_recipes(tmp_path):
    outputs = []
    for args in ([], ['default']):
        result = subprocess.run(['just', '--justfile', str(ROOT / 'agents.just'), *args],
                                cwd=tmp_path, capture_output=True, text=True)
        assert result.returncode == 0, result.stderr
        assert 'worker-create' in result.stdout and 'agent-send' in result.stdout
        assert not any(line.strip().startswith('default') for line in result.stdout.splitlines())
        outputs.append(result.stdout)
    assert outputs[0] == outputs[1]


def test_host_worker_hint_is_copyable_from_another_directory(recipe, tmp_path):
    result, _ = recipe('_worker-create', 'review', 'api')
    assert result.returncode == 0, result.stderr
    command = next(line.removeprefix('==> on host: ') for line in result.stdout.splitlines()
                   if line.startswith('==> on host: '))
    bindir = tmp_path / 'hint-bin'
    bindir.mkdir()
    stub = bindir / 'floatie'
    stub.write_text(f'#!{sys.executable}\nimport json, os, sys\n'
                    'print(json.dumps({"argv": sys.argv[1:], "cwd": os.getcwd()}))\n')
    stub.chmod(0o755)
    copied = subprocess.run(['bash', '-c', command], cwd=tmp_path, text=True,
                            capture_output=True, env={**os.environ,
                            'PATH': f'{bindir}:{os.environ["PATH"]}'})
    assert copied.returncode == 0, copied.stderr
    assert json.loads(copied.stdout) == {
        'argv': ['attach', 'review'],
        'cwd': str(tmp_path / "workspace 'quoted' $(touch injected-workspace)"),
    }
    assert not list(tmp_path.rglob('injected-workspace'))
