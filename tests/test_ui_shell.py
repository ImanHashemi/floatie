"""Task 3 shell contracts against Python-served, checked-in production assets."""
from datetime import datetime, timezone
import os
from pathlib import Path

from playwright.sync_api import expect, sync_playwright
import pytest

from ui_browser_helpers import SECTIONS, delay_observation, open_settings, refresh, synthetic_ui_server, wait_for_feedback


@pytest.fixture
def shell(tmp_path, monkeypatch):
    with synthetic_ui_server(tmp_path, monkeypatch) as origin, sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page(viewport={'width': 1440, 'height': 900})
        page.set_default_timeout(2500)
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.on('console', lambda message: errors.append(message.text) if message.type == 'error' else None)
        try:
            yield page, origin, errors
        finally:
            browser.close()


def launch(page, origin, fragment='workspace'):
    page.goto(origin + '/?t=test-token&view=compact#' + fragment)
    expect(page.get_by_role('navigation', name='Primary', exact=True)).to_be_visible()


def screenshot(page, name):
    directory = os.environ.get('TASK3_ARTIFACT_DIR')
    if directory:
        path = Path(directory)
        path.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(path / (name + '.png')), full_page=True)


@pytest.mark.parametrize('width', [1440, 390])
@pytest.mark.parametrize('theme', ['light', 'dark'])
def test_shell_routes_history_and_section_navigation(shell, width, theme):
    page, origin, errors = shell
    page.set_viewport_size({'width': width, 'height': 900})
    launch(page, origin)
    page.get_by_label('Theme', exact=True).select_option(theme)
    expect(page.locator('html')).to_have_attribute('data-theme', theme)
    primary = page.get_by_role('navigation', name='Primary', exact=True)
    expect(primary.get_by_role('link')).to_have_count(2)
    expect(page.get_by_role('heading', name='Workspace', exact=True)).to_be_visible()
    expect(page.get_by_test_id('ws-name')).to_have_text('demo')
    screenshot(page, f'workspace-{width}-{theme}')
    for section, label in SECTIONS.items():
        open_settings(page, section)
        assert page.url == origin + '/?t=test-token&view=compact#settings/' + section
        expect(primary.get_by_role('link', name='Settings')).to_have_attribute('aria-current', 'page')
        screenshot(page, f'settings-{section}-{width}-{theme}')
    page.go_back()
    expect(page.get_by_role('heading', name='Resources', exact=True)).to_be_visible()
    page.go_forward()
    expect(page.get_by_role('heading', name='Credentials', exact=True)).to_be_visible()
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    assert not errors


@pytest.mark.parametrize('section,label', list(SECTIONS.items()))
def test_supported_settings_deep_links(shell, section, label):
    page, origin, errors = shell
    launch(page, origin, 'settings/' + section)
    expect(page.get_by_role('heading', name=label, exact=True)).to_be_visible()
    assert page.url.endswith('#settings/' + section)
    assert not errors


def test_unknown_route_does_not_trap_browser_back(shell):
    page, origin, errors = shell
    launch(page, origin, 'settings/network')
    page.evaluate("location.hash = 'unknown-destination'")
    expect(page.get_by_role('heading', name='Workspace', exact=True)).to_be_visible()
    assert page.url == origin + '/?t=test-token&view=compact#workspace'
    page.go_back()
    expect(page.get_by_role('heading', name='Network', exact=True)).to_be_visible()
    page.go_forward()
    expect(page.get_by_role('heading', name='Workspace', exact=True)).to_be_visible()
    assert not errors


def test_theme_survives_storage_failure(shell):
    page, origin, errors = shell
    page.add_init_script("""Object.defineProperty(window, 'localStorage', {
        get() { throw new DOMException('Unavailable', 'SecurityError'); }
    });""")
    page.emulate_media(color_scheme='dark', reduced_motion='reduce')
    launch(page, origin)
    expect(page.locator('html')).to_have_attribute('data-theme', 'dark')
    page.get_by_label('Theme', exact=True).select_option('light')
    expect(page.locator('html')).to_have_attribute('data-theme', 'light')
    open_settings(page, 'network')
    expect(page.get_by_label('Theme', exact=True)).to_have_value('light')
    page.get_by_label('Theme', exact=True).select_option('system')
    page.emulate_media(color_scheme='light')
    expect(page.locator('html')).to_have_attribute('data-theme', 'light')
    page.emulate_media(color_scheme='dark')
    expect(page.locator('html')).to_have_attribute('data-theme', 'dark')
    screenshot(page, 'settings-network-dark-storage-unavailable')
    assert not errors


def test_theme_override_and_exact_approved_tokens(shell):
    page, origin, errors = shell
    launch(page, origin)
    for theme, expected in [
        ('light', ['#F5F8F8', '#FFFFFF', '#203A43', '#62747B', '#176B61', '#E8F2EF']),
        ('dark', ['#14252B', '#1B3037', '#E1EDEB', '#A8BDBC', '#9DDBD0', '#213E40']),
    ]:
        page.get_by_label('Theme', exact=True).select_option(theme)
        expect(page.locator('html')).to_have_attribute('data-theme', theme)
        assert page.evaluate("""() => ['--canvas','--surface','--text','--muted','--accent','--root-wash']
            .map(name => getComputedStyle(document.documentElement).getPropertyValue(name).trim().toUpperCase())
            .map(value => value.length === 4 ? '#' + [...value.slice(1)].map(digit => digit + digit).join('') : value)""") == expected
    page.reload()
    expect(page.get_by_label('Theme', exact=True)).to_have_value('dark')
    assert page.evaluate('Object.keys(localStorage)') == ['floatie-theme']
    assert page.evaluate('sessionStorage.length') == 0
    page.get_by_label('Theme', exact=True).select_option('system')
    assert page.evaluate('localStorage.length') == 0
    assert not errors


def test_bootstrap_asset_failure_shows_reopen_guidance(shell):
    page, origin, _ = shell
    page.route('**/assets/*.js', lambda route: route.abort())
    page.goto(origin + '/?t=test-token')
    expect(page.get_by_role('alert')).to_contain_text('restart and reopen')
    assert 'test-token' not in page.content()
    screenshot(page, 'missing-bundle')


def test_missing_launcher_token_never_starts_api_reads(shell):
    page, origin, errors = shell
    requests = []
    page.on('request', lambda request: requests.append(request.url))
    page.goto(origin + '/#workspace')
    expect(page.get_by_text('Open this page using floatie ui.', exact=True)).to_be_visible()
    expect(page.locator('#bootstrap-failure')).to_have_count(0)
    assert not any('/api/' in url for url in requests)
    assert page.evaluate('() => [localStorage.length, sessionStorage.length]') == [0, 0]
    assert not errors


def test_server_text_is_not_html(shell):
    page, origin, errors = shell
    hostile = '<img src=x onerror=alert(1)>'
    long_name = 'studio-' * 35 + hostile
    def state_response(route):
        response = route.fetch()
        data = response.json()
        data['workspace'] = {'name': long_name, 'dir': '/workspaces/' + long_name}
        data['config']['name'] = long_name
        data['identity'] = {'matches': False, 'error': hostile}
        route.fulfill(json=data)
    page.route('**/api/state', state_response)
    page.set_viewport_size({'width': 390, 'height': 844})
    launch(page, origin)
    expect(page.get_by_test_id('ws-name')).to_have_text(long_name)
    wait_for_feedback(page, 'Workspace identity')
    page.get_by_role('button', name='Workspace identity output', exact=True).click()
    expect(page.locator('pre')).to_have_text(hostile)
    expect(page.locator('img')).to_have_count(0)
    expect(page.get_by_text('test-token', exact=False)).to_have_count(0)
    page.get_by_role('button', name='VM details', exact=True).click()
    expect(page.get_by_text('/workspaces/' + long_name, exact=True)).to_be_visible()
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    screenshot(page, 'long-hostile-workspace-mobile')
    assert not errors


def test_keyboard_navigation_and_details_survive_refresh_and_routes(shell):
    page, origin, errors = shell
    launch(page, origin)
    primary = page.get_by_role('navigation', name='Primary', exact=True)
    primary.get_by_role('link', name='Workspace', exact=True).focus()
    page.keyboard.press('Tab')
    expect(primary.get_by_role('link', name='Settings', exact=True)).to_be_focused()
    page.keyboard.press('Enter')
    expect(page.get_by_role('heading', name='Resources', exact=True)).to_be_focused()
    primary.get_by_role('link', name='Workspace', exact=True).click()
    page.get_by_role('button', name='VM details', exact=True).click()
    expect(page.get_by_role('button', name='VM details')).to_have_attribute('aria-expanded', 'true')
    refresh(page)
    expect(page.get_by_role('button', name='VM details')).to_have_attribute('aria-expanded', 'true')
    open_settings(page, 'agents')
    primary.get_by_role('link', name='Workspace', exact=True).click()
    expect(page.get_by_role('button', name='VM details')).to_have_attribute('aria-expanded', 'true')
    assert page.evaluate("['controller', 'refresh', 'refreshBusy', 'view'].every(name => window[name] === undefined)")
    assert not errors


def test_late_observation_does_not_steal_navigation_focus(shell):
    page, origin, errors = shell
    launch(page, origin)
    refresh(page)
    with delay_observation(page) as delay:
        with page.expect_request('**/api/state'):
            open_settings(page, 'resources')
        page.get_by_role('navigation', name='Primary', exact=True).get_by_role('link', name='Workspace').click()
        page.get_by_label('Theme', exact=True).focus()
        delay.release()
        expect(page.get_by_test_id('ws-name')).to_have_text('demo')
        expect(page.get_by_label('Theme', exact=True)).to_be_focused()
        expect(page.get_by_role('heading', name='Workspace', exact=True)).to_be_visible()
    assert not errors


def test_one_background_poll_owner_keeps_shell_stable(shell):
    page, origin, errors = shell
    page.clock.install(time=datetime.now(timezone.utc))
    page.clock.pause_at(datetime.now(timezone.utc))
    requests = []
    page.on('request', lambda request: requests.append(request) if '/api/' in request.url else None)
    launch(page, origin)
    # Explicit refresh waits through initial state/discovery and establishes an idle controller.
    refresh(page)
    requests.clear()
    page.get_by_role('button', name='VM details').click()
    page.get_by_label('Theme', exact=True).focus()
    before = page.get_by_role('button', name='Refresh', exact=True).bounding_box()
    for _ in range(3):
        with page.expect_response('**/api/agents'):
            page.clock.fast_forward(3000)
        expect(page.get_by_role('button', name='Refresh', exact=True)).to_be_enabled()
    assert len(requests) == 3
    assert all(request.method == 'POST' and request.post_data_json == {'action': 'list'} for request in requests)
    assert page.get_by_role('button', name='Refresh', exact=True).bounding_box() == before
    expect(page.get_by_label('Theme', exact=True)).to_be_focused()
    expect(page.get_by_role('button', name='VM details')).to_have_attribute('aria-expanded', 'true')
    assert not errors


def test_failed_observation_keeps_identity_and_expansion(shell):
    page, origin, errors = shell
    # Keep the next scheduled poll from repairing the failed read during assertions.
    page.clock.install(time=datetime.now(timezone.utc))
    page.clock.pause_at(datetime.now(timezone.utc))
    page.set_viewport_size({'width': 390, 'height': 844})
    launch(page, origin)
    refresh(page)
    expect(page.get_by_role('region', name='Workspace identity', exact=True)).to_have_count(0)
    page.get_by_role('button', name='VM details').click()
    with delay_observation(page) as delay:
        with page.expect_request('**/api/state'):
            page.get_by_role('button', name='Refresh', exact=True).click()
        delay.release(failure=True)
        wait_for_feedback(page, 'Workspace observation')
        expect(page.get_by_role('status', name='Refresh status')).to_contain_text('Refresh incomplete')
        expect(page.get_by_test_id('ws-name')).to_have_text('demo')
        expect(page.get_by_role('button', name='VM details')).to_have_attribute('aria-expanded', 'true')
        expect(page.get_by_text('VM status unknown', exact=True).first).to_be_visible()
        expect(page.get_by_text('Connection unavailable', exact=True)).to_be_visible()
        expect(page.get_by_role('region', name='Workspace identity', exact=True)).to_have_count(0)
        expect(page.get_by_text('Reopen the UI', exact=False)).to_have_count(0)
        # Collapse the host path before saving synthetic, shareable visual evidence.
        page.get_by_role('button', name='VM details').click()
        screenshot(page, 'transport-failure-without-identity-warning')
    # This single deliberately aborted read produces Chromium's expected network diagnostic.
    assert len(errors) == 1 and 'net::ERR_CONNECTION_FAILED' in errors[0]
    refresh(page)
    expect(page.get_by_role('status', name='Refresh status')).to_have_text('Refreshed.')
    expect(page.get_by_role('region', name='Workspace observation')).to_have_count(0)
    expect(page.get_by_role('region', name='Workspace identity', exact=True)).to_have_count(0)


@pytest.mark.parametrize('source', ['identity', 'workspace-binding', 'config-binding', 'config-unavailable'])
def test_observed_identity_failure_survives_later_transport_failure(shell, source):
    page, origin, errors = shell
    launch(page, origin)
    refresh(page)
    warning = page.get_by_role('region', name='Workspace identity', exact=True)
    expect(warning).to_have_count(0)
    fail_transport = False

    def observe(route):
        if fail_transport:
            route.abort('connectionfailed')
            return
        data = route.fetch().json()
        if source == 'identity':
            data['identity'] = {'matches': False, 'error': 'Observed workspace identity mismatch.'}
        elif source == 'workspace-binding':
            data['workspace']['name'] = 'other-workspace'
        elif source == 'config-binding':
            data['config']['name'] = 'other-workspace'
        else:
            data['configError'] = 'Workspace configuration unavailable.'
        route.fulfill(json=data)

    page.route('**/api/state', observe)
    refresh(page)
    expect(warning).to_contain_text('Reopen the UI for the intended workspace.')
    expect(page.get_by_role('region', name='Workspace observation', exact=True)).to_have_count(0)
    observed_name = page.get_by_test_id('ws-name').inner_text()
    fail_transport = True
    refresh(page)
    expect(page.get_by_role('region', name='Workspace observation', exact=True)).to_be_visible()
    expect(page.get_by_text('Connection unavailable', exact=True)).to_be_visible()
    expect(warning).to_contain_text('Reopen the UI for the intended workspace.')
    expect(page.get_by_test_id('ws-name')).to_have_text(observed_name)
    screenshot(page, source + '-warning-retained-offline')
    assert len(errors) == 1 and 'net::ERR_CONNECTION_FAILED' in errors[0]
