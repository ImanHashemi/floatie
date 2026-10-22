"""Workers inventory acceptance: synthetic HTTP only; never touches a real VM."""
import copy
import json
import os
import shlex
from pathlib import Path

import pytest
from playwright.sync_api import expect
from test_agent_browser import agent_page
import ui_server as u
from ui_browser_helpers import refresh as explicit_refresh, open_settings as settings_navigation, delay_observation, scheduled_observation

# Disabled-control clicks below intentionally test native/React suppression.
# Actual callback entry: test_workspace_review's enabled-action freshness race.
# Direct action boundary: controller.test.ts's changed_read_blocks_dispatch,
# direct_root_call_refuses_an_observed_running_agent and reservation/unknown tests.

BASE = json.loads((Path(__file__).parent / 'fixtures/state_running.json').read_text())


def agent(target='main', state='running', pane='%0', harness='codex'):
    return dict(target=target, state=state, pane=pane, harness=harness, cwd='/vm/work')


def managed(name, live=True, repos=None):
    return dict(name=name, kind='worker', session=name, sessionLive=live, scopeKnown=True, managementReason=None,
                repos=repos if repos is not None else [dict(repo='api', branch='worker/'+name, clean=True, uncommitted=0, unpushed=0,
                path='/home/demo/work/.worktrees/'+name+'/api', observedAt={'secondsAgo': 0})])


def wire_state(state, now=None):
    state = copy.deepcopy(state)
    state.setdefault('configError', None)
    state.setdefault('generatedAt', {'secondsAgo': 0})
    state['config'].setdefault('name', state['workspace']['name'])
    for key in ('gitUserName', 'gitUserEmail'):
        state['config'].setdefault(key, None)
    for env in state['environments']:
        env.setdefault('scopeKnown', False)
        env.setdefault('managementReason', None)
        for repo in env['repos']:
            for key in ('clean', 'uncommitted', 'unpushed', 'observedAt'):
                repo.setdefault(key, None)
    if now is None:
        import time
        now = time.time() * 1000
    from datetime import datetime, timezone, timedelta
    observed = datetime.fromtimestamp(now / 1000, timezone.utc).replace(microsecond=0)
    def resolve(value):
        if isinstance(value, dict):
            if set(value) == {'secondsAgo'}:
                return (observed - timedelta(seconds=value['secondsAgo'])).isoformat()
            return {key: resolve(child) for key, child in value.items()}
        if isinstance(value, list): return [resolve(child) for child in value]
        return value
    return resolve(state)


@pytest.fixture
def workers(agent_page):
    page = agent_page
    page.set_default_timeout(1500)
    saved = page.request.get(page.url.split('/?')[0] + '/api/state',
                             headers={'X-Floatie-Token': 'browser-token'}).json()
    data = dict(state={**saved, **copy.deepcopy(BASE), 'config': {**saved['config'], **BASE['config']}}, agents=[agent(), agent('alpha', pane='%1')], calls=[], fail_state=False, fail_list=False, malformed=None)
    def route(route):
        url = route.request.url
        body = route.request.post_data_json if route.request.method == 'POST' else {}
        data['calls'].append((url.split('/api/')[-1], body))
        if url.endswith('/api/state'):
            if data['fail_state']:
                route.abort('connectionfailed')
            else:
                # The Playwright clock can lead or lag wall time. Future-dated
                # fixture evidence correctly disables controls in the app, so
                # timestamp synthetic reads with the clock the app observes.
                observed_now = getattr(page, '_fixture_now', None)
                if observed_now is None:
                    observed_now = page.evaluate('Date.now()')
                route.fulfill(json=wire_state(data['state'], observed_now))
        elif url.endswith('/api/agents'):
            if body['action'] == 'list':
                if data['fail_list']:
                    route.abort('connectionfailed')
                else:
                    route.fulfill(json=data['malformed'] or {'ok': True, 'agents': data['agents'], 'identity': data['state']['identity']})
            elif body['action'] == 'start':
                data['agents'] = [agent(harness=body['harness']), *[a for a in data['agents'] if a['target'] != 'main']]
                route.fulfill(json={'ok': True, **data['agents'][0], 'started': True})
            else:
                route.fulfill(json={'ok': False, 'error': 'browser must not read/send'})
        elif url.endswith('/api/config'):
            response = route.fetch()
            saved = response.json()
            if saved.get('ok'):
                data['state'].update(config=saved['config'], configRevision=saved['configRevision'])
            route.fulfill(response=response)
        elif url.endswith('/api/action'):
            route.fulfill(json={'ok': True, 'output': 'fixture action completed'})
        else:
            route.continue_()
    from datetime import datetime, timezone
    page.clock.install(time=datetime.now(timezone.utc))
    page.route('**/api/**', route)
    page.reload()
    expect(page.get_by_test_id('ws-name')).to_have_text('demo')
    return page, data


def open_workers(page, create=False):
    page.get_by_role('navigation', name='Primary', exact=True).get_by_role('link', name='Workspace', exact=True).click()
    explicit_refresh(page)
    if create and not page.get_by_test_id('worker-name').is_visible():
        page.get_by_role('button', name='New worker', exact=True).click()


def open_settings(page, section):
    settings_navigation(page, section)



def card(page, name):
    return page.locator(f'[data-worker="{name}"]')


def refreshed(page):
    explicit_refresh(page)


def test_single_inventory_root_first_and_sources_reconciled(workers):
    page, data = workers
    data['state']['environments'].append(managed('stopped', False))
    data['agents'].append(agent('discovered', pane='%8'))
    open_workers(page)
    expect(card(page, 'stopped')).to_be_visible()
    expect(card(page, 'discovered')).to_be_visible()
    assert page.locator('[data-worker]').count() == 4
    expect(page.locator('[data-worker]').first.locator('h3')).to_have_text('Orchestrator')
    expect(card(page, 'main')).to_contain_text('Root workspace')
    expect(card(page, 'discovered')).to_contain_text('Repository details unavailable')
    expect(card(page, 'discovered').get_by_role('button', name='Remove worker')).to_be_disabled()
    assert page.get_by_test_id('nav-environments').count() == 0
    assert page.get_by_test_id('nav-orchestrator').count() == 0
    assert page.locator('textarea').count() == 0
    assert all(body.get('action') not in ('read', 'send') for _, body in data['calls'])
    assert page.get_by_label('Worker name', exact=True).count() == 1


def test_host_scoped_command_quote_copy_failure_and_keyboard(workers):
    page, data = workers
    directory = "/host/team's workspace/$literal"
    data['state']['workspace']['dir'] = directory
    open_workers(page)
    command = card(page, 'alpha').locator('.command-value')
    expected = "cd -- '/host/team'\"'\"'s workspace/$literal' && floatie attach 'alpha'"
    expect(command).to_have_text(expected)
    card(page, 'alpha').locator('summary').click()
    assert shlex.split(command.inner_text()) == ['cd', '--', directory, '&&', 'floatie', 'attach', 'alpha']
    page.evaluate("Object.defineProperty(navigator,'clipboard',{configurable:true,value:{writeText:async text=>{window.copied=text;}}})")
    button = card(page, 'alpha').get_by_role('button', name='Copy attach command', exact=True)
    button.focus()
    page.keyboard.press('Tab')
    page.keyboard.press('Shift+Tab')
    expect(button).to_be_focused()
    assert button.evaluate("el=>getComputedStyle(el).outlineStyle") != 'none'
    page.keyboard.press('Enter')
    expect(card(page, 'alpha').locator('[role=status]')).to_contain_text('Copied')
    assert page.evaluate('window.copied') == expected
    if os.environ.get('TASK4_ARTIFACT_DIR'):
        page.screenshot(path=str(Path(os.environ['TASK4_ARTIFACT_DIR']) / 'clipboard-success.png'), full_page=True)
    page.evaluate("Object.defineProperty(navigator,'clipboard',{configurable:true,value:{writeText:async()=>{throw Error('denied')}}})")
    page.keyboard.press('Enter')
    expect(card(page, 'alpha').locator('[role=status]')).to_contain_text('Copy failed')
    expect(command).to_be_visible()
    assert page.evaluate('getSelection().toString()') == expected
    if os.environ.get('TASK4_ARTIFACT_DIR'):
        page.screenshot(path=str(Path(os.environ['TASK4_ARTIFACT_DIR']) / 'clipboard-fallback.png'), full_page=True)


@pytest.mark.parametrize('repos, suffix, warning', [
    ([dict(repo='api', branch='worker/beta', clean=True, uncommitted=0, unpushed=0)], "'beta' 'api'", 'Launch or resume'),
    ([dict(repo='api'), dict(repo='web')], "'beta'", 'saved repository set'),
])
def test_recreate_scope_and_agent_resumption_are_explicit(workers, repos, suffix, warning):
    page, data = workers
    data['state']['environments'].append(managed('beta', False, repos))
    open_workers(page)
    expect(card(page, 'beta').get_by_role('button', name='Copy recreate command', exact=True)).to_be_enabled()
    expect(card(page, 'beta').locator('.command-value')).to_have_text("cd -- '/ws' && floatie worker " + suffix)
    expect(card(page, 'beta')).to_contain_text(warning)
    expect(card(page, 'beta')).to_contain_text('Launch or resume')


@pytest.mark.parametrize('live, observation, expected', [
    (False, agent('beta', state='dead', pane='%8', harness=None), 'attach'),
    (False, agent('beta', pane='%8'), 'attach'),
    (True, None, 'attach'),
    (False, None, 'recreate'),
])
def test_terminal_authority_including_dead_and_filtered_sessions(workers, live, observation, expected):
    page, data = workers
    name = 'all' if live and not observation else 'beta'
    data['state']['environments'].append(managed(name, live))
    if observation:
        data['agents'].append(observation)
    open_workers(page)
    expect(card(page, name).get_by_role('button', name=f'Copy {expected} command', exact=True)).to_be_enabled()
    if observation and observation['state'] == 'dead':
        expect(card(page, name)).to_contain_text('exited')


@pytest.mark.parametrize('condition', ['unpushed', 'uncommitted', 'not-clean', 'missing-count', 'no-repos', 'stale'])
def test_removal_ineligible_is_disabled_and_handler_guarded(workers, condition):
    page, data = workers
    row = managed('safe')
    if condition == 'unpushed': row['repos'][0].update(unpushed=1, clean=False)
    if condition == 'uncommitted': row['repos'][0].update(uncommitted=1, clean=False)
    if condition == 'not-clean': row['repos'][0].update(clean=False, uncommitted=None)
    if condition == 'missing-count': del row['repos'][0]['unpushed']
    if condition == 'no-repos': row['repos'] = []
    data['state']['environments'].append(row)
    open_workers(page)
    card(page, 'safe').locator('summary').click()
    if condition == 'stale':
        data['fail_state'] = True
        page.get_by_role('button', name='Refresh', exact=True).click()
        expect(page.locator('#workers-status')).to_contain_text('unavailable')
    button = card(page, 'safe').get_by_role('button', name='Remove worker', exact=True)
    expect(button).to_be_disabled()
    button.evaluate('(el)=>el.click()')
    assert not any(body.get('action') == 'worker-rm' for _, body in data['calls'])
    expect(card(page, 'safe')).to_contain_text('Refresh before Remove' if condition == 'stale' else 'scope' if condition == 'no-repos' else 'Save and push')


def test_clean_removal_confirmation_names_all_effects_and_refreshes(workers):
    page, data = workers
    data['state']['environments'].append(managed('safe'))
    open_workers(page)
    card(page, 'safe').locator('summary').click()
    button = card(page, 'safe').get_by_role('button', name='Remove worker', exact=True)
    expect(button).to_be_enabled()
    button.click()
    dialog = page.get_by_role('dialog', name='Remove worker safe')
    expect(dialog).to_be_visible()
    assert not any(body.get('action') == 'worker-rm' for _, body in data['calls'])
    for word in ('safe', 'session', 'agent', 'stack', 'hook', 'worktrees', 'branch', 'data'):
        expect(dialog).to_contain_text(word)
    dialog.get_by_role('button', name='Cancel', exact=True).click()
    expect(button).to_be_focused()
    button.click()
    before = len(data['calls'])
    dialog.get_by_role('button', name='Remove worker', exact=True).click()
    expect(page.locator('#envlog')).to_contain_text('completed')
    expect(button).to_be_enabled()
    calls = data['calls'][before:]
    remove_index = next(i for i, (_, body) in enumerate(calls) if body.get('action') == 'worker-rm')
    assert any(endpoint == 'state' for endpoint, _ in calls[:remove_index])
    assert ('action', {'action':'worker-rm','params':{'name':'safe'},'expectedVm':'demo'}) in calls
    assert any(body.get('action') == 'list' for _, body in calls)


def test_stopped_vm_retains_inventory_and_recovers_both_sources(workers):
    page, data = workers
    open_workers(page, create=True)
    expect(card(page, 'alpha')).to_be_visible()
    data['state']['vm']['status'] = 'Stopped'
    saved = data['state']['environments']
    data['state']['environments'] = []
    with scheduled_observation(page, source='state', full=True):
        pass
    expect(page.locator('.vm-summary')).to_contain_text('VM stopped')
    expect(card(page, 'alpha')).to_be_visible()
    expect(page.locator('#workers-status')).to_contain_text('unavailable')
    expect(card(page, 'alpha')).to_contain_text('stale')
    expect(page.get_by_test_id('worker-create')).to_be_disabled()
    page.keyboard.press('Escape')
    expect(page.get_by_role('button', name='Start VM', exact=True)).to_be_enabled()
    assert not any(body.get('action') == 'start' for _, body in data['calls'])
    data['state']['vm']['status'] = 'Running'
    data['state']['environments'] = saved
    open_workers(page, create=True)
    expect(page.get_by_test_id('worker-create')).to_be_enabled()
    expect(card(page, 'alpha')).not_to_contain_text('stale')


def test_cold_stopped_vm_has_no_fabricated_inventory(workers):
    page, data = workers
    data['state']['vm']['status'] = 'Stopped'
    data['state']['environments'] = []
    expect(page.get_by_role('button', name='Refresh', exact=True)).to_be_enabled()
    data['calls'].clear()
    page.reload()
    open_workers(page)
    expect(page.locator('#workers-status')).to_contain_text('inventory unavailable until the VM is running')
    assert page.locator('#workers [data-worker]').count() == 0
    expect(card(page, 'main')).to_be_visible()
    expect(page.get_by_test_id('worker-create')).to_be_disabled()
    assert not any(body.get('action') == 'list' for _, body in data['calls'])


@pytest.mark.parametrize('failure', ['offline', 'malformed'])
def test_discovery_failure_is_not_absence_and_recovers(workers, failure):
    page, data = workers
    data['state']['environments'].append(managed('beta', False))
    open_workers(page, create=True)
    expect(card(page, 'beta').get_by_role('button', name='Copy recreate command')).to_be_visible()
    if failure == 'offline': data['fail_list'] = True
    else: data['malformed'] = {'ok':True, 'agents':[None]}
    with scheduled_observation(page, source='state', full=True):
        pass
    expect(page.locator('#workers-status')).to_contain_text('unavailable')
    expect(card(page, 'beta').get_by_role('button', name='Copy attach command')).to_be_visible()
    expect(page.get_by_test_id('worker-create')).to_be_disabled()
    data['fail_list'] = False
    data['malformed'] = None
    # Automatic recovery must refresh both sources after the outage.
    with scheduled_observation(page, source='state', full=True):
        pass
    expect(card(page, 'beta').get_by_role('button', name='Copy recreate command')).to_be_visible(timeout=7000)
    expect(page.get_by_test_id('worker-create')).to_be_enabled()


def test_refresh_serialization_and_dom_input_focus_preservation(workers):

    page, data = workers
    open_workers(page)
    row = card(page, 'alpha')
    row.locator('summary').click()
    page.get_by_role('button', name='New worker', exact=True).click()
    name = page.get_by_label('Worker name', exact=True)
    name.fill('draft-worker')
    picker = page.get_by_label('Repositories', exact=True)
    picker.select_option('api')
    picker.focus()
    before = sum(endpoint == 'state' for endpoint, _ in data['calls'])
    with scheduled_observation(page):
        expect(picker).to_be_focused()
        expect(name).to_have_value('draft-worker')
        assert row.locator('details').evaluate('el=>el.open')
        assert sum(endpoint == 'state' for endpoint, _ in data['calls']) == before
    page.keyboard.press('Escape')
    with delay_observation(page, 'state') as delay:
        page.get_by_role('button', name='Refresh', exact=True).click()
        page.wait_for_timeout(30)
        assert delay.route is not None
        button = page.get_by_role('button', name='Refresh', exact=True)
        expect(button).to_be_disabled()
        button.evaluate('el=>el.click()')
        button.evaluate('el=>el.click()')
        assert sum(endpoint == 'state' for endpoint, _ in data['calls']) == before
        delay.release()
    expect(page.get_by_role('status', name='Refresh status')).to_have_text('Refreshed.')
    assert sum(endpoint == 'state' for endpoint, _ in data['calls']) == before + 1
    page.get_by_role('button', name='New worker', exact=True).click()
    expect(page.get_by_test_id('worker-create')).to_be_enabled()
    expect(name).to_have_value('draft-worker')
    assert row.locator('details').evaluate('el=>el.open')


def test_root_start_is_serialized_and_running_root_cannot_be_replaced(workers):

    page, data = workers
    data['agents'][0] = agent(state='shell', harness=None)
    open_workers(page)
    picker = page.get_by_label('Start orchestrator with')
    expect(picker).to_be_enabled()
    picker.select_option('claude')
    held = []
    def start(route):
        if route.request.post_data_json['action'] != 'start': return route.fallback()
        held.append(route)
    page.route('**/api/agents', start)
    button = page.get_by_role('button', name='Start orchestrator', exact=True)
    button.click()
    page.wait_for_timeout(40)
    assert len(held) == 1
    button.evaluate('(el)=>el.click()')
    assert len(held) == 1
    held[0].fallback()
    expect(card(page, 'main')).to_contain_text('Claude running')
    expect(button).to_be_hidden()
    page.locator('#root-start').evaluate('(el)=>el.click()')
    assert len(held) == 1
    assert not any(body.get('action') in ('read','send') for _,body in data['calls'])


def test_ambiguous_root_has_details_without_pane_or_start_controls(workers):
    page, data = workers
    data['agents'][0] = {**agent(state='ambiguous',pane=None,harness=None), 'candidates':[agent(),agent(pane='%9',harness='claude')]}
    open_workers(page)
    expect(card(page,'main')).to_contain_text('Multiple agents')
    expect(page.get_by_role('button', name='Start orchestrator',exact=True)).to_be_hidden()
    card(page,'main').locator('summary').click()
    expect(card(page,'main')).to_contain_text('%9')
    assert page.locator('#agent-pane').count() == 0


def test_unknown_host_directory_disables_copy(workers):
    page,data=workers
    data['state']['workspace']['dir'] = ''
    open_workers(page)
    expect(card(page,'main').get_by_role('button',name='Copy attach command')).to_be_disabled()
    expect(card(page,'main')).to_contain_text('Host workspace directory unavailable')


def test_narrow_long_names_paths_and_literal_repo_content(workers):
    page,data=workers
    name='a'*60
    data['state']['environments'].append(managed(name, repos=[dict(repo='<img src=x onerror=alert(1)>', branch='branch/'+'long'*40, clean=True, uncommitted=0, unpushed=0)]))
    data['state']['environments'].append(managed('safe-focus'))
    data['agents'].append(agent(name,pane='%20'))
    data['agents'].append(agent('safe-focus',pane='%21'))
    data['state']['workspace']['dir']='/host/'+'very-long-workspace-name-'*12
    page.set_viewport_size({'width':390,'height':844})
    open_workers(page)
    card(page,name).locator('summary').click()
    assert card(page,name).locator('img').count() == 0
    expect(card(page,name)).to_contain_text('<img')
    assert page.evaluate('document.documentElement.scrollWidth') <= 390
    if os.environ.get('TASK4_ARTIFACT_DIR'):
        page.evaluate('scrollTo(0,0)')
        page.screenshot(path=str(Path(os.environ['TASK4_ARTIFACT_DIR']) / 'long-literal-details-390.png'), full_page=True)
    button=card(page,name).get_by_role('button',name='Copy attach command')
    button.focus()
    page.keyboard.press('Tab')
    # The literal malformed repository is inspection-only under the saved-scope
    # contract. Keyboard navigation skips its disabled Remove control.
    expect(card(page,name).get_by_role('button',name='Remove worker')).to_be_disabled()
    expect(card(page,name).locator('summary')).to_be_focused()
    card(page,'safe-focus').get_by_role('button',name='Copy attach command').focus()
    page.keyboard.press('Tab')
    expect(card(page,'safe-focus').get_by_role('button',name='Remove worker')).to_be_focused()


def test_removal_snapshot_expires_despite_successful_discovery(workers):

    page, data = workers
    data['state']['environments'].append(managed('safe'))
    open_workers(page)
    card(page, 'safe').locator('summary').click()
    button = card(page, 'safe').get_by_role('button', name='Remove worker', exact=True)
    expect(button).to_be_enabled()
    page.clock.fast_forward(61000)
    expect(button).to_be_disabled()
    button.evaluate('(el)=>el.click()')
    assert not any(body.get('action') == 'worker-rm' for _,body in data['calls'])
    expect(card(page, 'safe')).to_contain_text('Refresh')
    page._fixture_now = page.evaluate('Date.now()')
    explicit_refresh(page)
    expect(button).to_be_enabled()


def test_pending_vm_action_keeps_worker_mutations_disabled(workers):

    page, data = workers
    data['agents'][0] = agent(state='shell', harness=None)
    open_workers(page)
    expect(page.locator('#root-start')).to_be_enabled()
    held = []
    page.route('**/api/action', lambda route: held.append(route))
    page.get_by_role('button', name='VM details', exact=True).click()
    page.get_by_role('button', name='Apply workspace files', exact=True).click()
    page.wait_for_timeout(40)
    assert len(held) == 1
    expect(page.locator('#root-start')).to_be_disabled()
    page.locator('#root-start').evaluate('(el)=>el.click()')
    assert not any(endpoint=='agents' and body.get('action')=='start' for endpoint,body in data['calls'])
    data['agents'][0] = agent()
    held[0].fallback()
    expect(card(page,'main')).to_contain_text('Codex running')
    expect(page.locator('#root-start')).to_be_hidden()


def test_managed_worker_becoming_discovery_only_loses_remove(workers):
    page, data = workers
    open_workers(page)
    expect(card(page,'alpha')).to_be_visible()
    data['state']['environments'] = data['state']['environments'][:1]
    page.get_by_role('button', name='Refresh', exact=True).click()
    expect(card(page,'alpha')).to_contain_text('Repository details unavailable')
    expect(card(page,'alpha').locator('.remove')).to_be_disabled()


def test_config_and_firewall_apply_own_one_mutation_slot(workers):
    from test_settings_browser import field, add_entry, save_button, network_action, feedback, idle
    page, data = workers
    open_settings(page, 'network')
    expect(network_action(page, 'fw-reapply')).to_be_enabled()
    before = sum(endpoint=='state' for endpoint,_ in data['calls'])
    held = []
    page.route('**/api/config', lambda route: held.append(route))
    field(page, '#egress-input').fill('fixture.example.com')
    add_entry(page, 'network')
    save_button(page, 'network').click()
    page.wait_for_timeout(40)
    assert len(held) == 1
    expect(network_action(page)).to_be_disabled()
    refresh_control = page.get_by_role('button', name='Refresh', exact=True)
    expect(refresh_control).to_be_disabled()
    refresh_control.evaluate('el => el.click()')
    page.wait_for_timeout(100)
    assert sum(endpoint=='state' for endpoint,_ in data['calls']) == before
    held[0].fallback()
    idle(page)
    expect(feedback(page, 'network')).to_contain_text('saved')
    assert not [body for endpoint,body in data['calls'] if endpoint=='action']
    expect(network_action(page)).to_be_enabled()
    network_action(page).click()
    expect(feedback(page, 'network')).to_contain_text('completed')
    saved = page.request.get(page.url.split('/?')[0] + '/api/state',
                             headers={'X-Floatie-Token': 'browser-token'}).json()
    assert ('action', {'action':'firewall-on','params':{}, 'expectedVm':'demo', 'expectedRevision': saved['configRevision']}) in data['calls']
    expect(network_action(page, 'fw-reapply')).to_be_enabled()


@pytest.mark.parametrize('width', [1280, 390])
def test_card_actions_visible_and_spacing_has_no_empty_feedback_gap(workers, width):
    page, data = workers
    data['state']['environments'].append(managed('safe'))
    page.set_viewport_size({'width': width, 'height': 844})
    open_workers(page)
    row = card(page, 'safe')
    remove = row.get_by_role('button', name='Remove worker', exact=True)
    expect(remove).to_be_visible(timeout=1000)
    expect(remove).to_be_enabled()
    assert not row.locator('details').evaluate('el=>el.open')
    expect(card(page, 'alpha').get_by_role('button', name='Remove worker')).to_be_visible()
    expect(card(page, 'alpha').get_by_role('button', name='Remove worker')).to_be_disabled()
    assert 'Save and push' in card(page, 'alpha').get_by_role('button', name='Remove worker').get_attribute('title')
    copy_box = row.get_by_role('button', name='Copy attach command').bounding_box()
    remove_box = remove.bounding_box()
    assert abs(copy_box['y'] - remove_box['y']) <= 2
    assert remove_box['x'] >= copy_box['x'] + copy_box['width'] + 6
    feedback = row.locator('.copy-status')
    assert feedback.evaluate('el=>el.getBoundingClientRect().height') == 0
    expect(row.locator('.command-value')).to_be_hidden()
    row.locator('summary').click()
    expect(row.locator('.command-value')).to_be_visible()
    expect(page.get_by_role('button', name='Refresh', exact=True)).to_be_visible()
    assert page.evaluate('document.documentElement.scrollWidth') <= width


def test_copied_commands_execute_with_literal_host_path_in_real_shell(workers, tmp_path):
    import subprocess
    page, data = workers
    directory = tmp_path / "team's workspace $HOME $(printf substituted) `printf substituted` ; &"
    directory.mkdir()
    data['state']['workspace']['dir'] = str(directory)
    data['state']['environments'].append(managed('beta', False))
    open_workers(page)
    page.evaluate("Object.defineProperty(navigator,'clipboard',{configurable:true,value:{writeText:async text=>{window.copied=text;}}})")
    expect(card(page, 'beta').get_by_role('button', name='Copy recreate command')).to_be_enabled()
    for name, label, arguments in [('main', 'attach', ['attach']), ('alpha', 'attach', ['attach', 'alpha']), ('beta', 'recreate', ['worker', 'beta', 'api'])]:
        row = card(page, name)
        row.get_by_role('button', name=f'Copy {label} command', exact=True).click()
        expect(row.locator('.copy-status')).to_contain_text('Copied')
        command = page.evaluate('window.copied')
        assert command == row.locator('.command-value').text_content()
        # A shell function intercepts floatie: no CLI/VM action can be performed.
        script = 'floatie() { printf "%s\\0" "$PWD" "$@"; };\n' + command
        result = subprocess.run(['/bin/sh', '-c', script], cwd=tmp_path, capture_output=True, check=True)
        assert result.stdout.decode().split('\0') == [str(directory), *arguments, '']
        assert result.stderr == b''


@pytest.mark.parametrize('damage', ['scope', 'count-error', 'old-counts', 'inventory-failed'])
def test_unknown_or_stale_inventory_never_becomes_removable_with_fresh_agents(workers, damage):
    page, data = workers
    row = managed('safe')
    row.update(scopeKnown=True, managementReason=None)
    data['state']['environments'].append(row)
    if damage == 'scope':
        row.update(scopeKnown=False, managementReason='Saved registration mismatch')
    elif damage == 'count-error':
        row['repos'][0].update(error='Index inspection failed')
    elif damage == 'old-counts':
        data['state']['inventory'] = dict(inventoryKnown=True, observedAt={'secondsAgo': 61},
            countsObservedAt={'secondsAgo': 61}, countsError=None, error=None)
    else:
        data['state']['inventory'] = dict(inventoryKnown=False, observedAt=None,
            countsObservedAt=None, countsError='Guard unavailable', error='Inventory unavailable')
    data['agents'].append(agent('safe', pane='%9'))
    open_workers(page)
    row_el = card(page, 'safe')
    expect(row_el).to_contain_text('Codex running')
    button = row_el.get_by_role('button', name='Remove worker')
    expect(button).to_be_disabled(timeout=1000)
    button.evaluate('(el)=>el.click()')
    assert not any(body.get('action') == 'worker-rm' for _, body in data['calls'])
    row_el.locator('summary').click()
    if damage == 'scope': expect(row_el).to_contain_text('Saved registration mismatch')
    if damage == 'count-error':
        expect(row_el).to_contain_text('Index inspection failed')
        expect(row_el.locator('.repos')).not_to_contain_text('Clean')


def test_unknown_session_and_saved_empty_scope_offer_inspection_only(workers):
    page, data = workers
    data['state']['environments'].extend([
        dict(name='legacy-session', kind='unknown', session='legacy-session', sessionLive=True,
             scopeKnown=False, managementReason='Unrecognized session; inspect manually', repos=[]),
        dict(name='empty', kind='worker', session='empty', sessionLive=False,
             scopeKnown=False, managementReason='Empty saved scope; inspect manually', repos=[]),
    ])
    open_workers(page)
    expect(card(page, 'legacy-session')).to_be_visible(timeout=1000)
    expect(card(page, 'legacy-session').locator('.remove')).to_be_disabled()
    expect(card(page, 'empty').get_by_role('button', name='Copy recreate command')).to_have_count(0)
    expect(card(page, 'empty')).to_contain_text('Empty saved scope')


def test_transport_success_with_unknown_inventory_retains_previous_rows(workers):
    page, data = workers
    open_workers(page)
    expect(card(page, 'alpha')).to_be_visible()
    data['state']['environments'] = []
    data['state']['inventory'] = dict(inventoryKnown=False, observedAt=None, countsObservedAt=None,
        countsError='Guard unavailable', error='VM transport failed')
    page.get_by_role('button', name='Refresh', exact=True).click()
    expect(card(page, 'alpha')).to_be_visible()
    expect(card(page, 'alpha')).to_contain_text('Codex running')
    expect(card(page, 'alpha').get_by_role('button', name='Remove worker')).to_be_disabled()
    expect(page.locator('#workers-observed')).to_contain_text('stale')
    expect(page.locator('#workers-status')).to_contain_text('VM transport failed')


def test_unknown_vm_firewall_and_inventory_render_their_errors(workers):
    page, data = workers
    data['state']['vm'] = dict(status='unknown', observedAt=None, error='Lima transport failed')
    data['state']['firewall'].update(mode='unknown', observedAt=None, error='Firewall read failed')
    data['state']['environments'] = []
    data['state']['inventory'].update(inventoryKnown=False, observedAt=None, error='Inventory unavailable', countsObservedAt=None)
    explicit_refresh(page)
    page.get_by_role('button', name='VM details', exact=True).click()
    expect(page.get_by_role('region', name='VM details', exact=True)).to_contain_text('Lima transport failed', timeout=1000)
    expect(page.locator('#workers-observed')).to_contain_text('unavailable')
    open_settings(page, 'network')
    expect(page.get_by_role('region', name='Effective network policy', exact=True)).to_contain_text('Firewall read failed')


def test_individual_stale_repo_time_blocks_removal(workers):
    page, data = workers
    row = managed('safe')
    row['repos'][0]['observedAt'] = {'secondsAgo': 61}
    data['state']['environments'].append(row)
    open_workers(page)
    expect(card(page, 'safe').get_by_role('button', name='Remove worker')).to_be_disabled()


def test_creation_selection_does_not_expand_when_selected_repo_disappears(workers):
    page, data = workers
    open_workers(page, create=True)
    page.get_by_test_id('worker-repo').select_option('api')
    data['state']['availableRepos'] = ['web']
    with scheduled_observation(page, source='state', full=True):
        pass
    expect(page.get_by_test_id('worker-repo')).to_have_value('api', timeout=800)
    expect(page.get_by_test_id('worker-create')).to_be_disabled()
    assert not any(body.get('action') == 'worker-create' for _, body in data['calls'])
