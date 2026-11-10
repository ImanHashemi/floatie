"""Public discovery and transparent runner forwarding, without real services."""
import json
import os
from pathlib import Path
import re
import shutil
import shlex
import subprocess
import sys
from types import SimpleNamespace

import agent_tools

import pytest

ROOT = Path(__file__).resolve().parents[1]
JUST = shutil.which('just')
CONTRACT = json.loads((ROOT / 'tests/fixtures/command-entrypoints.json').read_text())


def metadata(path):
    result = subprocess.run([JUST, '--justfile', str(path), '--dump', '--dump-format', 'json'],
                            capture_output=True, text=True, check=True)
    return json.loads(result.stdout)


@pytest.fixture
def wrapper(tmp_path):
    # Both asset location and caller CWD must survive spaces, quotes and shell syntax.
    tool = tmp_path / "tool 'quoted' $(touch injected-tool)"
    workspace = tmp_path / "workspace 'quoted' $(touch injected-workspace)"
    bindir = tmp_path / "bin 'quoted'"
    for directory in (tool / 'bin', workspace, bindir):
        directory.mkdir(parents=True)
    for name in ('justfile', 'help.txt', 'bin/floatie'):
        shutil.copy2(ROOT / name, tool / name)
    (bindir / 'link').symlink_to(os.path.relpath(tool / 'bin/floatie', bindir))
    (bindir / 'floatie').symlink_to('link')
    # Never discover a workspace by searching parents or switching to tool CWD.
    (tmp_path / 'workspace.yml').write_text('name: wrong-parent\n')
    (workspace / 'workspace.yml').write_bytes(b'name: synthetic\r\n')
    for name in ('limactl', 'tmux', 'docker'):
        stub = bindir / name
        stub.write_text('#!/bin/sh\necho "unexpected service call" >&2\nexit 99\n')
        stub.chmod(0o755)

    def run(*args, stdin=b'', runner_stub=False):
        if runner_stub:
            stub = bindir / 'just'
            stub.write_text(f'#!{sys.executable}\n' + '''
import json, os, sys
print(json.dumps({'argv': sys.argv[1:], 'cwd': os.getcwd(),
                  'stdin': list(sys.stdin.buffer.read())}))
sys.exit(23)
''')
            stub.chmod(0o755)
        return subprocess.run([str(bindir / 'floatie'), *args], cwd=workspace,
                              input=stdin, capture_output=True, timeout=10,
                              env={**os.environ, 'PATH': f'{bindir}:{os.environ["PATH"]}'})
    yield run, tool, workspace
    assert not list(tmp_path.rglob('injected-*'))


def test_exact_help_forms_and_named_default_show_product_help(wrapper):
    run, _, _ = wrapper
    expected = (ROOT / 'help.txt').read_bytes()
    for args in ((), ('help',), ('--help',), ('-h',), ('default',)):
        result = run(*args)
        assert result.returncode == 0, result.stderr
        assert result.stdout == expected, args


@pytest.mark.parametrize('args', [
    ('--help', '--color', 'never'), ('-h', 'worker'), ('help', 'extra'),
    ('--quiet', 'help'), ('--list',), ('--show', 'worker'), ('--usage', 'agent-send'),
    ('--version',), ('--', '--help'), ('name=other', 'help'),
    ('--set', 'name', "vm 'quoted' $(touch injected-argv)", 'workers'),
    ('agent-send', 'UpperTarget', '--pane', '%42'),
    ('agent-wait', 'a' * 64, '--target', 'Other', '--cursor', 'opaque = "x"', '--seconds', '1'),
    ('worker', 'review', '--help'), ('',),
])
def test_other_argv_stdin_cwd_and_exit_code_are_forwarded(wrapper, args):
    run, tool, workspace = wrapper
    payload = b'line one\r\nquotes \' " $(touch injected-stdin)\n\x00\xff'
    result = run(*args, stdin=payload, runner_stub=True)
    assert result.returncode == 23
    assert result.stderr == b''
    assert json.loads(result.stdout) == {
        'argv': ['--justfile', str(tool / 'justfile'), '-d', str(workspace), *args],
        'cwd': str(workspace), 'stdin': list(payload),
    }


def test_version_is_still_the_runner_version(wrapper):
    run, _, _ = wrapper
    direct = subprocess.run([JUST, '--version'], capture_output=True)
    actual = run('--version')
    assert (actual.returncode, actual.stdout, actual.stderr) == (
        direct.returncode, direct.stdout, direct.stderr)


@pytest.mark.parametrize('filename,count', [('justfile', 32), ('agents.just', 8)])
def test_all_callable_names_and_current_parameters_are_preserved(filename, count):
    data = metadata(ROOT / filename)
    assert len(data['recipes']) == count
    assert set(data['recipes']) == set(CONTRACT[filename])
    assert {name: recipe['parameters'] for name, recipe in data['recipes'].items()} == CONTRACT[filename]
    assert data['aliases'] == {}
    assert data['recipes']['default']['private'] is True
    listed = subprocess.run([JUST, '--justfile', str(ROOT / filename), '--list'],
                            capture_output=True, text=True, check=True).stdout
    assert not re.search(r'^\s+default(?:\s|$)', listed, re.M)
    for name, recipe in data['recipes'].items():
        if not recipe['private']:
            assert re.search(r'^\s+' + re.escape(name) + r'(?:\s|$)', listed, re.M)


def test_help_groups_reference_real_recipes_with_daily_and_setup_order():
    help_text = (ROOT / 'help.txt').read_text()
    daily = help_text.split('Daily commands\n', 1)[1].split('\n\n', 1)[0]
    assert re.findall(r'^  floatie ([a-z-]+)', daily, re.M) == [
        'attach', 'orchestrate', 'worker', 'workers', 'start', 'stop', 'reload', 'help']
    assert help_text.index('floatie ui [port]') < help_text.index('Daily commands')
    setup = help_text.split('Setup (host, in order)\n', 1)[1].split('\n\n', 1)[0]
    assert setup.index('just install') < setup.index('floatie create') < setup.index('floatie codex-setup') < setup.index('floatie attach')
    names = set(re.findall(r'^  floatie ([a-z][a-z-]*)', help_text, re.M))
    public = {n for n, r in metadata(ROOT / 'justfile')['recipes'].items()
              if not r['private'] and n != 'default'}
    assert names | {'install'} == public
    for group in ('Worker maintenance', 'Destructive VM maintenance', 'Repair / UI diagnostics', 'Agent automation'):
        assert '\n' + group + '\n' in help_text
    assert "printf '%s\\n' 'Report progress.' | floatie agent-send review --pane %42" in help_text
    assert 'floatie agent-send review --pane %42 < prompt.txt' in help_text


@pytest.mark.parametrize('args', [(), ('help',), ('--help',), ('-h',)])
def test_help_interception_preserves_stdin_and_cwd(wrapper, args):
    run, tool, workspace = wrapper
    payload = b'untouched\r\n\x00\xff'
    result = run(*args, stdin=payload, runner_stub=True)
    assert result.returncode == 23
    assert json.loads(result.stdout) == {
        'argv': ['--justfile', str(tool / 'justfile'), '-d', str(workspace), 'help'],
        'cwd': str(workspace), 'stdin': list(payload),
    }


def test_compound_help_keeps_runner_usage_errors(wrapper):
    run, tool, workspace = wrapper
    actual = run('help', 'unexpected')
    direct = subprocess.run([JUST, '--justfile', str(tool / 'justfile'), '-d', str(workspace),
                             'help', 'unexpected'], capture_output=True)
    assert actual.returncode != 0
    assert (actual.returncode, actual.stdout, actual.stderr) == (
        direct.returncode, direct.stdout, direct.stderr)


@pytest.mark.parametrize('document', ['help.txt', 'docs/guides/commands.md'])
def test_documented_wait_examples_accept_their_corresponding_cursors(
        document, monkeypatch, tmp_path, capsys):
    """Run published argv through the real parser/wait, with synthetic observations."""
    monkeypatch.setenv('HOME', str(tmp_path))
    for target in ('review', 'other'):
        (tmp_path / 'work/.worktrees' / target).mkdir(parents=True)
    reads = []
    def read(target, pane=None):
        reads.append((target, pane))
        return {'ok': True, 'target': target, 'pane': pane,
                'cursor': f'{target}/{pane}/unchanged', 'output': 'synthetic output'}
    monkeypatch.setattr(agent_tools, 'read', read)
    def forbidden(*args, **kwargs):
        pytest.fail('documentation test reached real terminal/process boundary')
    monkeypatch.setattr(agent_tools, 'tmux', forbidden)
    monkeypatch.setattr(agent_tools, 'processes', forbidden)
    clock = [0.0]
    def sleep(seconds):
        clock[0] += seconds
    monkeypatch.setattr(agent_tools, 'time', SimpleNamespace(
        monotonic=lambda: clock[0], sleep=sleep))

    def invoke(args):
        status = agent_tools.main(args)
        response = json.loads(capsys.readouterr().out)
        assert status == 0, response
        assert response['ok'], response
        return response

    # Single-pane cursor comes from its read; aggregate cursor comes from its wait.
    cursor = invoke(['read', 'review', '--pane', '%42'])['cursor']
    multi_cursor = invoke(['wait', 'review', '--target', 'other', '--seconds', '0'])['cursor']
    variables = {'$cursor': cursor, '$multi_cursor': multi_cursor}
    examples = [shlex.split(line.strip())[1:] for line in (ROOT / document).read_text().splitlines()
                if line.strip().startswith('floatie agent-wait ') and '<' not in line]
    observed_modes = set()
    for example in examples:
        reads.clear()
        before = clock[0]
        args = ['wait', *[variables.get(arg, arg) for arg in example[1:]]]
        result = invoke(args)
        assert result['changed'] is False, 'cursor must match the corresponding observation'
        assert result['cursor'] == args[args.index('--cursor') + 1]
        assert clock[0] - before == float(args[args.index('--seconds') + 1])
        observed_modes.add(frozenset(reads))
    assert observed_modes == {
        frozenset({('review', '%42')}),
        frozenset({('review', None), ('other', None)}),
    }, 'publish both single-pane and multi-target examples'


def test_product_text_names_the_floatie_wrapper_not_bare_just_recipes():
    """A workspace has no justfile, so `just <recipe>` advice cannot be followed there."""
    recipes = sorted((name for name in CONTRACT['justfile']
                      if not name.startswith('_') and name != 'install'), key=len, reverse=True)
    bare = re.compile(r'\bjust\s+(?:%s)\b(?!-)' % '|'.join(map(re.escape, recipes)))
    sources = [ROOT / 'justfile', ROOT / 'agents.just', ROOT / 'help.txt', ROOT / 'lima.yaml',
               ROOT / 'bin/floatie', *ROOT.glob('*.sh'), *ROOT.glob('*.py'), *ROOT.glob('*.md'),
               *(ROOT / 'docs').rglob('*.md'),
               *(path for path in (ROOT / 'workspace-template').rglob('*') if path.is_file())]
    offending = [f'{path.relative_to(ROOT)}:{number}: {line.strip()}'
                 for path in sorted(sources)
                 for number, line in enumerate(path.read_text().splitlines(), 1)
                 if bare.search(line)]
    assert offending == []
