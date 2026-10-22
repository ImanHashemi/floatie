"""Exercise controls while observation requests are pending, not only after polling."""
import pytest
from contextlib import contextmanager
from playwright.sync_api import expect

from test_agent_browser import agent_page
from test_agent_ux import agent, managed, open_settings, workers
from test_workspace_browser import refresh
from ui_browser_helpers import scheduled_observation, delay_observation


def stopped_root(workers):
    page, data = workers
    data['agents'] = []
    data['state']['environments'][0]['sessionLive'] = False
    refresh(page)
    return page, data


@contextmanager
def settings_poll(page):
    from test_settings_browser import pause_polling
    pause_polling(page)
    with delay_observation(page, 'state') as pending:
        page.clock.fast_forward(3000)
        page.wait_for_timeout(40)
        assert pending.route is not None
        yield
        pending.release()
        page.wait_for_timeout(50)


def test_background_poll_preserves_controls_focus_and_layout(workers):
    page, _ = stopped_root(workers)
    picker = page.locator('#root-harness')
    picker.select_option('claude')
    picker.focus()
    before = page.locator('#workers-observed').bounding_box()
    color = page.locator('#root-start').evaluate('el => getComputedStyle(el).backgroundColor')
    with scheduled_observation(page):
        expect(picker).to_be_enabled()
        expect(picker).to_be_focused()
        expect(picker).to_have_value('claude')
        expect(page.locator('#root-start')).to_be_enabled()
        expect(page.locator('#workers-refreshing')).to_be_hidden()
        assert page.locator('#workers-observed').bounding_box() == before
        assert page.locator('#root-start').evaluate('el => getComputedStyle(el).backgroundColor') == color
    expect(picker).to_be_focused()
    expect(picker).to_have_value('claude')


@pytest.mark.parametrize('next_state', ['absent', 'running', 'failed'])
def test_start_clicked_during_poll_waits_and_rechecks_observation(workers, next_state):
    page, data = stopped_root(workers)
    page.locator('#root-harness').select_option('claude')
    with scheduled_observation(page):
        expect(page.locator('#root-start')).to_be_enabled()
        page.locator('#root-start').click()
        expect(page.locator('#root-harness')).to_be_disabled()
        assert not [body for endpoint, body in data['calls'] if body.get('action') == 'start']
        if next_state == 'running':
            data['agents'] = [agent()]
        elif next_state == 'failed':
            data['fail_list'] = True
    expect(page.locator('#workspace-content')).to_have_attribute('aria-busy', 'false')
    starts = [body for endpoint, body in data['calls'] if body.get('action') == 'start']
    assert len(starts) == (1 if next_state == 'absent' else 0)
    if starts:
        assert starts[0]['harness'] == 'claude'
    else:
        expect(page.locator('#root-start')).to_be_hidden()


def test_settings_save_during_background_read_is_not_dropped(workers):
    from test_settings_browser import field, save_button, panel, idle
    page, data = workers
    open_settings(page, 'resources')
    control = field(page, '#cfg-cpus')
    control.fill('12')
    with settings_poll(page):
        expect(save_button(page, 'resources')).to_be_enabled()
        expect(panel(page, 'resources').get_by_role('button', name='Discard', exact=True)).to_be_enabled()
        save_button(page, 'resources').click()
        expect(control).not_to_be_editable()
        control.focus()
        page.keyboard.press('End')
        page.keyboard.type('6')
        expect(control).to_have_value('12')
        assert not [body for endpoint, body in data['calls'] if endpoint == 'config']
    idle(page)
    writes = [body for endpoint, body in data['calls'] if endpoint == 'config']
    assert len(writes) == 1
    assert writes[0]['changes'] == {'cpus': 12}
    expect(control).to_have_value('12')


def test_refresh_clicked_during_poll_reserves_action_until_complete(workers):
    page, data = stopped_root(workers)
    with scheduled_observation(page):
        page.get_by_role('button', name='Refresh', exact=True).click()
        expect(page.locator('#root-start')).to_be_visible()
        expect(page.locator('#root-start')).to_be_disabled()
        assert not [body for endpoint, body in data['calls'] if body.get('action') == 'start']
    expect(page.get_by_role('status', name='Refresh status')).to_have_text('Refreshed.')
    expect(page.locator('#root-start')).to_be_enabled()
    page.locator('#root-start').click()
    expect(page.locator('#workspace-content')).to_have_attribute('aria-busy', 'false')
    assert len([body for endpoint, body in data['calls'] if body.get('action') == 'start']) == 1


def test_network_save_reserves_apply_until_observation_and_save_finish(workers):
    from test_settings_browser import field, add_entry, save_button, network_action, idle
    page, data = workers
    open_settings(page, 'network')
    control = field(page, '#egress-input')
    control.fill('extra.example')
    add_entry(page, 'network')
    with settings_poll(page):
        save_button(page, 'network').click()
        expect(network_action(page)).to_be_disabled()
        expect(control).not_to_be_editable()
    idle(page)
    expect(network_action(page)).to_be_enabled()
    network_action(page).click()
    idle(page)
    assert len([body for endpoint, body in data['calls'] if endpoint == 'config']) == 1
    assert len([body for endpoint, body in data['calls'] if body.get('action') == 'firewall-on']) == 1


@pytest.mark.parametrize('action', ['create', 'remove'])
def test_worker_dialog_locks_cancel_and_inputs_when_submit_waits_for_poll(workers, action):
    page, data = workers
    data['state']['environments'].append(managed('safe'))
    refresh(page)
    if action == 'create':
        page.locator('#new-worker').click()
        page.locator('#wname').fill('new-worker')
        page.locator('#wrepo').select_option('api')
    else:
        page.locator('[data-worker=safe] .remove').click()
    with scheduled_observation(page):
        page.locator('#wcreate' if action == 'create' else '#remove-confirm').click()
        expect(page.locator(f'#{action}-cancel')).to_be_disabled()
        page.keyboard.press('Escape')
        expect(page.locator(f'#{action}-dialog')).to_be_visible()
        if action == 'create':
            expect(page.locator('#wname')).not_to_be_editable()
            expect(page.locator('#wrepo')).to_be_disabled()
        expected = 'worker-create' if action == 'create' else 'worker-rm'
        assert not [body for endpoint, body in data['calls'] if body.get('action') == expected]
    expect(page.locator('#workspace-content')).to_have_attribute('aria-busy', 'false')
    assert len([body for endpoint, body in data['calls'] if body.get('action') == expected]) == 1
