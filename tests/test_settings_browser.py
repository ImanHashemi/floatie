"""Settings acceptance against synthetic observations and real config HTTP writes."""
import os
import re
from datetime import datetime, timezone
from pathlib import Path
import pytest
from playwright.sync_api import expect
from test_agent_browser import agent_page
from test_agent_ux import workers
from ui_browser_helpers import SECTIONS, delay_observation, open_settings, refresh
import ui_server as u

expect.set_options(timeout=1500)


@pytest.mark.parametrize('section,label,value', [
    ('resources', 'CPUs', '8'), ('repositories', 'Add repository', 'github.com/example/pending'),
    ('agents', 'Add Claude plugin', 'pending@market'), ('network', 'Add domain', 'pending.example'),
])
def test_unsaved_indicator_survives_navigation_and_discard(workers, section, label, value):
    page, _ = workers
    open_settings(page, section)
    page.get_by_label(label, exact=True).fill(value)
    indicator = page.get_by_role('status', name='Unsaved changes', exact=True)
    expect(indicator).to_have_text('Unsaved changes')
    page.get_by_role('navigation', name='Primary').get_by_role('link', name='Workspace', exact=True).click()
    open_settings(page, section)
    expect(indicator).to_have_text('Unsaved changes')
    page.get_by_role('button', name='Discard', exact=True).click()
    expect(indicator).to_have_count(0)
    if section == 'resources':
        page.get_by_label(label, exact=True).fill(value)
        save_button(page, section).click()
        expect(feedback(page, section)).to_contain_text('Changes saved.')
        expect(indicator).to_have_count(0)


@pytest.mark.parametrize('section,label,value,saved', [
    ('resources', 'CPUs', '8', '4 CPUs'),
    ('repositories', 'Add repository', 'github.com/example/pending', 'github.com/myorg/api'),
    ('agents', 'Git author name', 'Draft Author', 'Jane'),
])
def test_application_summary_separates_saved_values_from_draft(workers, section, label, value, saved):
    page, _ = workers
    open_settings(page, section)
    page.get_by_label(label, exact=True).fill(value)
    summary = page.get_by_role('region', name='Saved ' + SECTIONS[section], exact=True)
    expect(summary).to_contain_text(saved)
    expect(summary).not_to_contain_text(value)
    expect(page.get_by_role('region', name='Apply saved changes', exact=True)).to_contain_text('read configuration when executed')


def test_settings_observations_distinguish_desired_values_and_unknown(workers):
    page, data = workers
    data['state']['vm'].update(cpus=2, memoryGiB=3)
    data['state']['config']['repos'] = ['github.com/myorg/api', 'github.com/example/missing']
    data['state']['configRevision'] = 'b' * 64  # Changed saved content has a new revision.
    open_settings(page, 'resources')
    refresh(page)
    allocation = page.get_by_role('region', name='Observed allocation', exact=True)
    expect(allocation).to_contain_text('2 CPUs / 3 GiB')
    expect(page.get_by_label('CPUs', exact=True)).to_have_value('4')
    data['state']['vm']['observedAt'] = {'secondsAgo': 61}
    refresh(page)
    expect(allocation).to_contain_text('Allocation unknown')
    open_settings(page, 'repositories')
    data['state']['vm']['observedAt'] = {'secondsAgo': 0}
    refresh(page)
    availability = page.get_by_role('region', name='Observed repository availability', exact=True)
    expect(availability).to_contain_text('api — Clone name observed')
    expect(availability).to_contain_text('missing — Not observed')
    data['state']['inventory']['repositoryError'] = 'Synthetic clone observation failed'
    refresh(page)
    expect(availability).to_contain_text('Availability unknown')
    expect(availability).not_to_contain_text('Not observed')


@pytest.fixture(autouse=True)
def bounded_controls(agent_page):
    agent_page.set_default_timeout(1500)


def idle(page):
    expect(page.get_by_role('button', name='Refresh', exact=True)).to_be_enabled()


FIELD_LABELS = {
    '#cfg-cpus': 'CPUs', '#cfg-memoryGiB': 'Memory (GiB)',
    '#repos-input': 'Add repository', '#plugins-input': 'Add Claude plugin',
    '#cfg-gitUserName': 'Git author name', '#cfg-gitUserEmail': 'Git author email',
    '#egress-input': 'Add domain',
}
LIST_LABELS = {'repos': 'Repositories', 'plugins': 'Claude plugins', 'egress': 'Extra domains'}


def field(page, selector):
    # Keep baseline parameter IDs while using the public input labels.
    return page.get_by_label(FIELD_LABELS[selector], exact=True)


def panel(page, section):
    return page.get_by_role('region', name=SECTIONS[section], exact=True)


def feedback(page, section):
    return panel(page, section).get_by_role('status', name=SECTIONS[section] + ' status', exact=True)


def save_button(page, section):
    return panel(page, section).get_by_role('button', name='Save changes', exact=True)


def draft_list(page, key):
    return page.get_by_role('list', name=LIST_LABELS[key], exact=True)


def list_values(rows):
    return rows.evaluate_all('(rows)=>rows.map(row=>row.querySelector("button").getAttribute("aria-label").slice("Remove ".length))')


def add_entry(page, section):
    panel(page, section).get_by_role('button', name='Add to draft', exact=True).click()


def network_action(page, button='network-apply'):
    label = {'network-apply': 'Apply saved network policy', 'fw-reapply': 'Refresh DNS'}[button]
    return panel(page, 'network').get_by_role('button', name=label, exact=True)


def network_mode(page, permissive):
    return panel(page, 'network').get_by_role('button', name='Unrestricted' if permissive else 'Restricted', exact=True)


def network_confirmation(page):
    return page.get_by_role('dialog', name='Allow all outbound network access', exact=True)


def pause_polling(page):
    now = page.evaluate('Date.now()') + 100
    page.clock.pause_at(datetime.fromtimestamp(now / 1000, timezone.utc))
    page._fixture_now = now


def poll(page):
    # Settings polls always read state. The shared Workspace scheduling wrapper
    # inspects the Workspace-only VM summary, so reuse its HTTP delay directly.
    idle(page)
    # Route-entry observation may still be finishing while the public form is
    # already visible. Let that HTTP response settle before advancing its timer.
    page.wait_for_timeout(50)
    pause_polling(page)
    with delay_observation(page, source='state') as pending:
        page._fixture_now += 3000
        page.clock.fast_forward(3000)
        page.wait_for_timeout(40)
        assert pending.route is not None, 'Settings scheduled poll did not reach the state HTTP boundary'
        pending.release()
        page.wait_for_timeout(50)


def save(page, section):
    save_button(page, section).click()
    idle(page)


@pytest.mark.parametrize('section,selector,value', [
    ('resources', '#cfg-cpus', '12'), ('resources', '#cfg-memoryGiB', '6.25'),
    ('repositories', '#repos-input', 'github.com/example/new'),
    ('agents', '#plugins-input', 'tool@market'),
    ('agents', '#cfg-gitUserName', 'New Author'),
    ('agents', '#cfg-gitUserEmail', 'author@example.com'),
    ('network', '#egress-input', 'new.example.com'),
])
def test_every_input_survives_polls_switches_and_discard(workers, section, selector, value):
    page, data = workers
    open_settings(page, section)
    control = field(page, selector)
    control.fill(value)
    held = control.element_handle()
    for _ in range(3):
        poll(page)
        expect(control).to_be_focused()
        expect(control).to_have_value(value)
        assert control.evaluate('(element, held)=>element===held', held)
    open_settings(page, 'credentials')
    open_settings(page, section)
    expect(control).to_have_value(value)
    panel(page, section).get_by_role('button', name='Discard', exact=True).click()
    assert control.input_value() != value
    assert not [b for endpoint, b in data['calls'] if endpoint in ('config', 'action')]


def test_section_save_does_not_submit_or_rebase_dirty_siblings(workers):
    page, data = workers
    revision = data['state']['configRevision']
    open_settings(page, 'agents')
    field(page, '#cfg-gitUserName').fill('Draft Author')
    field(page, '#plugins-input').fill('pending@market')
    open_settings(page, 'resources')
    field(page, '#cfg-cpus').fill('8')
    save(page, 'resources')
    calls = [b for e, b in data['calls'] if e == 'config']
    assert calls == [{'expectedVm': 'demo', 'expectedRevision': revision, 'changes': {'cpus': 8}}]
    assert not [b for e, b in data['calls'] if e == 'action']
    saved_revision = data['state']['configRevision']
    assert saved_revision != revision
    open_settings(page, 'agents')
    expect(field(page, '#plugins-input')).to_have_value('pending@market')
    field(page, '#plugins-input').fill('')
    save(page, 'agents')
    expect(feedback(page, 'agents')).to_contain_text('conflict')
    # The reviewed draft engine blocks a known conflict before HTTP. It must
    # neither rebase the sibling automatically nor issue another config write.
    assert [b for e, b in data['calls'] if e == 'config'] == calls
    expect(panel(page, 'agents').get_by_role('region', name='Configuration conflict', exact=True)).to_contain_text('Draft Author')
    expect(panel(page, 'agents').get_by_role('region', name='Configuration conflict', exact=True)).to_contain_text('Jane')
    page.get_by_role('button', name='Use reviewed baseline', exact=True).click()
    save(page, 'agents')
    assert data['state']['config']['gitUserName'] == 'Draft Author'
    assert [b for e, b in data['calls'] if e == 'config'][-1] == {
        'expectedVm': 'demo', 'expectedRevision': saved_revision,
        'changes': {'gitUserName': 'Draft Author'},
    }


def test_two_tabs_review_latest_values_before_reapply(agent_page, tmp_path):
    page = agent_page
    second = page.context.browser.new_page()
    second.set_default_timeout(1500)
    second.goto(page.url)
    for tab in (page, second):
        tab.clock.install()
        tab.reload()
        pause_polling(tab)
    open_settings(page, 'resources')
    open_settings(second, 'resources')
    field(page, '#cfg-cpus').fill('8')
    field(second, '#cfg-cpus').fill('12')
    field(second, '#cfg-memoryGiB').fill('6.25')
    save(second, 'resources')
    save(page, 'resources')
    expect(feedback(page, 'resources')).to_contain_text('conflict')
    conflict = panel(page, 'resources').get_by_role('region', name='Configuration conflict', exact=True)
    expect(conflict).to_contain_text('12')
    expect(conflict).to_contain_text('8')
    expect(field(page, '#cfg-cpus')).to_have_value('8')
    page.get_by_role('button', name='Use reviewed baseline', exact=True).click()
    expect(field(page, '#cfg-memoryGiB')).to_have_value('6.25')
    save(page, 'resources')
    saved = u.read_config(str(tmp_path))[0]
    assert saved['cpus'] == 8 and saved['memoryGiB'] == 6.25
    second.close()


@pytest.mark.parametrize('section,key,value', [('repositories','repos','github.com/example/new'), ('agents','plugins','tool@market'), ('network','egress','added.example')])
def test_list_pending_add_remove_keyboard_and_unload(workers, section, key, value):
    page, data = workers
    open_settings(page, section)
    control = field(page, f'#{key}-input')
    control.fill(value)
    save(page, section)
    assert not [b for e,b in data['calls'] if e == 'config']
    expect(feedback(page, section)).to_contain_text(re.compile('add.*clear', re.I))
    assert page.evaluate("()=>{const e=new Event('beforeunload',{cancelable:true});window.dispatchEvent(e);return e.defaultPrevented}")
    add_entry(page, section)
    remove = page.get_by_role('button', name='Remove '+value, exact=True)
    remove.focus()
    page.keyboard.press('Enter')
    expect(remove).to_have_count(0)
    panel(page, section).get_by_role('button', name='Discard', exact=True).click()
    assert not page.evaluate("()=>{const e=new Event('beforeunload',{cancelable:true});window.dispatchEvent(e);return e.defaultPrevented}")
    assert page.evaluate('Object.keys(localStorage)') in ([], ['floatie-theme'])


@pytest.mark.parametrize('failure', ['validation', 'http', 'identity'])
def test_errors_keep_edits_and_commands_obey_identity(workers, failure):
    page, data = workers
    open_settings(page, 'resources')
    field(page, '#cfg-cpus').fill('invalid' if failure == 'validation' else '8')
    if failure == 'http':
        page.route('**/api/config', lambda route: route.fulfill(status=500,json={'ok':False,'error':'Synthetic write failure'}))
    if failure == 'identity':
        data['state']['identity'] = {'matches':False,'error':'Workspace identity changed'}
        refresh(page)
        expect(save_button(page, 'resources')).to_be_disabled()
        expect(page.get_by_role('button', name='Copy floatie rebuild', exact=True)).to_be_disabled()
        assert panel(page, 'resources').get_by_role('region', name='floatie rebuild command', exact=True).locator('code').inner_text() == ''
    else:
        save(page, 'resources')
        expect(feedback(page, 'resources')).to_contain_text(re.compile('cpus', re.I) if failure == 'validation' else 'Synthetic write failure')
    expect(field(page, '#cfg-cpus')).to_have_value('invalid' if failure == 'validation' else '8')


def test_sparse_keys_and_manual_git_unset(agent_page, tmp_path):
    (tmp_path/'workspace.yml').write_text('name: demo\n# keep comment\n')
    page = agent_page
    page.reload()
    open_settings(page, 'resources')
    expect(field(page, '#cfg-cpus')).to_have_value('')
    expect(field(page, '#cfg-memoryGiB')).to_have_value('')
    field(page, '#cfg-memoryGiB').fill('2.5')
    save(page, 'resources')
    assert u.read_config(str(tmp_path))[0]['cpus'] is None
    assert u.read_config(str(tmp_path))[0]['memoryGiB'] == 2.5
    assert '# keep comment' in (tmp_path/'workspace.yml').read_text()
    open_settings(page, 'agents')
    field(page, '#cfg-gitUserName').fill('Author')
    save(page, 'agents')
    field(page, '#cfg-gitUserName').fill('')
    save(page, 'agents')
    expect(feedback(page, 'agents')).to_contain_text('manual')
    assert u.read_config(str(tmp_path))[0]['gitUserName'] == 'Author'


@pytest.mark.parametrize('button', ['network-apply','fw-reapply'])
def test_stale_saved_policy_rejected_while_draft_retained(agent_page, tmp_path, monkeypatch, button):
    page = agent_page
    page.clock.install()
    page.reload()
    open_settings(page, 'network')
    pause_polling(page)
    # Real HTTP fixture reads after the 409 must share the paused browser's
    # clock. Otherwise a slower runner timestamps the next observation in the
    # browser's future, correctly disabling the second policy action.
    frozen_now = datetime.fromtimestamp(page._fixture_now / 1000, timezone.utc)
    class PausedFixtureTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return frozen_now.astimezone(tz) if tz else frozen_now.replace(tzinfo=None)
    monkeypatch.setattr(u, 'datetime', PausedFixtureTime)
    field(page, '#egress-input').fill('pending.example')
    _, revision, _ = u.read_config(str(tmp_path))
    u.save_config(str(tmp_path), revision, {'permissive':True, 'egress':['new-saved.example']})
    firewall_calls = []
    monkeypatch.setattr(u, 'run_action', lambda *a, **kw: firewall_calls.append(a))
    with page.expect_response('**/api/action') as response:
        network_action(page, button).click()
    assert response.value.status == 409
    idle(page)
    assert firewall_calls == []
    expect(page.get_by_role('region', name='Saved network policy', exact=True)).to_contain_text('new-saved.example')
    expect(field(page, '#egress-input')).to_have_value('pending.example')
    network_mode(page, False).click()
    network_action(page, button).click()
    confirmation = network_confirmation(page)
    expect(confirmation).to_be_visible()
    expect(confirmation).to_contain_text('new-saved.example')
    expect(confirmation).not_to_contain_text('pending.example')
    confirmation.get_by_role('button', name='Cancel', exact=True).click()
    assert firewall_calls == []


def test_guidance_distinguishes_mixed_effects_and_unknown_observation(workers):
    page, data = workers
    open_settings(page, 'resources')
    field(page, '#cfg-cpus').fill('8')
    save(page, 'resources')
    expect(panel(page, 'resources')).to_contain_text('Requires VM rebuild')
    expect(panel(page, 'resources')).to_contain_text('signing identity')
    open_settings(page, 'repositories')
    field(page, '#repos-input').fill('github.com/example/new')
    add_entry(page, 'repositories')
    page.get_by_role('button', name='Remove github.com/myorg/api', exact=True).click()
    save(page, 'repositories')
    guidance = panel(page, 'repositories').get_by_role('region', name='Apply saved changes', exact=True)
    expect(guidance).to_contain_text('clone missing')
    expect(guidance).to_contain_text('manual reconciliation')
    expect(guidance).not_to_contain_text('rebuild')
    data['state']['firewall'] = {'mode':'unknown','error':'Observation unavailable','observedAt':None,
                               'permissiveConfigured':None,'egress':None}
    open_settings(page, 'network')
    expect(page.get_by_role('region', name='Effective network policy', exact=True)).to_contain_text('unknown')
    expect(page.get_by_role('region', name='Effective network policy', exact=True)).not_to_contain_text('OFF')
    open_settings(page, 'credentials')
    expect(panel(page, 'credentials')).to_contain_text('Values are never sent to the browser or displayed.')


@pytest.mark.parametrize('width', [390, 720])
def test_all_settings_narrow_keyboard_and_artifacts(workers, width):
    page, data = workers
    data['state']['workspace']['dir'] = "/host/quoted ' workspace/$literal"
    page.set_viewport_size({'width':width,'height':900 if width == 390 else 450})
    for theme in ['light','dark']:
        page.get_by_label('Theme', exact=True).select_option(theme)
        for section in ['resources','repositories','agents','network','credentials']:
            open_settings(page, section)
            assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
            form = panel(page, section)
            for control in form.locator('input:visible').all():
                assert control.bounding_box()['height'] >= 44
            if section in ('repositories', 'agents', 'network'):
                key = {'repositories':'repos','agents':'plugins','network':'egress'}[section]
                assert field(page, f'#{key}-input').bounding_box()['width'] >= 160
            buttons = form.locator('button:visible')
            for button in buttons.all():
                assert button.bounding_box()['height'] >= 44
            if buttons.count():
                buttons.first.focus()
                page.keyboard.press('Tab')
                assert page.evaluate('document.activeElement!==document.body')
            if os.environ.get('TASK10_ARTIFACT_DIR'):
                page.evaluate('scrollTo(0, 0)')
                page.screenshot(path=str(Path(os.environ['TASK10_ARTIFACT_DIR'])/f'{section}-{width}-{theme}.png'), full_page=True)
    open_settings(page, 'resources')
    block = panel(page, 'resources').get_by_role('region', name='floatie rebuild command', exact=True)
    expect(block.locator('code')).to_have_text("cd -- '/host/quoted '\"'\"' workspace/$literal' && floatie rebuild")


def test_list_refresh_order_matches_saved_values_without_replacing_buttons(workers):
    page, data = workers
    data['state']['config']['repos'] = ['github.com/example/a','github.com/example/b']
    data['state']['configRevision'] = 'c' * 64
    open_settings(page, 'repositories')
    button=page.get_by_role('button',name='Remove github.com/example/a',exact=True)
    button.focus()
    data['state']['config']['repos'].reverse()
    data['state']['configRevision']='d'*64
    poll(page)
    assert list_values(draft_list(page, 'repos').get_by_role('listitem')) == ['github.com/example/b','github.com/example/a']
    expect(button).to_be_focused()


def test_plugin_add_remove_and_git_edit_have_separate_application_guidance(workers):
    page, data = workers
    open_settings(page, 'agents')
    field(page, '#plugins-input').fill('old@market')
    add_entry(page, 'agents')
    save(page, 'agents')
    page.get_by_role('button', name='Remove old@market', exact=True).click()
    field(page, '#plugins-input').fill('new@market')
    add_entry(page, 'agents')
    field(page, '#cfg-gitUserName').fill('New Author')
    save(page, 'agents')
    note=panel(page, 'agents').get_by_role('region', name='Apply saved changes', exact=True)
    expect(note).to_contain_text('reload attempts installation')
    expect(note).to_contain_text('does not uninstall')
    expect(note).to_contain_text('repository-local overrides')
    assert not [body for endpoint,body in data['calls'] if endpoint=='action']


def test_real_unload_warning_retains_page_on_cancel(workers):
    page, _=workers
    open_settings(page, 'repositories')
    field(page, '#repos-input').fill('pending-repository')
    dialogs=[]
    page.once('dialog',lambda dialog:(dialogs.append(dialog.type),dialog.dismiss()))
    page.evaluate('location.reload()')
    page.wait_for_timeout(150)
    assert dialogs==['beforeunload']
    expect(field(page, '#repos-input')).to_have_value('pending-repository')


def test_identity_mismatch_keeps_locally_edited_drafts(workers):
    page, data = workers
    open_settings(page, 'resources')
    field(page, '#cfg-cpus').fill('8')
    data['state']['identity']={'matches':False,'error':'Workspace identity changed'}
    refresh(page)
    field(page, '#cfg-cpus').fill('12')
    refresh(page)
    expect(field(page, '#cfg-cpus')).to_have_value('12')
    expect(save_button(page, 'resources')).to_be_disabled()


@pytest.mark.parametrize('section,recipe', [('resources','rebuild'),('repositories','create'),('agents','reload'),('agents','codex-setup'),('agents','attach'),('credentials','create')])
def test_settings_copy_executes_literal_directory_only(workers, tmp_path, section, recipe):
    import subprocess
    page,data=workers
    directory=tmp_path / "space ' $HOME $(touch injected) `touch injected` ; &"
    directory.mkdir()
    data['state']['workspace']['dir']=str(directory)
    open_settings(page,section)
    page.evaluate("Object.defineProperty(navigator,'clipboard',{configurable:true,value:{writeText:async text=>{window.copied=text}}})")
    block=panel(page, section).get_by_role('region', name=f'floatie {recipe} command', exact=True)
    block.get_by_role('button', name=f'Copy floatie {recipe}', exact=True).click()
    expect(block.get_by_role('status')).to_have_text('Copied')
    command=page.evaluate('window.copied')
    result=subprocess.run(['/bin/sh','-c','floatie() { printf "%s\\0" "$PWD" "$@"; };\n'+command],cwd=tmp_path,capture_output=True,check=True)
    assert result.stdout.decode().split('\0')==[str(directory),recipe,'']
    assert not list(tmp_path.rglob('injected'))
    page.evaluate("Object.defineProperty(navigator,'clipboard',{configurable:true,value:{writeText:async()=>{throw Error('denied')}}})")
    block.get_by_role('button', name=f'Copy floatie {recipe}', exact=True).click()
    expect(block.get_by_role('status')).to_contain_text('Copy failed')
    assert page.evaluate('getSelection().toString()')==command


def test_sparse_network_mode_uses_existing_server_default(agent_page, tmp_path):
    (tmp_path/'workspace.yml').write_text('name: demo\n')
    page=agent_page
    page.reload()
    open_settings(page,'network')
    # Unlike absent numeric/Git fields, the actual parser returns False for absent permissive.
    assert u.read_config(str(tmp_path))[0]['permissive'] is False
    expect(page.get_by_role('region', name='Saved network policy', exact=True)).to_contain_text('Restricted')
    expect(network_mode(page, False)).to_have_attribute('aria-pressed','true')
    expect(network_mode(page, True)).to_have_attribute('aria-pressed','false')


def test_failed_config_observation_keeps_save_error_visible(workers):
    page,data=workers
    open_settings(page,'resources')
    field(page, '#cfg-cpus').fill('8')
    page.route('**/api/config',lambda route: route.fulfill(status=500,json={'ok':False,'error':'Synthetic write failure'}))
    save(page,'resources')
    data['state']['configError']='Synthetic read failure'
    data['state']['configRevision']=None
    refresh(page)
    expect(feedback(page, 'resources')).to_contain_text('Synthetic write failure')
    expect(panel(page, 'resources')).to_contain_text('Synthetic read failure')


@pytest.mark.parametrize('section,key,duplicate,other', [
    ('repositories','repos','github.com/example/api','github.com/example/web'),
    ('agents','plugins','tool@market','other@market'),
    ('network','egress','duplicate.example','other.example'),
])
@pytest.mark.parametrize('remove_occurrence',[0,1],ids=['first','last'])
def test_saved_duplicate_list_occurrences_survive_poll_reorder_and_removal(
        agent_page,tmp_path,section,key,duplicate,other,remove_occurrence):
    page=agent_page
    page.clock.install()
    _,revision,_=u.read_config(str(tmp_path))
    u.save_config(str(tmp_path),revision,{key:[duplicate,duplicate,other]})
    page.reload()
    open_settings(page,section)
    pause_polling(page)
    rows=draft_list(page, key).get_by_role('listitem')
    expect(rows).to_have_count(3)
    original_rows=rows.element_handles()
    page.get_by_role('button',name='Remove '+duplicate,exact=True).nth(1).focus()
    before=(tmp_path/'workspace.yml').read_bytes()
    for _ in range(3):
        poll(page)
        expect(rows).to_have_count(3)
        assert rows.evaluate_all('(els,original)=>els.every((el,i)=>el===original[i])',original_rows)
        assert original_rows[1].evaluate('(row)=>document.activeElement===row.querySelector("button")')
    assert (tmp_path/'workspace.yml').read_bytes()==before

    # A real external save reorders existing occurrences and adds another duplicate.
    _,revision,_=u.read_config(str(tmp_path))
    u.save_config(str(tmp_path),revision,{key:[other,duplicate,duplicate,duplicate]})
    poll(page)
    expect(rows).to_have_count(4)
    assert rows.evaluate_all('(els,original)=>els.slice(0,3).every((el,i)=>el===original[[2,0,1][i]])',original_rows)
    for _ in range(3):
        poll(page)
        expect(rows).to_have_count(4)
        assert list_values(rows)==[other,duplicate,duplicate,duplicate]
        assert original_rows[1].evaluate('(row)=>document.activeElement===row.querySelector("button")')

    # Decreasing multiplicity retires only the unused occurrence, not other rows.
    _,revision,_=u.read_config(str(tmp_path))
    _,saved_revision=u.save_config(str(tmp_path),revision,{key:[duplicate,other,duplicate]})
    poll(page)
    expect(rows).to_have_count(3)
    assert rows.evaluate_all('(els,original)=>els.every((el,i)=>el===original[[0,2,1][i]])',original_rows)
    before=(tmp_path/'workspace.yml').read_bytes()
    calls=[]
    page.on('request',lambda request:calls.append((request.url.split('/api/')[-1],request.post_data_json)) if request.method=='POST' else None)
    page.get_by_role('button',name='Remove '+duplicate,exact=True).nth(remove_occurrence).focus()
    page.keyboard.press('Enter')
    expected=[other,duplicate] if remove_occurrence==0 else [duplicate,other]
    expected_nodes=[2,1] if remove_occurrence==0 else [0,2]
    for _ in range(3):
        poll(page)
        expect(rows).to_have_count(2)
        assert list_values(rows)==expected
        assert rows.evaluate_all('(els,original)=>els.every((el,i)=>el===original[i])',[original_rows[i] for i in expected_nodes])
        expect(page.get_by_role('button',name='Remove '+other,exact=True)).to_be_focused()
    assert (tmp_path/'workspace.yml').read_bytes()==before
    save(page,section)
    assert u.read_config(str(tmp_path))[0][key]==expected
    assert [body for endpoint,body in calls if endpoint=='config']==[
        {'expectedVm':'demo','expectedRevision':saved_revision,'changes':{key:expected}}
    ]
    assert not [body for endpoint,body in calls if endpoint=='action']


def test_network_confirmation_waits_for_read_and_rejects_changed_saved_policy(workers):
    from test_ui_polling import settings_poll
    page, data = workers
    data['state']['config']['permissive'] = True
    data['state']['configRevision'] = 'e' * 64
    open_settings(page, 'network')
    refresh(page)
    network_action(page).click()
    confirmation = network_confirmation(page)
    expect(confirmation).to_contain_text('Unrestricted')
    with settings_poll(page):
        confirmation.get_by_role('button', name='Apply saved policy', exact=True).click()
        expect(confirmation.get_by_role('button', name='Cancel', exact=True)).to_be_disabled()
        assert not [body for endpoint, body in data['calls'] if endpoint == 'action']
        data['state']['config']['permissive'] = False
        data['state']['configRevision'] = 'f' * 64
    expect(confirmation.get_by_role('status')).to_contain_text('Saved network policy changed')
    assert not [body for endpoint, body in data['calls'] if endpoint == 'action']
    # The original confirmation cannot silently adopt the newly observed policy.
    expect(confirmation).to_contain_text('Unrestricted')
    confirmation.get_by_role('button', name='Cancel', exact=True).click()
    network_action(page).click()
    idle(page)
    writes = [body for endpoint, body in data['calls'] if endpoint == 'action']
    assert writes == [{'action': 'firewall-on', 'params': {}, 'expectedVm': 'demo', 'expectedRevision': 'f' * 64}]


def test_settings_desktop_visual_and_readonly_credentials(workers):
    page, _ = workers
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.set_viewport_size({'width': 1280, 'height': 900})
    open_settings(page, 'resources')
    field(page, '#cfg-cpus').fill('8')
    for theme in ['light', 'dark']:
        page.get_by_label('Theme', exact=True).select_option(theme)
        for section in ['resources', 'repositories', 'agents', 'network', 'credentials']:
            open_settings(page, section)
            expect(page.get_by_role('navigation', name='Settings sections', exact=True)).to_be_visible()
            assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
            if section == 'credentials':
                expect(panel(page, section).locator('input')).to_have_count(0)
                expect(panel(page, section).get_by_role('button', name='Save changes', exact=True)).to_have_count(0)
            if os.environ.get('TASK10_ARTIFACT_DIR'):
                page.evaluate('scrollTo(0, 0)')
                page.screenshot(path=str(Path(os.environ['TASK10_ARTIFACT_DIR']) / f'{section}-1280-{theme}.png'), full_page=True)
    assert page.evaluate('Object.keys(localStorage)') == ['floatie-theme']
    assert page.evaluate('Object.keys(sessionStorage)') == []
    assert errors == []


def test_network_conflict_response_remains_reviewable_when_followup_read_fails(workers):
    page, data = workers
    open_settings(page, 'network')
    refresh(page)
    field(page, '#egress-input').fill('pending.example')
    latest = {**data['state']['config'], 'permissive': True, 'egress': ['latest.example']}
    calls = []
    def conflict(route):
        calls.append(route.request.post_data_json)
        data['fail_state'] = True
        route.fulfill(status=409, json={'ok': False, 'error': 'Configuration revision conflict',
            'config': latest, 'configRevision': 'd' * 64})
    page.route('**/api/action', conflict)
    network_action(page).click()
    idle(page)
    expect(feedback(page, 'network')).to_contain_text('revision conflict')
    expect(page.get_by_role('region', name='Saved network policy', exact=True)).to_contain_text('latest.example')
    expect(field(page, '#egress-input')).to_have_value('pending.example')
    expect(network_action(page)).to_be_disabled()
    assert len(calls) == 1
    page.unroute('**/api/action', conflict)
    data['fail_state'] = False
    data['state'].update(config=latest, configRevision='d' * 64)
    refresh(page)
    network_action(page).click()
    confirmation = network_confirmation(page)
    expect(confirmation).to_contain_text('latest.example')
    expect(confirmation).not_to_contain_text('pending.example')
    confirmation.get_by_role('button', name='Cancel', exact=True).click()
    assert not [body for endpoint, body in data['calls'] if endpoint == 'action']


def test_save_completion_keeps_a_newer_followup_observation(workers, tmp_path):
    page, data = workers
    open_settings(page, 'agents')
    field(page, '#cfg-gitUserName').fill('Retained author')
    open_settings(page, 'resources')
    field(page, '#cfg-cpus').fill('8')
    calls, later = [], []
    def concurrent_edit(route):
        calls.append(route.request.post_data_json)
        if len(calls) != 1:
            route.fallback()
            return
        response = route.fetch()
        saved = response.json()
        assert saved['ok']
        config, revision = u.save_config(str(tmp_path), saved['configRevision'], {'memoryGiB': 6.25})
        later.append(revision)
        data['state'].update(config=config, configRevision=revision)
        route.fulfill(response=response)
    page.route('**/api/config', concurrent_edit)
    save(page, 'resources')
    expect(feedback(page, 'resources')).to_contain_text('saved')
    expect(field(page, '#cfg-cpus')).to_have_value('8')
    expect(field(page, '#cfg-memoryGiB')).to_have_value('6.25')
    field(page, '#cfg-cpus').fill('12')
    save(page, 'resources')
    assert calls[1] == {'expectedVm': 'demo', 'expectedRevision': later[0], 'changes': {'cpus': 12}}
    open_settings(page, 'agents')
    expect(field(page, '#cfg-gitUserName')).to_have_value('Retained author')
    save(page, 'agents')
    expect(feedback(page, 'agents')).to_contain_text('conflict')
    assert len(calls) == 2  # The dirty sibling still requires explicit review.


def draft_status(page, section):
    return panel(page, section).get_by_role('status', name='Unsaved changes', exact=True)


def test_inline_unsaved_scalar_survives_navigation_and_failure_until_save(workers):
    page, data = workers
    open_settings(page, 'resources')
    field(page, '#cfg-cpus').fill('invalid')
    expect(draft_status(page, 'resources')).to_have_text('Unsaved changes')
    save(page, 'resources')
    expect(feedback(page, 'resources')).to_contain_text(re.compile('cpus', re.I))
    expect(draft_status(page, 'resources')).to_have_text('Unsaved changes')
    open_settings(page, 'credentials')
    open_settings(page, 'resources')
    expect(draft_status(page, 'resources')).to_have_text('Unsaved changes')
    field(page, '#cfg-cpus').fill('8')
    save(page, 'resources')
    expect(draft_status(page, 'resources')).to_have_count(0)
    expect(feedback(page, 'resources')).to_contain_text('saved')
    assert not [body for endpoint, body in data['calls'] if endpoint == 'action']


@pytest.mark.parametrize('section,key,value', [
    ('repositories', 'repos', 'github.com/example/pending'),
    ('agents', 'plugins', 'pending@market'), ('network', 'egress', 'pending.example'),
])
def test_inline_unsaved_pending_survives_sibling_save_and_discard(workers, section, key, value):
    page, data = workers
    open_settings(page, section)
    field(page, f'#{key}-input').fill(value)
    expect(draft_status(page, section)).to_have_text('Unsaved changes')
    save(page, section)
    expect(feedback(page, section)).to_contain_text(re.compile('add.*clear', re.I))
    expect(draft_status(page, section)).to_have_text('Unsaved changes')
    assert not [body for endpoint, body in data['calls'] if endpoint == 'config']
    open_settings(page, 'resources')
    field(page, '#cfg-cpus').fill('8')
    save(page, 'resources')
    expect(draft_status(page, 'resources')).to_have_count(0)
    open_settings(page, section)
    expect(field(page, f'#{key}-input')).to_have_value(value)
    expect(draft_status(page, section)).to_have_text('Unsaved changes')
    expect(panel(page, section).get_by_role('region', name='Configuration conflict', exact=True)).to_be_visible()
    panel(page, section).get_by_role('button', name='Discard', exact=True).click()
    expect(draft_status(page, section)).to_have_count(0)
    expect(field(page, f'#{key}-input')).to_have_value('')


@pytest.mark.parametrize('section,selector,draft_value,changes,saved_text,later_text', [
    ('resources', '#cfg-cpus', '8', {'cpus': 12}, r'4 CPUs', r'12 CPUs'),
    ('repositories', '#repos-input', 'github.com/example/draft', {'repos': ['github.com/other/api.git']},
     r'github.com/myorg/api', r'github.com/other/api.git'),
    ('agents', '#cfg-gitUserName', 'Draft Author', {'plugins': ['saved@market'], 'gitUserName': 'Saved Author'},
     r'Git author name\s*Jane', r'Git author name\s*Saved Author'),
])
def test_saved_summary_tracks_latest_observation_separate_from_draft(workers, section, selector, draft_value, changes, saved_text, later_text):
    page, data = workers
    open_settings(page, section)
    field(page, selector).fill(draft_value)
    if section == 'agents':
        field(page, '#plugins-input').fill('pending@market')
    guidance = panel(page, section).get_by_role('region', name='Apply saved changes', exact=True)
    saved = guidance.get_by_role('region', name='Saved ' + SECTIONS[section], exact=True)
    expect(saved).to_contain_text(re.compile(saved_text))
    expect(saved).not_to_contain_text(draft_value)
    expect(guidance.get_by_text(re.compile('read configuration when executed.*do not pin', re.I))).to_be_visible()
    data['state']['config'].update(changes)
    data['state']['configRevision'] = 'e' * 64
    refresh(page)
    expect(field(page, selector)).to_have_value(draft_value)
    expect(saved).to_contain_text(re.compile(later_text))
    expect(saved).not_to_contain_text(draft_value)
    if section == 'agents':
        expect(saved).to_contain_text('saved@market')
        expect(saved).to_contain_text('jane@x.com')
        expect(saved).not_to_contain_text('pending@market')
    assert not [body for endpoint, body in data['calls'] if endpoint in ('config', 'action')]


def test_saved_summary_preserves_sparse_values_and_marks_read_failure(workers):
    page, data = workers
    data['state']['config'].update(cpus=None, memoryGiB=None)
    data['state']['configRevision'] = 'c' * 64
    open_settings(page, 'resources')
    refresh(page)
    saved = panel(page, 'resources').get_by_role('region', name='Saved Resources', exact=True)
    expect(saved).to_contain_text(re.compile(r'Not configured CPUs'))
    expect(saved).to_contain_text(re.compile(r'Not configured GiB'))
    field(page, '#cfg-cpus').fill('8')
    data['fail_state'] = True
    refresh(page)
    expect(saved).to_contain_text(re.compile('Last known.*Refresh', re.I))
    expect(saved).to_contain_text(re.compile(r'Not configured CPUs'))
    expect(page.get_by_role('button', name='Copy floatie rebuild', exact=True)).to_be_disabled()


def test_observed_resources_remain_distinct_after_save(workers):
    page, data = workers
    open_settings(page, 'resources')
    field(page, '#cfg-cpus').fill('8')
    field(page, '#cfg-memoryGiB').fill('6.25')
    save(page, 'resources')
    saved = panel(page, 'resources').get_by_role('region', name='Saved Resources', exact=True)
    observed = panel(page, 'resources').get_by_role('region', name='Observed allocation', exact=True)
    expect(saved).to_contain_text(re.compile(r'8 CPUs'))
    expect(saved).to_contain_text(re.compile(r'6.25 GiB'))
    expect(observed).to_contain_text(re.compile(r'4 CPUs'))
    expect(observed).to_contain_text(re.compile(r'4 GiB'))
    expect(observed).not_to_contain_text(re.compile('unknown', re.I))
    assert not [body for endpoint, body in data['calls'] if endpoint == 'action']


@pytest.mark.parametrize('fault', ['stale', 'future', 'vm_error', 'read_error', 'missing'])
def test_observed_resources_do_not_present_unavailable_values_as_current(workers, fault):
    page, data = workers
    open_settings(page, 'resources')
    refresh(page)
    observed = panel(page, 'resources').get_by_role('region', name='Observed allocation', exact=True)
    expect(observed).to_contain_text(re.compile(r'4 CPUs'))
    if fault == 'read_error':
        data['fail_state'] = True
    elif fault == 'missing':
        data['state']['vm'].update(cpus=None, memoryGiB=None)
    elif fault == 'vm_error':
        data['state']['vm']['error'] = 'Synthetic allocation read failed'
    else:
        data['state']['vm']['observedAt'] = {'secondsAgo': 61 if fault == 'stale' else -5}
    refresh(page)
    if fault == 'missing':
        expect(observed).to_contain_text(re.compile(r'Unknown CPUs'))
        expect(observed).to_contain_text(re.compile(r'Unknown GiB'))
        expect(observed).not_to_contain_text(re.compile(r'0 CPUs'))
    else:
        expect(observed).to_contain_text(re.compile('unknown', re.I))
        expect(observed).to_contain_text('Last known:')
        expect(observed).to_contain_text(re.compile(r'4 CPUs'))
    if fault == 'vm_error':
        expect(observed).to_contain_text('Synthetic allocation read failed')


def test_observed_repository_names_do_not_claim_saved_urls_are_applied(workers):
    page, data = workers
    open_settings(page, 'repositories')
    field(page, '#repos-input').fill('github.com/other/api.git')
    add_entry(page, 'repositories')
    field(page, '#repos-input').fill('github.com/example/new')
    add_entry(page, 'repositories')
    save(page, 'repositories')
    observed = panel(page, 'repositories').get_by_role('region', name='Observed repository availability', exact=True)
    expect(observed).not_to_contain_text(re.compile('unknown', re.I))
    expect(observed).to_contain_text(re.compile(r'api — Clone name observed'))
    expect(observed).to_contain_text(re.compile(r'new — Not observed'))
    expect(observed).to_contain_text(re.compile('do not verify.*remote URL', re.I))
    assert not [body for endpoint, body in data['calls'] if endpoint == 'action']
    data['state']['availableRepos'].append('new')
    refresh(page)
    expect(observed).to_contain_text(re.compile(r'new — Clone name observed'))


@pytest.mark.parametrize('fault', ['stale', 'repository_error', 'read_error', 'stopped'])
def test_observed_repository_availability_becomes_unknown_without_current_evidence(workers, fault):
    page, data = workers
    open_settings(page, 'repositories')
    refresh(page)
    observed = panel(page, 'repositories').get_by_role('region', name='Observed repository availability', exact=True)
    expect(observed).to_contain_text(re.compile(r'api — Clone name observed'))
    if fault == 'stale':
        data['state']['inventory']['repositoryObservedAt'] = {'secondsAgo': 61}
    elif fault == 'repository_error':
        data['state']['inventory']['repositoryError'] = 'Synthetic repository scan failed'
    elif fault == 'stopped':
        data['state']['vm']['status'] = 'Stopped'
    else:
        data['fail_state'] = True
    refresh(page)
    expect(observed).to_contain_text(re.compile('unknown', re.I))
    expect(observed).not_to_contain_text('Clone name observed')
    expect(observed).not_to_contain_text('Not observed')
    if fault == 'repository_error':
        expect(observed).to_contain_text('Synthetic repository scan failed')
