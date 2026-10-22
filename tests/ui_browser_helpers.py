"""Browser adapters use public controls and test-owned HTTP interception only."""
from contextlib import contextmanager
import json
from pathlib import Path
import re

from playwright.sync_api import expect

from test_ui_assets import running_server
from workspace_fixtures import workspace_fixture
import ui_server

ROOT = Path(__file__).resolve().parents[1]
SECTIONS = {
    'resources': 'Resources', 'repositories': 'Repositories',
    'agents': 'Agents & Git', 'network': 'Network', 'credentials': 'Credentials',
}


@contextmanager
def synthetic_ui_server(tmp_path, monkeypatch):
    """Real Python static/API server with synthetic observations; never a VM."""
    state, _ = workspace_fixture()
    fixture = tmp_path / 'state.json'
    fixture.write_text(json.dumps(state))
    (tmp_path / 'workspace.yml').write_text(
        ui_server.render_workspace_edit('name: demo\n', state['config']))
    monkeypatch.setenv('FLOATIE_UI_FIXTURES', str(fixture))
    with running_server(ROOT, tmp_path) as port:
        yield f'http://127.0.0.1:{port}'


def refresh(page):
    with page.expect_request(lambda request: request.method == 'GET' and request.url.endswith('/api/state')):
        page.get_by_role('button', name='Refresh', exact=True).click()
    expect(page.get_by_role('button', name='Refresh', exact=True)).to_be_enabled()
    expect(page.get_by_role('status', name='Refresh status')).to_have_text(
        re.compile(r'^(Refreshed\.|Refresh incomplete\. Inspect the observation details\.)$'))


def open_settings(page, section):
    page.get_by_role('navigation', name='Primary', exact=True).get_by_role('link', name='Settings', exact=True).click()
    selector = page.get_by_label('Settings section', exact=True)
    if selector.is_visible():
        selector.select_option(section)
    else:
        page.get_by_role('navigation', name='Settings sections', exact=True).get_by_role('link', name=SECTIONS[section], exact=True).click()
    expect(page.get_by_role('heading', name=SECTIONS[section], exact=True)).to_be_visible()


def wait_for_feedback(page, text):
    expect(page.get_by_role('status').filter(has_text=text).first).to_be_visible()


class DelayedObservation:
    def __init__(self, page, source):
        if source not in ('state', 'agents'):
            raise ValueError('Only state or agent-list observations may be delayed')
        self.page = page
        self.source = source
        self.route = None
        self.used = False
        self.pattern = '**/api/' + source

    def handle(self, route):
        request = route.request
        matches = (self.source == 'state' and request.method == 'GET') or (
            self.source == 'agents' and request.method == 'POST'
            and request.post_data_json.get('action') == 'list')
        if matches and not self.used:
            self.used = True
            self.route = route
        else:
            route.fallback()

    def release(self, *, failure=False):
        assert self.route is not None, 'No matching observation has reached the delay'
        route, self.route = self.route, None
        if failure:
            route.abort('connectionfailed')
        else:
            route.fallback()


@contextmanager
def delay_observation(page, source='state'):
    """Hold only the first matching read, never an agent-start mutation."""
    delay = DelayedObservation(page, source)
    page.route(delay.pattern, delay.handle)
    try:
        yield delay
    finally:
        if delay.route is not None:
            delay.release(failure=True)
        page.unroute(delay.pattern, delay.handle)


@contextmanager
def scheduled_observation(page, source='agents', full=False):
    """Exercise the real scheduled poll. Full retry follows one failed discovery read."""
    from datetime import datetime, timezone
    # pause_at cannot move a running clock backwards while the protocol request
    # is in flight. A small future point also works after a previous pause.
    now = page.evaluate('Date.now()') + 100
    page.clock.pause_at(datetime.fromtimestamp(now / 1000, timezone.utc))
    page._fixture_now = now
    def advance():
        page._fixture_now += 3000
        page.clock.fast_forward(3000)
        page.wait_for_timeout(40)
    # Running Workspace polls agents only; failed discovery makes the next poll
    # fetch state as well. Stopped/absent/unavailable observations already do so.
    running = 'VM running' in page.locator('.vm-summary').inner_text()
    failed = page.get_by_role('region', name='Agent observation', exact=True).count() or page.get_by_role('region', name='Workspace observation', exact=True).count()
    if full and running and not failed:
        with delay_observation(page, 'agents') as retry:
            advance()
            assert retry.route is not None
            retry.release(failure=True)
            expect(page.get_by_role('region', name='Agent observation', exact=True)).to_be_visible()
            page.wait_for_timeout(40)
    with delay_observation(page, source) as delay:
        advance()
        assert delay.route is not None, 'Scheduled observation did not reach the HTTP boundary'
        yield delay
        if delay.route is not None:
            delay.release()
            page.wait_for_timeout(50)
