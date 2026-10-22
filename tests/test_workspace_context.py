"""Exercise placement recipes with real writers/tar and an isolated Lima boundary."""
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
from types import SimpleNamespace

import pytest


REPO = Path(__file__).resolve().parents[1]


@pytest.fixture
def placed_context(tmp_path):
    just = shutil.which('just')
    assert just, 'placement integration tests require just on PATH'
    workspace = tmp_path / "workspace 'quoted' $(touch path-injected)"
    workspace.mkdir()
    (workspace / 'workspace.yml').write_text('name: sample-vm\n')
    guest = tmp_path / 'guest'
    (guest / 'work').mkdir(parents=True)
    host = tmp_path / 'host'
    host.mkdir()
    bindir = tmp_path / 'bin'
    bindir.mkdir()
    for name, executable in [('just', just), ('python3', sys.executable)]:
        (bindir / name).symlink_to(executable)
    log = tmp_path / 'calls.jsonl'
    archives = tmp_path / 'archives'
    archives.mkdir()
    limactl = bindir / 'limactl'
    limactl.write_text(f'#!{sys.executable}\n' + r'''
import json, os, pathlib, subprocess, sys
args = sys.argv[1:]
data = sys.stdin.buffer.read()  # Archives must never pass through a text decoder.
with open(os.environ['TEST_CALLS'], 'a') as log:
    log.write(json.dumps({'argv': args, 'stdin_bytes': len(data)}) + '\n')
if args == ['shell', 'sample-vm', '--', 'bash', '-lc', 'command -v codex >/dev/null']:
    sys.exit(1)
if args[:5] != ['shell', 'sample-vm', '--', 'bash', '-c']:
    sys.exit('unhandled VM operation: ' + repr(args))
script = args[5]
extra = args[6:]
tools = {'.local/share/floatie/' + name for name in
         ('agent_tools.py', 'worker_inventory.py', 'agents.just')}
tools.update(('.local/bin/floatie-worker-init', '.local/bin/floatie-worker-rm'))
context = (extra[:1] == ['--'] and len(extra) >= 2
           and extra[1] in ('AGENTS.md', 'CLAUDE.md', 'work/AGENTS.md', 'work/CLAUDE.md'))
legacy_context = not extra and script == 'mkdir -p "$HOME/work"; cat > "$HOME/work/CLAUDE.md"'
tool = len(extra) == 2 and extra[0] == '--' and extra[1] in tools
archive = (extra in (['--', 'work/.agents/skills'], ['--', '.claude/skills'])
           and script == 'set -eu; mkdir -p "$HOME/$1"; tar -C "$HOME/$1" -xf -')
if not (context or legacy_context or tool or archive):
    sys.exit('unhandled VM operation: ' + repr(args))
if archive:
    (pathlib.Path(os.environ['TEST_ARCHIVES']) / (extra[1].replace('/', '_') + '.tar')).write_bytes(data)
env = {key: os.environ[key] for key in ('PATH', 'TEST_FAILURE', 'TEST_REAL_CAT')}
env['HOME'] = os.environ['TEST_GUEST']
if context or legacy_context:
    if env['TEST_FAILURE'] == 'truncate':
        data = data[:7]  # Successful transport EOF, but incomplete source bytes.
else:
    env['TEST_FAILURE'] = ''
sys.exit(subprocess.run(['bash', '-c', script, *extra], input=data,
                        cwd=env['HOME'], env=env, timeout=15).returncode)
''')
    limactl.chmod(0o755)
    # Fail the actual guest writer after partial output, rather than short-circuiting Lima.
    cat = bindir / 'cat'
    cat.write_text(f'#!{sys.executable}\n' + r'''
import os, sys
if os.environ.get('TEST_FAILURE') == 'write':
    sys.stdout.buffer.write(sys.stdin.buffer.read(7))
    sys.stdout.buffer.flush()
    print('injected write failure', file=sys.stderr)
    sys.exit(42)
os.execv(os.environ['TEST_REAL_CAT'], ['cat', *sys.argv[1:]])
''')
    cat.chmod(0o755)

    def run(recipe='_place-context', failure=''):
        result = subprocess.run(
            [just, '--justfile', str(REPO / 'justfile'), '-d', str(workspace), recipe],
            stdin=subprocess.DEVNULL, text=True, capture_output=True, timeout=30,
            env={'PATH': str(bindir) + os.pathsep + '/usr/bin:/bin', 'HOME': str(host),
                 'TEST_CALLS': str(log), 'TEST_GUEST': str(guest),
                 'TEST_ARCHIVES': str(archives),
                 'TEST_FAILURE': failure, 'TEST_REAL_CAT': shutil.which('cat')},
        )
        assert 'unhandled VM operation' not in result.stderr, result.stderr
        return result

    return SimpleNamespace(workspace=workspace, guest=guest, run=run, log=log,
                           archives=archives)


def test_agents_and_claude_place_without_codex(placed_context):
    p = placed_context
    for name, data in [('AGENTS.md', b'# Shared\r\nExact bytes\x00\xff\n'),
                       ('CLAUDE.md', b'@AGENTS.md\r\n')]:
        (p.workspace / name).write_bytes(data)
    result = p.run()
    assert result.returncode == 0, result.stderr
    for name in ('AGENTS.md', 'CLAUDE.md'):
        assert (p.guest / 'work' / name).is_file(), f'{name} was not placed'
        assert (p.guest / 'work' / name).read_bytes() == (p.workspace / name).read_bytes()
    assert not (p.guest / '.codex').exists()


def test_context_paths_and_contents_remain_literal(placed_context):
    p = placed_context
    data = b'$(touch content-injected)\n`touch other-injected`\n"quotes" \'single\'\n'
    for name in ('AGENTS.md', 'CLAUDE.md'):
        (p.workspace / name).write_bytes(data)
    result = p.run()
    assert result.returncode == 0, result.stderr
    for name in ('AGENTS.md', 'CLAUDE.md'):
        assert (p.guest / 'work' / name).is_file(), f'{name} was not placed'
        assert (p.guest / 'work' / name).read_bytes() == data
    assert not list(p.workspace.parent.rglob('*injected'))


@pytest.mark.parametrize('name', ['AGENTS.md', 'CLAUDE.md'])
def test_failed_context_copy_preserves_existing_file(placed_context, name):
    p = placed_context
    (p.workspace / name).write_bytes(b'replacement instructions\n')
    destination = p.guest / 'work' / name
    destination.write_bytes(b'prior instructions\n')
    result = p.run(failure='write')
    assert result.returncode != 0, 'failed writer was not propagated'
    assert 'injected write failure' in result.stderr
    assert destination.read_bytes() == b'prior instructions\n'
    assert sorted(path.name for path in destination.parent.iterdir()) == [name]


@pytest.mark.parametrize('name', ['AGENTS.md', 'CLAUDE.md'])
def test_truncated_context_stream_preserves_existing_file(placed_context, name):
    p = placed_context
    (p.workspace / name).write_bytes(b'replacement instructions\n')
    destination = p.guest / 'work' / name
    destination.write_bytes(b'prior instructions\n')
    result = p.run(failure='truncate')
    assert result.returncode != 0, 'truncated transfer was accepted'
    assert 'context transfer digest mismatch' in result.stderr
    assert destination.read_bytes() == b'prior instructions\n'
    assert sorted(path.name for path in destination.parent.iterdir()) == [name]


def test_absent_context_does_not_delete_existing_guidance(placed_context):
    p = placed_context
    for name in ('AGENTS.md', 'CLAUDE.md'):
        (p.guest / 'work' / name).write_bytes(b'custom guidance\n')
    result = p.run()
    assert result.returncode == 0, result.stderr
    for name in ('AGENTS.md', 'CLAUDE.md'):
        assert (p.guest / 'work' / name).read_bytes() == b'custom guidance\n'


def test_shared_skill_reaches_both_harness_locations(placed_context):
    p = placed_context
    source = p.workspace / '.agents/skills/example'
    source.mkdir(parents=True)
    data = b'# Skill\r\nExact archive bytes\x00\xff\n'
    (source / 'SKILL.md').write_bytes(data)
    (source / 'linked.md').symlink_to('SKILL.md')
    if sys.platform == 'darwin':
        subprocess.run(['xattr', '-w', 'user.floatie-test', 'synthetic metadata',
                        str(source / 'SKILL.md')], check=True)
    else:
        os.setxattr(source / 'SKILL.md', 'user.floatie-test', b'synthetic metadata')
    destinations = [p.guest / 'work/.agents/skills', p.guest / '.claude/skills']
    for destination in destinations:
        (destination / 'unrelated').mkdir(parents=True)
        (destination / 'unrelated/SKILL.md').write_bytes(b'keep me\n')
    result = p.run()
    assert result.returncode == 0, result.stderr
    transferred = list(p.archives.glob('*.tar'))
    assert len(transferred) == 2
    for archive in transferred:
        with tarfile.open(archive) as contents:
            for member in contents:
                assert not Path(member.name).name.startswith('._')
                assert not any('xattr' in key.lower() for key in member.pax_headers)
    for destination in destinations:
        for name in ('SKILL.md', 'linked.md'):
            placed = destination / 'example' / name
            assert placed.read_bytes() == data
            assert not placed.is_symlink()
            if sys.platform == 'darwin':
                attributes = subprocess.run(['xattr', str(placed)], check=True,
                                            capture_output=True, text=True)
                # macOS may add provenance to any locally created file. Compare
                # with an unarchived control; archive headers above must be clean.
                control = subprocess.run(['xattr', str(destination / 'unrelated/SKILL.md')],
                                         check=True, capture_output=True, text=True)
                assert attributes.stdout.splitlines() == control.stdout.splitlines()
            else:
                assert not os.listxattr(placed)
        assert (destination / 'unrelated/SKILL.md').read_bytes() == b'keep me\n'
        assert not list(destination.rglob('._*'))
