"""Real temporary Git storage; tmux is always a recorded subprocess boundary."""
import importlib
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


@pytest.fixture
def inventory(tmp_path, monkeypatch):
    # Import inside fixture so the initial RED run records the missing interface.
    assert (Path(__file__).resolve().parents[1] / 'worker_inventory.py').exists(), 'shared inventory not implemented'
    module = importlib.import_module('worker_inventory')
    work = tmp_path / 'work'
    work.mkdir()
    boundary = {'sessions': '', 'rc': 0, 'error': '', 'git_error': False, 'calls': []}
    real_run = subprocess.run

    def run(argv, **kwargs):
        boundary['calls'].append(argv)
        if argv[0] == 'tmux':
            assert argv[1:3] == ['-u', 'list-panes'] and '-a' in argv
            return subprocess.CompletedProcess(argv, boundary['rc'], boundary['sessions'], boundary['error'])
        assert argv[0] == 'git', f'unexpected process: {argv}'
        if boundary['git_error']:
            raise FileNotFoundError('git unavailable')
        return real_run(argv, **kwargs)

    monkeypatch.setattr(module.subprocess, 'run', run)
    return module, work, boundary, real_run


def git(run, path, *args):
    return run(['git', '-C', str(path), *args], capture_output=True, text=True, check=True,
               env={**os.environ, 'GIT_CONFIG_GLOBAL': '/dev/null', 'GIT_CONFIG_NOSYSTEM': '1'}).stdout.strip()


def clone(run, work, name='api'):
    path = work / name
    path.mkdir()
    git(run, path, 'init', '-q')
    git(run, path, 'config', 'user.name', 'Inventory Test')
    git(run, path, 'config', 'user.email', 'test@example.invalid')
    (path / 'file').write_text('original\n')
    git(run, path, 'add', 'file')
    git(run, path, 'commit', '-qm', 'initial')
    return path


def saved(run, work, root, name='alpha'):
    path = work / '.worktrees' / name / root.name
    git(run, root, 'worktree', 'add', '-qb', 'worker/' + name, str(path))
    return path


def by_name(result):
    return {row['name']: row for row in result['environments']}


def test_inventory_union_excludes_root_from_human_and_keeps_unknown(inventory, capsys):
    module, work, boundary, run = inventory
    root = clone(run, work)
    path = saved(run, work, root)
    boundary['sessions'] = f'main\t{work}\t{work}\t\nbeta\t{work}/.worktrees/beta\t{work}/.worktrees/beta\tcodex\nlegacy\t/elsewhere\t/elsewhere\t\n'
    result = module.collect(work)
    rows = by_name(result)
    assert list(rows) == ['workspace', 'alpha', 'beta', 'legacy']
    assert rows['workspace']['kind'] == 'workspace' and rows['workspace']['session'] == 'main'
    assert rows['alpha']['sessionLive'] is False and rows['alpha']['scopeKnown'] is True
    assert rows['alpha']['repos'][0]['path'] == str(path)
    assert rows['beta']['repos'] == [] and rows['beta']['scopeKnown'] is False
    assert rows['beta']['kind'] == 'worker'
    assert rows['legacy']['kind'] == 'unknown' and rows['legacy']['managementReason']
    assert rows['legacy']['scopeKnown'] is False
    assert result['inventoryKnown'] is True and result['availableRepos'] == ['api']
    assert result['observedAt'] and result['sessionObservedAt'] and not result['error']
    assert module.main(['--work-dir', str(work), '--human']) == 0
    out = capsys.readouterr().out
    assert 'alpha' in out and 'beta' in out and 'legacy' in out
    assert not any(line.split()[:1] == ['main'] for line in out.splitlines())
    assert 'floatie attach' in out and 'floatie orchestrate' in out


def test_empty_saved_parent_and_plain_child_are_unknown_not_lost(inventory):
    module, work, _, run = inventory
    clone(run, work)
    (work / '.worktrees/empty').mkdir(parents=True)
    (work / '.worktrees/plain/not-a-repo').mkdir(parents=True)
    rows = by_name(module.collect(work))
    assert rows['empty']['repos'] == [] and not rows['empty']['scopeKnown']
    assert rows['empty']['managementReason']
    repo = rows['plain']['repos'][0]
    assert repo['error'] and repo['clean'] is None
    assert not rows['plain']['scopeKnown']


def test_only_actual_clones_and_one_name_per_common_storage(inventory):
    module, work, _, run = inventory
    root = clone(run, work)
    (work / 'stack').mkdir()
    (work / 'not-clone').mkdir()
    (work / 'broken/.git').mkdir(parents=True)
    git(run, root, 'worktree', 'add', '-qb', 'alias', str(work / 'z-alias'))
    (work / 'symlink').symlink_to(root, target_is_directory=True)
    result = module.collect(work)
    assert result['availableRepos'] == ['api']
    assert 'broken' not in result['availableRepos']
    assert result['error'] and result['inventoryKnown'] is False


@pytest.mark.parametrize('damage', ['sibling-gitdir', 'wrong-common', 'missing', 'nested-registration', 'git-symlink', 'duplicate-common'])
def test_saved_identity_matches_producer_and_rejects_partial_scope(inventory, damage):
    module, work, _, run = inventory
    root = clone(run, work)
    path = saved(run, work, root)
    sibling = saved(run, work, root, 'sibling')
    if damage == 'sibling-gitdir':
        (path / '.git').write_text((sibling / '.git').read_text())
    elif damage == 'wrong-common':
        other = clone(run, work, 'other')
        external = work / 'external'
        git(run, other, 'worktree', 'add', '-qb', 'external', str(external))
        (path / '.git').write_text((external / '.git').read_text())
    elif damage == 'missing':
        path.rename(work / 'moved')
    elif damage == 'nested-registration':
        git(run, root, 'worktree', 'add', '-qb', 'nested', str(path.parent / 'nested/child'))
    elif damage == 'git-symlink':
        (path / '.git').unlink()
        (path / '.git').symlink_to(sibling / '.git')
    else:
        git(run, root, 'worktree', 'add', '-qb', 'alias', str(work / 'z-alias'))
        git(run, root, 'worktree', 'add', '-qb', 'second', str(path.parent / 'z-alias'))
    row = by_name(module.collect(work))['alpha']
    assert row['scopeKnown'] is False and row['managementReason']


def test_dirty_and_unpublished_work_is_recognized_without_writes(inventory):
    module, work, boundary, run = inventory
    root = clone(run, work)
    path = saved(run, work, root)
    git(run, path, 'switch', '-c', 'custom')
    (path / 'file').write_text('local commit\n')
    git(run, path, 'commit', '-qam', 'unpublished')
    (path / 'file').write_text('dirty\n')
    before = {p: p.read_bytes() for p in root.rglob('*') if p.is_file()}
    boundary['calls'].clear()
    row = by_name(module.collect(work))['alpha']
    assert row['scopeKnown'] is True and row['repos'][0]['branch'] == 'custom'
    assert row['repos'][0]['clean'] is None  # inventory does not infer safety counts
    assert (path / 'file').read_text() == 'dirty\n'
    assert before == {p: p.read_bytes() for p in root.rglob('*') if p.is_file()}
    for call in boundary['calls']:
        if call[0] == 'git':
            assert call[3] in ('rev-parse', 'worktree', 'symbolic-ref')
            if call[3] == 'worktree': assert call[4] == 'list'


@pytest.mark.parametrize('rc,error,known', [(0, '', True), (1, 'no server running on /tmp/tmux-501/default', True),
                                         (1, 'permission denied', False)])
def test_tmux_empty_vs_failure(inventory, rc, error, known):
    module, work, boundary, run = inventory
    saved(run, work, clone(run, work))
    boundary.update(rc=rc, error=error)
    result = module.collect(work)
    assert result['inventoryKnown'] is known
    assert by_name(result)['alpha']['sessionLive'] is (False if known else None)
    assert bool(result['sessionError']) is not known
    assert bool(result['sessionObservedAt']) is known


def test_unavailable_git_does_not_report_empty_available_scope(inventory):
    module, work, boundary, run = inventory
    saved(run, work, clone(run, work))
    boundary['git_error'] = True
    result = module.collect(work)
    assert result['inventoryKnown'] is False and result['error']
    assert by_name(result)['alpha']['scopeKnown'] is False


def test_unsupported_saved_name_stays_inspectable(inventory):
    module, work, _, run = inventory
    saved(run, work, clone(run, work), 'Manual-Worker')
    row = by_name(module.collect(work))['Manual-Worker']
    assert row['scopeKnown'] is True and row['managementReason']


def test_saved_registration_in_another_common_store_is_not_adopted(inventory):
    module, work, _, run = inventory
    root = clone(run, work)
    path = saved(run, work, root)
    other = clone(run, work, 'other')
    other_path = saved(run, work, other, 'spare')
    # Corrupt only the second store's registration: alpha's own .git still matches
    # api, but other also claims this path. Reconnect rejects that common-dir mismatch.
    gitdir = Path(git(run, other_path, 'rev-parse', '--absolute-git-dir'))
    (gitdir / 'gitdir').write_text(str(path / '.git') + '\n')
    row = by_name(module.collect(work))['alpha']
    assert row['scopeKnown'] is False and row['managementReason']


def test_tmux_executable_unavailable_preserves_saved_row(inventory, monkeypatch):
    module, work, _, run = inventory
    saved(run, work, clone(run, work))
    previous = module.subprocess.run
    def unavailable(argv, **kwargs):
        if argv[0] == 'tmux': raise FileNotFoundError('tmux unavailable')
        return previous(argv, **kwargs)
    monkeypatch.setattr(module.subprocess, 'run', unavailable)
    result = module.collect(work)
    assert result['sessionError'] and not result['sessionObservedAt']
    assert by_name(result)['alpha']['sessionLive'] is None


def test_malformed_tmux_does_not_become_empty_success(inventory):
    module, work, boundary, _ = inventory
    boundary['sessions'] = 'not a session record\n'
    result = module.collect(work)
    assert result['inventoryKnown'] is False and result['sessionError']


def test_json_cli_streams_without_importing_host_modules(inventory, tmp_path):
    module, work, _, run = inventory
    saved(run, work, clone(run, work))
    bindir = tmp_path / 'bin'
    bindir.mkdir()
    tmux = bindir / 'tmux'
    tmux.write_text('#!/bin/sh\nexit 0\n')
    tmux.chmod(0o755)
    result = run([sys.executable, '-', '--work-dir', str(work)],
        input=Path(module.__file__).read_text(), text=True, capture_output=True,
        env={**os.environ, 'PATH': str(bindir) + os.pathsep + os.environ['PATH']}, check=True)
    output = json.loads(result.stdout)
    assert output['availableRepos'] == ['api']
    assert by_name(output)['alpha']['sessionLive'] is False


def test_unborn_symbolic_head_is_available_without_writing_or_inventing_counts(inventory):
    module, work, _, run = inventory
    root = work / 'api'
    root.mkdir()
    git(run, root, 'init', '-q')
    git(run, root, 'symbolic-ref', 'HEAD', 'refs/heads/newborn')
    (root / 'draft').write_text('uncommitted initialization\n')
    before = {p: p.read_bytes() for p in root.rglob('*') if p.is_file()}
    result = module.collect(work)
    assert result['availableRepos'] == ['api']
    assert result['inventoryKnown'] is True and result['error'] is None
    row = by_name(result)['workspace']
    assert row['scopeKnown'] is True
    repo, = row['repos']
    assert repo['branch'] == 'newborn' and not repo.get('error')
    assert all(repo[key] is None for key in ('clean', 'uncommitted', 'unpushed'))
    assert before == {p: p.read_bytes() for p in root.rglob('*') if p.is_file()}


def test_branch_read_failure_keeps_valid_identity_but_unknown_repo_state(inventory, monkeypatch):
    module, work, _, run = inventory
    clone(run, work)
    previous = module.subprocess.run
    def fail_branch(argv, **kwargs):
        if argv[0] == 'git' and (argv[3] == 'symbolic-ref' or '--abbrev-ref' in argv):
            return subprocess.CompletedProcess(argv, 128, '', 'branch read failed')
        return previous(argv, **kwargs)
    monkeypatch.setattr(module.subprocess, 'run', fail_branch)
    result = module.collect(work)
    assert result['availableRepos'] == ['api']
    repo, = by_name(result)['workspace']['repos']
    assert repo['branch'] is None and 'branch read failed' in repo['error']
    assert all(repo[key] is None for key in ('clean', 'uncommitted', 'unpushed'))


def test_detached_head_retains_existing_branch_display(inventory):
    module, work, _, run = inventory
    root = clone(run, work)
    git(run, root, 'checkout', '--detach', '-q')
    result = module.collect(work)
    assert result['availableRepos'] == ['api']
    repo, = by_name(result)['workspace']['repos']
    assert repo['branch'] == 'HEAD' and not repo.get('error')


def test_c_locale_tmux_keeps_inventory_field_separators(tmp_path, monkeypatch):
    """Model real tmux's non-UTF-8 formatting at the executable boundary.

    Under LC_ALL=C, tmux replaces literal format tabs with underscores unless
    the client requests UTF-8 with -u. Underscores in paths cannot be delimiters.
    """
    import worker_inventory as module
    work = tmp_path / 'work_with_underscores'
    work.mkdir()
    bindir = tmp_path / 'bin'
    bindir.mkdir()
    calls = tmp_path / 'tmux-calls.json'
    command = bindir / 'tmux'
    command.write_text(f'#!{sys.executable}\n' + '''import json, os, sys
from pathlib import Path
args = sys.argv[1:]
Path(os.environ['TMUX_TEST_CALLS']).write_text(json.dumps({'args':args,'locale':os.environ.get('LC_ALL')}))
utf8 = bool(args and args[0] == '-u')
if utf8: args = args[1:]
assert args == ['list-panes','-a','-F','#{session_name}\\t#{session_path}\\t#{pane_current_path}\\t#{@floatie-agent}']
line = '\\t'.join(['sentinel',os.environ['TMUX_TEST_WORK'],os.environ['TMUX_TEST_WORK'],''])
print(line.replace('\\t','_') if os.environ.get('LC_ALL') == 'C' and not utf8 else line)
''')
    command.chmod(0o755)
    monkeypatch.setenv('PATH', str(bindir) + os.pathsep + os.environ['PATH'])
    monkeypatch.setenv('TMUX_TEST_CALLS', str(calls))
    monkeypatch.setenv('TMUX_TEST_WORK', str(work))
    result = module.collect(work)
    assert result['inventoryKnown'] is True, result['sessionError']
    assert result['sessionError'] is None and result['sessionObservedAt']
    row = by_name(result)['sentinel']
    assert row['sessionLive'] is True and row['kind'] == 'unknown'
    assert not row['scopeKnown'] and row['repos'] == []
    assert json.loads(calls.read_text())['locale'] == 'C'
