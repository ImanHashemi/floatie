"""Execute UI copy with a literal shell stub; real attach recipe with fake VM boundary."""
import subprocess
import pytest
from playwright.sync_api import expect
from test_agent_browser import agent_page
from test_agent_ux import workers, managed, card, open_workers
from test_agent_recipes import recipe


@pytest.mark.parametrize('name,live,repos,args', [
    ('main', True, [], ['attach']),
    ('review', True, ['api'], ['attach', 'review']),
    ('review', False, ['api'], ['worker', 'review', 'api']),
    ('review', False, ['api', 'web'], ['worker', 'review']),
])
def test_copy_executes_only_literal_workspace_and_saved_scope(workers, tmp_path, name, live, repos, args):
    page, data = workers
    directory = tmp_path / "team's workspace $HOME $(touch injected) `touch injected` ; &"
    directory.mkdir()
    data['state']['workspace']['dir'] = str(directory)
    if name != 'main':
        data['state']['environments'].append(managed(name, live, [dict(repo=r) for r in repos]))
    open_workers(page)
    row = card(page, name)
    page.evaluate("Object.defineProperty(navigator,'clipboard',{configurable:true,value:{writeText:async t=>{window.copied=t}}})")
    row.locator('.copy').click()
    expect(row.locator('.copy-status')).to_contain_text('Copied')
    command = page.evaluate('window.copied')
    result = subprocess.run(['/bin/sh', '-c', 'floatie() { printf "%s\\0" "$PWD" "$@"; };\n' + command], cwd=tmp_path, capture_output=True, check=True)
    assert result.stdout.decode().split('\0') == [str(directory), *args, '']
    assert result.stderr == b''
    assert not list(tmp_path.rglob('injected'))
    row.locator('summary').click()
    expect(row.locator('.command-note')).to_contain_text('configuration at execution')


def test_missing_review_does_not_attach_review_other(recipe):
    result, calls = recipe('attach', 'review', env={'TEST_LIVE_SESSIONS': '["review-other"]'})
    assert result.returncode != 0
    assert calls[0]['argv'] == ['list', 'example-vm', '--format', '{{.Status}}']
    assert [c['argv'][3:] for c in calls[1:]] == [['tmux', 'has-session', '-t', '=review']]
    assert 'no tmux session' in result.stderr
