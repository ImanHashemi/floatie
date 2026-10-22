import ui_server as u

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
    (tmp_path / ".mcp.json").write_text(MCP)
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
