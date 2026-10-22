"""Exercise provisioning without sudo, apt, Codex, or a real AppArmor kernel."""
import os
from pathlib import Path
import subprocess

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / 'install-codex.sh'
POLICY = 'profile bwrap /usr/bin/bwrap {\n}\nprofile unpriv_bwrap {\n}\n'


@pytest.fixture
def guest(tmp_path):
    root = tmp_path / 'guest'
    for path in ('etc/apparmor.d', 'usr/share/apparmor/extra-profiles',
                 'sys/kernel/security/apparmor', 'bin', 'home'):
        (root / path).mkdir(parents=True, exist_ok=True)
    (root / 'etc/os-release').write_text('ID=ubuntu\nVERSION_ID="24.04"\n')
    (root / 'sys/kernel/security/apparmor/profiles').write_text('')
    # Redirect only filesystem fixtures; production has no test-specific paths.
    script = SCRIPT.read_text()
    for path in ('/etc/os-release', '/etc/apparmor.d', '/usr/share/apparmor',
                 '/sys/kernel/security/apparmor'):
        script = script.replace(path, str(root) + path)
    (root / 'install.sh').write_text(script)
    stubs = {
        'sudo': 'exec "$@"',
        'sysctl': 'echo "${TEST_RESTRICTION:-1}"',
        'bwrap': 'exit 0',
        'codex': 'echo "codex $*" >> "$TEST_LOG"\n'
                 'if [ "$1" = sandbox ]; then exit "${TEST_SANDBOX_EXIT:-0}"; fi',
        'apt-get': 'echo "apt-get $*" >> "$TEST_LOG"\n'
                   '[ "${TEST_PACKAGE_MISSING:-0}" = 0 ] || exit 0\n'
                   'printf "%s" "$TEST_POLICY" > "$TEST_ROOT/usr/share/apparmor/extra-profiles/bwrap-userns-restrict"',
        'apparmor_parser': 'echo "apparmor_parser $*" >> "$TEST_LOG"\n'
                           'printf "bwrap (enforce)\\nunpriv_bwrap (enforce)\\n" > "$TEST_ROOT/sys/kernel/security/apparmor/profiles"',
    }
    for name, body in stubs.items():
        executable = root / 'bin' / name
        executable.write_text('#!/bin/sh\n' + body + '\n')
        executable.chmod(0o755)

    def run(**extra):
        env = {**os.environ, 'HOME': str(root / 'home'),
               'PATH': str(root / 'bin') + os.pathsep + os.environ['PATH'],
               'TEST_ROOT': str(root), 'TEST_LOG': str(root / 'calls'),
               'TEST_POLICY': POLICY, **extra}
        return subprocess.run(['bash', str(root / 'install.sh')], env=env,
                              text=True, capture_output=True)

    return root, run


def calls(root):
    log = root / 'calls'
    return log.read_text() if log.exists() else ''


def test_missing_package_is_installed_and_profile_loaded_once(guest):
    root, run = guest
    for _ in range(2):
        result = run()
        assert result.returncode == 0, result.stderr
    assert (root / 'etc/apparmor.d/bwrap-userns-restrict').read_text() == POLICY
    assert calls(root).count('apt-get install') == 1
    assert calls(root).count('apparmor_parser -r') == 1
    assert calls(root).count('codex sandbox -- /bin/true') == 2


def test_packaged_profile_does_not_install_packages(guest):
    root, run = guest
    (root / 'usr/share/apparmor/extra-profiles/bwrap-userns-restrict').write_text(POLICY)
    result = run()
    assert result.returncode == 0, result.stderr
    assert 'apt-get' not in calls(root)
    assert 'apparmor_parser -r' in calls(root)


def test_existing_active_distribution_profile_is_preserved(guest):
    root, run = guest
    policy = POLICY + '# administrator comment\n'
    (root / 'etc/apparmor.d/bwrap-userns-restrict').write_text(policy)
    (root / 'sys/kernel/security/apparmor/profiles').write_text(
        'bwrap (enforce)\nunpriv_bwrap (enforce)\n')
    result = run()
    assert result.returncode == 0, result.stderr
    assert (root / 'etc/apparmor.d/bwrap-userns-restrict').read_text() == policy
    assert 'apt-get' not in calls(root)
    assert 'apparmor_parser' not in calls(root)


@pytest.mark.parametrize('restriction, release', [
    ('0', 'ID=ubuntu\nVERSION_ID="24.04"\n'),
    ('1', 'ID=ubuntu\nVERSION_ID="25.04"\n'),
    ('1', 'ID=debian\nVERSION_ID="12"\n'),
])
def test_other_systems_and_disabled_restriction_skip_apparmor(guest, restriction, release):
    root, run = guest
    (root / 'etc/os-release').write_text(release)
    result = run(TEST_RESTRICTION=restriction)
    assert result.returncode == 0, result.stderr
    assert 'apt-get' not in calls(root)
    assert 'apparmor_parser' not in calls(root)
    assert 'codex sandbox -- /bin/true' in calls(root)


@pytest.mark.parametrize('name', ['floatie-bwrap', 'vendor-bwrap'])
def test_conflicting_policy_fails_without_changes(guest, name):
    root, run = guest
    conflicting = root / 'etc/apparmor.d' / name
    conflicting.write_text('profile custom /usr/bin/bwrap {\n}\n')
    result = run()
    assert result.returncode == 1
    assert 'administrator reconcile' in result.stderr
    assert not (root / 'etc/apparmor.d/bwrap-userns-restrict').exists()
    assert 'apt-get' not in calls(root)
    assert conflicting.read_text() == 'profile custom /usr/bin/bwrap {\n}\n'


@pytest.mark.parametrize('dangling', [False, True])
def test_explicitly_disabled_policy_is_not_reenabled(guest, dangling):
    root, run = guest
    disabled = root / 'etc/apparmor.d/disable'
    disabled.mkdir()
    if dangling:
        (disabled / 'bwrap-userns-restrict').symlink_to(root / 'missing-policy')
    else:
        (disabled / 'bwrap-userns-restrict').write_text('')
    result = run()
    assert result.returncode == 1
    assert 'explicitly disabled' in result.stderr
    assert 'apparmor_parser' not in calls(root)


def test_package_without_profile_has_actionable_error(guest):
    root, run = guest
    result = run(TEST_PACKAGE_MISSING='1')
    assert result.returncode == 1
    assert 'profile is missing' in result.stderr
    assert 'apparmor_parser' not in calls(root)


def test_failed_sandbox_check_does_not_claim_success(guest):
    root, run = guest
    result = run(TEST_RESTRICTION='0', TEST_SANDBOX_EXIT='1')
    assert result.returncode == 1
    assert 'sandbox verification failed' in result.stderr
