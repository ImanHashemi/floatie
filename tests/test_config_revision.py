"""Config CAS uses actual temporary files; no VM or other external commands."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import os
from pathlib import Path
import threading

import pytest
import ui_server as u


@pytest.fixture
def config_file(tmp_path):
    path = tmp_path / 'workspace.yml'
    path.write_bytes(b'name: demo\r\ncpus: 4\r\n# untouched\r\n')
    path.chmod(0o640)
    return path


def test_revision_hashes_exact_bytes(config_file):
    config, revision, raw = u.read_config(str(config_file.parent))
    assert config['cpus'] == 4
    assert raw == config_file.read_bytes()
    assert revision == hashlib.sha256(raw).hexdigest()


def test_save_and_stale_revision(config_file, monkeypatch):
    monkeypatch.setattr(u.subprocess, 'run', lambda *a, **kw: pytest.fail('save invoked subprocess'))
    ws = str(config_file.parent)
    _, rev, _ = u.read_config(ws)
    config, new_rev = u.save_config(ws, rev, {'cpus': 8})
    assert config['cpus'] == 8 and new_rev == hashlib.sha256(config_file.read_bytes()).hexdigest()
    assert config_file.stat().st_mode & 0o777 == 0o640
    saved = config_file.read_bytes()
    with pytest.raises(u.ConfigConflict) as error:
        u.save_config(ws, rev, {'cpus': 12})
    assert error.value.config['cpus'] == 8 and error.value.revision == new_rev
    assert config_file.read_bytes() == saved


def test_two_concurrent_writers_have_one_winner(config_file):
    ws = str(config_file.parent)
    _, rev, _ = u.read_config(ws)
    gate = threading.Barrier(2)
    def save(cpus):
        gate.wait()
        try:
            return u.save_config(ws, rev, {'cpus': cpus})
        except u.ConfigConflict:
            return 'conflict'
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(save, [8, 12]))
    assert results.count('conflict') == 1
    assert u.read_config(ws)[0]['cpus'] in (8, 12)


@pytest.mark.parametrize('boundary', ['replace', 'fsync'])
def test_atomic_write_failure_keeps_file_and_cleans_temp(config_file, monkeypatch, boundary):
    ws = str(config_file.parent)
    _, rev, before = u.read_config(ws)
    def fail(*args):
        raise OSError('synthetic write failure')
    monkeypatch.setattr(u.os, boundary, fail)
    with pytest.raises(OSError):
        u.save_config(ws, rev, {'cpus': 8})
    assert config_file.read_bytes() == before
    assert list(config_file.parent.iterdir()) == [config_file]


def test_rechecks_external_edit_before_replace(config_file, monkeypatch):
    ws = str(config_file.parent)
    _, rev, _ = u.read_config(ws)
    external = b'name: demo\ncpus: 16\n'
    fsync = u.os.fsync
    def edit(fd):
        fsync(fd)
        config_file.write_bytes(external)
    monkeypatch.setattr(u.os, 'fsync', edit)
    with pytest.raises(u.ConfigConflict) as error:
        u.save_config(ws, rev, {'cpus': 8})
    assert error.value.revision == hashlib.sha256(external).hexdigest()
    assert config_file.read_bytes() == external
    assert list(config_file.parent.iterdir()) == [config_file]


@pytest.mark.parametrize('raw', [b'cpus: 4\n\xff', None])
def test_unreadable_or_invalid_encoding_is_error(tmp_path, raw):
    if raw is not None:
        (tmp_path / 'workspace.yml').write_bytes(raw)
    with pytest.raises((OSError, ValueError)):
        u.read_config(str(tmp_path))


@pytest.mark.parametrize('revision', [None, '', 4, 'not-a-hash'])
def test_revision_required_before_write(config_file, revision):
    before = config_file.read_bytes()
    with pytest.raises(ValueError):
        u.save_config(str(config_file.parent), revision, {'cpus': 8})
    assert config_file.read_bytes() == before


def test_state_config_and_revision_share_one_read(config_file, monkeypatch):
    provider = u.LiveProvider('.', str(config_file.parent), 'demo')
    monkeypatch.setattr(provider, 'vm', lambda: {'status': 'Stopped'})
    monkeypatch.setattr(provider, 'inventory', lambda: {'environments': [], 'availableRepos': []})
    read = u.read_config
    calls = []
    def once(ws):
        calls.append(ws)
        result = read(ws)
        config_file.write_text('name: demo\ncpus: 16\n')
        return result
    monkeypatch.setattr(u, 'read_config', once)
    original = config_file.read_bytes()
    state = u.build_state(provider)
    assert state['config']['cpus'] == 4
    assert state['configRevision'] == hashlib.sha256(original).hexdigest()
    assert len(calls) == 1


@pytest.mark.parametrize('raw', [None, b'cpus: 4\n\xff', b'cpus: [4]\n'])
def test_failed_config_state_is_explicitly_unknown(tmp_path, raw, monkeypatch):
    if raw is not None:
        (tmp_path / 'workspace.yml').write_bytes(raw)
    provider = u.LiveProvider('.', str(tmp_path), 'demo')
    monkeypatch.setattr(provider, 'vm', lambda: {'status': 'Stopped'})
    monkeypatch.setattr(provider, 'inventory', lambda: {'environments': [], 'availableRepos': []})
    state = u.build_state(provider)
    assert state['config'] == {}
    assert state['configRevision'] is None and state['configError']
    assert state['firewall']['permissiveConfigured'] is None
    assert state['firewall']['egress'] is None


@pytest.mark.parametrize('newline', [b'\n', b'\r\n'], ids=['lf', 'crlf'])
@pytest.mark.parametrize('variable,expected', [
    ('name', b'demo'), ('cpus', b'8'), ('memory', b'6.25'), ('permissive', b'true'),
    ('repos', b'https://github.com/myorg/api.git\nhttps://example.com/team/web.git'),
    ('egress', b'first.example\nsecond.example'),
    ('plugins', b'org/marketplace\nplugin@marketplace'),
    ('git_user_name', b'Jane Smith'), ('git_user_email', b'jane@example.com'),
])
def test_saved_config_matches_actual_just_parsers(tmp_path, newline, variable, expected):
    import subprocess
    original = newline.join([
        b'# untouched header', b'name: demo', b'cpus: 4', b'memory: 4GiB',
        b'permissive: false', b'repos: []', b'egress: []', b'plugins: []',
        b'git_user_name: Jane Smith', b'git_user_email: jane@example.com',
        b'custom: [keep, exactly]', b'',
    ])
    path = tmp_path / 'workspace.yml'
    path.write_bytes(original)
    _, revision, _ = u.read_config(str(tmp_path))
    changes = {'cpus': 8, 'memoryGiB': 6.25, 'permissive': True,
               'repos': ['github.com/myorg/api.git', 'https://example.com/team/web.git'],
               'egress': ['first.example', 'second.example'],
               'plugins': ['org/marketplace', 'plugin@marketplace']}
    config, saved_revision = u.save_config(str(tmp_path), revision, changes)
    saved = path.read_bytes()
    assert all(config[key] == value for key, value in changes.items())
    assert saved_revision == hashlib.sha256(saved).hexdigest()
    # Saving must retain untouched bytes and the file's line endings.
    assert saved.startswith(b'# untouched header' + newline + b'name: demo' + newline)
    assert saved.endswith(b'custom: [keep, exactly]' + newline)
    assert saved.count(b'\n') == saved.count(newline)
    assert saved.count(b'\r') == (saved.count(b'\n') if newline == b'\r\n' else 0)
    result = subprocess.run(['just', '--justfile', str(Path(__file__).resolve().parents[1] / 'justfile'),
                             '-d', str(tmp_path), '--evaluate', variable], capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr
    # Keep CR bytes visible; text=True/splitlines() would hide the regression.
    assert result.stdout.rstrip(b'\n') == expected


def test_sparse_section_save_preserves_other_fields_and_rejects_dirty_sibling(config_file):
    ws = str(config_file.parent)
    _, original, _ = u.read_config(ws)
    saved, revision = u.save_config(ws, original, {'memoryGiB': 2.5})
    assert saved['cpus'] == 4 and saved['memoryGiB'] == 2.5
    assert b'# untouched' in config_file.read_bytes()
    with pytest.raises(u.ConfigConflict):
        u.save_config(ws, original, {'repos': ['github.com/example/new']})
    assert u.read_config(ws)[1] == revision
