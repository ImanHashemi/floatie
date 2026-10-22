"""Focused final-review regressions; owned synthetic browser/HTTP boundaries only."""
import subprocess
import os
from pathlib import Path

import pytest
from playwright.sync_api import expect

from test_agent_browser import agent_page
from test_agent_ux import workers
from ui_browser_helpers import scheduled_observation, refresh as public_refresh


def test_removal_focus_with_browser_clock_offset(workers):
    """Fixture observations use the controlled browser clock, not wall time."""
    from datetime import datetime, timezone
    from test_agent_ux import managed, card
    page, data = workers
    page.clock.set_fixed_time(datetime.fromtimestamp((page.evaluate('Date.now()') - 5000) / 1000, timezone.utc))
    data['state']['environments'].append(managed('safe'))
    public_refresh(page)
    expect(page.get_by_role('button', name='New worker', exact=True)).to_be_enabled()
    card(page, 'safe').locator('.remove').click()
    def complete(route):
        data['state']['environments'] = [row for row in data['state']['environments'] if row['name'] != 'safe']
        route.fulfill(json={'ok': True, 'outcome': 'completed', 'output': 'Worker removed'})
    page.route('**/api/action', complete)
    page.get_by_role('dialog', name='Remove worker safe').get_by_role('button', name='Remove worker', exact=True).click()
    expect(card(page, 'safe')).to_have_count(0)
    expect(page.get_by_role('button', name='New worker', exact=True)).to_be_focused()


def test_collapsed_vm_start_reports_failure_and_diagnostics(workers):
    from datetime import datetime, timezone
    page, data = workers
    now = page.evaluate('Date.now()') + 100
    page.clock.pause_at(datetime.fromtimestamp(now / 1000, timezone.utc))
    page._fixture_now = now
    data['state']['vm']['status'] = 'Stopped'
    public_refresh(page)
    expect(page.locator('#vm-details')).to_have_count(0)
    calls = []
    def fail(route):
        calls.append(route.request.post_data_json)
        route.fulfill(json={'ok': False, 'error': 'Synthetic start failure', 'output': 'Synthetic Lima diagnostic'})
    page.route('**/api/action', fail)
    start = page.get_by_role('button', name='Start VM', exact=True)
    start.click()
    expect(start).to_be_enabled()
    assert calls == [{'action': 'start', 'params': {}, 'expectedVm': 'demo'}]
    feedback = page.get_by_role('region', name='VM action', exact=True)
    expect(feedback).to_be_visible()
    expect(feedback).to_contain_text('Synthetic start failure')
    expect(feedback).to_contain_text('Synthetic Lima diagnostic')
    if os.environ.get('TASK4_FIX_ARTIFACT_DIR'):
        page.screenshot(path=str(Path(os.environ['TASK4_FIX_ARTIFACT_DIR']) / 'collapsed-start-failure.png'))
    page.get_by_role('button', name='VM details', exact=True).click()
    expect(feedback).to_have_count(1)
    expect(feedback).to_be_visible()


def test_removed_previous_row_does_not_steal_header_focus(workers):
    page, data = workers
    public_refresh(page)
    row = page.locator('[data-worker=alpha]')
    row.locator('.copy').focus()
    theme = page.get_by_label('Theme', exact=True)
    theme.focus()
    expect(theme).to_be_focused()
    data['state']['environments'] = [env for env in data['state']['environments'] if env['name'] != 'alpha']
    data['agents'] = [agent for agent in data['agents'] if agent['target'] != 'alpha']
    with scheduled_observation(page, source='state', full=True):
        pass
    expect(row).to_have_count(0)
    expect(theme).to_be_focused()
    expect(page.get_by_role('status', name='Worker updates')).to_be_empty()
    if os.environ.get('TASK4_FIX_ARTIFACT_DIR'):
        page.screenshot(path=str(Path(os.environ['TASK4_FIX_ARTIFACT_DIR']) / 'header-focus-retained.png'))


@pytest.mark.parametrize('target', ['header', 'blank-canvas'])
def test_removed_previous_row_does_not_reclaim_focus_after_nonfocusable_click(workers, target):
    page, data = workers
    page.set_viewport_size({'width': 1280, 'height': 900})
    public_refresh(page)
    row = page.locator('[data-worker=alpha]')
    copy = row.locator('.copy')
    copy.focus()
    expect(copy).to_be_focused()
    copy.evaluate('''el => el.addEventListener('blur', event => {
        el.dataset.blurTarget = event.relatedTarget?.tagName ?? 'null';
    }, {once: true})''')
    if target == 'header':
        page.locator('.brand').click()
    else:
        assert page.evaluate('document.elementFromPoint(1278, 898).tagName') in ['HTML', 'BODY']
        page.mouse.click(1278, 898)
    # Both real pointer departures have no next focusable element. They must
    # relinquish row ownership, unlike a control disabled by an observation.
    expect(copy).to_have_attribute('data-blur-target', 'null')
    assert page.evaluate('document.activeElement === document.body')
    data['state']['environments'] = [env for env in data['state']['environments'] if env['name'] != 'alpha']
    data['agents'] = [agent for agent in data['agents'] if agent['target'] != 'alpha']
    with scheduled_observation(page, source='state', full=True):
        pass
    expect(row).to_have_count(0)
    assert page.evaluate('document.activeElement === document.body'), 'Removal reclaimed focus after the user clicked away'
    expect(page.get_by_role('status', name='Worker updates')).to_be_empty()
    if os.environ.get('TASK4_FOCUS_ARTIFACT_DIR'):
        page.screenshot(path=str(Path(os.environ['TASK4_FOCUS_ARTIFACT_DIR']) / f'{target}-focus-released.png'))


@pytest.mark.parametrize('action', ['stop', 'root', 'remove'])
def test_enabled_action_click_rechecks_freshness_before_effects(workers, action):
    from datetime import datetime, timezone
    page, data = workers
    now = page.evaluate('Date.now()') + 100
    page.clock.pause_at(datetime.fromtimestamp(now / 1000, timezone.utc))
    page._fixture_now = now
    if action == 'root':
        data['agents'][0].update(state='shell', harness=None)
    if action == 'remove':
        for repo in data['state']['environments'][1]['repos']:
            repo.update(clean=True, uncommitted=0, unpushed=0)
    public_refresh(page)
    if action == 'stop': page.get_by_role('button', name='VM details', exact=True).click()
    button = page.locator({'stop': '[data-testid=act-stop]', 'root': '#root-start', 'remove': '[data-worker=alpha] .remove'}[action])
    expect(button).to_be_enabled()
    # Change wall time without running polling/render timers. The committed
    # enabled button invokes the real callback, whose current-time guard must
    # reject stale evidence before opening a confirmation or dispatching.
    page.clock.set_fixed_time(datetime.fromtimestamp((now + 61000) / 1000, timezone.utc))
    expect(button).to_be_enabled()
    button.click()
    expect(page.locator('#stop-dialog')).not_to_be_visible()
    expect(page.locator('#remove-dialog')).not_to_be_visible()
    expect(page.locator('#envlog')).to_be_empty()
    assert not [body for endpoint, body in data['calls'] if endpoint == 'action' or body.get('action') == 'start']


def idle(page):
    expect(page.locator('#workspace-content')).to_have_attribute('aria-busy', 'false')


def automatic_observation(page):
    # The polling path, deliberately not the user-authorized retry/unlock path.
    with scheduled_observation(page, source='state', full=True):
        pass
    idle(page)


def explicit_refresh(page):
    public_refresh(page)
    idle(page)


def uncertain_action(page, data, action, response):
    data['state']['vm']['status'] = 'Stopped' if action == 'start' else 'Running'
    automatic_observation(page)
    page.get_by_role('button', name='VM details', exact=True).click()
    calls, confirmations = [], []
    def dispatch(route):
        calls.append(route.request.post_data_json)
        if response == 'lost':
            route.abort('connectionfailed')
        else:
            route.fulfill(json={'ok': False, 'outcome': 'unknown', 'output': 'Completion was not observed'})
    page.route('**/api/action', dispatch)
    button = page.get_by_test_id('act-' + action)
    button.click()
    if action == 'stop':
        dialog = page.get_by_role('dialog', name='Stop VM demo')
        confirmations.append(dialog.inner_text())
        dialog.get_by_role('button', name='Stop VM', exact=True).click()
    idle(page)  # mutate's own healthy refresh must not authorize another attempt
    expect(page.locator('#log')).to_contain_text('Outcome unknown')
    return button, calls, confirmations


@pytest.mark.parametrize('action', ['reload', 'start', 'stop'])
@pytest.mark.parametrize('response', ['unknown', 'lost'])
def test_unknown_vm_action_requires_successful_explicit_refresh(workers, action, response):
    page, data = workers
    button, calls, confirmations = uncertain_action(page, data, action, response)
    expect(button).to_be_disabled()
    note = page.locator('#vmnote')
    expect(note).to_contain_text('Outcome unknown')
    expect(note).to_contain_text('Refresh')
    # Disabled-control coverage; enabled callback races below and direct
    # controller.perform tests establish the independent action guards.
    button.evaluate('el=>el.click()')
    assert len(calls) == 1 and len(confirmations) == (1 if action == 'stop' else 0)
    automatic_observation(page)
    expect(button).to_be_disabled()
    expect(note).to_contain_text('Refresh')
    data['fail_state'] = True
    explicit_refresh(page)
    expect(button).to_be_disabled()
    expect(note).to_contain_text('Refresh')
    data['fail_state'] = False
    automatic_observation(page)
    expect(button).to_be_disabled()
    data['state']['vm']['observedAt'] = {'secondsAgo': 61}
    explicit_refresh(page)
    expect(button).to_be_disabled()
    data['state']['vm']['observedAt'] = {'secondsAgo': 0}
    explicit_refresh(page)
    expect(button).to_be_enabled()
    expect(note).not_to_contain_text('Outcome unknown')
    assert len(calls) == 1 and len(confirmations) == (1 if action == 'stop' else 0)
    button.click()
    if action == 'stop':
        dialog = page.get_by_role('dialog', name='Stop VM demo')
        confirmations.append(dialog.inner_text())
        dialog.get_by_role('button', name='Stop VM', exact=True).click()
    idle(page)
    assert len(calls) == 2 and len(confirmations) == (2 if action == 'stop' else 0)


def test_unknown_stop_guard_precedes_confirmation(workers):
    page, data = workers
    _, calls, confirmations = uncertain_action(page, data, 'stop', 'unknown')
    # Disabled UI here; the enabled-click race above tests the actual callback,
    # and controller.test.ts directly invokes perform with unknown outcomes.
    expect(page.get_by_test_id('act-stop')).to_be_disabled()
    page.get_by_test_id('act-stop').evaluate('el=>el.click()')
    expect(page.locator('#stop-dialog')).not_to_be_visible()
    assert len(confirmations) == 1
    assert len(calls) == 1


def absent(page, data):
    data['state']['vm'].update(status='absent', error=None, observedAt={'secondsAgo': 0})
    automatic_observation(page)
    button = page.get_by_role('button', name='Copy create command', exact=True, include_hidden=True)
    expect(button).to_be_visible()
    expect(button).to_be_enabled()
    return button


@pytest.mark.parametrize('clipboard', ['success', 'failure'])
def test_absent_vm_copy_uses_literal_workspace_and_fallback(workers, tmp_path, clipboard):
    page, data = workers
    directory = tmp_path / "studio's $HOME $(touch injected) `touch injected` ; &"
    directory.mkdir()
    data['state']['workspace']['dir'] = str(directory)
    button = absent(page, data)
    page.set_viewport_size({'width':390,'height':900})
    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
    page.evaluate('''(failure)=>Object.defineProperty(navigator,'clipboard',{configurable:true,value:{writeText:async text=>{
      window.copied=text;if(failure)throw Error('unavailable');
    }}})''', clipboard == 'failure')
    button.focus()
    page.keyboard.press('Enter')
    setup = page.locator('#vm-setup')
    expect(setup.get_by_role('status')).to_contain_text('Copied' if clipboard == 'success' else 'Copy failed')
    command = page.evaluate('window.copied')
    result = subprocess.run(['/bin/sh', '-c', 'floatie() { printf "%s\\0" "$PWD" "$@"; };\n' + command],
                            cwd=tmp_path, capture_output=True, check=True)
    assert result.stdout.decode().split('\0') == [str(directory), 'create', '']
    assert not list(tmp_path.rglob('injected'))
    if clipboard == 'failure':
        expect(setup.locator('.command-value')).to_be_visible()
        expect(setup.locator('.command-value')).to_be_focused()
        assert page.evaluate('getSelection().toString()') == command
    assert not [body for endpoint,body in data['calls'] if endpoint == 'action']
    assert page.locator('[data-pane=workspace] [data-command=create]').count() == 1
    assert page.evaluate('''()=>{const ids=[...document.querySelectorAll('[id]')].map(el=>el.id);return ids.length===new Set(ids).size}''')


@pytest.mark.parametrize('damage', ['unknown', 'stale', 'error', 'lost', 'mismatch', 'running'])
def test_absent_vm_copy_requires_current_matching_observation(workers, damage):
    page, data = workers
    button = absent(page, data)
    page.evaluate("Object.defineProperty(navigator,'clipboard',{configurable:true,value:{writeText:async()=>{window.copyCalls=(window.copyCalls||0)+1;}}})")
    if damage == 'unknown': data['state']['vm']['status'] = 'unknown'
    elif damage == 'stale': data['state']['vm']['observedAt'] = {'secondsAgo': 61}
    elif damage == 'error': data['state']['vm']['error'] = 'VM observation failed'
    elif damage == 'lost': data['fail_state'] = True
    elif damage == 'mismatch': data['state']['identity'] = {'matches':False,'error':'Workspace identity changed'}
    else: data['state']['vm']['status'] = 'Running'
    automatic_observation(page)
    expect(page.locator('#vm-setup')).to_be_hidden()
    expect(page.locator('#vmnote')).not_to_contain_text("run 'floatie create'")
    expect(button).to_be_disabled()
    assert page.locator('#vm-setup .command-value').text_content() == ''
    button.evaluate('el=>el.click()')
    assert page.evaluate('window.copyCalls||0') == 0


@pytest.mark.parametrize('clipboard', ['success', 'failure'])
def test_absent_vm_copy_ignores_late_clipboard_result_after_mismatch(workers, clipboard):
    page, data = workers
    button = absent(page, data)
    page.evaluate("Object.defineProperty(navigator,'clipboard',{configurable:true,value:{writeText:()=>new Promise((resolve,reject)=>{window.finishCopy=resolve;window.failCopy=reject;})}})")
    button.click()
    page.wait_for_function('() => !!window.finishCopy')
    data['state']['identity'] = {'matches':False,'error':'Workspace identity changed'}
    automatic_observation(page)
    page.evaluate('''failure=>{if(failure)window.failCopy(Error('denied'));else window.finishCopy();}''', clipboard == 'failure')
    expect(page.locator('#vm-setup')).to_be_hidden()
    assert page.locator('#vm-setup .command-value').text_content() == ''
    expect(page.locator('#vm-setup [role=status]')).to_be_empty()
