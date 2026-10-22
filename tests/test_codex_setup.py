"""Run the real setup/firewall recipes without contacting a Lima VM."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest


REPO = Path(__file__).resolve().parents[1]


@pytest.fixture
def setup(tmp_path):
    just = shutil.which('just')
    assert just, 'recipe integration tests require just on PATH'
    workspace = tmp_path / 'workspace with spaces'
    workspace.mkdir()
    bindir = tmp_path / 'bin'
    bindir.mkdir()
    log = tmp_path / 'calls.jsonl'
    # Keep real just dispatch and workspace parsing; intercept only the VM boundary.
    # Record stdin so passing the wrong guest script cannot satisfy these tests.
    limactl = bindir / 'limactl'
    limactl.write_text(f'#!{sys.executable}\n' + '''
import json, os, pathlib, sys
script = sys.stdin.read()
with open(os.environ['TEST_CALLS'], 'a') as log:
    log.write(json.dumps({'argv': sys.argv[1:], 'script': script}) + '\\n')
if script == pathlib.Path(os.environ['TEST_FIREWALL']).read_text():
    print('guest firewall completed', flush=True)
    sys.exit(int(os.environ['TEST_FIREWALL_EXIT']))
''')
    limactl.chmod(0o755)

    def run(config='', firewall_exit=0):
        (workspace / 'workspace.yml').write_text('name: sample-vm\n' + config)
        result = subprocess.run(
            [just, '--justfile', str(REPO / 'justfile'), '-d', str(workspace),
             'codex-setup'], text=True, capture_output=True, timeout=30,
            env={**os.environ, 'PATH': str(bindir) + os.pathsep + os.environ['PATH'],
                 'TEST_CALLS': str(log), 'TEST_FIREWALL': str(REPO / 'firewall.sh'),
                 'TEST_FIREWALL_EXIT': str(firewall_exit)},
        )
        calls = [json.loads(line) for line in log.read_text().splitlines()]
        return result, calls

    return run


@pytest.mark.parametrize('config, firewall_args', [
    ('', []),
    ('egress:\n  - api.example.com  # custom service\n  - packages.example.net\n',
     ['--', 'api.example.com', 'packages.example.net']),
    ('permissive: true\negress:\n  - api.example.com\n', ['--', '--off']),
])
def test_setup_refreshes_workspace_firewall_before_login(setup, config, firewall_args):
    # Catches the missing refresh, bypassing workspace policy, or advising login early.
    result, calls = setup(config)
    assert result.returncode == 0, result.stderr
    firewall_calls = [call for call in calls
                      if call['script'] == (REPO / 'firewall.sh').read_text()]
    assert len(firewall_calls) == 1, 'setup did not refresh the guest firewall once'
    assert firewall_calls[0]['argv'] == ['shell', 'sample-vm', '--', 'bash', '-s',
                                       *firewall_args]
    assert result.stdout.index('guest firewall completed') < result.stdout.index('Ready:')
    assert 'codex login --device-auth' in result.stdout


@pytest.mark.parametrize('config', ['', 'permissive: true\n'])
def test_failed_firewall_does_not_report_ready_or_advise_login(setup, config):
    # Catches ignored refresh errors and a Ready message printed before refresh.
    result, _ = setup(config, firewall_exit=42)
    assert 'Ready:' not in result.stdout
    assert 'codex login' not in result.stdout
    assert result.returncode != 0
