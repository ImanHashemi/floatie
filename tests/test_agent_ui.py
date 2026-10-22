"""Agent API regressions: validation, safe transport and isolated fixtures."""
import io
import json
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest
import ui_server as u

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / 'tests/fixtures/state_running.json'


@pytest.fixture
def agent_server(monkeypatch, tmp_path):
    monkeypatch.setenv('FLOATIE_UI_FIXTURES', str(FIXTURE))
    (tmp_path / 'workspace.yml').write_text('name: demo\n')
    return u.make_handler('test-token', str(ROOT), str(tmp_path), 'demo')


def post(handler, payload=None, *, raw=None, token='test-token', headers=None, path='/api/agents'):
    """Exercise real HTTP parsing/dispatch without requiring a listening socket."""
    body = raw if raw is not None else json.dumps(payload).encode()
    h = {'Host': '127.0.0.1:8787', 'Content-Type': 'application/json',
         'Content-Length': str(len(body)), **(headers or {})}
    if token is not None:
        h['X-Floatie-Token'] = token
    request = (f'POST {path} HTTP/1.0\r\n' +
               ''.join(f'{k}: {v}\r\n' for k, v in h.items()) + '\r\n').encode() + body
    class Connection:
        def __init__(self):
            self.output = bytearray()
        def settimeout(self, seconds):
            pass
        def makefile(self, *args):
            return io.BytesIO(request)
        def sendall(self, data):
            self.output.extend(data)
    connection = Connection()
    handler(connection, ('127.0.0.1', 12345), SimpleNamespace())
    head, response = bytes(connection.output).split(b'\r\n\r\n', 1)
    return int(head.split()[1]), json.loads(response)


@pytest.mark.parametrize('kwargs', [dict(token=None), dict(token='wrong'),
                                    dict(headers={'Host': 'evil.example:123'})])
def test_agents_require_existing_token_and_loopback_host(agent_server, kwargs):
    assert post(agent_server, {'action': 'list'}, **kwargs)[0] == 403


@pytest.mark.parametrize('payload', [None, [], 'list', 42, {}, {'action': []},
    {'action': 'exec'}, {'action': 'list', 'message': 'oops'},
    {'action': 'read'}, {'action': 'read', 'target': 7},
    {'action': 'read', 'target': '../main'}, {'action': 'read', 'target': 'main\n'},
    {'action': 'read', 'target': '$(touch bad)'}, {'action': 'read', 'target': '--help'},
    {'action': 'read', 'target': 'main', 'pane': '1'},
    {'action': 'read', 'target': 'main', 'pane': []},
    {'action': 'start', 'expectedVm': 'demo', 'target': 'main', 'harness': 'sh'},
    {'action': 'start', 'expectedVm': 'demo', 'target': 'main', 'harness': []},
    {'action': 'start', 'expectedVm': 'demo', 'target': 'main', 'pane': '%1'},
    {'action': 'send', 'expectedVm': 'demo', 'target': 'main', 'message': []},
    {'action': 'send', 'expectedVm': 'demo', 'target': 'main', 'message': '  \n'},
    {'action': 'send', 'expectedVm': 'demo', 'target': 'main', 'message': 'hello\x1b[31m'},
    {'action': 'send', 'expectedVm': 'demo', 'target': 'main', 'message': '\ud800'},
    {'action': 'send', 'expectedVm': 'demo', 'target': 'main', 'message': 'hi', 'extra': True},
])
def test_invalid_shapes_are_400_without_dispatch(agent_server, monkeypatch, payload):
    monkeypatch.setattr(u.subprocess, 'run', lambda *a, **k: pytest.fail('invalid request dispatched'))
    status, body = post(agent_server, payload)
    assert status == 400
    assert body['error']


@pytest.mark.parametrize('raw,headers', [(b'{', {}), (b'\xff', {}),
    (b'{}', {'Content-Length': '-1'}), (b'{}', {'Content-Length': 'abc'}),
    (b'{}', {'Content-Length': '65537'}),
    (b'{}', {'Transfer-Encoding': 'chunked'}),
    (json.dumps({'action': 'send', 'expectedVm': 'demo', 'target': 'main', 'message': 'x' * 65536}).encode(), {})],
    ids=['bad-json', 'bad-utf8', 'negative-length', 'bad-length', 'huge-length', 'chunked', 'huge-body'])
def test_request_bounds_and_bad_json_are_400(agent_server, raw, headers):
    assert post(agent_server, raw=raw, headers=headers)[0] == 400


def test_fixture_launch_read_send_are_deterministic_and_never_touch_vm(agent_server, monkeypatch):
    monkeypatch.setattr(u.subprocess, 'run', lambda *a, **k: pytest.fail('fixture called subprocess'))
    first = post(agent_server, {'action': 'list'})[1]
    assert first == post(agent_server, {'action': 'list'})[1]
    assert {'main', 'alpha', 'beta'} <= {r['target'] for r in first['agents']}
    assert post(agent_server, {'action': 'send', 'expectedVm': 'demo', 'target': 'beta', 'message': 'hi'})[1]['ok'] is False
    start = post(agent_server, {'action': 'start', 'expectedVm': 'demo', 'target': 'main', 'harness': 'claude'})[1]
    assert start['started'] is True and start['harness'] == 'claude'
    assert post(agent_server, {'action': 'start', 'expectedVm': 'demo', 'target': 'main', 'harness': 'codex'})[1]['started'] is False
    before = post(agent_server, {'action': 'read', 'target': 'main'})[1]
    assert before == post(agent_server, {'action': 'read', 'target': 'main'})[1]
    message = 'literal $(touch forbidden)\n<img src=x onerror=alert(1)>'
    sent = post(agent_server, {'action': 'send', 'expectedVm': 'demo', 'target': 'main', 'pane': start['pane'], 'message': message})[1]
    assert sent['status'] == 'input submitted'
    after = post(agent_server, {'action': 'read', 'target': 'main'})[1]
    assert message in after['output'] and after['cursor'] != before['cursor']
    assert after['state'] == 'running' and after['observedAt']
    assert post(agent_server, {'action': 'send', 'expectedVm': 'demo', 'target': 'main', 'pane': '%999', 'message': 'hi'})[1]['ok'] is False
    assert post(agent_server, {'action': 'read', 'target': 'missing'})[1]['ok'] is False


@pytest.mark.parametrize('payload,tail,stdin', [
    ({'action': 'list'}, ['list'], None),
    ({'action': 'read', 'target': 'alpha', 'pane': '%12'}, ['read', 'alpha', '--pane', '%12'], None),
    ({'action': 'start', 'expectedVm': 'demo', 'target': 'main', 'harness': 'claude'}, ['start', 'main', 'claude'], None),
    ({'action': 'send', 'expectedVm': 'demo', 'target': 'alpha', 'pane': '%12', 'message': 'literal $(touch forbidden)\n"quoted"'},
     ['send', 'alpha', '--pane', '%12'], 'literal $(touch forbidden)\n"quoted"'),
])
def test_bridge_uses_fixed_remote_command_argv_and_literal_stdin(agent_server, monkeypatch, payload, tail, stdin):
    monkeypatch.delenv('FLOATIE_UI_FIXTURES')
    calls = []
    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return SimpleNamespace(returncode=0, stdout='{"ok": true}', stderr='')
    monkeypatch.setattr(u.subprocess, 'run', run)
    code, result = post(agent_server, payload)
    assert code == 200 and result['ok'] is True
    if payload['action'] in ('list', 'read'):
        assert result['identity']['matches'] is True
    argv, options = calls[0]
    assert argv == ['limactl', 'shell', 'demo', '--', 'bash', '-lc',
                    'exec python3 "$HOME/.local/share/floatie/agent_tools.py" "$@"', '--', *tail]
    assert options.get('shell', False) is False
    assert options['input'] == stdin
    assert options['text'] is True and 0 < options['timeout'] <= 60
    if stdin:
        assert stdin not in argv


@pytest.mark.parametrize('result,expected', [
    (SimpleNamespace(returncode=1, stdout='{"ok":false,"error":"ambiguous target"}', stderr=''), 'ambiguous target'),
    (SimpleNamespace(returncode=1, stdout='', stderr='helper missing'), 'helper missing'),
    (SimpleNamespace(returncode=0, stdout='not json', stderr=''), 'JSON'),
    (SimpleNamespace(returncode=0, stdout='[]', stderr=''), 'JSON'),
    (subprocess.TimeoutExpired('helper', 30), 'timed out'),
    (OSError('unavailable'), 'unavailable'),
])
def test_helper_failures_remain_visible_json(agent_server, monkeypatch, result, expected):
    monkeypatch.delenv('FLOATIE_UI_FIXTURES')
    def run(*args, **kwargs):
        if isinstance(result, Exception):
            raise result
        return result
    monkeypatch.setattr(u.subprocess, 'run', run)
    status, body = post(agent_server, {'action': 'read', 'target': 'main'})
    assert status in (200, 502)
    assert body['ok'] is False and expected in body['error']


@pytest.mark.parametrize('target', ['Manual-Worker', 'a' * 64])
def test_manually_discovered_target_names_match_helper_contract(agent_server, monkeypatch, target):
    monkeypatch.delenv('FLOATIE_UI_FIXTURES')
    monkeypatch.setattr(u.subprocess, 'run', lambda *a, **k: SimpleNamespace(returncode=0, stdout='{"ok":true}', stderr=''))
    assert post(agent_server, {'action': 'read', 'target': target})[0] == 200


@pytest.mark.parametrize('target', ['all', 'stack'])
def test_reserved_non_agent_targets_rejected(agent_server, target):
    assert post(agent_server, {'action': 'read', 'target': target})[0] == 400


def test_fixture_output_remains_bounded(agent_server):
    for _ in range(3):
        status, body = post(agent_server, {'action': 'send', 'expectedVm': 'demo', 'target': 'alpha', 'message': ('safe synthetic line\n' * 150)})
        assert status == 200 and body['ok']
    output = post(agent_server, {'action': 'read', 'target': 'alpha'})[1]['output']
    assert len(output.splitlines()) <= 200
    assert len(output.encode('utf-8')) <= 65536
