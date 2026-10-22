"""Fixture-only browser acceptance; requires an existing usable Playwright install."""
from pathlib import Path
import threading

import pytest
import ui_server as u

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def agent_page(monkeypatch, tmp_path):
    import json
    config = json.loads((ROOT / "tests/fixtures/state_running.json").read_text())["config"]
    (tmp_path / "workspace.yml").write_text(u.render_workspace_edit("name: demo\n", config))
    pw_api = pytest.importorskip('playwright.sync_api')
    monkeypatch.setenv('FLOATIE_UI_FIXTURES', str(ROOT / 'tests/fixtures/state_running.json'))
    try:
        server = u.make_server('127.0.0.1', 0, 'browser-token', str(ROOT), str(tmp_path), 'demo')
    except PermissionError:
        pytest.skip('sandbox disallows loopback server; coordinator runs browser acceptance')
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with pw_api.sync_playwright() as pw:
            browser = pw.chromium.launch()
            try:
                page = browser.new_page()
                page.goto(f'http://127.0.0.1:{server.server_port}/?t=browser-token')
                yield page
            finally:
                browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
