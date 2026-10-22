import json, os, re, socket, subprocess, sys, time
from datetime import datetime, timezone, timedelta
from pathlib import Path
import pytest
from playwright.sync_api import expect
import ui_server as u
from test_agent_browser import agent_page
from test_agent_ux import open_workers, open_settings
from ui_browser_helpers import refresh, scheduled_observation
from test_settings_browser import (
    add_entry, draft_list, feedback, field, idle, network_action,
    network_confirmation, network_mode, panel, pause_polling, poll,
    save, save_button,
)

FLOATIE = Path(__file__).resolve().parent.parent
FIX = FLOATIE / "tests" / "fixtures"


def _free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p


def _start(fixture, workspace):
    config = json.loads((FIX / fixture).read_text())["config"]
    keys = {'memoryGiB': 'memory', 'gitUserName': 'git_user_name', 'gitUserEmail': 'git_user_email'}
    lines = ['name: demo']
    for key, value in config.items():
        if key == 'name' or value is None:
            continue
        rendered = (str(value) + 'GiB' if key == 'memoryGiB' else
                    str(value).lower() if isinstance(value, bool) else str(value))
        lines.append(keys.get(key, key) + ':' + ('' if isinstance(value, list) else ' ' + rendered))
        if isinstance(value, list):
            lines.extend('  - ' + item for item in value)
    (workspace / "workspace.yml").write_text('\n'.join(lines) + '\n')
    port = _free_port()
    env = {**os.environ, "FLOATIE_UI_FIXTURES": str(FIX / fixture), "FLOATIE_VM": "demo", "PYTHONUNBUFFERED": "1"}
    proc = subprocess.Popen([sys.executable, str(FLOATIE / "ui_server.py"), "--port", str(port), "--workspace", str(workspace)],
                            cwd=str(FLOATIE), env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    token = ""
    for _ in range(50):
        line = proc.stdout.readline()
        if "?t=" in line:
            token = line.split("?t=")[1].split()[0].strip(); break
    time.sleep(0.3)
    return proc, port, token


@pytest.fixture
def page_ctx(tmp_path):
    from playwright.sync_api import sync_playwright
    procs = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch()

        def open_state(fixture):
            workspace = tmp_path / str(len(procs)); workspace.mkdir()
            proc, port, token = _start(fixture, workspace); procs.append(proc)
            page = browser.new_page()
            # Clock installation crosses the browser protocol before it starts
            # advancing. Leave a small, fresh-age margin so real-server fixture
            # timestamps cannot appear in the simulated browser's future.
            page.clock.install(time=datetime.now(timezone.utc) + timedelta(seconds=1))
            page.set_default_timeout(1500)
            page.config_path = workspace / "workspace.yml"
            page.goto(f"http://127.0.0.1:{port}/?t={token}")
            expect(page.get_by_test_id('ws-name')).to_have_text('demo')
            idle(page)
            return page

        yield open_state
        browser.close()
    for p in procs:
        p.terminate()
        p.wait(timeout=5)


def test_overview_rollup(page_ctx):
    page = page_ctx("state_running.json")
    expect(page.locator('.vm-summary')).to_contain_text('VM running')
    expect(page.locator('#workers > [data-worker]')).to_have_count(2)  # Saved alpha plus discovered beta.
    expect(page.get_by_role('region', name='Orchestrator', exact=True).locator('[data-worker]')).to_have_count(1)


def test_workers_cards(page_ctx):
    page = page_ctx("state_running.json")
    open_workers(page)
    alpha = page.locator('[data-worker="alpha"]')
    expect(alpha).to_be_visible()
    alpha.locator('summary').click()
    expect(alpha).to_contain_text("worker/alpha")
    expect(alpha.locator('code.command-value')).to_have_text("cd -- '" + str(page.config_path.parent) + "' && floatie attach 'alpha'")
    expect(alpha.get_by_role("button", name="Remove worker")).to_be_disabled()
    expect(page.locator('[data-worker]').first.locator('h3')).to_have_text('Orchestrator')


def test_live_discovery_overrides_offline_snapshot(page_ctx):
    page = page_ctx("state_worker_offline.json")
    open_workers(page)
    beta = page.locator('[data-worker="beta"]')
    # Fixture discovery observes a real shell pane despite the older snapshot.
    expect(beta.get_by_role('button', name='Copy attach command')).to_be_visible()
    expect(beta).to_contain_text('Terminal present')


def test_state_aware_buttons_running(page_ctx):
    page = page_ctx("state_running.json")
    page.get_by_role('button', name='VM details', exact=True).click()
    expect(page.get_by_test_id('act-start')).to_be_disabled()
    expect(page.get_by_test_id('act-start')).to_be_hidden()
    expect(page.get_by_role('button', name='Stop VM', exact=True)).to_be_enabled()
    expect(page.get_by_role('button', name='Apply workspace files', exact=True)).to_be_enabled()


def test_state_aware_buttons_stopped(page_ctx):
    page = page_ctx("state_stopped.json")
    page.get_by_role('button', name='VM details', exact=True).click()
    expect(page.get_by_role('button', name='Start VM', exact=True)).to_be_enabled()
    expect(page.get_by_test_id('act-stop')).to_be_disabled()
    expect(page.get_by_test_id('act-stop')).to_be_hidden()


def test_state_aware_buttons_absent(page_ctx):
    page = page_ctx("state_absent.json")
    page.get_by_role('button', name='VM details', exact=True).click()
    assert page.get_by_test_id("act-start").is_disabled()
    assert page.get_by_test_id("act-stop").is_disabled()
    assert page.get_by_test_id("act-reload").is_disabled()


def test_clean_worker_removal_posts_recipe(page_ctx):
    page = page_ctx("state_running.json")
    # Retain the server's complete identity/config-revision response shape.
    state = page.request.get(page.url.split('/?')[0] + '/api/state',
        headers={'X-Floatie-Token': page.url.split('?t=')[1].split('#')[0]}).json()
    for repo in state['environments'][1]['repos']:
        repo.update(clean=True, uncommitted=0, unpushed=0)
    page.route('**/api/state', lambda route: route.fulfill(json=u.FixtureProvider.resolve_times(state)))
    open_workers(page)
    page.locator('[data-worker="alpha"] summary').click()
    page.locator('[data-worker="alpha"]').get_by_role("button", name="Remove worker").click()
    page.get_by_role('dialog', name='Remove worker alpha').get_by_role('button', name='Remove worker', exact=True).click()
    expect(page.get_by_role('region', name='Workspace action', exact=True)).to_contain_text('would run')
    log = page.get_by_role('region', name='Workspace action', exact=True).inner_text()
    assert "worker-rm" in log and "alpha" in log


def test_worker_create_validation(page_ctx):
    page = page_ctx("state_running.json")
    calls = []
    page.on('request', lambda request: calls.append(request) if request.url.endswith('/api/action') else None)
    open_workers(page, create=True)
    page.get_by_test_id("worker-name").fill("Bad Name!")
    page.get_by_test_id("worker-create").click()
    expect(page.get_by_role('dialog', name='New worker', exact=True).get_by_role('status')).to_contain_text('1–63 lowercase')
    expect(page.get_by_test_id('worker-name')).to_have_value('Bad Name!')
    assert calls == []


def test_secrets_section(page_ctx):
    page = page_ctx("state_running.json")
    open_settings(page, 'credentials')
    expect(panel(page, 'credentials')).to_contain_text('GITHUB_TOKEN')
    expect(panel(page, 'credentials').get_by_role('button', name='Save changes', exact=True)).to_have_count(0)


def test_firewall_banner_on(page_ctx):
    page = page_ctx("state_running.json")
    open_settings(page, 'network')
    banner = page.get_by_role('region', name='Effective network policy', exact=True).inner_text()
    assert "ON" in banner and "enforcing" in banner


def test_firewall_banner_off(page_ctx):
    page = page_ctx("state_fw_off.json")
    open_settings(page, 'network')
    assert "OFF" in page.get_by_role('region', name='Effective network policy', exact=True).inner_text()


def test_firewall_divergence_note(page_ctx):
    page = page_ctx("state_fw_diverged.json")
    open_settings(page, 'network')
    expect(page.get_by_role('region', name='Saved network policy', exact=True)).to_contain_text('Restricted')
    expect(page.get_by_role('region', name='Effective network policy', exact=True)).to_contain_text('OFF')
    expect(panel(page, 'network')).to_contain_text(re.compile('differ|not.*applied', re.I))
    expect(network_action(page)).to_be_enabled()


def test_firewall_toggle_off_requires_explicit_save_and_apply(page_ctx):
    page = page_ctx("state_running.json")   # currently ON
    open_settings(page, 'network')
    network_mode(page, True).click()
    save_button(page, 'network').click()
    expect(feedback(page, 'network')).to_contain_text('saved')
    idle(page)
    network_action(page, 'network-apply').click()
    network_confirmation(page).get_by_role('button', name='Apply saved policy', exact=True).click()
    expect(feedback(page, 'network')).to_contain_text('would run')
    expect(feedback(page, 'network')).not_to_contain_text('applying')
    assert "firewall" in feedback(page, 'network').inner_text()


def test_firewall_reapply_enabled_when_on(page_ctx):
    page = page_ctx("state_running.json")
    open_settings(page, 'network')
    expect(network_action(page, 'fw-reapply')).to_be_enabled()


def test_firewall_reapply_available_for_saved_unrestricted_policy(page_ctx):
    page = page_ctx("state_fw_off.json")
    open_settings(page, 'network')
    expect(network_action(page, 'fw-reapply')).to_be_enabled()


def test_egress_add_posts_domain(page_ctx):
    page = page_ctx("state_running.json")
    open_settings(page, 'network')
    field(page, '#egress-input').fill("api.example.com")
    add_entry(page, 'network')
    save_button(page, 'network').click()
    expect(feedback(page, 'network')).to_contain_text('saved')
    idle(page)
    network_action(page, 'network-apply').click()
    expect(feedback(page, 'network')).to_contain_text('would run')
    assert "would run" in feedback(page, 'network').inner_text()


def test_config_has_no_firewall_controls(page_ctx):
    page = page_ctx("state_running.json")
    open_settings(page, 'resources')
    expect(panel(page, 'resources').get_by_role('button', name='Apply saved network policy', exact=True)).to_have_count(0)
    cfg = panel(page, 'resources').inner_text()
    assert "permissive" not in cfg.lower()
    assert "egress" not in cfg.lower()


def test_reapply_reenables_buttons(page_ctx):
    page = page_ctx("state_running.json")
    open_settings(page, 'network')
    network_action(page, 'fw-reapply').click()
    expect(feedback(page, 'network')).to_contain_text('would run')
    idle(page)
    expect(network_action(page, 'fw-reapply')).to_be_enabled()
    open_settings(page, 'resources')
    field(page, '#cfg-cpus').fill('8')
    expect(save_button(page, 'resources')).to_be_enabled()
    expect(page.get_by_role('dialog', name='New worker', exact=True)).to_be_hidden()
    expect(page.get_by_role('button', name='New worker', exact=True)).to_be_hidden()
    open_workers(page, create=True)
    expect(page.get_by_test_id("worker-create")).to_be_enabled()


def test_saved_unknown_and_failed_repo_fixture_stays_inspectable(page_ctx):
    page = page_ctx('state_worker_offline.json')
    open_workers(page)
    empty = page.locator('[data-worker="empty"]')
    expect(empty).to_be_visible(timeout=1000)
    expect(empty).to_contain_text('Empty saved scope')
    expect(empty.get_by_role('button', name='Remove worker')).to_be_disabled()
    expect(empty.get_by_role('button', name='Copy recreate command')).to_have_count(0)
    legacy = page.locator('[data-worker="legacy"]')
    expect(legacy).to_contain_text('Unrecognized session')
    expect(legacy.get_by_role('button', name='Remove worker')).to_be_disabled()
    damaged = page.locator('[data-worker="damaged"]')
    damaged.locator('summary').click()
    expect(damaged.locator('.repos')).to_contain_text('Git inspection failed')
    expect(damaged.locator('.repos')).to_contain_text('Uncommitted unknown')
    expect(damaged.get_by_role('button', name='Remove worker')).to_be_disabled()


@pytest.mark.parametrize('caller', ['config', 'mode', 'add', 'remove'])
@pytest.mark.parametrize('stale', [False, True])
def test_all_config_callers_use_revisions_and_preserve_conflicts(page_ctx, caller, stale):
    page = page_ctx('state_running.json')
    section = 'resources' if caller == 'config' else 'network'
    # Edit a sibling through the same public navigation a user follows.
    if caller != 'config':
        open_settings(page, 'resources')
        field(page, '#cfg-cpus').fill('9')
    open_settings(page, section)
    idle(page)
    pause_polling(page)
    calls = []
    page.on('request', lambda request: calls.append((request.url.split('/api/')[-1], request.post_data_json))
            if request.method == 'POST' else None)
    saved = page.request.get(page.url.split('/?')[0] + '/api/state', headers={'X-Floatie-Token': page.url.split('?t=')[1].split('#')[0]}).json()
    path = page.config_path
    old = path.read_bytes()
    if caller == 'config':
        field(page, '#cfg-cpus').fill('8')
        expected = {'cpus': 8}
    elif caller == 'mode':
        network_mode(page, True).click()
        expected = {'permissive': True}
    elif caller == 'add':
        field(page, '#egress-input').fill('api.example.com')
        add_entry(page, 'network')
        expected = {'egress': [*saved['config']['egress'], 'api.example.com']}
    else:
        draft_list(page, 'egress').get_by_role('button', name=re.compile('^Remove ')).first.click()
        expected = {'egress': saved['config']['egress'][1:]}
    if stale:
        path.write_bytes(old + b'# external edit\n')
    save(page, section)
    log = feedback(page, section)
    expect(log).to_contain_text(re.compile('conflict' if stale else 'saved', re.I))
    saves = [body for endpoint, body in calls if endpoint == 'config']
    assert saves == [{'expectedVm': 'demo', 'expectedRevision': saved['configRevision'], 'changes': expected}]
    actions = [body for endpoint, body in calls if endpoint == 'action']
    assert actions == []
    if stale:
        assert path.read_bytes() == old + b'# external edit\n'
        if caller == 'add':
            # Added text belongs to the retained list; unfinished text has its
            # own separate acceptance cases and must never be auto-added.
            expect(draft_list(page, 'egress')).to_contain_text('api.example.com')
        if caller == 'mode':
            expect(network_mode(page, True)).to_have_attribute('aria-pressed', 'true')
        if caller == 'remove':
            expect(draft_list(page, 'egress').get_by_role('listitem')).to_have_count(0)
        refresh(page)
        if caller == 'remove':
            field(page, '#egress-input').fill('next.example.com')
            add_entry(page, 'network')
        save(page, section)
        expect(log).to_contain_text(re.compile('conflict|review.*baseline', re.I))
        # A now-known conflict is blocked locally; no newer revision may be
        # substituted into an automatic retry and no side effect may run.
        assert [body for endpoint, body in calls if endpoint == 'config'] == saves
        assert not [body for endpoint, body in calls if endpoint == 'action']
        assert path.read_bytes() == old + b'# external edit\n'
        open_settings(page, 'resources')
        expect(field(page, '#cfg-cpus')).to_have_value('8' if caller == 'config' else '9')
    else:
        config = u.parse_workspace_yml(path.read_text())
        assert all(config[key] == value for key, value in expected.items())
        import hashlib
        new_revision = hashlib.sha256(path.read_bytes()).hexdigest()
        if caller != 'config':
            network_action(page).click()
            if caller == 'mode':
                network_confirmation(page).get_by_role('button', name='Apply saved policy', exact=True).click()
            expect(log).to_contain_text('would run')
            idle(page)
            actions = [body for endpoint, body in calls if endpoint == 'action']
            assert len(actions) == 1 and actions[0]['expectedRevision'] == new_revision
            open_settings(page, 'resources')
            expect(field(page, '#cfg-cpus')).to_have_value('9')
        else:
            # A second successful save uses the revision returned by the first.
            field(page, '#cfg-cpus').fill('12')
            save(page, 'resources')
            assert [body for endpoint, body in calls if endpoint == 'config'][-1] == {
                'expectedVm': 'demo', 'expectedRevision': new_revision, 'changes': {'cpus': 12}}


def test_config_validation_keeps_input_focus_and_baseline(page_ctx):
    page = page_ctx('state_running.json')
    open_settings(page, 'resources')
    idle(page)
    cpus = field(page, '#cfg-cpus')
    cpus.fill('4garbage')
    save_button(page, 'resources').click()
    expect(feedback(page, 'resources')).to_contain_text(re.compile('cpus', re.I))
    idle(page)
    expect(cpus).to_have_value('4garbage')
    cpus.focus()
    poll(page)
    expect(cpus).to_be_focused()
    expect(cpus).to_have_value('4garbage')
    open_settings(page, 'network')
    open_settings(page, 'resources')
    expect(cpus).to_have_value('4garbage')
    assert u.parse_workspace_yml(page.config_path.read_text())['cpus'] == 4


@pytest.mark.parametrize('first', ['remove', 'mode'])
@pytest.mark.parametrize('conflict', [False, True], ids=['recover', 'conflict-again'])
def test_failed_network_edit_survives_other_caller(agent_page, tmp_path, monkeypatch, first, conflict):
    page = agent_page
    page.clock.install()
    path = tmp_path / 'workspace.yml'
    _, revision, _ = u.read_config(str(tmp_path))
    u.save_config(str(tmp_path), revision, {'egress': ['first.example', 'second.example']})
    original = path.read_bytes()
    import hashlib
    revision = hashlib.sha256(original).hexdigest()
    page.reload()
    open_settings(page, 'resources')
    idle(page)
    field(page, '#cfg-cpus').fill('9')
    open_settings(page, 'network')
    idle(page)
    pause_polling(page)
    calls = []
    page.on('request', lambda request: calls.append((request.url.split('/api/')[-1], request.post_data_json))
            if request.method == 'POST' else None)
    replace = u.os.replace
    failures = []
    def fail_once(source, destination):
        if Path(destination) == path and not failures:
            failures.append(True)
            raise OSError('synthetic atomic replace failure')
        return replace(source, destination)
    monkeypatch.setattr(u.os, 'replace', fail_once)
    def remove():
        page.get_by_role('button', name='Remove first.example', exact=True).click()
    def mode():
        network_mode(page, True).click()
    (remove if first == 'remove' else mode)()
    save_button(page, 'network').click()
    expect(feedback(page, 'network')).to_contain_text('Unable to read or atomically save')
    idle(page)
    assert failures == [True] and path.read_bytes() == original
    assert not [body for endpoint, body in calls if endpoint == 'action']
    first_changes = {'egress': ['second.example']} if first == 'remove' else {'permissive': True}
    assert [body for endpoint, body in calls if endpoint == 'config'] == [
        {'expectedVm': 'demo', 'expectedRevision': revision, 'changes': first_changes}]
    if first == 'remove':
        expect(page.get_by_role('button', name='Remove first.example', exact=True)).to_have_count(0)
        # This text has not been added to the list; the mode save must retain it.
        field(page, '#egress-input').fill('not-submitted.example')
    else:
        expect(network_mode(page, True)).to_have_attribute('aria-pressed', 'true')
    if conflict:
        path.write_bytes(original + b'# concurrent external edit\n')
    (mode if first == 'remove' else remove)()
    if first == 'remove':
        save_button(page, 'network').click()
        expect(feedback(page, 'network')).to_contain_text(re.compile('add.*clear', re.I))
        expect(field(page, '#egress-input')).to_have_value('not-submitted.example')
        assert len([body for endpoint, body in calls if endpoint == 'config']) == 1
        # Pending text must be explicitly cleared or added before Save.
        field(page, '#egress-input').fill('')
    save_button(page, 'network').click()
    expect(feedback(page, 'network')).to_contain_text('conflict' if conflict else 'saved')
    idle(page)
    pending = {'egress': ['second.example'], 'permissive': True}
    assert [body for endpoint, body in calls if endpoint == 'config'][-1] == {
        'expectedVm': 'demo', 'expectedRevision': revision, 'changes': pending}
    expect(page.get_by_role('button', name='Remove first.example', exact=True)).to_have_count(0)
    expect(page.get_by_role('button', name='Remove second.example', exact=True)).to_have_count(1)
    if first == 'remove':
        expect(field(page, '#egress-input')).to_have_value('')
    actions = [body for endpoint, body in calls if endpoint == 'action']
    if conflict:
        assert not actions
        assert path.read_bytes() == original + b'# concurrent external edit\n'
        expect(network_mode(page, True)).to_have_attribute('aria-pressed', 'true')
        # Another caller cannot silently retry with a newer baseline.
        before_retry = [body for endpoint, body in calls if endpoint == 'config']
        network_mode(page, True).click()
        save_button(page, 'network').click()
        expect(feedback(page, 'network')).to_contain_text('conflict')
        idle(page)
        assert [body for endpoint, body in calls if endpoint == 'config'] == before_retry
        assert not [body for endpoint, body in calls if endpoint == 'action']
    else:
        config, saved_revision, _ = u.read_config(str(tmp_path))
        assert config['egress'] == ['second.example'] and config['permissive'] is True
        assert config['cpus'] == 4  # Unrelated Config draft remains unsaved.
        assert actions == []
        expect(feedback(page, 'network')).to_contain_text(re.compile('saved', re.I))
    open_settings(page, 'resources')
    expect(field(page, '#cfg-cpus')).to_have_value('9')


def test_network_save_and_apply_are_separate_and_use_saved_policy(page_ctx):
    page = page_ctx('state_running.json')
    open_settings(page, 'network')
    idle(page)
    calls = []
    page.on('request', lambda r: calls.append((r.url.split('/api/')[-1], r.post_data_json)) if r.method == 'POST' else None)
    network_mode(page, True).click()
    save_button(page, 'network').click()
    expect(feedback(page, 'network')).to_contain_text('saved')
    idle(page)
    assert not [b for e,b in calls if e == 'action']
    revision = [b for e,b in calls if e == 'config'][0]['expectedRevision']
    saved_revision = u.read_config(str(page.config_path.parent))[1]
    assert saved_revision != revision
    # A restrictive draft must not hide the confirmation for SAVED unrestricted policy.
    network_mode(page, False).click()
    network_action(page, 'network-apply').click()
    confirmation = network_confirmation(page)
    expect(confirmation).to_be_visible()
    expect(confirmation).to_contain_text(re.compile('saved', re.I))
    confirmation.get_by_role('button', name='Cancel', exact=True).click()
    assert not [b for e,b in calls if e == 'action']
    network_action(page, 'network-apply').click()
    confirmation.get_by_role('button', name='Apply saved policy', exact=True).click()
    expect(feedback(page, 'network')).to_contain_text('would run')
    actions = [b for e,b in calls if e == 'action']
    assert len(actions) == 1 and actions[0]['expectedRevision'] == saved_revision
    assert actions[0]['expectedVm'] == 'demo'
    expect(network_mode(page, False)).to_have_attribute('aria-pressed', 'true')


def test_worker_creation_freezes_displayed_scope(page_ctx):
    page = page_ctx('state_running.json')
    open_workers(page, create=True)
    idle(page)
    calls = []
    page.on('request', lambda r: calls.append(r.post_data_json) if r.url.endswith('/api/action') else None)
    page.get_by_test_id('worker-name').fill('new-worker')
    page.get_by_test_id('worker-create').click()
    expect(page.get_by_role('region', name='Workspace action', exact=True)).to_contain_text('Worker terminal created')
    assert calls[0]['params']['repos'] == calls[0]['params']['availableRepos']
    assert calls[0]['params']['repos'] and calls[0]['expectedVm'] == 'demo'


def test_identity_change_disables_mutations_but_keeps_original_target_visible(page_ctx):
    page = page_ctx('state_running.json')
    open_settings(page, 'resources')
    idle(page)
    field(page, '#cfg-cpus').fill('9')
    page.config_path.write_text('name: replacement\ncpus: 12\n')
    refresh(page)
    idle(page)
    expect(page.get_by_test_id('ws-name')).to_have_text('demo')
    expect(save_button(page, 'resources')).to_be_disabled()
    expect(field(page, '#cfg-cpus')).to_have_value('9')
    open_workers(page)
    expect(page.get_by_role('button', name='New worker', exact=True)).to_be_disabled()
    expect(page.get_by_role('button', name='Start orchestrator', exact=True, include_hidden=True)).to_be_disabled()
    expect(page.locator('[data-worker="alpha"]')).to_be_visible()
    open_settings(page, 'network')
    expect(network_action(page, 'network-apply')).to_be_disabled()
    expect(save_button(page, 'network')).to_be_disabled()


def test_unknown_action_outcome_is_visible_and_never_retried(page_ctx):
    page = page_ctx('state_running.json')
    open_settings(page, 'network')
    calls = []
    def timeout(route):
        calls.append(route.request.post_data_json)
        route.fulfill(json={'ok': False, 'outcome': 'unknown', 'exitCode': None,
                            'output': 'phase 1 done. Timed out. Outcome unknown.'})
    page.route('**/api/action', timeout)
    network_action(page, 'network-apply').click()
    expect(feedback(page, 'network')).to_contain_text('Outcome unknown')
    idle(page)
    refresh(page)
    idle(page)
    assert len(calls) == 1


def test_lightweight_discovery_exposes_identity_mismatch_on_workers(page_ctx):
    page = page_ctx('state_running.json')
    expect(page.get_by_role('region', name='Workspace identity', exact=True)).to_be_hidden(timeout=800)
    open_workers(page)
    idle(page)
    page.config_path.write_text('name: replacement\n')
    with scheduled_observation(page, source='agents'):
        pass
    expect(page.get_by_role('region', name='Workspace identity', exact=True)).to_be_visible(timeout=800)
    page.get_by_role('button', name='Workspace identity output', exact=True).click()
    expect(page.get_by_role('region', name='Workspace identity', exact=True)).to_contain_text('original VM')
    expect(page.get_by_role('button', name='New worker', exact=True)).to_be_disabled()
    expect(page.locator('[data-worker="alpha"]')).to_be_visible()


@pytest.mark.parametrize('button', ['network-apply', 'fw-reapply'])
def test_saved_domains_remain_reviewable_after_external_edit_with_dirty_draft(page_ctx, button):
    page = page_ctx('state_running.json')
    workspace = str(page.config_path.parent)
    _, revision, _ = u.read_config(workspace)
    u.save_config(workspace, revision, {'egress': ['originally-reviewed.example', 'still-original.example']})
    page.reload()
    open_settings(page, 'network')
    idle(page)
    field(page, '#egress-input').fill('unsaved-draft.example')
    add_entry(page, 'network')
    network_mode(page, True).click()
    _, revision, _ = u.read_config(workspace)
    _, saved_revision = u.save_config(workspace, revision, {
        'egress': ['externally-saved.example', 'second-saved.example'], 'permissive': False})
    refresh(page)
    idle(page)
    saved = page.get_by_role('region', name='Saved network policy', exact=True)
    expect(saved).to_contain_text('Restricted')
    expect(saved).to_contain_text('externally-saved.example', timeout=800)
    expect(saved).to_contain_text('second-saved.example')
    expect(saved).not_to_contain_text('originally-reviewed.example')
    for domain in ['originally-reviewed.example', 'still-original.example', 'unsaved-draft.example']:
        expect(draft_list(page, 'egress')).to_contain_text(domain)
    expect(network_mode(page, True)).to_have_attribute('aria-pressed', 'true')
    calls = []
    page.on('request', lambda r: calls.append(r.post_data_json) if r.url.endswith('/api/action') else None)
    network_action(page, button).click()
    expect(feedback(page, 'network')).to_contain_text('externally-saved.example')
    expect(feedback(page, 'network')).to_contain_text('second-saved.example')
    expect(feedback(page, 'network')).not_to_contain_text('originally-reviewed.example')
    idle(page)
    assert len(calls) == 1 and calls[0]['expectedRevision'] == saved_revision
    expect(draft_list(page, 'egress')).to_contain_text('unsaved-draft.example')
    expect(network_mode(page, True)).to_have_attribute('aria-pressed', 'true')


def test_root_launch_real_timeout_keeps_unknown_error_and_partial_output(page_ctx, monkeypatch):
    page = page_ctx('state_running.json')
    open_workers(page)
    idle(page)
    attempts, requests = [], []
    def timeout(argv, **kwargs):
        attempts.append(argv)
        assert argv[:3] == ['limactl', 'shell', 'demo']
        assert argv[-3:] == ['start', 'main', 'codex']
        raise subprocess.TimeoutExpired(argv, 40, output=b'launch phase finished\n',
                                        stderr=b'waiting for terminal acknowledgement\n')
    def launch(route):
        payload = route.request.post_data_json
        if payload['action'] != 'start':
            route.continue_()
            return
        requests.append(payload)
        args, message = u.agent_arguments(payload)
        with monkeypatch.context() as context:
            context.setattr(u.subprocess, 'run', timeout)
            result = u.run_agent('demo', args, message)
        assert result['outcome'] == 'unknown' and 'timed out' in result['error']
        route.fulfill(json=result)
    page.route('**/api/agents', launch)
    page.get_by_role('button', name='Start orchestrator', exact=True).click()
    log = page.get_by_role('region', name='Workspace action', exact=True)
    expect(log).to_contain_text('Outcome unknown', timeout=800)
    log.get_by_role('button', name='Workspace action output', exact=True).click()
    expect(log).to_contain_text('timed out')
    expect(log).to_contain_text('launch phase finished')
    expect(log).to_contain_text('waiting for terminal acknowledgement')
    idle(page)
    refresh(page)
    idle(page)
    with scheduled_observation(page, source='agents'):
        pass  # One ordinary discovery cycle must not retry the launch.
    assert len(requests) == len(attempts) == 1


@pytest.mark.parametrize('result,expected', [
    ({'ok': True, 'outcome': 'completed', 'output': 'ordinary success'}, ['ordinary success']),
    ({'ok': False, 'outcome': 'failed', 'output': 'phase output', 'error': 'ordinary failure'},
     ['phase output', 'ordinary failure']),
])
def test_mutation_keeps_ordinary_success_and_failure_feedback(page_ctx, result, expected):
    page = page_ctx('state_running.json')
    open_settings(page, 'network')
    page.route('**/api/action', lambda route: route.fulfill(json=result))
    network_action(page, 'network-apply').click()
    for text in expected:
        expect(feedback(page, 'network')).to_contain_text(text, timeout=800)
    expect(feedback(page, 'network')).not_to_contain_text('Outcome unknown')
