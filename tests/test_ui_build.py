"""Exercise real build parity, Python delivery and authenticated shell bootstrap."""
from html.parser import HTMLParser
import os
from pathlib import Path
import shutil
import subprocess
from urllib.parse import urlsplit

from playwright.sync_api import expect, sync_playwright
import pytest

from test_ui_assets import HEADERS, request, running_server
from ui_browser_helpers import synthetic_ui_server

ROOT = Path(__file__).resolve().parents[1]


class AssetReferences(HTMLParser):
    def __init__(self):
        super().__init__()
        self.assets = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "script" and "src" in attrs:
            self.assets.append(attrs["src"])
        if tag == "link" and attrs.get("rel") in {"stylesheet", "modulepreload", "icon"}:
            self.assets.append(attrs["href"])


def test_built_index_references_are_served_by_python(tmp_path):
    parser = AssetReferences()
    parser.feed((ROOT / "ui/dist/index.html").read_text())
    assert parser.assets, "production entry must load its bundle"
    with running_server(ROOT, tmp_path) as port:
        for path in parser.assets:
            assert path.startswith("/assets/") and "?" not in path
            status, headers, body = request(port, path)
            assert status == 200
            assert body == (ROOT / "ui/dist" / path.lstrip("/")).read_bytes()
            assert {key: headers.get(key) for key in HEADERS} == HEADERS
    assert not list((ROOT / "ui/dist").rglob("*.map"))


def test_built_javascript_contains_distributed_notices(tmp_path):
    notices = (ROOT / "ui/THIRD_PARTY_NOTICES.txt").read_bytes()
    assert notices.strip(), "canonical distributed notices must not be empty"
    assert b"*/" not in notices, "notices must fit in one preserved license comment"
    entries = sorted((ROOT / "ui/dist").rglob("*.js"))
    assert entries, "production build must emit JavaScript"
    banner = b"/*!\n" + notices + b"*/"
    with running_server(ROOT, tmp_path) as port:
        for entry in entries:
            expected = entry.read_bytes()
            assert expected.startswith(banner), f"{entry.name} lacks leading distributed notices"
            status, headers, body = request(port, "/" + entry.relative_to(ROOT / "ui/dist").as_posix())
            assert status == 200
            assert body == expected
            assert {key: headers.get(key) for key in HEADERS} == HEADERS


@pytest.fixture
def build_checkout(tmp_path):
    # All destructive scenarios run in a disposable checkout, never the worktree.
    ui = tmp_path / "ui"
    shutil.copytree(ROOT / "ui", ui, ignore=shutil.ignore_patterns("node_modules", ".venv", ".cache", ".test-build", "__pycache__"))
    (ui / "node_modules").symlink_to(ROOT / "ui/node_modules", target_is_directory=True)
    subprocess.run(["git", "init", "--quiet", str(tmp_path)], check=True)
    subprocess.run(["git", "add", "ui/dist"], cwd=tmp_path, check=True)
    return tmp_path


@pytest.mark.parametrize("change,diagnostic", [
    ("clean", "dist matches the clean build"),
    ("source", "dist bytes differ"),
    ("missing", "dist file set differs"),
    ("extra", "dist file set differs"),
    ("ignored", "dist file set differs"),
    ("untracked", "dist tracking differs"),
])
def test_check_dist_detects_drift(build_checkout, change, diagnostic):
    ui = build_checkout / "ui"
    asset = next((ui / "dist/assets").glob("*.js"))
    if change == "source":
        source = ui / "index.html"
        source.write_text(source.read_text().replace("<title>Floatie</title>", "<title>Changed source</title>"))
    elif change == "missing":
        asset.unlink()
    elif change in {"extra", "ignored"}:
        (ui / "dist/extra.js").write_text("/* unexpected artifact */")
        if change == "ignored":
            with (ui / ".gitignore").open("a") as ignored:
                ignored.write("dist/extra.js\n")
    elif change == "untracked":
        subprocess.run(["git", "rm", "--cached", str(asset.relative_to(build_checkout))], cwd=build_checkout, check=True, capture_output=True)
    before = {path.relative_to(ui / "dist"): path.read_bytes() for path in (ui / "dist").rglob("*") if path.is_file()}
    result = subprocess.run(["node", "scripts/check-dist.mjs"], cwd=ui, text=True, capture_output=True, timeout=90)
    assert result.returncode == (0 if change == "clean" else 1), result.stdout + result.stderr
    assert diagnostic in result.stdout + result.stderr, result.stdout + result.stderr
    after = {path.relative_to(ui / "dist"): path.read_bytes() for path in (ui / "dist").rglob("*") if path.is_file()}
    assert after == before, "parity check must not rewrite shipped files"


def assert_private_subrequest(origin, token, url, headers):
    parsed = urlsplit(url)
    assert url.startswith(origin + '/')
    assert token not in url
    assert not headers.get('referer')
    if parsed.path.startswith('/assets/'):
        assert not parsed.query
        assert 'x-floatie-token' not in headers
    else:
        assert parsed.path in {'/api/state', '/api/agents', '/api/base-domains'}
        assert not parsed.query
        assert headers.get('x-floatie-token') == token


@pytest.mark.parametrize("load_bundle", [True, False])
def test_browser_bootstrap_and_missing_bundle_guidance(tmp_path, monkeypatch, load_bundle):
    with synthetic_ui_server(tmp_path, monkeypatch) as origin, sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page()
        errors = []
        requests = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.on("request", lambda req: requests.append((req.url, req.headers)))
        if not load_bundle:
            page.route("**/assets/*.js", lambda route: route.abort())
        try:
            page.goto(origin + "/?t=test-token&view=compact#settings-resources")
            if load_bundle:
                expect(page.get_by_role('heading', name='Workspace', exact=True)).to_be_visible()
                expect(page.get_by_test_id('ws-name')).to_have_text('demo')
                expect(page.locator("#bootstrap-failure")).to_have_count(0)
                assert page.url == origin + "/?t=test-token&view=compact#workspace"
                assert not errors
                assert page.evaluate("() => [localStorage.length, sessionStorage.length]") == [0, 0]
                assert "test-token" not in page.content()
                assert page.context.cookies() == []
            else:
                expect(page.get_by_role("alert")).to_contain_text("restart and reopen")
            for url, headers in requests[1:]:
                assert_private_subrequest(origin, 'test-token', url, headers)
            artifacts = os.environ.get("TASK1_ARTIFACT_DIR")
            if artifacts:
                destination = Path(artifacts)
                destination.mkdir(parents=True, exist_ok=True)
                page.screenshot(path=str(destination / ("bootstrap.png" if load_bundle else "missing-bundle.png")))
        finally:
            browser.close()


@pytest.mark.parametrize("navigation", ["reload", "cold-back"])
def test_browser_bootstrap_survives_fresh_document(tmp_path, monkeypatch, navigation):
    with synthetic_ui_server(tmp_path, monkeypatch) as origin, sync_playwright() as playwright:
        # A restored JS heap would conceal the missing-token regression on Back.
        browser = playwright.chromium.launch(args=["--disable-features=BackForwardCache"])
        page = browser.new_page()
        page.add_init_script("""window.addEventListener('pageshow', event => {
            window.__bootstrapPagePersisted = event.persisted;
        });""")
        errors = []
        requests = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.on("request", lambda req: requests.append((req.resource_type, req.url, req.headers)))
        try:
            token = "test-token"
            launch_url = origin + f"/?t={token}&view=compact#settings/resources"
            page.goto(launch_url)
            expect(page.get_by_role('heading', name='Resources', exact=True)).to_be_visible()
            expect(page.get_by_test_id('ws-name')).to_have_text('demo')
            page.evaluate("window.__previousBootstrapDocument = true")

            if navigation == "reload":
                response = page.reload()
            else:
                # A real server response at another URL creates a different document.
                away = page.goto(origin + "/away")
                assert away.status == 404
                response = page.go_back()

            assert response is not None and response.status == 200
            expect(page.get_by_role('heading', name='Resources', exact=True)).to_be_visible()
            assert page.evaluate("window.__previousBootstrapDocument === undefined")
            assert page.evaluate("window.__bootstrapPagePersisted") is False
            expected_type = "reload" if navigation == "reload" else "back_forward"
            assert page.evaluate("performance.getEntriesByType('navigation')[0].type") == expected_type
            expect(page.get_by_test_id('ws-name')).to_have_text('demo')
            assert page.url == launch_url
            assert not errors
            assert token not in page.content()
            assert page.evaluate("() => [localStorage.length, sessionStorage.length]") == [0, 0]
            assert page.context.cookies() == []
            for resource_type, url, headers in requests:
                assert not headers.get("referer")
                if resource_type != "document":
                    assert_private_subrequest(origin, token, url, headers)
            artifacts = os.environ.get("TASK1_ARTIFACT_DIR")
            if artifacts:
                destination = Path(artifacts)
                destination.mkdir(parents=True, exist_ok=True)
                page.screenshot(path=str(destination / f"bootstrap-{navigation}.png"))
        finally:
            browser.close()
