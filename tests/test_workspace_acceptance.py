"""Final synthetic acceptance. No real VM, credentials or existing browser profile."""
import json
import base64
import os
from pathlib import Path
import re
import subprocess
from urllib.parse import urlsplit

import pytest
from playwright.sync_api import expect

import ui_server as u
from test_agent_browser import agent_page
from test_agent_ux import workers, agent, managed, card, open_settings, wire_state
from test_worker_rm import make_workspace, make_stubs, run_worker_rm, branches
from test_worker_scope import boundary, saved_worker
from test_worker_init_refresh import make_origin_and_clone, run_init, _git
from workspace_fixtures import workspace_fixture
from ui_browser_helpers import SECTIONS, delay_observation, refresh as explicit_refresh

ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = os.environ.get('TASK11_EVIDENCE_DIR')


def idle(page):
    expect(page.get_by_role('button', name='Refresh', exact=True)).to_be_enabled()
    expect(page.locator('#workspace-content')).to_have_attribute('aria-busy', 'false')


def observe(page):
    explicit_refresh(page)


def navigate(page, destination):
    page.get_by_role('navigation', name='Primary', exact=True).get_by_role(
        'link', name=destination, exact=True).click()


def evidence(name, value):
    if EVIDENCE:
        directory = Path(EVIDENCE)
        directory.mkdir(parents=True, exist_ok=True)
        (directory / (name + '.json')).write_text(json.dumps(value, indent=2) + '\n')


def screenshot(page, name):
    if EVIDENCE:
        directory = Path(EVIDENCE)
        directory.mkdir(parents=True, exist_ok=True)
        if '-zoom-' in name:
            # Capture the actual browser surface. Playwright's viewport clip
            # uses CSS dimensions and crops native browser zoom on macOS.
            session = page.context.new_cdp_session(page)
            try:
                captured = session.send('Page.captureScreenshot', {
                    'format': 'png', 'captureBeyondViewport': False, 'fromSurface': True,
                })
                (directory / (name + '.png')).write_bytes(base64.b64decode(captured['data']))
            finally:
                session.detach()
        else:
            page.screenshot(path=str(directory / (name + '.png')), full_page=False)


def test_disabled_remove_cannot_bypass_execution_guard(workers, tmp_path, monkeypatch):
    """Real HTTP -> dispatcher -> real preservation script; only transport is fake."""
    page, data = workers
    home = make_workspace(tmp_path, {'api': 'worker/alpha'}, worker='alpha')
    worktree = home / 'work/.worktrees/alpha/api'
    (worktree / 'f.txt').write_text('unsaved work\n')
    log = tmp_path / 'docker.log'
    bindir = make_stubs(tmp_path, log)
    # The browser's observation and the disk both describe an unsafe worker.
    data['state']['environments'] = [data['state']['environments'][0], managed('alpha')]
    data['state']['environments'][1]['repos'][0].update(clean=False, uncommitted=1)
    observe(page)
    expect(card(page, 'alpha').locator('.remove')).to_be_disabled()
    commands = []
    original_run = subprocess.run
    def transport(cmd, **kwargs):
        assert cmd[0] == 'just' and cmd[-2:] == ['worker-rm', 'alpha']
        commands.append(cmd)
        # Avoid recursion: run the actual shared script under its isolated Git/HOME boundary.
        with monkeypatch.context() as local:
            local.setattr(subprocess, 'run', original_run)
            return run_worker_rm(home, bindir, log, worker='alpha')
    monkeypatch.delenv('FLOATIE_UI_FIXTURES', raising=False)
    monkeypatch.setattr(u.subprocess, 'run', transport)
    response = page.request.post(page.url.split('/?')[0] + '/api/action',
        headers={'X-Floatie-Token': 'browser-token'},
        data={'action': 'worker-rm', 'expectedVm': 'demo', 'params': {'name': 'alpha'}})
    result = response.json()
    assert not result['ok'] and result['outcome'] == 'failed', result
    assert len(commands) == 1 and not log.exists()
    assert (worktree / 'f.txt').read_text() == 'unsaved work\n'
    with monkeypatch.context() as local:
        local.setattr(subprocess, 'run', original_run)
        assert 'worker/alpha' in branches(home, 'api')


def test_copied_recreate_then_manual_discovery_preserves_saved_scope(workers, tmp_path, boundary):
    page, data = workers
    bindir, sessions, calls, launches = boundary
    home, _ = make_origin_and_clone(tmp_path, 'api')
    make_origin_and_clone(tmp_path, 'web')
    worktree = saved_worker(home)
    tip = _git('rev-parse', 'HEAD', cwd=worktree)
    (worktree / 'notes').write_text('keep draft\n')
    data['state']['workspace']['dir'] = str(tmp_path)
    data['state']['environments'].append(managed('review', False))
    observe(page)
    page.evaluate("Object.defineProperty(navigator,'clipboard',{value:{writeText:async text=>{window.copied=text}}})")
    card(page, 'review').locator('.copy').click()
    expect(card(page, 'review').locator('.copy-status')).to_contain_text('Copied')
    # Execute copied shell syntax with a recording wrapper, then the real shared script.
    result = subprocess.run(['/bin/sh', '-c', 'floatie() { printf "%s\\0" "$@"; };\n' + page.evaluate('window.copied')],
        capture_output=True, check=True)
    args = result.stdout.decode().split('\0')[:-1]
    assert args == ['worker', 'review', 'api']
    result = run_init(home, bindir, *args[1:])
    assert result.returncode == 0, result.stderr
    assert json.loads(sessions.read_text()) == ['review'] and not launches.exists()
    assert _git('rev-parse', 'HEAD', cwd=worktree) == tip
    assert (worktree / 'notes').read_text() == 'keep draft\n'
    assert sorted(p.name for p in worktree.parent.iterdir()) == ['api']
    data['state']['environments'][-1]['sessionLive'] = True
    observe(page)
    expect(card(page, 'review')).to_contain_text('agent not observed')
    data['agents'].append(agent('review', pane='%42', harness='claude'))
    observe(page)
    expect(card(page, 'review')).to_contain_text('Claude running')
    expect(card(page, 'review').locator('.copy')).to_have_text('Copy attach')
    assert not [b for e,b in data['calls'] if e == 'agents' and b['action'] != 'list']


def test_conflict_after_workspace_navigation_retains_reviewed_draft(agent_page, tmp_path):
    page = agent_page
    open_settings(page, 'resources')
    page.get_by_label('CPUs', exact=True).fill('8')
    navigate(page, 'Workspace')
    path = tmp_path / 'workspace.yml'
    path.write_text(u.render_workspace_edit(path.read_text(), {'cpus': 12, 'memoryGiB': 6}))
    navigate(page, 'Settings')
    idle(page)
    page.get_by_role('button', name='Save changes', exact=True).click()
    idle(page)
    expect(page.get_by_role('status', name='Resources status', exact=True)).to_contain_text('conflict')
    expect(page.get_by_label('CPUs', exact=True)).to_have_value('8')
    assert u.read_config(str(tmp_path))[0]['cpus'] == 12
    page.get_by_role('button', name='Use reviewed baseline').click()
    expect(page.get_by_label('Memory (GiB)', exact=True)).to_have_value('6')
    page.get_by_role('button', name='Save changes', exact=True).click()
    idle(page)
    assert u.read_config(str(tmp_path))[0]['cpus'] == 8


def test_delayed_mutation_response_does_not_restore_connection_or_duplicate(workers):
    page, data = workers
    open_settings(page, 'resources')
    page.get_by_label('CPUs', exact=True).fill('8')
    navigate(page, 'Workspace')
    idle(page)
    page.get_by_role('button', name='VM details', exact=True).click()
    pending = []
    page.route('**/api/action', lambda route: pending.append(route))
    page.get_by_role('button', name='Apply workspace files', exact=True).click()
    expect(page.get_by_role('button', name='Refresh', exact=True)).to_be_disabled()
    assert len(pending) == 1
    data['fail_state'] = data['fail_list'] = True
    navigate(page, 'Settings')
    navigate(page, 'Workspace')
    pending[0].fulfill(json={'ok': True, 'outcome': 'completed', 'output': 'Workspace files applied'})
    idle(page)
    feedback = page.get_by_role('region', name='VM action', exact=True)
    expect(feedback).to_contain_text('Workspace files applied')
    expect(page.get_by_role('button', name='Apply workspace files', exact=True)).to_be_disabled()
    expect(page.locator('.connection')).to_contain_text('unavailable')
    open_settings(page, 'resources')
    expect(page.get_by_label('CPUs', exact=True)).to_have_value('8')
    data['fail_state'] = data['fail_list'] = False
    observe(page)
    navigate(page, 'Workspace')
    idle(page)
    expect(page.get_by_role('button', name='Apply workspace files', exact=True)).to_be_enabled()
    assert len(pending) == 1


def clean_state(count=56):
    state, agents = workspace_fixture(count)
    state['workspace'] = {'name': 'studio', 'dir': '/workspaces/studio'}
    state['config'].update(name='studio', repos=['github.com/example/api', 'github.com/example/web'],
        gitUserName='Example Author', gitUserEmail='author@example.com', egress=['api.example.com'])
    state['firewall']['egress'] = ['api.example.com']
    state['configRevision'] = 'a' * 64
    return state, agents


def tab_zoom(worker, url, factor=None):
    return worker.evaluate('''async ({url,factor})=>{
      const tabs=await chrome.tabs.query({});const tab=tabs.find(t=>t.url===url);
      if(!tab)throw Error('Acceptance tab missing');
      if(factor!==null)await chrome.tabs.setZoom(tab.id,factor);
      return {factor:await chrome.tabs.getZoom(tab.id),settings:await chrome.tabs.getZoomSettings(tab.id)};
    }''', {'url': url, 'factor': factor})


def ratio(first, second):
    def lum(color):
        rgb = [float(n)/255 for n in re.findall(r'[\d.]+', color)[:3]]
        rgb = [n/12.92 if n <= .04045 else ((n+.055)/1.055)**2.4 for n in rgb]
        return sum(n*w for n,w in zip(rgb, [.2126,.7152,.0722]))
    a,b = sorted([lum(first),lum(second)])
    return (b+.05)/(a+.05)


def rendered_audit(page):
    styles = page.locator('button,input,select,summary,nav a,h2,h3,p,label,.muted,.process,.changes,.remove-note').evaluate_all('''els=>els.filter(el=>el.checkVisibility() && (el.textContent.trim() || el.matches('input'))).map(el=>{
      const css=getComputedStyle(el);let parent=el,bg;
      while(parent){bg=getComputedStyle(parent).backgroundColor;if(bg!=='rgba(0, 0, 0, 0)')break;parent=parent.parentElement;}
      return {label:el.textContent.trim().slice(0,60),fg:css.color,bg,border:css.borderTopColor,
        boundary:el.matches('button,input,select'),outside:(()=>{let p=el.parentElement,c;while(p){c=getComputedStyle(p).backgroundColor;if(c!=='rgba(0, 0, 0, 0)')return c;p=p.parentElement;}return bg;})(),transition:css.transitionDuration};
    })''')
    for style in styles:
        assert ratio(style['fg'], style['bg']) >= 4.5, style
        assert style['transition'] == '0s', style
        if style['boundary']:
            # Filled actions are identified by their contrasting fill; outlined controls by border.
            assert max(ratio(style['border'], style['outside']),ratio(style['bg'], style['outside'])) >= 3, style
    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
    return {'textMinimum':min(ratio(s['fg'],s['bg']) for s in styles),
            'boundaryMinimum':min(max(ratio(s['border'],s['outside']),ratio(s['bg'],s['outside'])) for s in styles if s['boundary'])}


def key_activate(page, control):
    control.focus()
    page.keyboard.press('Enter')


def key_settings(page, section):
    selector = page.get_by_label('Settings section', exact=True)
    if selector.is_visible():
        selector.focus()
        # Native type-ahead works without the OS popup that headless macOS
        # Chromium does not expose to arrow-key input. Sections are visited
        # in menu order, so the next "r" moves Resources to Repositories.
        if selector.input_value() != section:
            page.keyboard.press(SECTIONS[section][0].lower())
            page.keyboard.press('Tab')
        expect(selector).to_have_value(section)
    else:
        key_activate(page, page.get_by_role('navigation', name='Settings sections', exact=True)
                     .get_by_role('link', name=SECTIONS[section], exact=True))
    expect(page.get_by_role('heading', name=SECTIONS[section], exact=True)).to_be_visible()


def capture_browser_errors(page):
    """Ignore only an obsolete observation's navigation cancellation."""
    errors = []
    pending_reads = {}
    route = urlsplit(page.url).fragment
    generation = 0

    def navigated(frame):
        nonlocal route, generation
        if frame != page.main_frame:
            return
        current = urlsplit(frame.url).fragment
        if current != route:
            route = current
            generation += 1

    def started(request):
        url = urlsplit(request.url)
        origin = urlsplit(page.url)
        same_origin = (url.scheme, url.netloc) == (origin.scheme, origin.netloc)
        read = (request.method == 'GET' and url.path in ('/api/state', '/api/base-domains')) or (
            request.method == 'POST' and url.path == '/api/agents'
            and request.post_data_json == {'action': 'list'})
        if same_origin and read:
            pending_reads[request] = generation

    def failed(request):
        started_generation = pending_reads.pop(request, None)
        obsolete = started_generation is not None and started_generation < generation
        if not (obsolete and request.failure == 'net::ERR_ABORTED'):
            errors.append(request.url.split('?')[0] + ': ' + str(request.failure))

    page.on('framenavigated', navigated)
    page.on('request', started)
    page.on('requestfinished', lambda request: pending_reads.pop(request, None))
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.on('console', lambda message: errors.append(message.text) if message.type == 'error' else None)
    page.on('requestfailed', failed)
    return errors


@pytest.mark.parametrize('capture_before_load', [False, True])
def test_browser_error_capture_accepts_navigation_cancelled_read(agent_page, capture_before_load):
    page = agent_page.context.browser.new_page() if capture_before_load else agent_page
    errors = capture_browser_errors(page)
    if capture_before_load:
        assert page.url == 'about:blank'
        page.goto(agent_page.url)
    open_settings(page, 'resources')
    observe(page)
    with delay_observation(page) as pending:
        with page.expect_request(lambda request: request.url.endswith('/api/state')):
            page.get_by_role('button', name='Refresh', exact=True).click()
        assert pending.route is not None
        with page.expect_event('requestfailed') as failed:
            navigate(page, 'Workspace')
            pending.release()
    assert failed.value.url.endswith('/api/state')
    assert failed.value.failure == 'net::ERR_ABORTED'
    observe(page)
    expect(page.get_by_role('status', name='Refresh status')).to_have_text('Refreshed.')
    assert not errors, errors


@pytest.mark.parametrize('endpoint,method,payload,navigation,failure', [
    ('state', 'GET', None, False, 'aborted'),
    ('state', 'GET', None, 'before-request', 'aborted'),
    ('unrelated', 'GET', None, True, 'aborted'),
    ('agents', 'POST', {'action': 'start'}, True, 'aborted'),
    ('state', 'GET', None, False, 'connectionfailed'),
    ('state', 'GET', None, True, 'connectionfailed'),
])
def test_browser_error_capture_retains_request_failures(
        agent_page, endpoint, method, payload, navigation, failure):
    page = agent_page
    open_settings(page, 'resources')
    observe(page)
    errors = capture_browser_errors(page)
    pending = []
    # Only this explicitly tagged browser fetch is intercepted. Application reads
    # still reach the real synthetic server, including after navigation.
    pattern = '**/api/' + endpoint + '?capture-regression'
    def hold(route):
        pending.append(route)
        page.evaluate("console.debug('capture regression held')")
    page.route(pattern, hold)
    try:
        if navigation == 'before-request':
            navigate(page, 'Workspace')
            idle(page)
        with page.expect_console_message(predicate=lambda message: message.text == 'capture regression held'):
            page.evaluate("""({endpoint,method,payload}) => {
                void fetch('/api/' + endpoint + '?capture-regression', {
                    method, body: payload === null ? undefined : JSON.stringify(payload),
                }).catch(() => {});
            }""", {'endpoint': endpoint, 'method': method, 'payload': payload})
        assert len(pending) == 1
        if navigation is True:
            navigate(page, 'Workspace')
        with page.expect_event('requestfailed', predicate=lambda request: request.url.endswith('?capture-regression')) as failed:
            pending[0].abort(failure)
        assert failed.value.failure == 'net::ERR_' + ('ABORTED' if failure == 'aborted' else 'CONNECTION_FAILED')
        assert failed.value.url.split('?')[0] + ': ' + failed.value.failure in errors
    finally:
        page.unroute(pattern)


@pytest.mark.parametrize('layout', ['desktop', 'narrow', 'zoom'])
@pytest.mark.parametrize('theme', ['light', 'dark'])
def test_rendered_surfaces_with_verified_browser_zoom(agent_page, tmp_path, layout, theme):
    """Full Chromium + extension browser zoom; no device/CSS scale emulation."""
    extension = ROOT / 'tests/fixtures/browser-zoom'
    context = agent_page.context.browser.browser_type.launch_persistent_context(str(tmp_path / 'chromium-profile'),
        channel='chromium', headless=True, no_viewport=True,
        args=[f'--disable-extensions-except={extension}', f'--load-extension={extension}',
              '--window-size=' + ('390,900' if layout == 'narrow' else '1440,900')])
    try:
        worker = context.service_workers[0] if context.service_workers else context.wait_for_event('serviceworker')
        page = context.pages[0]
        page.set_default_timeout(2000)
        errors = capture_browser_errors(page)
        page.emulate_media(color_scheme=theme, reduced_motion='reduce')
        state, agents = clean_state()
        def route(route):
            endpoint = route.request.url.split('/api/')[-1]
            if endpoint == 'state': route.fulfill(json=wire_state(state))
            elif endpoint == 'agents':
                assert route.request.post_data_json['action'] == 'list'
                route.fulfill(json={'ok': True, 'agents': agents, 'identity': state['identity']})
            elif endpoint == 'base-domains': route.fulfill(json={'domains':['github.com','api.example.com']})
            else: pytest.fail('Visual acceptance must not mutate: ' + endpoint)
        page.route('**/api/**', route)
        page.goto(agent_page.url)
        observe(page)
        before = page.evaluate('({width:innerWidth,dpr:devicePixelRatio,outer:outerWidth})')
        assert tab_zoom(worker, page.url)['factor'] == 1
        if layout == 'zoom':
            assert tab_zoom(worker, page.url, 2)['factor'] == 2
            page.wait_for_function('(width)=>Math.abs(innerWidth-width/2)<=1', arg=before['width'])
            assert page.evaluate('devicePixelRatio') == pytest.approx(before['dpr']*2)
            assert page.evaluate('outerWidth') == before['outer']
        if layout == 'narrow':
            # Headless browser window minimums vary; a 390px viewport is a separate test.
            page.set_viewport_size({'width':390,'height':900})
        metrics = {'browser':context.browser.version,'layout':layout,'theme':theme,'before':before,
            'after':page.evaluate('({width:innerWidth,height:innerHeight,dpr:devicePixelRatio,outer:outerWidth})'),
            'zoom':tab_zoom(worker,page.url),'contrast':{}}
        expect(page.locator('#worker-count')).to_have_text('56 of 56 workers')
        if layout == 'desktop':
            # Use exact acceptance viewport for density, independent of native chrome height.
            page.set_viewport_size({'width':1440,'height':900})
            metrics['fullyVisibleRows'] = page.locator('#workers > [data-worker]').evaluate_all('(rows)=>rows.filter(r=>r.getBoundingClientRect().top>=0 && r.getBoundingClientRect().bottom<=900).length')
            assert metrics['fullyVisibleRows'] >= 8
        metrics['after'] = page.evaluate('({width:innerWidth,height:innerHeight,dpr:devicePixelRatio,outer:outerWidth})')
        metrics['contrast']['workspace'] = rendered_audit(page)
        screenshot(page, f'workspace-{layout}-{theme}')
        page.get_by_label('Search workers').fill('WEB')
        page.get_by_label('Worker filter').select_option('changes')
        expect(page.locator('#worker-count')).to_have_text('1 of 56 workers')
        expect(page.locator('#orchestrator [data-worker=main]')).to_be_visible()
        page.get_by_label('Search workers').fill('missing')
        key_activate(page,page.get_by_role('button', name='Clear filters'))
        expect(page.locator('#worker-count')).to_have_text('56 of 56 workers')
        key_activate(page,page.get_by_role('button', name='New worker', exact=True))
        dialog = page.get_by_role('dialog', name='New worker', exact=True)
        page.get_by_label('Worker name', exact=True).fill('main')
        key_activate(page,dialog.get_by_role('button',name='Create worker',exact=True))
        expect(dialog.locator('[role=status]')).to_contain_text('reserved')
        metrics['contrast']['createError'] = rendered_audit(page)
        page.get_by_label('Worker name', exact=True).fill('review')
        for _ in range(9):
            page.keyboard.press('Tab')
            assert dialog.evaluate('el=>el.contains(document.activeElement)')
        page.keyboard.press('Escape')
        expect(page.locator('#new-worker')).to_be_focused()
        remove = card(page,'worker-00').locator('.remove')
        key_activate(page,remove)
        dialog = page.get_by_role('dialog',name='Remove worker worker-00')
        metrics['contrast']['removeDialog'] = rendered_audit(page)
        for _ in range(7):
            page.keyboard.press('Shift+Tab')
            assert dialog.evaluate('el=>el.contains(document.activeElement)')
        page.keyboard.press('Escape')
        expect(remove).to_be_focused()
        remove.hover()
        focus = remove.evaluate("""el=>{
            let parent=el.parentElement, background;
            while(parent){background=getComputedStyle(parent).backgroundColor;
                if(background!=='rgba(0, 0, 0, 0)')break;parent=parent.parentElement;}
            return {color:getComputedStyle(el).outlineColor,style:getComputedStyle(el).outlineStyle,background};
        }""")
        assert focus['style'] == 'solid' and ratio(focus['color'],focus['background']) >= 3, focus
        metrics['focusContrast'] = ratio(focus['color'],focus['background'])
        metrics['contrast']['focusedRemove'] = rendered_audit(page)
        key_activate(page, page.get_by_role('navigation', name='Primary', exact=True)
                     .get_by_role('link', name='Settings', exact=True))
        for section in ['resources','repositories','agents','network','credentials']:
            key_settings(page, section)
            idle(page)
            metrics['contrast'][section] = rendered_audit(page)
            if layout in ('narrow','zoom'):
                for control in page.locator('.settings-panel button:visible,.settings-panel input:visible').all():
                    assert control.bounding_box()['height'] >= 44
            screenshot(page, f'settings-{section}-{layout}-{theme}')
        if layout == 'zoom': assert tab_zoom(worker,page.url)['factor'] == 2
        assert not errors, errors
        metrics['browserErrors'] = errors
        evidence(f'rendered-{layout}-{theme}',metrics)
    finally:
        context.close()
