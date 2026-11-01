import ui_server as u
import json
import pytest


def test_read_secrets_uses_dedicated_source(tmp_path):
    (tmp_path / '.env.secrets').write_text('VM_ONLY_TOKEN=synthetic-secret-value\n')
    for filename, key, name, url in [
        ('.floatie-mcp.json', 'VM_ONLY_TOKEN', 'vm-only', 'https://vm.example/mcp'),
        ('.mcp.json', 'HOST_ONLY_TOKEN', 'project-only', 'https://host.example/mcp'),
    ]:
        (tmp_path / filename).write_text(json.dumps({'mcpServers': {
            name: {'type': 'http', 'url': url,
                   'headers': {'Authorization': 'Bearer ${' + key + '}'}}}}))
    rows = u.read_secrets(str(tmp_path))
    by_key = {row['key']: row['state'] for row in rows}
    assert by_key['VM_ONLY_TOKEN'] == 'set'
    assert 'HOST_ONLY_TOKEN' not in by_key
    assert all(set(row) == {'key', 'state'} for row in rows)
    assert 'synthetic-secret-value' not in repr(rows)

ENV = """\
GITHUB_TOKEN=
ANTHROPIC_API_KEY=github_pat_DUMMY
LINEAR_API_KEY=lin_api_xxx
"""

MCP = '{"mcpServers": {"linear": {"type":"http","headers":{"Authorization":"Bearer ${LINEAR_API_KEY}"}}}}'


def test_classify():
    assert u.classify_secret("GITHUB_TOKEN", "") == "unset"
    assert u.classify_secret("GITHUB_TOKEN", "github_pat_DUMMY") == "placeholder"
    assert u.classify_secret("GITHUB_TOKEN", "ghp_real") == "set"


def test_mcp_referenced_vars():
    assert u.mcp_referenced_vars(MCP) == {"LINEAR_API_KEY"}


def test_read_secrets_reports_state_only(tmp_path):
    (tmp_path / ".env.secrets").write_text(ENV)
    (tmp_path / ".floatie-mcp.json").write_text(MCP)
    rows = u.read_secrets(str(tmp_path))
    by_key = {r["key"]: r["state"] for r in rows}
    # Always-expected core secrets + the MCP-referenced var, all present:
    assert by_key["GITHUB_TOKEN"] == "unset"
    assert by_key["LINEAR_API_KEY"] == "set"
    # CLAUDE_CODE_OAUTH_TOKEN is no longer a core secret — not tracked in .env.secrets.
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in by_key
    # No values leak: each row has only key + state.
    assert all(set(r.keys()) == {"key", "state"} for r in rows)
    assert "lin_api_xxx" not in repr(rows)


def test_read_secrets_missing_file(tmp_path):
    rows = u.read_secrets(str(tmp_path))   # no .env.secrets present
    by_key = {r["key"]: r["state"] for r in rows}
    assert by_key["GITHUB_TOKEN"] == "unset"


@pytest.mark.parametrize('source_case', ['missing', 'empty', 'project_only', 'both'])
def test_read_secrets_source_matrix(tmp_path, source_case):
    workspace = tmp_path / "workspace 'quoted' $(touch path-injected)"
    workspace.mkdir()
    (workspace / '.env.secrets').write_text('VM_ONLY_TOKEN=synthetic-secret-value\n')
    if source_case in ('project_only', 'both'):
        (workspace / '.mcp.json').write_text(json.dumps({'mcpServers': {'project-only': {
            'type': 'http', 'url': 'https://host.example/mcp',
            'headers': {'Authorization': 'Bearer ${HOST_ONLY_TOKEN}'}}}}))
    if source_case in ('empty', 'both'):
        servers = {} if source_case == 'empty' else {'vm-only': {
            'type': 'http', 'url': 'https://vm.example/mcp',
            'headers': {'Authorization': 'Bearer ${VM_ONLY_TOKEN}',
                        'X-Literal': '$(touch content-injected)'}}}
        (workspace / '.floatie-mcp.json').write_text(json.dumps({'mcpServers': servers}))
    rows = u.read_secrets(str(workspace))
    assert u.read_secrets(str(workspace)) == rows
    by_key = {row['key']: row['state'] for row in rows}
    assert 'HOST_ONLY_TOKEN' not in by_key
    if source_case == 'both':
        assert by_key['VM_ONLY_TOKEN'] == 'set'
    else:
        assert 'VM_ONLY_TOKEN' not in by_key
    assert all(set(row) == {'key', 'state'} for row in rows)
    assert 'synthetic-secret-value' not in repr(rows)
    assert not list(tmp_path.rglob('*injected'))
