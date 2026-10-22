"""Transport tests: no real tmux server or host process inspection."""
import importlib.util
import io
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest


@pytest.fixture
def api():
    path = Path(__file__).resolve().parents[1] / 'agent_tools.py'
    assert path.exists(), 'Task 1 transport has not been implemented'
    spec = importlib.util.spec_from_file_location('agent_tools', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def process(pid, ppid, argv, group=20, start='123'):
    return dict(pid=pid, ppid=ppid, pgrp=group, tpgid=group,
                state='S', start=start, argv=argv, exe=argv[0])


@pytest.fixture
def boundary(api, monkeypatch, tmp_path):
    monkeypatch.setenv('HOME', str(tmp_path))
    (tmp_path / 'work/.worktrees/alpha').mkdir(parents=True)
    class Boundary:
        panes = [('alpha', '%1', 10, 0, '')]
        processes = {10: process(10, 1, ['/bin/bash'], group=10)}
        calls = []
        output = 'ready\n'
        clock = 0.0
        guard = threading.Lock()
        retained = set()
        exit_immediately = False

        def tmux(self, *args, input=None):
            with self.guard:
                self.calls.append((args, input))
                if args[0] == 'list-panes':
                    return '\n'.join('\t'.join(map(str, p)) for p in self.panes)
                if args[0] in ('new-window', 'new-session'):
                    target = args[args.index('-s')+1] if '-s' in args else args[args.index('-t')+1].lstrip('=')
                    self.panes.append((target, '%2', 20, 0, ''))
                if args[0] in ('new-window', 'new-session', 'respawn-pane'):
                    pane = args[args.index('-t')+1] if args[0] == 'respawn-pane' else '%2'
                    item = next(p for p in self.panes if p[1] == pane)
                    command = args[args.index('-c')+2:]
                    if self.exit_immediately and command[0] in ('codex', 'claude'):
                        self.processes.pop(item[2], None)
                        self.panes.remove(item)
                        if pane in self.retained:
                            self.panes.append((item[0], pane, item[2], 1, item[4]))
                        self.output = 'harness startup failed'
                    else:
                        self.processes[item[2]] = process(item[2], 1, ['/usr/bin/' + command[0], *command[1:]])
                        self.panes[self.panes.index(item)] = (item[0], pane, item[2], 0, item[4])
                    return pane + '\n'
                if args[0] == 'set-option':
                    pane = args[args.index('-t')+1]
                    item = next(p for p in self.panes if p[1] == pane)
                    if args[-2:] == ('remain-on-exit', 'on'):
                        self.retained.add(pane)
                    if args[-2] == '@floatie-agent':
                        self.panes[self.panes.index(item)] = (*item[:4], args[-1])
                if args[0] == 'capture-pane':
                    return self.output
                return ''

        def sleep(self, seconds):
            self.clock += seconds

        def agent(self):
            self.processes[11] = process(11, 10, ['/usr/bin/node', '/opt/lib/node_modules/@openai/codex/bin/codex.js'])
            self.processes[12] = process(12, 11, ['/opt/codex/codex'])
            return self

    b = Boundary()
    b.real_tmux = api.tmux
    monkeypatch.setattr(api, 'tmux', b.tmux)
    monkeypatch.setattr(api, 'processes', lambda: b.processes)
    monkeypatch.setattr(api.time, 'sleep', b.sleep)
    monkeypatch.setattr(api.time, 'monotonic', lambda: b.clock)
    return b


def test_manual_node_wrapper_is_one_primary_and_reused(api, boundary):
    boundary.agent()
    row = api.start('alpha', 'claude')
    assert (row['started'], row['pane'], row['harness'], row['state']) == (False, '%1', 'codex', 'running')
    assert not any(a[0] in ('new-window', 'respawn-pane') for a, _ in boundary.calls)


@pytest.mark.parametrize('argv', [
    ['/bin/echo', 'codex'], ['/usr/bin/node', '/tmp/tool.js', 'claude'],
    ['/usr/bin/node', '-e', 'codex'], ['/bin/bash', '-c', 'claude'],
    ['/usr/bin/python', 'codex'],
])
def test_argument_words_are_not_agents(api, boundary, argv):
    boundary.processes[11] = process(11, 10, argv)
    assert api.list_agents()['agents'][0]['state'] == 'shell'
    with pytest.raises(ValueError):
        api.send('alpha', 'hello')


def test_background_agent_is_not_a_message_target(api, boundary):
    p = process(11, 10, ['/usr/bin/codex'])
    p['tpgid'] = 10
    boundary.processes[11] = p
    with pytest.raises(ValueError):
        api.send('alpha', 'hello')
    with pytest.raises(ValueError):
        api.start('alpha')


def test_ambiguity_and_exact_pane_selection(api, boundary):
    boundary.agent()
    boundary.panes.append(('alpha', '%3', 30, 0, ''))
    boundary.processes[30] = process(30, 1, ['/usr/bin/claude'], group=30)
    row = api.list_agents()['agents'][0]
    assert row['state'] == 'ambiguous' and len(row['candidates']) == 2
    with pytest.raises(ValueError):
        api.send('alpha', 'hello')
    with pytest.raises(ValueError):
        api.start('alpha')
    assert api.read('alpha', '%3')['harness'] == 'claude'
    with pytest.raises(ValueError):
        api.read('alpha', '%999')


def test_start_preserves_shell_and_sets_explicit_worktree(api, boundary):
    result = api.start('alpha')
    assert result['started'] and result['state'] == 'running'
    launch = next(a for a, _ in boundary.calls if a[0] == 'new-window')
    assert launch[launch.index('-c')+1] == str(Path.home() / 'work/.worktrees/alpha')
    respawn = next(a for a, _ in boundary.calls if a[0] == 'respawn-pane')
    assert respawn[respawn.index('-t')+1] == '%2' and '-k' in respawn
    assert ('alpha', '%1', 10, 0, '') in boundary.panes
    assert '-k' not in launch


def test_concurrent_start_only_launches_once(api, boundary):
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: api.start('alpha'), range(2)))
    assert sorted(r['started'] for r in results) == [False, True]
    assert sum(a[0] == 'new-window' for a, _ in boundary.calls) == 1


def test_empty_root_starts_with_shared_policy_and_vm_commands(api, boundary):
    boundary.panes = []
    assert api.start('main')['started']
    launch = next(a for a, _ in boundary.calls if a[0] == 'respawn-pane')
    prompt = launch[-1]
    assert 'agents.just' in prompt and 'orchestrate' in prompt
    assert 'independent' in prompt and 'native' in prompt


@pytest.mark.parametrize('target', ['main', 'alpha'])
def test_new_codex_agents_use_vm_as_sandbox_boundary(api, boundary, target):
    assert api.start(target, 'codex')['started']
    launch = next(a for a, _ in boundary.calls if a[0] == 'respawn-pane')
    command = launch[launch.index('-c')+2:]
    assert command[:-1] == ('codex', '--sandbox', 'danger-full-access',
                           '--ask-for-approval', 'on-request')
    assert command[-1]  # The initial assignment remains one literal argument.


def test_claude_launch_keeps_existing_permission_configuration(api, boundary):
    assert api.start('alpha', 'claude')['started']
    launch = next(a for a, _ in boundary.calls if a[0] == 'respawn-pane')
    command = launch[launch.index('-c')+2:]
    assert command[:-1] == ('claude',)


def test_dead_known_pane_can_be_reused_without_killing_shell(api, boundary):
    boundary.panes = [('alpha', '%2', 20, 1, 'codex')]
    assert api.read('alpha')['state'] == 'dead'
    with pytest.raises(ValueError):
        api.send('alpha', 'hello')
    assert api.start('alpha')['started']
    launch = next(a for a, _ in boundary.calls if a[0] == 'respawn-pane')
    assert launch[launch.index('-t')+1] == '%2' and '-k' not in launch


def test_literal_multiline_buffer_and_separate_enter(api, boundary):
    boundary.agent()
    prompt = 'literal $(touch forbidden)\nsecond line'
    result = api.send('alpha', prompt)
    assert result['status'] == 'input submitted'
    load = next((a, text) for a, text in boundary.calls if a[0] == 'load-buffer')
    assert load[1] == prompt and load[0][-1] == '-'
    paste = next(a for a, _ in boundary.calls if a[0] == 'paste-buffer')
    assert '-p' in paste and '-r' in paste and paste[paste.index('-t')+1] == '%1'
    assert boundary.calls[-1][0] == ('send-keys', '-t', '%1', 'Enter')


@pytest.mark.parametrize('message', ['', ' \n', '\x1b[20~', 'a\x00b', 'x\rEnter', 'ü' * 32769], ids=['empty', 'blank', 'escape', 'nul', 'return', 'oversized'])
def test_invalid_message_never_reaches_tmux(api, boundary, message):
    with pytest.raises(ValueError):
        api.send('alpha', message)
    assert boundary.calls == []


@pytest.mark.parametrize('target', ['-a', '../alpha', 'alpha:1', 'a;b', 'all', ''])
def test_invalid_target_rejected_before_tmux(api, boundary, target):
    with pytest.raises(ValueError):
        api.start(target)
    assert boundary.calls == []


def test_bounded_read_cursor_detects_exit_and_process_replacement(api, boundary):
    boundary.agent()
    boundary.output = ('ü' * 500 + '\n') * 400
    first = api.read('alpha')
    assert len(first['output'].encode()) <= 65536
    assert len(first['output'].splitlines()) <= 200
    assert first['observedAt'] and first['state'] == 'running'
    boundary.processes[11]['start'] = '999'
    assert api.read('alpha')['cursor'] != first['cursor']
    boundary.processes = {10: boundary.processes[10]}
    assert api.read('alpha')['state'] == 'shell'


def test_wait_initial_snapshot_immediate_then_bounded_timeout(api, boundary):
    boundary.agent()
    first = api.wait(['alpha'], seconds=30)
    assert first['changed'] and boundary.clock == 0
    next_result = api.wait(['alpha'], cursor=first['cursor'], seconds=1000)
    assert not next_result['changed'] and boundary.clock == 30
    assert len(next_result['agents']) == 1
    with pytest.raises(ValueError):
        api.wait(['alpha', 'main'], pane='%1')


def test_cli_json_error_and_send_stdin(api, boundary, monkeypatch, capsys):
    boundary.agent()
    monkeypatch.setattr(api.sys, 'stdin', io.StringIO('literal $HOME'))
    assert api.main(['send', 'alpha']) == 0
    assert json.loads(capsys.readouterr().out)['status'] == 'input submitted'
    assert api.main(['read', '../alpha']) != 0
    assert json.loads(capsys.readouterr().out)['ok'] is False


def test_launch_selects_only_new_agent_window(api, boundary):
    api.start('alpha')
    assert ('select-window', '-t', '%2') in [a for a, _ in boundary.calls]
    boundary.calls.clear()
    api.start('alpha')
    assert not any(a[0] == 'select-window' for a, _ in boundary.calls)


def test_proc_parser_handles_parentheses_and_node_symlink(api, monkeypatch, tmp_path):
    proc = tmp_path / 'proc'
    entry = proc / '23'
    entry.mkdir(parents=True)
    # Fields 3..22: state, parent, process group, session, tty, foreground group,
    # then counters through starttime. The comm field can itself contain ')'.
    fields = ['S', '10', '23', '10', '1', '23'] + ['0'] * 13 + ['456']
    (entry / 'stat').write_text('23 (node (wrapper)) ' + ' '.join(fields))
    script = tmp_path / 'lib/node_modules/@openai/codex/bin/codex.js'
    script.parent.mkdir(parents=True)
    script.write_text('')
    alias = tmp_path / 'bin/codex'
    alias.parent.mkdir()
    alias.symlink_to(script)
    (entry / 'cmdline').write_bytes(b'node\0' + str(alias).encode() + b'\0untrusted prompt claude\0')
    (entry / 'exe').symlink_to('/usr/bin/node')
    (entry / 'cwd').symlink_to(tmp_path)
    monkeypatch.setattr(api, 'PROC', proc)
    p = api.processes()[23]
    assert (p['ppid'], p['pgrp'], p['tpgid'], p['start']) == (10, 23, 23, '456')
    assert api.harness(p) == 'codex'


def test_wait_cli_repeated_targets_and_cursor(api, boundary, capsys):
    boundary.agent()
    boundary.panes.append(('main', '%3', 30, 0, ''))
    boundary.processes[30] = process(30, 1, ['/usr/bin/claude'], group=30)
    assert api.main(['wait', 'alpha', '--target', 'main']) == 0
    result = json.loads(capsys.readouterr().out)
    assert [row['target'] for row in result['agents']] == ['alpha', 'main']
    assert api.main(['wait', 'alpha', '--target', 'main', '--cursor', result['cursor'], '--seconds', '0']) == 0
    assert not json.loads(capsys.readouterr().out)['changed']


def test_process_exit_after_paste_does_not_send_enter(api, boundary, monkeypatch):
    boundary.agent()
    def exit_on_sleep(seconds):
        boundary.processes = {10: boundary.processes[10]}
    monkeypatch.setattr(api.time, 'sleep', exit_on_sleep)
    with pytest.raises(ValueError, match='Enter was not sent'):
        api.send('alpha', 'hello')
    assert not any(a[0] == 'send-keys' for a, _ in boundary.calls)


def test_wait_subprocess_budget_cannot_exceed_thirty_seconds(api, boundary, monkeypatch):
    boundary.agent()
    original_tmux = api.tmux
    # Use the real subprocess adapter, but advance fake time at its boundary.
    def run(argv, **kwargs):
        if boundary.clock + 2 > 30:
            assert kwargs['timeout'] <= 30 - boundary.clock
            raise api.subprocess.TimeoutExpired(argv, kwargs['timeout'])
        boundary.clock += 2
        return api.subprocess.CompletedProcess(argv, 0, original_tmux(*argv[1:]), '')
    monkeypatch.setattr(api.subprocess, 'run', run)
    monkeypatch.setattr(api, 'tmux', boundary.real_tmux)
    initial = api.wait(['alpha'])
    boundary.clock = 0
    result = api.wait(['alpha'], cursor=initial['cursor'], seconds=30)
    assert not result['changed'] and boundary.clock <= 30


def test_subprocess_failure_does_not_echo_stderr(api, monkeypatch):
    def run(argv, **kwargs):
        assert isinstance(argv, list) and not kwargs.get('shell')
        return api.subprocess.CompletedProcess(argv, 1, '', 'private secret diagnostic')
    monkeypatch.setattr(api.subprocess, 'run', run)
    with pytest.raises(ValueError) as failure:
        api.tmux('capture-pane', '-p', '-t', '%1')
    assert 'private secret' not in str(failure.value)


def test_running_row_reports_agent_current_directory(api, boundary):
    boundary.agent()
    boundary.processes[11]['cwd'] = str(Path.home() / 'work/.worktrees/alpha/service')
    assert api.read('alpha')['cwd'].endswith('/alpha/service')


def test_unknown_live_known_pane_blocks_duplicate_start(api, boundary):
    boundary.panes = [('alpha', '%1', 10, 0, 'codex')]
    with pytest.raises(ValueError, match='still alive'):
        api.start('alpha')
    assert not any(a[0] in ('new-window', 'respawn-pane') for a, _ in boundary.calls)


@pytest.mark.parametrize('invocation', ['claude', 'version-path', 'launcher-symlink'])
def test_native_versioned_claude_is_discovered_and_reused(api, boundary, invocation):
    executable = Path.home() / '.local/share/claude/versions/2.1.281'
    executable.parent.mkdir(parents=True)
    executable.touch()
    launcher = Path.home() / '.local/bin/claude'
    launcher.parent.mkdir(parents=True)
    launcher.symlink_to(executable)
    argv0 = {'claude': 'claude', 'version-path': str(executable),
             'launcher-symlink': str(launcher)}[invocation]
    p = process(11, 10, [argv0, 'read context'])
    p['exe'] = str(executable)
    boundary.processes[11] = p
    row = api.read('alpha')
    assert (row['state'], row['harness'], row['pane']) == ('running', 'claude', '%1')
    assert api.start('alpha', 'codex')['started'] is False
    assert not any(a[0] in ('new-window', 'respawn-pane') for a, _ in boundary.calls)


@pytest.mark.parametrize('case', [
    'other-directory', 'other-home', 'nested-directory', 'lookalike-directory',
    'not-version', 'argument-only', 'wrong-argv0', 'wrong-launcher', 'empty-argv',
])
def test_versioned_claude_requires_install_path_and_executable_argv(api, boundary, case):
    home = Path.home()
    executable = home / '.local/share/claude/versions/2.1.281'
    p = process(11, 10, ['claude'])
    p['exe'] = str(executable)
    if case == 'other-directory':
        p['exe'] = '/tmp/2.1.281'
    elif case == 'other-home':
        p['exe'] = str(home / 'someone-else/.local/share/claude/versions/2.1.281')
    elif case == 'nested-directory':
        p['exe'] = str(executable.parent / 'nested/2.1.281')
    elif case == 'lookalike-directory':
        p['exe'] = str(home / '.local/share/claude/versions-extra/2.1.281')
    elif case == 'not-version':
        p['exe'] = str(executable.parent / 'tool')
    elif case == 'argument-only':
        p['exe'], p['argv'] = '/bin/echo', ['echo', 'claude', str(executable)]
    elif case == 'wrong-argv0':
        p['argv'] = ['python', 'claude', str(executable)]
    elif case == 'wrong-launcher':
        p['argv'] = ['/tmp/claude', str(executable)]
    else:
        p['argv'] = []
    boundary.processes[11] = p
    assert api.list_agents()['agents'][0]['state'] == 'shell'
    with pytest.raises(ValueError):
        api.send('alpha', 'hello')


@pytest.mark.parametrize('target', ['main', 'alpha'])
def test_immediate_launch_failure_retains_exact_pane_and_diagnostics(api, boundary, target):
    if target == 'main':
        boundary.panes = []
    boundary.exit_immediately = True
    with pytest.raises(ValueError, match='inspect pane %2'):
        api.start(target)
    row = api.read(target, '%2')
    assert row['state'] == 'dead' and row['output'] == 'harness startup failed'
    calls = [a for a, _ in boundary.calls]
    setup = calls.index(('set-option', '-p', '-t', '%2', 'remain-on-exit', 'on'))
    launch = next(i for i, a in enumerate(calls) if a[0] == 'respawn-pane')
    assert setup < launch
    if target == 'alpha':
        assert ('alpha', '%1', 10, 0, '') in boundary.panes


def test_dead_known_pane_sets_retention_before_unforced_respawn(api, boundary):
    boundary.panes = [('alpha', '%2', 20, 1, 'codex')]
    boundary.exit_immediately = True
    with pytest.raises(ValueError, match='inspect pane %2'):
        api.start('alpha')
    assert api.read('alpha', '%2')['output'] == 'harness startup failed'
    launch = next(a for a, _ in boundary.calls if a[0] == 'respawn-pane')
    assert '-k' not in launch


@pytest.mark.parametrize('harness,relative', [('codex', 'codex.js'), ('claude', './cli.js')])
def test_relative_node_script_uses_process_cwd(api, boundary, harness, relative):
    package = '@openai/codex/bin' if harness == 'codex' else '@anthropic-ai/claude-code'
    cwd = Path.home() / 'lib/node_modules' / package
    p = process(11, 10, ['/usr/bin/node', relative, 'some prompt'])
    p['cwd'] = str(cwd)
    boundary.processes[11] = p
    row = api.read('alpha')
    assert row['state'] == 'running' and row['harness'] == harness


def test_relative_node_script_does_not_resolve_against_helper_cwd(api, boundary, monkeypatch):
    helper_cwd = Path.home() / 'lib/node_modules/@openai/codex/bin'
    helper_cwd.mkdir(parents=True)
    monkeypatch.chdir(helper_cwd)
    p = process(11, 10, ['/usr/bin/node', 'codex.js'])
    p['cwd'] = str(Path.home() / 'unrelated')
    boundary.processes[11] = p
    assert api.read('alpha')['state'] == 'shell'


@pytest.mark.parametrize('pane', [None, '%1'])
def test_single_wait_accepts_read_cursor_without_fake_change(api, boundary, pane):
    boundary.agent()
    first = api.read('alpha', pane)
    result = api.wait(['alpha'], pane=pane, cursor=first['cursor'], seconds=1)
    assert not result['changed'] and boundary.clock == 1
    assert result['cursor'] == first['cursor']
    boundary.output = 'new response'
    result = api.wait(['alpha'], pane=pane, cursor=first['cursor'], seconds=1)
    assert result['changed'] and boundary.clock == 1


def test_native_claude_update_keeps_old_running_process_discoverable(api, boundary):
    versions = Path.home() / '.local/share/claude/versions'
    versions.mkdir(parents=True)
    old, new = versions / '2.1.281', versions / '2.1.282'
    old.touch()
    new.touch()
    launcher = Path.home() / '.local/bin/claude'
    launcher.parent.mkdir(parents=True)
    launcher.symlink_to(old)
    p = process(11, 10, [str(launcher)])
    p['exe'] = str(old)
    boundary.processes[11] = p
    before = api.read('alpha')
    assert before['harness'] == 'claude'
    launcher.unlink()
    launcher.symlink_to(new)
    after = api.read('alpha')
    assert (after['state'], after['harness'], after['cursor']) == ('running', 'claude', before['cursor'])
    assert api.start('alpha', 'codex')['started'] is False
    assert not any(a[0] in ('new-window', 'respawn-pane') for a, _ in boundary.calls)


def test_launch_timeout_requests_inspection_without_retry(api, boundary, monkeypatch):
    original = boundary.tmux
    def timeout_after_launch(*args, **kwargs):
        result = original(*args, **kwargs)
        if args[0] == 'respawn-pane':
            raise api.subprocess.TimeoutExpired('tmux', 3)
        return result
    monkeypatch.setattr(api, 'tmux', timeout_after_launch)
    with pytest.raises(ValueError, match='inspect pane %2 before retrying'):
        api.start('alpha')
    assert sum(a[0] == 'respawn-pane' for a, _ in boundary.calls) == 1
    assert api.read('alpha', '%2')['state'] == 'running'
    assert api.start('alpha')['started'] is False
