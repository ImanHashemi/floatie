import pytest
import ui_server as u
from functools import partial
command = partial(u.action_command, vm_name='demo', policy={'permissive': False, 'egress': []})


def test_known_actions_map_to_recipes():
    cmd = command("/repo", "/ws", "firewall-off", {})
    assert cmd[-1:] == ["firewall"]
    cmd = command("/repo", "/ws", "start", {})
    assert cmd[-1] == "start"


def test_worker_create_validates_name():
    with pytest.raises(ValueError):
        command("/repo", "/ws", "worker-create", {"name": "Bad Name!"})
    cmd = command("/repo", "/ws", "worker-create", {"name": "alpha", "repos": ["api"]})
    assert cmd[-3:] == ["_worker-create", "alpha", "api"]
    with pytest.raises(ValueError):
        command("/repo", "/ws", "worker-create", {"name": "alpha"})


def test_worker_create_rejects_reserved_names():
    for n in ("workspace", "main"):
        with pytest.raises(ValueError):
            command("/r", "/w", "worker-create", {"name": n})


def test_worker_rm_validates_name():
    with pytest.raises(ValueError):
        command("/repo", "/ws", "worker-rm", {"name": "../evil"})
    assert command("/repo", "/ws", "worker-rm", {"name": "beta"})[-2:] == ["worker-rm", "beta"]


def test_remaining_actions_map():
    assert command("/r", "/w", "stop", {})[-1] == "stop"
    assert command("/r", "/w", "reload", {})[-1] == "reload"
    assert command("/r", "/w", "firewall-on", {})[-1] == "firewall"


def test_unknown_action_rejected():
    with pytest.raises(ValueError):
        command("/repo", "/ws", "destroy", {})


def test_run_action_fixture_mode_echoes(monkeypatch):
    monkeypatch.setenv("FLOATIE_UI_FIXTURES", "/some/fixture.json")
    res = u.run_action("/repo", "/ws", "start", {}, vm_name="demo", policy={"permissive": False, "egress": []})
    assert res["ok"] is True
    assert res["fixture"] is True
    assert res["exitCode"] == 0
    assert "start" in res["output"]


def test_timeout_retains_phase_output_and_unknown_outcome(monkeypatch):
    import subprocess
    monkeypatch.delenv('FLOATIE_UI_FIXTURES', raising=False)
    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], 1, output=b'terminal stopped\n', stderr=b'worktree phase pending')
    monkeypatch.setattr(u.subprocess, 'run', timeout)
    result = u.run_action('/repo', '/ws', 'worker-rm', {'name': 'safe'}, vm_name='demo')
    assert not result['ok'] and result['outcome'] == 'unknown'
    assert 'terminal stopped' in result['output']
    assert 'worktree phase pending' in result['output']
