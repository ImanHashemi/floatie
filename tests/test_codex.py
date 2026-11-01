import importlib.util
import json
from pathlib import Path
import stat
import shlex
import subprocess
import tomllib

import pytest

spec = importlib.util.spec_from_file_location('apply_codex', Path(__file__).resolve().parents[1] / 'apply-codex.py')
codex = importlib.util.module_from_spec(spec)
spec.loader.exec_module(codex)


def test_codex_uses_dedicated_source(tmp_path, monkeypatch):
    target = tmp_path / 'codex'
    monkeypatch.setenv('CODEX_HOME', str(target))
    monkeypatch.setattr(codex.sys, 'argv', ['apply-codex.py', str(tmp_path)])
    for filename, servers in [('.floatie-mcp.json', {'vm-only': {'type': 'http', 'url': 'https://vm.example/mcp'}}),
                              ('.mcp.json', {'project-only': {'type': 'http', 'url': 'https://host.example/mcp'}})]:
        (tmp_path / filename).write_text(json.dumps({'mcpServers': servers}))
    codex.main()
    config = tomllib.loads((target / 'config.toml').read_text())
    assert config['mcp_servers']['vm-only']['url'] == 'https://vm.example/mcp'
    assert 'project-only' not in config['mcp_servers']


def test_mcp_reload_preserves_settings_and_replaces_managed_servers(monkeypatch):
    monkeypatch.setenv('BASIC_TOKEN', 'secret-value')
    source = {'mcpServers': {
        'oauth': {'type': 'http', 'url': 'https://example.com/mcp'},
        'basic': {'type': 'http', 'url': 'https://example.com/basic', 'headers': {'Authorization': '${BASIC_TOKEN}'}},
        'bearer': {'type': 'http', 'url': 'https://example.com/bearer', 'headers': {'Authorization': 'Bearer ${TOKEN}'}},
        'local': {'command': 'npx', 'args': ['-y', 'example'], 'env': {'KEY': '${BASIC_TOKEN}'}},
    }}
    original = '# personal comment\nmodel = "example-model"\n[mcp_servers.personal]\nurl = "https://personal.example/mcp"\n'
    first = codex.render(source, original)
    data = tomllib.loads(first)
    assert data['model'] == 'example-model'
    assert data['mcp_servers']['basic']['env_http_headers']['Authorization'] == 'BASIC_TOKEN'
    assert data['mcp_servers']['bearer']['bearer_token_env_var'] == 'TOKEN'
    assert data['mcp_servers']['local']['env']['KEY'] == 'secret-value'
    assert tomllib.loads(codex.render(source, first)) == data
    removed = tomllib.loads(codex.render({'mcpServers': {}}, first))
    assert set(removed['mcp_servers']) == {'personal'}
    assert '# personal comment' in first


@pytest.mark.parametrize('source, original', [
    ({'mcpServers': {'x': {'type': 'sse', 'url': 'https://example.com'}}}, ''),
    ({'mcpServers': {'x': {'type': 'http', 'url': '${MISSING_FLOATIE_TEST}'}}}, ''),
    ({'mcpServers': {'x': {'type': 'http', 'url': 'https://example.com'}}}, '[mcp_servers.x]\nurl="existing"'),
    ({}, 'invalid = ['),
])
def test_invalid_input_fails_before_writing(source, original):
    with pytest.raises(ValueError):
        codex.render(source, original)


@pytest.mark.parametrize("custom_home", [False, True])
def test_apply_preserves_auth_and_personal_guidance(tmp_path, monkeypatch, custom_home):
    guest = tmp_path / 'guest'
    default = guest / '.codex'
    default.mkdir(parents=True)
    (default / 'sentinel').write_text('keep default state')
    target = guest / 'custom codex state' if custom_home else default
    if custom_home:
        monkeypatch.setenv('CODEX_HOME', str(target))
    else:
        monkeypatch.delenv('CODEX_HOME', raising=False)
    target.mkdir(parents=True, exist_ok=True)
    (target / 'auth.json').write_text('existing-auth')
    (target / 'AGENTS.md').write_text('Personal instructions\n')
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    (workspace / 'AGENTS.md').write_text('Workspace instructions')
    (workspace / 'CLAUDE.md').write_text('Legacy instructions')
    (workspace / '.floatie-mcp.json').write_text(json.dumps({'mcpServers': {
        'vm-only': {'type': 'http', 'url': 'https://vm.example/mcp'}}}))
    monkeypatch.setattr(Path, 'home', lambda: guest)
    monkeypatch.setattr(codex.sys, 'argv', ['apply-codex.py', str(workspace)])
    codex.main()
    codex.main()
    assert (target / 'auth.json').read_text() == 'existing-auth'
    guidance = (target / 'AGENTS.md').read_text()
    assert 'Personal instructions' in guidance
    assert guidance.count('Workspace instructions') == 1
    assert 'Legacy instructions' not in guidance
    assert stat.S_IMODE((target / 'config.toml').stat().st_mode) == 0o600
    config = tomllib.loads((target / 'config.toml').read_text())
    assert config['mcp_servers']['vm-only']['url'] == 'https://vm.example/mcp'
    assert (default / 'sentinel').read_text() == 'keep default state'
    if custom_home:
        assert set(p.name for p in default.iterdir()) == {'sentinel'}


@pytest.mark.parametrize('source_case', ['missing', 'empty', 'project_only', 'both'])
def test_codex_mcp_source_matrix(tmp_path, monkeypatch, source_case):
    workspace = tmp_path / "workspace 'quoted' $(touch path-injected)"
    workspace.mkdir()
    target = tmp_path / 'codex'
    target.mkdir()
    monkeypatch.setenv('CODEX_HOME', str(target))
    monkeypatch.setattr(codex.sys, 'argv', ['apply-codex.py', str(workspace)])
    personal = '[mcp_servers.personal]\nurl = "https://personal.example/mcp"\n'
    original = codex.render({'mcpServers': {'previous': {
        'type': 'http', 'url': 'https://old.example/mcp'}}}, personal)
    (target / 'config.toml').write_text(original)
    if source_case in ('project_only', 'both'):
        (workspace / '.mcp.json').write_text(json.dumps({'mcpServers': {
            'project-only': {'type': 'http', 'url': 'https://host.example/mcp'}}}))
    if source_case in ('empty', 'both'):
        servers = {} if source_case == 'empty' else {
            'vm-only': {'type': 'http', 'url': 'https://vm.example/mcp',
                        'headers': {'X-Literal': '$(touch content-injected)'}}}
        (workspace / '.floatie-mcp.json').write_text(json.dumps({'mcpServers': servers}))
    codex.main()
    first = tomllib.loads((target / 'config.toml').read_text())
    codex.main()
    assert tomllib.loads((target / 'config.toml').read_text()) == first
    assert first['mcp_servers']['personal']['url'] == 'https://personal.example/mcp'
    assert set(first['mcp_servers']) == ({'personal', 'vm-only'} if source_case == 'both' else {'personal'})
    if source_case == 'both':
        assert first['mcp_servers']['vm-only']['url'] == 'https://vm.example/mcp'
        assert first['mcp_servers']['vm-only']['http_headers']['X-Literal'] == '$(touch content-injected)'
    assert not list(tmp_path.rglob('*injected'))


def test_prefixed_header_resolves_at_connection_without_persisting_secret(monkeypatch):
    source = {'mcpServers': {'basic': {'type': 'http', 'url': 'https://example.com/mcp',
        'headers': {'Authorization': 'Basic ${FLOATIE_TEST_TOKEN}'}}}}
    monkeypatch.setenv('FLOATIE_TEST_TOKEN', 'first-secret')
    rendered = codex.render(source, '')
    cfg = tomllib.loads(rendered)['mcp_servers']['basic']
    assert 'http_headers' not in cfg
    assert 'first-secret' not in rendered
    command = shlex.split(cfg['http_headers_helper'])
    def resolved():
        return json.loads(subprocess.check_output(command, text=True))
    assert resolved() == {'Authorization': 'Basic first-secret'}
    monkeypatch.setenv('FLOATIE_TEST_TOKEN', 'rotated-secret')
    assert resolved() == {'Authorization': 'Basic rotated-secret'}
