"""Public static bytes stay confined to a synthetic production build."""
from contextlib import contextmanager
from http.client import HTTPConnection
from pathlib import Path
import threading

import pytest

import ui_server

SENTINEL = b"outside-build-sentinel"
HEADERS = {
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
}


@contextmanager
def running_server(repo, workspace):
    server = ui_server.make_server("127.0.0.1", 0, "test-token", str(repo), str(workspace), "demo")
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
    thread.start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def request(port, path, *, host=None, token=None, method="GET"):
    connection = HTTPConnection("127.0.0.1", port, timeout=5)
    headers = {}
    if host is not None:
        headers["Host"] = host
    if token is not None:
        headers["X-Floatie-Token"] = token
    try:
        connection.request(method, path, headers=headers)
        response = connection.getresponse()
        return response.status, dict(response.getheaders()), response.read()
    finally:
        connection.close()


@pytest.fixture
def built_repo(tmp_path):
    repo = tmp_path / "tool"
    build = repo / "ui" / "dist"
    assets = build / "assets"
    assets.mkdir(parents=True)
    (build / "index.html").write_bytes(b'<!doctype html><html><script src="/assets/entry-Ab123.js"></script></html>')
    (assets / "entry-Ab123.js").write_bytes(b"/* bundled javascript */")
    (assets / "entry-Ab123.css").write_bytes(b"body { color: teal; }")
    (assets / "nested").mkdir()
    (assets / "nested" / "icon-123.svg").write_bytes(b'<svg xmlns="http://www.w3.org/2000/svg"/>')
    for path in [repo / "ui" / "index.html", repo / "ui_server.py", repo / ".env.secrets",
                 repo / "workspace.yml", build / "secret.js", assets / "entry.js.map",
                 assets / "source.ts", assets / "source.tsx", assets / "secrets.json",
                 assets / "page.html", assets / ".env.secrets"]:
        path.write_bytes(SENTINEL)
    (assets / "escape.js").symlink_to(repo / ".env.secrets")
    (assets / "escape-dir").symlink_to(repo, target_is_directory=True)
    (assets / "loop.js").symlink_to(assets / "loop.js")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "workspace.yml").write_text("name: demo\ncpus: 4\n")
    (workspace / ".env.secrets").write_bytes(SENTINEL)
    return repo, workspace


@pytest.fixture
def static_server(built_repo):
    with running_server(*built_repo) as port:
        yield port


@pytest.mark.parametrize("path,content_type,body", [
    ("/", "text/html; charset=utf-8", b"<!doctype html>"),
    ("/?t=test-token", "text/html; charset=utf-8", b"<!doctype html>"),
    ("/assets/entry-Ab123.js", "text/javascript; charset=utf-8", b"/* bundled javascript */"),
    ("/assets/entry-Ab123.css", "text/css; charset=utf-8", b"body { color: teal; }"),
    ("/assets/nested/icon-123.svg", "image/svg+xml", b"<svg"),
])
def test_production_entry_and_assets(static_server, path, content_type, body):
    status, headers, data = request(static_server, path)
    assert status == 200
    assert headers["Content-Type"] == content_type
    assert data.startswith(body)
    assert SENTINEL not in data
    assert int(headers["Content-Length"]) == len(data)
    assert {key: headers.get(key) for key in HEADERS} == HEADERS


@pytest.mark.parametrize("path", [
    "/assets/../secret.js", "/assets/../../.env.secrets",
    "/assets/%2e%2e/secret.js", "/assets/%252e%252e/secret.js",
    "/assets/%2e%2e%2fsecret.js", "/assets/%2E%2E%5Csecret.js",
    "/assets/nested%2ficon-123.svg", "/assets/nested%5cicon-123.svg",
    "/assets/%256eested%252ficon-123.svg", "/assets/entry-Ab123.js%00",
    "/assets/%00entry-Ab123.js", "/assets/%ff.js", "/assets/%zz.js",
    "/assets//entry-Ab123.js", "/assets/./entry-Ab123.js", "/assets/entry-Ab123.js/",
    "/assets/escape.js", "/assets/escape-dir/.env.secrets", "/assets/loop.js",
    "/assets/", "/assets/nested", "/assets/missing.js", "/assets/entry.js.map",
    "/assets/source.ts", "/assets/source.tsx", "/assets/secrets.json",
    "/assets/page.html", "/assets/.env.secrets", "/assets/entry-Ab123.js.map",
    "/ui/index.html", "/ui/dist/index.html", "/ui/src/main.tsx", "/ui_server.py",
    "/workspace.yml", "/.env.secrets", "/index.html", "/settings",
])
def test_static_path_cannot_escape_build(static_server, path):
    status, headers, body = request(static_server, path)
    assert status == 404
    assert SENTINEL not in body
    assert {key: headers.get(key) for key in HEADERS} == HEADERS


@pytest.mark.parametrize("path", ["/", "/assets/entry-Ab123.js", "/api/state", "/api/nope"])
def test_bad_host_precedes_public_and_api_bytes(static_server, path):
    status, headers, body = request(static_server, path, host="evil.example", token="test-token")
    assert status == 403
    assert SENTINEL not in body
    assert {key: headers.get(key) for key in HEADERS} == HEADERS


@pytest.mark.parametrize("token,status", [(None, 403), ("wrong", 403), ("test-token", 404)])
def test_unknown_api_is_guarded_and_never_spa(static_server, token, status):
    actual, headers, body = request(static_server, "/api/nope", token=token)
    assert actual == status
    assert headers["Content-Type"] == "application/json"
    assert b"<html" not in body and SENTINEL not in body
    assert {key: headers.get(key) for key in HEADERS} == HEADERS


@pytest.mark.parametrize("entry_kind", ["missing", "directory", "symlink", "build-symlink"])
def test_missing_build_is_actionable_and_never_serves_source(built_repo, entry_kind):
    repo, _ = built_repo
    build = repo / "ui" / "dist"
    entry = build / "index.html"
    entry.unlink()
    if entry_kind == "directory":
        entry.mkdir()
    elif entry_kind == "symlink":
        entry.symlink_to(repo / "ui" / "index.html")
    elif entry_kind == "build-symlink":
        build.rename(repo / "old-build")
        build.symlink_to(repo / "old-build", target_is_directory=True)
    with running_server(*built_repo) as port:
        status, headers, body = request(port, "/")
    assert status == 503
    assert b"build" in body and b"<script" not in body
    assert SENTINEL not in body
    assert {key: headers.get(key) for key in HEADERS} == HEADERS


def test_unsupported_method_has_security_headers(static_server):
    status, headers, _ = request(static_server, "/", method="PUT")
    assert status == 501
    assert {key: headers.get(key) for key in HEADERS} == HEADERS


@pytest.mark.parametrize("token,expected", [("test-token", 200), ("wrong", 403)])
def test_api_responses_share_security_headers(static_server, monkeypatch, token, expected):
    monkeypatch.setenv("FLOATIE_UI_FIXTURES", str(Path(__file__).parent / "fixtures/state_running.json"))
    status, headers, body = request(static_server, "/api/state", token=token)
    assert status == expected
    assert headers["Content-Type"] == "application/json"
    assert {key: headers.get(key) for key in HEADERS} == HEADERS
    assert SENTINEL not in body


def test_static_response_contract_and_literal_nul(built_repo):
    from ui_assets import static_response
    build = built_repo[0] / "ui/dist"
    for route in ["/api/state", "/api/nope", "/settings", "/ui/src/main.tsx"]:
        assert static_response(build, route) is None
    for route in ["/assets/file\0.js", "/assets/nested\\icon-123.svg"]:
        response = static_response(build, route)
        assert response.status == 404
        assert SENTINEL not in response.body
