"""Final integration regressions: real browser/server, synthetic VM, temp config."""
import json
import os
from pathlib import Path

import pytest
from playwright.sync_api import expect
from test_agent_browser import agent_page
from test_agent_ux import managed, workers
from ui_browser_helpers import open_settings, refresh
import ui_server as u


def set_hidden(page, hidden):
    # Control the browser platform signal, never application state or handlers.
    page.evaluate("""hidden => {
      Object.defineProperty(document, 'hidden', {configurable: true, value: hidden});
      document.dispatchEvent(new Event('visibilitychange'));
    }""", hidden)


def screenshot(page, name):
    directory = os.environ.get('FLOATIE_INTEGRATION_SCREENSHOTS')
    if directory:
        destination = Path(directory)
        destination.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(destination / (name + '.png')), full_page=True)


def test_confirmed_save_keeps_response_when_visibility_cancels_followup(workers, tmp_path):
    page, data = workers
    open_settings(page, 'resources')
    refresh(page)
    original = page.get_by_label('CPUs', exact=True).input_value()
    page.get_by_label('CPUs', exact=True).fill('8')
    pending = []
    page.route('**/api/config', lambda route: pending.append(route))
    with page.expect_request('**/api/config'):
        page.get_by_role('button', name='Save changes', exact=True).click()
    expect(page.get_by_role('button', name='Save changes', exact=True)).to_be_disabled()
    assert len(pending) == 1
    set_hidden(page, True)
    response = pending[0].fetch()
    saved = response.json()
    assert saved['ok'] and saved['config']['cpus'] == 8
    config, revision, _ = u.read_config(str(tmp_path))
    assert config['cpus'] == 8 and revision == saved['configRevision']
    data['state'].update(config=saved['config'], configRevision=saved['configRevision'])
    pending[0].fulfill(response=response)
    status = page.get_by_role('status', name='Resources status', exact=True)
    expect(status).to_contain_text('Changes saved.')
    summary = page.get_by_role('region', name='Saved Resources', exact=True)
    print(json.dumps({'scenario': 'hidden confirmed save', 'before': original,
        'server_saved': config['cpus'],
        'displayed': page.get_by_label('CPUs', exact=True).input_value(),
        'saved_summary': summary.inner_text(), 'feedback': status.inner_text()}))
    screenshot(page, 'hidden-save')
    expect(page.get_by_label('CPUs', exact=True)).to_have_value('8')
    expect(summary).to_contain_text('8 CPUs')
    data['fail_state'] = True
    set_hidden(page, False)
    expect(page.get_by_role('region', name='Workspace observation', exact=True)).to_be_visible()
    expect(page.get_by_label('CPUs', exact=True)).to_have_value('8')
    expect(summary).to_contain_text('8 CPUs')
    expect(status).to_contain_text('Changes saved.')
    assert len(pending) == 1


def test_hidden_network_conflict_keeps_response_policy(workers):
    page, data = workers
    open_settings(page, 'network')
    refresh(page)
    page.get_by_label('Add domain', exact=True).fill('pending.example')
    pending = []
    page.route('**/api/action', lambda route: pending.append(route))
    with page.expect_request('**/api/action'):
        page.get_by_role('button', name='Apply saved network policy', exact=True).click()
    assert len(pending) == 1
    set_hidden(page, True)
    newer = {**data['state']['config'], 'permissive': True, 'egress': ['newer.example']}
    pending[0].fulfill(status=409, json={'ok': False, 'error': 'Configuration revision conflict',
        'config': newer, 'configRevision': 'd' * 64})
    status = page.get_by_role('status', name='Network status', exact=True)
    expect(status).to_contain_text('revision conflict')
    saved = page.get_by_role('region', name='Saved network policy', exact=True)
    print(json.dumps({'scenario': 'hidden network conflict', 'response_domains': newer['egress'],
        'displayed_saved_policy': saved.inner_text(), 'draft': page.get_by_label('Add domain', exact=True).input_value()}))
    screenshot(page, 'hidden-network-conflict')
    expect(saved).to_contain_text('newer.example')
    expect(saved).to_contain_text('Unrestricted')
    expect(saved).to_contain_text('d' * 64)
    expect(page.get_by_role('region', name='Configuration conflict', exact=True)).to_contain_text('newer.example')
    expect(page.get_by_label('Add domain', exact=True)).to_have_value('pending.example')
    data['fail_state'] = True
    set_hidden(page, False)
    expect(page.get_by_role('region', name='Workspace observation', exact=True)).to_be_visible()
    expect(saved).to_contain_text('newer.example')
    expect(page.get_by_label('Add domain', exact=True)).to_have_value('pending.example')
    assert len(pending) == 1


def open_workspace_dialog(page, kind):
    open_settings(page, 'resources')
    page.get_by_role('navigation', name='Primary', exact=True).get_by_role('link', name='Workspace', exact=True).click()
    refresh(page)
    if kind == 'create':
        page.get_by_role('button', name='New worker', exact=True).click()
        page.get_by_label('Worker name', exact=True).fill('retained-worker')
    elif kind == 'remove':
        page.locator('[data-worker="alpha"]').get_by_role('button', name='Remove worker', exact=True).click()
    else:
        page.get_by_role('button', name='VM details', exact=True).click()
        page.get_by_role('button', name='Stop VM', exact=True).click()
    dialog = page.locator('#' + kind + '-dialog')
    expect(dialog).to_be_visible()
    return dialog


@pytest.mark.parametrize('kind', ['create', 'remove', 'stop'])
def test_back_from_workspace_dialog_leaves_settings_interactive(workers, kind):
    page, data = workers
    data['state']['environments'][1] = managed('alpha')
    dialog = open_workspace_dialog(page, kind)
    page.go_back()
    heading = page.get_by_role('heading', name='Resources', exact=True)
    expect(heading).to_be_visible()
    print(json.dumps(page.evaluate("""kind => ({hash: location.hash,
        open: document.getElementById(kind + '-dialog').open,
        visible: document.getElementById(kind + '-dialog').checkVisibility(),
        modal: document.querySelectorAll('dialog:modal').length,
        focusedTag: document.activeElement.tagName})""", kind)))
    screenshot(page, 'back-' + kind + '-modal')
    expect(page.locator('dialog:modal')).to_have_count(0)
    expect(heading).to_be_focused()
    page.get_by_label('CPUs', exact=True).fill('6')
    page.go_forward()
    expect(dialog).to_be_visible()
    if kind == 'create':
        expect(page.get_by_label('Worker name', exact=True)).to_have_value('retained-worker')
    elif kind == 'remove':
        expect(dialog).to_contain_text('alpha')
    dialog.get_by_role('button', name='Cancel', exact=True).click()
    expect(page.locator('dialog:modal')).to_have_count(0)
    assert not [body for endpoint, body in data['calls'] if endpoint in ('worker', 'action', 'config')]


@pytest.mark.parametrize('kind,button,action', [
    ('create', 'Create worker', 'worker-create'),
    ('remove', 'Remove worker', 'worker-rm'),
    ('stop', 'Stop VM', 'stop'),
])
def test_route_away_preserves_dispatched_workspace_operation(workers, kind, button, action):
    page, data = workers
    data['state']['environments'][1] = managed('alpha')
    dialog = open_workspace_dialog(page, kind)
    pending = []
    page.route('**/api/action', lambda route: pending.append(route))
    with page.expect_request('**/api/action'):
        dialog.get_by_role('button', name=button, exact=True).click()
    expect(dialog.get_by_role('button', name='Cancel', exact=True)).to_be_disabled()
    assert len(pending) == 1
    body = pending[0].request.post_data_json
    assert body['action'] == action and body['expectedVm'] == 'demo'
    if kind == 'create':
        assert body['params'] == {'name': 'retained-worker', 'repos': ['api', 'web'], 'availableRepos': ['api', 'web']}
    elif kind == 'remove':
        assert body['params'] == {'name': 'alpha'}
    page.go_back()
    expect(page.locator('dialog:modal')).to_have_count(0)
    expect(page.get_by_role('heading', name='Resources', exact=True)).to_be_focused()
    page.go_forward()
    expect(dialog).to_be_visible()
    expect(dialog.get_by_role('button', name=button, exact=True)).to_be_disabled()
    page.keyboard.press('Escape')
    expect(dialog).to_be_visible()
    page.go_back()
    pending[0].fulfill(json={'ok': True, 'output': 'Synthetic operation completed after navigation'})
    expect(page.get_by_role('button', name='Save changes', exact=True)).to_be_enabled()
    page.go_forward()
    expect(page.locator('dialog:modal')).to_have_count(0)
    scope = 'VM action' if kind == 'stop' else 'Workspace action'
    expect(page.get_by_role('region', name=scope, exact=True).get_by_role('status')).to_contain_text('completed after navigation')
    assert len(pending) == 1
