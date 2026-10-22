import json
import hashlib
import pytest
import threading
import urllib.request
import urllib.error
from pathlib import Path
import ui_server as u

FIX = str(Path(__file__).parent / "fixtures" / "state_running.json")


def _start(monkeypatch, tmp_path):
    (tmp_path / "workspace.yml").write_text("name: demo\ncpus: 4\n")
    monkeypatch.setenv("FLOATIE_UI_FIXTURES", FIX)
    httpd = u.make_server("127.0.0.1", 0, token="secret-token",
                          repo_dir=str(Path(__file__).parent.parent), ws_dir=str(tmp_path), vm_name="demo")
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, port


def _get(port, path, token=None, host=None):
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}")
    if token:
        req.add_header("X-Floatie-Token", token)
    if host:
        req.add_header("Host", host)
    return urllib.request.urlopen(req, timeout=5)


def test_state_requires_token(monkeypatch, tmp_path):
    httpd, port = _start(monkeypatch, tmp_path)
    try:
        try:
            _get(port, "/api/state")
            assert False, "expected 403"
        except urllib.error.HTTPError as e:
            assert e.code == 403
        resp = _get(port, "/api/state", token="secret-token")
        data = json.loads(resp.read())
        assert data["vm"]["status"] == "Running"
    finally:
        httpd.shutdown()


def test_non_loopback_host_rejected(monkeypatch, tmp_path):
    httpd, port = _start(monkeypatch, tmp_path)
    try:
        try:
            _get(port, "/api/state", token="secret-token", host="evil.example.com")
            assert False, "expected 403"
        except urllib.error.HTTPError as e:
            assert e.code == 403
    finally:
        httpd.shutdown()


def test_index_served(monkeypatch, tmp_path):
    httpd, port = _start(monkeypatch, tmp_path)
    try:
        resp = _get(port, "/")
        body = resp.read().decode()
        assert "<html" in body.lower()
    finally:
        httpd.shutdown()


def test_action_requires_token(monkeypatch, tmp_path):
    httpd, port = _start(monkeypatch, tmp_path)
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{port}/api/action", method="POST",
                                     data=json.dumps({"action": "start", "expectedVm": "demo"}).encode())
        try:
            urllib.request.urlopen(req, timeout=5)
            assert False, "expected 403"
        except urllib.error.HTTPError as e:
            assert e.code == 403
        # with token, fixture mode echoes the recipe
        req2 = urllib.request.Request(f"http://127.0.0.1:{port}/api/action", method="POST",
                                      data=json.dumps({"action": "start", "expectedVm": "demo"}).encode())
        req2.add_header("X-Floatie-Token", "secret-token")
        body = json.loads(urllib.request.urlopen(req2, timeout=5).read())
        assert body["fixture"] is True and "start" in body["output"]
    finally:
        httpd.shutdown()


def test_config_requires_token_and_fixture_writes_real_file(monkeypatch, tmp_path):
    httpd, port = _start(monkeypatch, tmp_path)
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{port}/api/config", method="POST",
                                     data=json.dumps({"expectedVm": "demo", "expectedRevision": hashlib.sha256((tmp_path / "workspace.yml").read_bytes()).hexdigest(), "changes": {"cpus": 9}}).encode())
        try:
            urllib.request.urlopen(req, timeout=5)
            assert False, "expected 403"
        except urllib.error.HTTPError as e:
            assert e.code == 403
        req2 = urllib.request.Request(f"http://127.0.0.1:{port}/api/config", method="POST",
                                      data=json.dumps({"expectedVm": "demo", "expectedRevision": hashlib.sha256((tmp_path / "workspace.yml").read_bytes()).hexdigest(), "changes": {"cpus": 9}}).encode())
        req2.add_header("X-Floatie-Token", "secret-token")
        body = json.loads(urllib.request.urlopen(req2, timeout=5).read())
        assert body["ok"] is True
        assert u.parse_workspace_yml((tmp_path / "workspace.yml").read_text())["cpus"] == 9
        state = json.loads(_get(port, "/api/state", token="secret-token").read())
        assert state["config"] == body["config"]
        assert state["configRevision"] == body["configRevision"]
    finally:
        httpd.shutdown()


def test_unknown_path_404(monkeypatch, tmp_path):
    httpd, port = _start(monkeypatch, tmp_path)
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{port}/api/nope")
        req.add_header("X-Floatie-Token", "secret-token")
        try:
            urllib.request.urlopen(req, timeout=5)
            assert False, "expected 404"
        except urllib.error.HTTPError as e:
            assert e.code == 404
    finally:
        httpd.shutdown()


def test_config_bad_input_returns_400_no_corruption(monkeypatch, tmp_path):
    monkeypatch.delenv("FLOATIE_UI_FIXTURES", raising=False)  # real (non-fixture) config path
    ws = tmp_path
    wsyml = ws / "workspace.yml"
    original = "name: demo\ncpus: 4\n\nrepos:\n  - github.com/myorg/api\n\npermissive: false\n"
    wsyml.write_text(original)
    httpd = u.make_server("127.0.0.1", 0, token="secret-token",
                          repo_dir=str(Path(__file__).parent.parent), ws_dir=str(ws), vm_name="demo")
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        # newline-injection attempt -> 400, file untouched
        req = urllib.request.Request(f"http://127.0.0.1:{port}/api/config", method="POST",
                                     data=json.dumps({"expectedVm": "demo", "expectedRevision": hashlib.sha256(wsyml.read_bytes()).hexdigest(), "changes": {"repos": ["x\npermissive: true"]}}).encode())
        req.add_header("X-Floatie-Token", "secret-token")
        try:
            urllib.request.urlopen(req, timeout=5)
            assert False, "expected 400"
        except urllib.error.HTTPError as e:
            assert e.code == 400
        assert wsyml.read_text() == original  # not corrupted, no injected key
        assert "permissive: true" not in wsyml.read_text()
        # a valid edit still works (sanity)
        req2 = urllib.request.Request(f"http://127.0.0.1:{port}/api/config", method="POST",
                                      data=json.dumps({"expectedVm": "demo", "expectedRevision": hashlib.sha256(wsyml.read_bytes()).hexdigest(), "changes": {"cpus": 8}}).encode())
        req2.add_header("X-Floatie-Token", "secret-token")
        body = json.loads(urllib.request.urlopen(req2, timeout=5).read())
        assert body["ok"] is True
        assert "cpus: 8" in wsyml.read_text()
    finally:
        httpd.shutdown()


def _post_config(port, payload):
    if isinstance(payload, dict):
        payload = {'expectedVm': 'demo', **payload}
    req = urllib.request.Request(f'http://127.0.0.1:{port}/api/config', method='POST',
        data=json.dumps(payload).encode(), headers={'X-Floatie-Token': 'secret-token'})
    try:
        response = urllib.request.urlopen(req, timeout=5)
    except urllib.error.HTTPError as error:
        response = error
    return response.status, json.loads(response.read())


def test_http_stale_revision_returns_latest_without_vm_or_secrets(monkeypatch, tmp_path):
    server, port = _start(monkeypatch, tmp_path)
    try:
        monkeypatch.setattr(u.subprocess, 'run', lambda *a, **kw: pytest.fail('config invoked VM'))
        (tmp_path / '.env.secrets').write_text('GITHUB_TOKEN=secret-sentinel-never-return\n')
        path = tmp_path / 'workspace.yml'
        rev = hashlib.sha256(path.read_bytes()).hexdigest()
        external = b'name: demo\ncpus: 12\n'
        path.write_bytes(external)
        code, body = _post_config(port, {'expectedRevision': rev, 'changes': {'cpus': 8}})
        assert code == 409
        assert body['configRevision'] == hashlib.sha256(external).hexdigest()
        assert body['config']['cpus'] == 12
        assert path.read_bytes() == external
        assert 'secret-sentinel' not in json.dumps(body)
        code, body = _post_config(port, {'expectedRevision': body['configRevision'], 'changes': {'cpus': 8}})
        assert code == 200 and body['config']['cpus'] == 8
        assert 'secret-sentinel' not in json.dumps(body)
    finally:
        server.shutdown()
        server.server_close()


@pytest.mark.parametrize('payload', [[], None, {}, {'expectedRevision': 'a'*64, 'changes': []}])
def test_http_bad_config_shape_is_400(monkeypatch, tmp_path, payload):
    server, port = _start(monkeypatch, tmp_path)
    try:
        path = tmp_path / 'workspace.yml'
        before = path.read_bytes()
        code, body = _post_config(port, payload)
        assert code == 400 and not body.get('ok')
        assert path.read_bytes() == before
    finally:
        server.shutdown()
        server.server_close()
