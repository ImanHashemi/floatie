"""Task 8 shell acceptance with isolated HTTP fixtures only."""
import os
from pathlib import Path
import pytest
from playwright.sync_api import expect

expect.set_options(timeout=1500)
from test_agent_browser import agent_page
from test_agent_ux import workers
from workspace_fixtures import workspace_fixture
from ui_browser_helpers import refresh as explicit_refresh, scheduled_observation, open_settings


def load(workers, count=10):
    page, data = workers
    page.set_default_timeout(1200)
    state, agents = workspace_fixture(count)
    state['configRevision'] = data['state']['configRevision']
    state['config']['name'] = state['workspace']['name']
    data.update(state=state, agents=agents)
    page.reload()
    expect(page.get_by_test_id('ws-name')).to_have_text('studio')
    explicit_refresh(page)
    return page, data


def refresh(page):
    explicit_refresh(page)


def test_routes_and_settings_destinations(workers):
    page, _ = load(workers)
    nav = page.get_by_role('navigation', name='Primary')
    expect(nav.get_by_role('link')).to_have_count(2, timeout=800)
    expect(page.get_by_role('heading', name='Workspace', exact=True)).to_be_visible()
    nav.get_by_role('link', name='Settings', exact=True).click()
    expect(page.get_by_label('CPUs', exact=True)).to_be_visible()
    page.get_by_role('navigation', name='Settings sections').get_by_role('link', name='Repositories', exact=True).click()
    expect(page.get_by_label('Add repository', exact=True)).to_be_visible()
    page.go_back()
    expect(page.get_by_label('CPUs', exact=True)).to_be_visible()
    page.go_forward()
    expect(page.get_by_label('Add repository', exact=True)).to_be_visible()
    page.evaluate("location.hash='nonsense'")
    expect(page.get_by_role('heading', name='Workspace', exact=True)).to_be_visible()
    assert page.url.endswith('#workspace')


@pytest.mark.parametrize('count', [0, 1, 10, 56])
def test_inventory_count_and_density(workers, count):
    page, _ = load(workers, count)
    page.set_viewport_size({'width': 1440, 'height': 900})
    expect(page.locator('#worker-count')).to_have_text(f'{count} of {count} workers', timeout=800)
    expect(page.locator('#workers > [data-worker]')).to_have_count(count)
    expect(page.locator('#orchestrator [data-worker=main]')).to_be_visible()
    assert page.locator('#new-worker').bounding_box()['y'] < page.locator('#workers').bounding_box()['y']
    if count:
        columns = page.locator('.worker-columns > span').evaluate_all('(els)=>els.map(el=>el.getBoundingClientRect().x)')
        for row in page.locator('#workers > [data-worker]').all():
            cells = row.locator(':scope > .worker-name,:scope > .process,:scope > .changes,:scope > .row-actions').evaluate_all('(els)=>els.map(el=>el.getBoundingClientRect().x)')
            assert all(abs(header - cell) <= 1 for header, cell in zip(columns, cells)), (columns, cells)
    if count >= 10:
        complete = page.locator('#workers > [data-worker]').evaluate_all('(rows)=>rows.filter(r=>r.getBoundingClientRect().bottom<=900 && r.getBoundingClientRect().top>=0).length')
        if os.environ.get('TASK8_ARTIFACT_DIR'):
            page.screenshot(path=str(Path(os.environ['TASK8_ARTIFACT_DIR']) / f'FINAL-desktop-{count}-light.png'), full_page=False)
            (Path(os.environ['TASK8_ARTIFACT_DIR']) / f'FINAL-desktop-{count}-rows.txt').write_text(str(complete))
        assert complete >= 8
        if os.environ.get('TASK8_ARTIFACT_DIR'):
            page.get_by_label('Theme', exact=True).select_option('dark')
            page.screenshot(path=str(Path(os.environ['TASK8_ARTIFACT_DIR']) / f'FINAL-desktop-{count}-dark.png'), full_page=False)
    if count == 0:
        expect(page.locator('#empty-workers')).to_have_text('No workers yet. Create your first worker.')


def test_combined_search_filter_and_pinned_root(workers):
    page, _ = load(workers, 56)
    page.get_by_label('Search workers').fill('WEB')
    expect(page.locator('#worker-count')).to_have_text('3 of 56 workers', timeout=800)
    page.get_by_label('Worker filter').select_option('changes')
    expect(page.locator('#worker-count')).to_have_text('1 of 56 workers')
    page.get_by_label('Search workers').fill('WORKER-01')
    expect(page.locator('#worker-count')).to_have_text('1 of 56 workers')
    page.get_by_label('Search workers').fill('does-not-exist')
    expect(page.locator('#empty-workers')).to_contain_text('No workers match')
    expect(page.locator('#orchestrator [data-worker=main]')).to_be_visible()
    page.get_by_role('button', name='Clear filters').click()
    expect(page.locator('#worker-count')).to_have_text('56 of 56 workers')


@pytest.mark.parametrize('failure', ['state', 'agents', 'both', 'old-counts', 'old-repo', 'unknown'])
def test_independent_observations_and_positive_filters(workers, failure):
    page, data = load(workers)
    if failure in ('state', 'both'): data['fail_state'] = True
    if failure in ('agents', 'both'): data['fail_list'] = True
    if failure == 'old-counts': data['state']['inventory']['countsObservedAt'] = {'secondsAgo': 61}
    if failure == 'old-repo':
        for row in data['state']['environments']:
            for repo in row['repos']: repo['observedAt'] = {'secondsAgo': 61}
    if failure == 'unknown':
        for row in data['state']['environments']:
            for repo in row['repos']: repo.update(clean=None, uncommitted=None, unpushed=None)
    refresh(page)
    expect(page.locator('#workers > [data-worker]')).to_have_count(10)
    if failure in ('agents', 'both'):
        page.get_by_label('Worker filter').select_option('running')
        expect(page.locator('#worker-count')).to_have_text('0 of 10 workers')
    elif failure in ('state', 'old-counts'):
        expect(page.locator('[data-worker=worker-00] .process')).to_contain_text('Codex running')
    page.get_by_label('Worker filter').select_option('changes')
    expect(page.locator('#worker-count')).to_have_text('2 of 10 workers' if failure == 'agents' else '0 of 10 workers')
    data['fail_state'] = data['fail_list'] = False
    revision = data['state']['configRevision']
    data['state'], data['agents'] = workspace_fixture(10)
    data['state']['configRevision'] = revision
    data['state']['config']['name'] = data['state']['workspace']['name']
    refresh(page)
    expect(page.locator('#worker-count')).to_have_text('2 of 10 workers')


def test_refresh_preserves_dom_focus_expansion_and_scroll(workers):
    page, data = load(workers, 56)
    page.get_by_label('Search workers').fill('worker')
    row = page.locator('[data-worker=worker-20]')
    row.locator('summary').click()
    row.locator('.copy').focus()
    page.evaluate('window.savedRow=document.querySelector("[data-worker=worker-20]"); window.savedY=scrollY')
    data['state']['environments'].reverse()
    with scheduled_observation(page, source='state', full=True):
        pass
    assert page.evaluate('savedRow===document.querySelector("[data-worker=worker-20]")')
    expect(row.locator('.copy')).to_be_focused()
    assert row.locator('details').evaluate('el=>el.open')
    assert page.evaluate('Math.abs(scrollY-savedY)') < 2
    expect(page.get_by_label('Search workers')).to_have_value('worker')


@pytest.mark.parametrize('width', [390, 720])
def test_responsive_theme_and_controls(workers, width):
    page, _ = load(workers)
    page.set_viewport_size({'width': width, 'height': 900})
    # Narrow viewport coverage; actual browser zoom is tested in acceptance.
    page.locator('[data-worker=worker-00] summary').click()
    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
    page.get_by_label('Theme', exact=True).select_option('dark')
    assert page.evaluate('getComputedStyle(document.body).backgroundColor') == 'rgb(20, 37, 43)'
    if os.environ.get('TASK8_ARTIFACT_DIR'):
        page.evaluate('scrollTo(0,0)')
        page.screenshot(path=str(Path(os.environ['TASK8_ARTIFACT_DIR']) / f'FINAL-workspace-{width}-dark.png'), full_page=width == 390)
    page.reload()
    expect(page.get_by_test_id('ws-name')).to_have_text('studio')
    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
    expect(page.get_by_label('Theme', exact=True)).to_have_value('dark')
    page.get_by_label('Theme', exact=True).select_option('light')
    assert page.evaluate('getComputedStyle(document.body).backgroundColor') == 'rgb(245, 248, 248)'
    if os.environ.get('TASK8_ARTIFACT_DIR'):
        page.evaluate('scrollTo(0,0)')
        page.screenshot(path=str(Path(os.environ['TASK8_ARTIFACT_DIR']) / f'FINAL-workspace-{width}-light.png'), full_page=width == 390)


def test_failed_counts_preserve_last_known_repo_observation(workers):
    page, data = load(workers)
    row = page.locator('[data-worker=worker-01]')
    row.locator('summary').click()
    expect(row.locator('.repos')).to_contain_text('1 uncommitted')
    before = row.locator('time').first.get_attribute('datetime')
    data['state']['inventory'].update(countsError='Counts unavailable', countsObservedAt=None)
    data['state']['environments'][2]['repos'][0].update(clean=None, uncommitted=None, unpushed=None)
    refresh(page)
    expect(row.locator('.repos')).to_contain_text('1 uncommitted')
    expect(row.locator('.changes')).to_contain_text('stale')
    assert row.locator('time').first.get_attribute('datetime') == before
    page.get_by_label('Worker filter').select_option('changes')
    expect(page.locator('#worker-count')).to_have_text('0 of 10 workers')


def test_settings_native_controls_keep_focus_on_refresh(workers):
    from test_settings_browser import poll
    page, _ = load(workers)
    open_settings(page, 'network')
    remove = page.get_by_role('button', name='Remove api.langfuse.com', exact=True)
    remove.focus()
    poll(page)
    expect(remove).to_be_focused()
    page.get_by_role('button', name='Unrestricted', exact=True).focus()
    page.keyboard.press('Enter')
    expect(page.get_by_role('button', name='Unrestricted', exact=True)).to_have_attribute('aria-pressed', 'true')
    page.get_by_label('Add domain', exact=True).fill('draft.example')
    poll(page)
    expect(page.get_by_label('Add domain', exact=True)).to_be_focused()
    expect(page.get_by_label('Add domain', exact=True)).to_have_value('draft.example')


@pytest.mark.parametrize('filter_name, count', [('all', 10), ('running', 8), ('absent', 2), ('changes', 2)])
def test_positive_filters_use_observed_evidence(workers, filter_name, count):
    page, _ = load(workers)
    page.get_by_label('Worker filter').select_option(filter_name)
    expect(page.locator('#worker-count')).to_have_text(f'{count} of 10 workers')
    expect(page.locator('#workers > [data-worker]:visible')).to_have_count(count)


@pytest.mark.parametrize('theme', ['light', 'dark'])
def test_rendered_contrast_and_system_preference(workers, theme):
    page, _ = load(workers)
    page.emulate_media(color_scheme=theme, reduced_motion='reduce')
    expect(page.locator('html')).to_have_attribute('data-theme', theme)
    page.locator('[data-worker=worker-01] summary').click()
    styles = page.locator('button,input,select,summary,h1,h2,h3,nav a,.muted,.process,.changes,.descriptor,.remove-note,#worker-count').evaluate_all('''els=>els.filter(el=>el.checkVisibility() && el.getBoundingClientRect().height && (el.textContent.trim() || el.tagName==='INPUT')).map(el=>{
      let parent=el,bg='';while(parent){bg=getComputedStyle(parent).backgroundColor;if(bg!=='rgba(0, 0, 0, 0)')break;parent=parent.parentElement;}
      const css=getComputedStyle(el);return {text:el.textContent.slice(0,60),fg:css.color,bg,transition:css.transitionDuration};
    })''')
    import re
    def luminance(color):
        values = [int(n) / 255 for n in re.findall(r'\d+', color)[:3]]
        rgb = [v / 12.92 if v <= .04045 else ((v + .055) / 1.055) ** 2.4 for v in values]
        return sum(a * b for a, b in zip(rgb, [.2126, .7152, .0722]))
    ratios = []
    for style in styles:
        light, dark = sorted([luminance(style['fg']), luminance(style['bg'])], reverse=True)
        ratio = (light + .05) / (dark + .05)
        assert ratio >= 4.5, (style, ratio)
        assert style['transition'] == '0s'
        ratios.append(ratio)
    copy = page.locator('[data-worker=worker-00] .copy')
    copy.hover()
    copy.focus()
    page.keyboard.press('Tab')
    page.keyboard.press('Shift+Tab')
    expect(copy).to_be_focused()
    assert copy.evaluate('el=>getComputedStyle(el).outlineStyle') == 'solid'
    assert copy.bounding_box()['height'] >= 32
    if os.environ.get('TASK8_ARTIFACT_DIR'):
        import json
        (Path(os.environ['TASK8_ARTIFACT_DIR']) / f'contrast-{theme}.json').write_text(json.dumps({'min_ratio': min(ratios), 'combinations': styles}, indent=2))


def test_poll_restores_focus_temporarily_disabled_for_refresh(workers):
    page, _ = load(workers)
    remove = page.locator('[data-worker=worker-00] .remove')
    remove.focus()
    with scheduled_observation(page, source='state', full=True):
        pass
    expect(remove).to_be_focused()


def test_unknown_route_does_not_trap_browser_back(workers):
    page, _ = load(workers)
    open_settings(page, 'repositories')
    page.evaluate("location.hash='unknown-destination'")
    expect(page.get_by_role('heading', name='Workspace', exact=True)).to_be_visible()
    page.go_back()
    expect(page.get_by_label('Add repository', exact=True)).to_be_visible()


@pytest.mark.parametrize('width', [1000, 1001, 1024, 1079, 1080, 1081])
def test_responsive_boundary_contains_worker_actions(workers, width):
    page, _ = load(workers)
    page.set_viewport_size({'width': width, 'height': 900})
    for expanded in [False, True]:
        if expanded:
            page.locator('[data-worker=worker-00] summary').click()
        geometry = page.locator('#workers > [data-worker]').evaluate_all('''rows=>{
          const inventory=document.querySelector('#workers').getBoundingClientRect();
          const clipped=[];
          for(const row of rows){
            const bounds=row.getBoundingClientRect();
            for(const control of row.querySelectorAll('summary,.remove')){
              const box=control.getBoundingClientRect();
              if(box.left<Math.max(bounds.left,inventory.left) ||
                 box.right>Math.min(bounds.right,inventory.right) ||
                 box.top<bounds.top || box.bottom>bounds.bottom)
                clipped.push({worker:row.dataset.worker,control:control.textContent,
                              left:box.left,right:box.right,rowLeft:bounds.left,rowRight:bounds.right});
            }
          }
          return {documentWidth:document.documentElement.scrollWidth,viewport:innerWidth,clipped};
        }''')
        # Check containment as well as document overflow: the list clips its children.
        assert geometry['documentWidth'] <= geometry['viewport'] and not geometry['clipped'], geometry
    if os.environ.get('TASK8_ARTIFACT_DIR'):
        page.evaluate('scrollTo(0,0)')
        page.screenshot(path=str(Path(os.environ['TASK8_ARTIFACT_DIR']) / f'FINAL-boundary-{width}.png'))


@pytest.mark.parametrize('theme', ['light', 'dark'])
@pytest.mark.parametrize('selector', ['#worker-search', '#egress-input'])
def test_native_input_placeholder_contrast(workers, theme, selector):
    page, _ = load(workers)
    page.get_by_label('Theme', exact=True).select_option(theme)
    if selector == '#egress-input':
        open_settings(page, 'network')
    control = page.locator(selector)
    expect(control).to_be_visible()
    colors = control.evaluate('''el=>{
        const placeholder=getComputedStyle(el,'::placeholder');
        return {foreground:placeholder.color,opacity:Number(placeholder.opacity),
                background:getComputedStyle(el).backgroundColor};
    }''')
    import re
    def luminance(color):
        rgb = [int(n) / 255 for n in re.findall(r'\d+', color)[:3]]
        linear = [n / 12.92 if n <= .04045 else ((n + .055) / 1.055) ** 2.4 for n in rgb]
        return sum(channel * weight for channel, weight in zip(linear, [.2126, .7152, .0722]))
    lighter, darker = sorted(map(luminance, [colors['foreground'], colors['background']]), reverse=True)
    ratio = (lighter + .05) / (darker + .05)
    assert colors['opacity'] == 1 and ratio >= 4.5, (theme, selector, colors, ratio)
    if os.environ.get('TASK8_ARTIFACT_DIR'):
        import json
        (Path(os.environ['TASK8_ARTIFACT_DIR']) / f'placeholder-{selector[1:]}-{theme}.json').write_text(
            json.dumps({**colors, 'contrast': ratio}, indent=2) + '\n')


# Task 9: exercise the shipped UI against isolated HTTP boundaries.
from test_agent_ux import agent, managed, card, open_workers


def create_dialog(page, name='feature-a'):
    page.get_by_role('button', name='New worker', exact=True).click()
    dialog = page.get_by_role('dialog', name='New worker', exact=True)
    expect(dialog).to_be_visible()
    page.get_by_label('Worker name', exact=True).fill(name)
    return dialog


def action_calls(data, action):
    return [body for _, body in data['calls'] if body.get('action') == action]


def test_create_dialog_keyboard_and_missing_clones(workers):
    page, data = workers
    data['state']['config']['repos'] = ['github.com/example/api', 'github.com/example/missing.git']
    refresh(page)
    dialog = create_dialog(page)
    expect(dialog).to_contain_text('api, web')
    expect(dialog).to_contain_text('missing')
    expect(dialog).to_contain_text('setup')
    for key in ['Tab'] * 10 + ['Shift+Tab'] * 10:
        page.keyboard.press(key)
        assert dialog.evaluate('el=>el.contains(document.activeElement)')
    page.keyboard.press('Escape')
    expect(dialog).to_be_hidden()
    expect(page.locator('#new-worker')).to_be_focused()
    page.locator('#new-worker').click()
    expect(page.locator('#wname')).to_have_value('feature-a')
    assert not action_calls(data, 'worker-create')


@pytest.mark.parametrize('repo,expected', [('', ['api', 'web']), ('api', ['api'])])
def test_create_frozen_scope_and_focus_created_row(workers, repo, expected):
    page, data = workers
    dialog = create_dialog(page)
    page.get_by_label('Repositories', exact=True).select_option(repo)
    def complete(route):
        data['calls'].append(('action', route.request.post_data_json))
        data['state']['environments'].append(managed('feature-a'))
        route.fulfill(json={'ok': True, 'outcome': 'completed', 'output': 'terminal created'})
    page.route('**/api/action', complete)
    dialog.get_by_role('button', name='Create worker', exact=True).click()
    expect(dialog).to_be_hidden()
    expect(card(page, 'feature-a').locator('.copy')).to_be_focused()
    assert action_calls(data, 'worker-create')[-1]['params'] == dict(name='feature-a', repos=expected, availableRepos=['api', 'web'])
    expect(page.locator('#envlog')).to_contain_text('No agent or service stack')


@pytest.mark.parametrize('name', ['main', 'stack', 'workspace', 'all', 'Bad', '-bad', 'a'*64])
def test_create_rejects_invalid_names_without_request(workers, name):
    page, data = workers
    dialog = create_dialog(page, name)
    dialog.get_by_role('button', name='Create worker', exact=True).click()
    expect(dialog.locator('[role=status]')).to_contain_text('Name')
    assert not action_calls(data, 'worker-create')


def test_duplicate_live_name_offers_attach_without_creation(workers):
    page, data = workers
    dialog = create_dialog(page, 'alpha')
    dialog.get_by_role('button', name='Create worker', exact=True).click()
    expect(dialog.locator('[role=status]')).to_contain_text('Already exists')
    expect(dialog).to_contain_text('attach')
    assert not action_calls(data, 'worker-create')


def test_changed_availability_retains_draft_and_requires_review(workers):
    page, data = workers
    dialog = create_dialog(page)
    data['state']['availableRepos'] = ['api']
    dialog.get_by_role('button', name='Create worker', exact=True).click()
    expect(dialog.locator('[role=status]')).to_contain_text('changed')
    expect(page.locator('#wname')).to_have_value('feature-a')
    assert not action_calls(data, 'worker-create')


def test_partial_creation_retains_draft_and_reports_reduced_scope(workers):
    page, data = workers
    dialog = create_dialog(page)
    def partial(route):
        data['calls'].append(('action', route.request.post_data_json))
        data['state']['environments'].append(managed('feature-a', False))
        route.fulfill(status=409, json={'ok': False, 'outcome': 'failed', 'output': 'Created api; web failed'})
    page.route('**/api/action', partial)
    dialog.get_by_role('button', name='Create worker', exact=True).click()
    expect(page.locator('#wname')).to_have_value('feature-a')
    feedback = dialog.locator('[role=status]')
    for text in ['Requested: api, web', 'Surviving scope: api', 'Missing: web', 'cannot', 'Remove']:
        expect(feedback).to_contain_text(text)
    expect(page.locator('#wname')).to_be_focused()
    dialog.get_by_role('button', name='Inspect worker', exact=True).click()
    expect(card(page, 'feature-a').locator('.command-value')).to_contain_text("floatie worker 'feature-a' 'api'")
    expect(card(page, 'feature-a').locator('.copy')).to_be_focused()
    assert len(action_calls(data, 'worker-create')) == 1


@pytest.mark.parametrize('lost', [False, True])
def test_remove_unknown_requires_explicit_refresh(workers, lost):
    page, data = workers
    data['state']['environments'].append(managed('safe'))
    refresh(page)
    button = card(page, 'safe').locator('.remove')
    button.click()
    dialog = page.get_by_role('dialog', name='Remove worker safe')
    def uncertain(route):
        data['calls'].append(('action', route.request.post_data_json))
        if lost: route.abort('connectionfailed')
        else: route.fulfill(json={'ok': False, 'outcome': 'unknown', 'output': 'terminal phase may have completed'})
    page.route('**/api/action', uncertain)
    dialog.get_by_role('button', name='Remove worker', exact=True).click()
    expect(dialog.locator('[role=status]')).to_contain_text('Outcome unknown')
    expect(dialog.get_by_role('button', name='Remove worker', exact=True)).to_be_disabled()
    page.keyboard.press('Escape')
    expect(button).to_be_disabled()
    with scheduled_observation(page, source='state', full=True):
        pass  # background observation alone cannot authorize a retry
    expect(button).to_be_disabled()
    page.get_by_role('button', name='Refresh', exact=True).click()
    expect(button).to_be_enabled()
    assert len(action_calls(data, 'worker-rm')) == 1


def test_remove_confirmation_rechecks_changed_preservation_state(workers):
    page, data = workers
    data['state']['environments'].append(managed('safe'))
    refresh(page)
    button = card(page, 'safe').locator('.remove')
    button.click()
    dialog = page.get_by_role('dialog', name='Remove worker safe')
    for key in ['Tab'] * 8 + ['Shift+Tab'] * 8:
        page.keyboard.press(key)
        assert dialog.evaluate('el=>el.contains(document.activeElement)')
    page.keyboard.press('Escape')
    expect(button).to_be_focused()
    button.click()
    data['state']['environments'][-1]['repos'][0].update(unpushed=1, clean=False)
    dialog.get_by_role('button', name='Remove worker', exact=True).click()
    expect(dialog.locator('[role=status]')).to_contain_text('changed')
    assert not action_calls(data, 'worker-rm')
    assert card(page, 'safe').get_by_role('button', name='Stop', exact=True).count() == 0
    assert card(page, 'main').locator('.remove').count() == 0


@pytest.mark.parametrize('state,pane,label', [('shell','%7','Start orchestrator'), ('dead',None,'Start orchestrator'), ('dead','%7','Restart orchestrator'), ('absent',None,'Start orchestrator')])
def test_root_selector_does_not_launch_and_uses_explicit_action(workers, state, pane, label):
    page, data = workers
    data['agents'] = [] if state == 'absent' else [agent(state=state, pane=pane, harness=None)]
    data['state']['environments'][0]['sessionLive'] = pane is not None
    refresh(page)
    page.get_by_label('Start orchestrator with').select_option('claude')
    assert not action_calls(data, 'start') and not action_calls(data, 'send')
    # Race/reuse returns the actual harness and observed pane, not the selection.
    def reuse(route):
        body = route.request.post_data_json
        if body['action'] != 'start': return route.fallback()
        data['calls'].append(('agents', body))
        data['agents'] = [agent(pane='%42', harness='codex')]
        route.fulfill(json={'ok': True, 'started': False, **data['agents'][0]})
    page.route('**/api/agents', reuse)
    page.get_by_role('button', name=label, exact=True).click()
    expect(card(page, 'main')).to_contain_text('Codex running')
    card(page, 'main').locator('summary').click()
    expect(card(page, 'main')).to_contain_text('%42')
    expect(card(page, 'main')).to_contain_text('handoff')
    expect(card(page, 'main')).to_contain_text('other harness')
    assert len(action_calls(data, 'start')) == 1
    assert not action_calls(data, 'send') and not action_calls(data, 'read')


def test_root_background_refusal_is_reported_without_replacing_pane(workers):
    page, data = workers
    data['agents'] = [agent(state='shell', pane='%7', harness=None)]
    refresh(page)
    def refuse(route):
        if route.request.post_data_json['action'] != 'start': return route.fallback()
        data['calls'].append(('agents', route.request.post_data_json))
        route.fulfill(json={'ok': False, 'error': 'Background agent present; inspect manually'})
    page.route('**/api/agents', refuse)
    page.locator('#root-start').click()
    expect(page.locator('#envlog')).to_contain_text('Background agent')
    assert data['agents'][0]['pane'] == '%7'
    assert not action_calls(data, 'send') and not action_calls(data, 'read')


@pytest.mark.parametrize('damage', ['missing-scope', 'empty', 'reserved', 'uppercase', 'unknown-agent'])
def test_uncertain_or_unsupported_scope_never_offers_managed_recreation(workers, damage):
    page, data = workers
    name = 'all' if damage == 'reserved' else 'Legacy' if damage == 'uppercase' else 'safe'
    row = managed(name, False)
    if damage == 'missing-scope': del row['scopeKnown']
    if damage == 'empty': row['repos'] = []
    if damage == 'unknown-agent': data['agents'].append(agent(name, state='unknown', pane=None, harness=None))
    data['state']['environments'].append(row)
    refresh(page)
    expect(card(page, name).get_by_role('button', name='Copy recreate command')).to_have_count(0)
    if damage != 'unknown-agent':
        expect(card(page, name).locator('.remove')).to_be_disabled()
        expect(card(page, name)).to_contain_text('inspect')


@pytest.mark.parametrize('lost', [False, True])
def test_create_unknown_retains_draft_and_requires_explicit_refresh(workers, lost):
    page, data = workers
    dialog = create_dialog(page)
    def uncertain(route):
        data['calls'].append(('action', route.request.post_data_json))
        if lost: route.abort('connectionfailed')
        else: route.fulfill(json={'ok':False, 'outcome':'unknown', 'output':'creation timed out'})
    page.route('**/api/action', uncertain)
    dialog.get_by_role('button', name='Create worker', exact=True).click()
    expect(dialog.locator('[role=status]')).to_contain_text('Outcome unknown')
    expect(page.locator('#wname')).to_have_value('feature-a')
    expect(page.locator('#wcreate')).to_be_disabled()
    with scheduled_observation(page, source='state', full=True):
        pass
    expect(page.locator('#wcreate')).to_be_disabled()
    dialog.get_by_role('button', name='Refresh observations').click()
    expect(page.locator('#wcreate')).to_be_enabled()
    assert len(action_calls(data, 'worker-create')) == 1


def test_server_availability_conflict_keeps_requested_draft(workers):
    page, data = workers
    dialog = create_dialog(page)
    def conflict(route):
        data['calls'].append(('action', route.request.post_data_json))
        data['state']['availableRepos'] = ['web']
        route.fulfill(status=409, json={'ok':False, 'error':'Available repositories changed; review selection'})
    page.route('**/api/action', conflict)
    dialog.get_by_role('button', name='Create worker', exact=True).click()
    expect(dialog.locator('[role=status]')).to_contain_text('Available repositories changed')
    expect(page.locator('#wname')).to_have_value('feature-a')
    assert len(action_calls(data, 'worker-create')) == 1


@pytest.mark.parametrize('width', [390, 720])
def test_lifecycle_dialogs_fit_narrow_viewports(workers, width):
    page, data = workers
    page.set_viewport_size({'width':width, 'height':600})
    dialog = create_dialog(page)
    for modal in ['create', 'remove']:
        if modal == 'remove':
            page.keyboard.press('Escape')
            data['state']['environments'].append(managed('safe'))
            refresh(page)
            card(page, 'safe').locator('.remove').click()
            dialog = page.get_by_role('dialog', name='Remove worker safe')
        bounds = dialog.bounding_box()
        assert bounds['x'] >= 0 and bounds['x'] + bounds['width'] <= width
        assert dialog.evaluate('el=>el.scrollWidth<=el.clientWidth')
        if os.environ.get('TASK9_ARTIFACT_DIR'):
            page.screenshot(path=str(Path(os.environ['TASK9_ARTIFACT_DIR']) / f'{modal}-{width}.png'))


def test_absent_root_reports_not_started_with_attach_in_details(workers):
    page, data = workers
    data['agents'] = []
    data['state']['environments'][0]['sessionLive'] = False
    refresh(page)
    root = card(page, 'main')
    expect(root.locator('.process')).to_have_text('Not started')
    expect(root.locator('.copy')).to_be_hidden()
    root.locator('summary').click()
    expect(root.locator('.command-value')).to_have_text("cd -- '/ws' && floatie attach")
    expect(page.locator('#root-start')).to_be_enabled()
    assert not action_calls(data, 'start')


@pytest.mark.parametrize('completed', [False, True])
def test_removal_reports_phase_result_and_reobserves_survivors(workers, completed):
    page, data = workers
    data['state']['environments'].append(managed('safe'))
    refresh(page)
    card(page, 'safe').locator('.remove').click()
    dialog = page.get_by_role('dialog', name='Remove worker safe')
    def result(route):
        data['calls'].append(('action', route.request.post_data_json))
        if completed:
            data['state']['environments'] = [row for row in data['state']['environments'] if row['name'] != 'safe']
        route.fulfill(json={'ok':completed, 'outcome':'completed' if completed else 'failed',
                            'output':'terminal stopped; worktrees removed' if completed else 'terminal stopped; hook failed; worktrees remain'})
    page.route('**/api/action', result)
    dialog.get_by_role('button', name='Remove worker', exact=True).click()
    if completed:
        expect(dialog).to_be_hidden()
        expect(card(page, 'safe')).to_have_count(0)
        expect(page.locator('#new-worker')).to_be_focused()
    else:
        expect(dialog.locator('[role=status]')).to_contain_text('terminal stopped; hook failed; worktrees remain')
        expect(card(page, 'safe')).to_be_visible()
    assert len(action_calls(data, 'worker-rm')) == 1


def test_unknown_config_save_can_reobserve_while_vm_stopped(workers):
    from test_settings_browser import field, save_button, feedback, idle
    # Shared mutation handling must not make config-only Settings depend on a running VM.
    page, data = workers
    data['state']['vm']['status'] = 'Stopped'
    open_settings(page, 'resources')
    field(page, '#cfg-cpus').fill('6')
    saves = []
    def lose_response(route):
        saves.append(route.request.post_data_json)
        route.abort('connectionfailed')
    page.route('**/api/config', lose_response)
    save_button(page, 'resources').click()
    expect(feedback(page, 'resources')).to_contain_text('Outcome unknown')
    idle(page)
    refresh(page)
    save_button(page, 'resources').click()
    page.wait_for_timeout(100)
    assert len(saves) == 2


# Fix round 1: regressions derived from the independent review's two probes.
from test_agent_ux import open_settings


@pytest.mark.parametrize('firewall', [
    dict(mode='unknown', observedAt=None, error='Firewall read failed'),
    dict(mode='on', observedAt={'secondsAgo':61}, error=None),
    dict(mode='unknown', observedAt={'secondsAgo':0}, error=None),
    dict(mode='on', observedAt=None, error=None),
    dict(mode='on', observedAt={'secondsAgo':0}, error='Firewall read failed'),
])
def test_network_unknown_retry_requires_current_firewall_observation(workers, firewall):
    from test_settings_browser import network_action, feedback, panel, idle, poll
    page, data = workers
    open_settings(page, 'network')
    revision = data['state']['configRevision']
    calls = []
    def apply(route):
        calls.append(route.request.post_data_json)
        if len(calls) == 1:
            data['state']['firewall'] = {**data['state']['firewall'], **firewall}
            route.fulfill(json={'ok':False, 'outcome':'unknown', 'output':'apply timed out'})
        else:
            route.fulfill(json={'ok':True, 'outcome':'completed', 'output':'policy applied'})
    page.route('**/api/action', apply)
    network_action(page).click()
    expect(feedback(page, 'network')).to_contain_text('Outcome unknown')
    idle(page)
    refresh(page)
    # Native dispatch must remain disabled; controller tests exercise direct calls.
    network_action(page).evaluate('el => el.click()')
    assert len(calls) == 1
    expect(network_action(page)).to_be_disabled()
    expect(network_action(page, 'fw-reapply')).to_be_disabled()
    recovery = panel(page, 'network').get_by_role('region', name='Network recovery', exact=True)
    expect(recovery).to_be_visible()
    expect(recovery).to_contain_text('Refresh')
    if firewall['error']:
        expect(recovery).to_contain_text('Firewall read failed')
    data['state']['firewall'].update(mode='on', observedAt={'secondsAgo':0}, error=None)
    poll(page)  # automatic/ordinary reads do not unlock retries
    expect(network_action(page)).to_be_disabled()
    refresh(page)
    expect(recovery).to_be_hidden()
    expect(network_action(page)).to_be_enabled()
    expect(network_action(page, 'fw-reapply')).to_be_enabled()
    network_action(page, 'fw-reapply').click()
    expect(feedback(page, 'network')).to_contain_text('policy applied')
    assert len(calls) == 2
    assert calls[1]['expectedVm'] == 'demo' and calls[1]['expectedRevision'] == revision


@pytest.mark.parametrize('origin', ['resources', 'network'])
@pytest.mark.parametrize('damage', ['read-failed', 'revision-missing', 'config-error'])
def test_unknown_config_lock_is_visible_in_both_editors_and_preserves_revision(workers, origin, damage):
    from test_settings_browser import field, add_entry, save_button, feedback, panel, draft_list, idle
    page, data = workers
    revision = data['state']['configRevision']
    open_settings(page, 'resources')
    field(page, '#cfg-cpus').fill('6')
    open_settings(page, 'network')
    field(page, '#egress-input').fill('review.example.com')
    add_entry(page, 'network')
    calls = []
    def save(route):
        calls.append(route.request.post_data_json)
        if len(calls) == 1: route.abort('connectionfailed')
        else: route.fulfill(status=409, json={'ok':False, 'error':'Configuration changed; review revision conflict'})
    page.route('**/api/config', save)
    open_settings(page, origin)
    save_button(page, origin).click()
    idle(page)
    for section in ['resources', 'repositories', 'agents', 'network']:
        open_settings(page, section)
        expect(save_button(page, section)).to_be_disabled()
        recovery = panel(page, section).get_by_role('region', name='Save recovery', exact=True)
        expect(recovery).to_be_visible()
        expect(recovery).to_contain_text('Outcome unknown')
        expect(recovery).to_contain_text('Refresh')
    save_button(page, 'network').evaluate('el => el.click()')
    assert len(calls) == 1
    if damage == 'read-failed': data['fail_state'] = True
    elif damage == 'revision-missing': data['state']['configRevision'] = None
    else: data['state']['configError'] = 'Configuration read failed'
    refresh(page)
    expect(save_button(page, 'network')).to_be_disabled()
    open_settings(page, 'resources')
    expect(save_button(page, 'resources')).to_be_disabled()
    assert len(calls) == 1
    data['fail_state'] = False
    data['state'].pop('configError', None)
    data['state']['configRevision'] = 'b' * 64
    refresh(page)
    expect(field(page, '#cfg-cpus')).to_have_value('6')
    open_settings(page, 'network')
    expect(save_button(page, 'network')).to_be_enabled()
    expect(panel(page, 'network').get_by_role('region', name='Save recovery', exact=True)).to_be_hidden()
    expect(draft_list(page, 'egress')).to_contain_text('review.example.com')
    # A successful observation releases the retry gate, not the dirty draft's revision.
    target = 'network' if origin == 'resources' else 'resources'
    open_settings(page, target)
    save_button(page, target).click()
    expect(feedback(page, target)).to_contain_text('conflict')
    # An observed conflict now blocks locally instead of sending a redundant
    # stale POST. Recovery cannot silently substitute the observed revision.
    assert len(calls) == 1 and calls[0]['expectedRevision'] == revision
    expect(panel(page, target).get_by_role('region', name='Configuration conflict', exact=True)).to_be_visible()
    page.get_by_role('button', name='Use reviewed baseline', exact=True).click()
    save_button(page, target).click()
    expect(feedback(page, target)).to_contain_text('revision conflict')
    assert len(calls) == 2 and calls[1]['expectedRevision'] == 'b' * 64
    assert calls[1]['expectedVm'] == 'demo'


@pytest.mark.parametrize('accept',[False,True],ids=['cancel','confirm'])
def test_stop_vm_confirmation_names_observed_target_and_whole_vm_scope(workers,accept):
    page,data=workers
    open_workers(page)
    page.get_by_role('button', name='VM details', exact=True).click()
    page.get_by_test_id('act-stop').click()
    dialog = page.get_by_role('dialog', name='Stop VM demo')
    expect(dialog).to_be_visible()
    text = dialog.inner_text().lower()
    for word in ['demo', 'terminals', 'agents', 'services', 'files', 'retained']:
        assert word in text
    dialog.get_by_role('button', name='Stop VM' if accept else 'Cancel', exact=True).click()
    expect(dialog).to_be_hidden()
    assert action_calls(data,'stop')==([{'action':'stop','params':{},'expectedVm':'demo'}] if accept else [])


def test_stop_handler_rechecks_identity_after_confirmation(workers):
    page, data = workers
    open_workers(page)
    page.get_by_role('button', name='VM details', exact=True).click()
    page.get_by_test_id('act-stop').click()
    dialog = page.get_by_role('dialog', name='Stop VM demo')
    expect(dialog).to_be_visible()
    data['state']['identity'] = {'matches': False, 'error': 'Changed during confirmation'}
    with scheduled_observation(page, source='state', full=True):
        pass
    dialog.get_by_role('button', name='Stop VM', exact=True).click()
    expect(dialog.get_by_role('status')).to_contain_text('identity')
    assert not action_calls(data, 'stop')


def test_focused_worker_removed_by_poll_moves_to_list_heading(workers):
    page, data = load(workers)
    row = card(page, 'worker-00')
    row.locator('.copy').focus()
    data['state']['environments'] = [env for env in data['state']['environments'] if env['name'] != 'worker-00']
    data['agents'] = [a for a in data['agents'] if a['target'] != 'worker-00']
    with scheduled_observation(page, source='state', full=True):
        pass
    expect(row).to_have_count(0)
    expect(page.get_by_role('heading', name='Workers', exact=True)).to_be_focused()
    expect(page.get_by_role('status', name='Worker updates')).to_contain_text('worker-00')


@pytest.mark.parametrize('target',['main','alpha','saved-worker'])
@pytest.mark.parametrize('mismatch_source',['state','discovery'])
def test_identity_mismatch_clears_all_worker_commands_and_guards_copy(workers,target,mismatch_source):
    page,data=workers
    data['state']['environments'].append(managed('saved-worker',False))
    open_workers(page)
    row=card(page,target)
    row.locator('summary').click()
    # Make the fallback command visible before identity changes.
    page.evaluate("Object.defineProperty(navigator,'clipboard',{configurable:true,value:{writeText:async()=>{throw Error('denied')}}})")
    row.locator('.copy').click()
    expect(row.locator('.copy-status')).to_contain_text('Copy failed')
    assert row.locator('.command-value').inner_text()
    if mismatch_source=='state':
        data['state']['identity']={'matches':False,'error':'Workspace identity changed'}
    else:
        def discovery(route):
            if route.request.post_data_json.get('action')!='list': return route.fallback()
            route.fulfill(json={'ok':True,'agents':data['agents'],'identity':{'matches':False,'error':'Workspace identity changed'}})
        page.route('**/api/agents',discovery)
    refresh(page)
    for name in ['main','alpha','saved-worker']:
        item=card(page,name)
        expect(item.locator('.copy')).to_be_disabled()
        expect(item.locator('.command-value')).to_have_text('')
        expect(item.locator('.copy-note')).to_contain_text('reopen')
    page.evaluate("window.copyAttempts=[];Object.defineProperty(navigator,'clipboard',{configurable:true,value:{writeText:async text=>copyAttempts.push(text)}})")
    # Disabled-control coverage only. Direct callback guards are tested in
    # commandCopy.test.ts; this click is intentionally suppressed by the UI.
    row.locator('.copy').evaluate('(button)=>button.click()')
    assert page.evaluate('copyAttempts')==[]
    expect(row.locator('.command-value')).to_be_empty()
    expect(row.locator('.copy-status')).not_to_contain_text('Copied')


def test_identity_mismatch_during_copy_failure_does_not_restore_stale_fallback(workers):
    page,data=workers
    open_workers(page)
    row=card(page,'main')
    page.evaluate("Object.defineProperty(navigator,'clipboard',{configurable:true,value:{writeText:()=>new Promise((resolve,reject)=>window.rejectCopy=reject)}})")
    row.locator('.copy').click()
    data['state']['identity']={'matches':False,'error':'Workspace identity changed'}
    refresh(page)
    page.evaluate("rejectCopy(Error('denied'))")
    expect(row.locator('.command-value')).to_have_text('')
    expect(row.locator('.copy-status')).not_to_contain_text('Copy failed')
    expect(row.locator('.copy-note')).to_contain_text('reopen')


@pytest.mark.parametrize('mode',['on','off'])
@pytest.mark.parametrize('failure',['failed-read','expired-firewall','expired-vm'])
def test_network_observation_failure_or_expiry_is_unknown_without_replacing_draft(workers,mode,failure):
    from test_settings_browser import field, draft_list, network_mode, poll
    page,data=workers
    data['state']['firewall']['mode']=mode
    open_settings(page,'network')
    field(page, '#egress-input').fill('retained-draft.example')
    original = draft_list(page, 'egress').inner_text()
    desired = network_mode(page, False).get_attribute('aria-pressed')
    effective = page.get_by_role('region', name='Effective network policy', exact=True)
    expect(effective).to_contain_text('ON' if mode=='on' else 'OFF')
    if failure=='failed-read':
        data['fail_state']=True
        refresh(page)
    else:
        # Supply an expired observation at the API boundary while leaving the
        # other observation current. No controller state or globals are changed.
        data['state']['firewall' if failure=='expired-firewall' else 'vm']['observedAt'] = {'secondsAgo':65}
        poll(page)
    expect(effective).to_contain_text('Effective policy unknown')
    expect(effective).to_contain_text('Last known')
    assert draft_list(page, 'egress').inner_text() == original
    expect(network_mode(page, False)).to_have_attribute('aria-pressed', desired)
    assert not action_calls(data,'firewall-on')
    open_workers(page)
    expect(page.locator('.workspace-exception').filter(has_text='Network observation unknown')).to_be_visible()
    open_settings(page, 'network')
    data['fail_state']=False
    data['state']['firewall']['observedAt'] = data['state']['vm']['observedAt'] = {'secondsAgo':0}
    refresh(page)
    expect(effective).to_contain_text('ON' if mode=='on' else 'OFF')
    expect(field(page, '#egress-input')).to_have_value('retained-draft.example')
    open_workers(page)
    expect(page.locator('.workspace-exception').filter(has_text='Network observation unknown')).to_be_hidden()


@pytest.mark.parametrize('count', [1, 10, 56])
@pytest.mark.parametrize('width', [1440, 390])
@pytest.mark.parametrize('theme', ['light', 'dark'])
def test_workspace_visual_matrix_and_no_console_errors(workers, count, width, theme):
    page, _ = workers
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.on('console', lambda message: errors.append(message.text) if message.type == 'error' else None)
    page, _ = load(workers, count)
    page.set_viewport_size({'width': width, 'height': 900})
    page.get_by_label('Theme', exact=True).select_option(theme)
    expect(page.locator('#workers > [data-worker]')).to_have_count(count)
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    if width == 1440 and count >= 10:
        assert page.locator('#workers > [data-worker]').evaluate_all('(rows)=>rows.filter(row=>row.getBoundingClientRect().bottom<=900).length') >= 8
    if os.environ.get('TASK4_ARTIFACT_DIR'):
        page.screenshot(path=str(Path(os.environ['TASK4_ARTIFACT_DIR']) / f'workspace-{count}-{width}-{theme}.png'), full_page=False)
    assert errors == []


def test_workspace_view_state_survives_routes_and_polls(workers):
    page, _ = load(workers, 56)
    page.get_by_label('Search workers').fill('worker-0')
    page.get_by_label('Worker filter').select_option('running')
    row = card(page, 'worker-00')
    row.locator('summary').click()
    page.get_by_role('navigation', name='Primary', exact=True).get_by_role('link', name='Settings', exact=True).click()
    page.get_by_role('heading', name='Resources', exact=True).wait_for()
    page.get_by_role('navigation', name='Primary', exact=True).get_by_role('link', name='Workspace', exact=True).click()
    expect(page.get_by_label('Search workers')).to_have_value('worker-0')
    expect(page.get_by_label('Worker filter')).to_have_value('running')
    assert row.locator('details').evaluate('el=>el.open')
    with scheduled_observation(page):
        pass
    assert row.locator('details').evaluate('el=>el.open')
    expect(page.get_by_label('Search workers')).to_have_value('worker-0')


@pytest.mark.parametrize('damage', ['none', 'stale', 'disk-error', 'vm-error'])
def test_vm_details_keeps_disk_and_allocation_evidence_independent(workers, damage):
    page, data = workers
    data['state']['vm'].update(diskPct=31, diskObservedAt={'secondsAgo': 61 if damage == 'stale' else 0},
                              diskError='Disk read failed' if damage == 'disk-error' else None)
    if damage == 'vm-error': data['state']['vm']['error'] = 'VM read failed'
    refresh(page)
    page.get_by_role('button', name='VM details', exact=True).click()
    details = page.locator('#vm-details')
    expect(details).to_contain_text('4 CPUs / 4 GiB allocated')
    expect(details).to_contain_text('31% used' if damage in ['none', 'vm-error'] else 'Unknown · last known 31%')
    if damage == 'disk-error': expect(details).to_contain_text('Disk read failed')
    if damage == 'vm-error':
        expect(details).to_contain_text('VM read failed')
        expect(details.get_by_role('button', name='Stop VM', exact=True)).to_be_disabled()
    else:
        expect(details.get_by_role('button', name='Apply workspace files', exact=True)).to_be_enabled()
    expect(details).to_contain_text('context, tools, skills and stack files')
    expect(details).to_contain_text('does not restart')
