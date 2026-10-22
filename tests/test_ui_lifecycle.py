"""Detached host lifecycle, isolated state and fixture-owned children only.

No VM calls and no port-to-PID lookups. Even failed assertions clean up only
Popen handles created by this fixture; registration data never drives cleanup.
"""
import importlib
import importlib.util
import json
import os
import shutil
import socket
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from python39_runtime import compatibility_interpreter

REPO = Path(__file__).resolve().parents[1]
FLOATIE = REPO / "bin" / "floatie"


def _free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _listening(port):
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.2):
            return True
    except OSError:
        return False


def _floatie(ws, *args):
    return subprocess.run([str(FLOATIE), *args], cwd=ws, capture_output=True,
                          text=True, timeout=15)


@pytest.fixture
def lifecycle(tmp_path, monkeypatch):
    # Replace all global state before importing or invoking lifecycle code.
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("FLOATIE_UI_FIXTURES", str(REPO / "tests/fixtures/state_running.json"))
    assert importlib.util.find_spec("ui_process") is not None, "verified UI process lifecycle is missing"
    module = importlib.import_module("ui_process")
    ws = tmp_path / "workspace with spaces"
    ws.mkdir()
    (ws / "workspace.yml").write_text("name: floatie-uitest\n")
    children = []
    popen = subprocess.Popen

    def owned_popen(*args, **kwargs):
        child = popen(*args, **kwargs)
        children.append(child)
        return child

    monkeypatch.setattr(module.subprocess, "Popen", owned_popen)
    try:
        yield module, ws, tmp_path / "state/floatie", children
    finally:
        for child in reversed(children):
            if child.poll() is None:
                child.terminate()
                try:
                    child.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait(timeout=3)


def test_ui_starts_detached_then_stops(lifecycle):
    u, ws, state, children = lifecycle
    port = _free_port()
    before = time.monotonic()
    record = u.start_server(REPO, ws, "floatie-uitest", port)
    assert time.monotonic() - before < 10
    assert record["pid"] in [p.pid for p in children]
    assert u.matches_process(record)
    assert _listening(port)
    assert u.read_registration(state, ws) == record
    assert Path(record["log_path"]).stat().st_mode & 0o777 == 0o600
    assert u.registration_path(state, ws).stat().st_mode & 0o777 == 0o600
    assert state.stat().st_mode & 0o777 == 0o700

    # Real public recipe reuses the fixture-owned server and prints its token URL.
    result = _floatie(ws, "ui", str(port))
    assert result.returncode == 0, result.stderr
    assert "already running" in result.stdout and "/?t=" in result.stdout
    assert u.read_registration(state, ws) == record
    result = _floatie(ws, "ui-stop")
    assert result.returncode == 0, result.stderr
    assert not u.registration_path(state, ws).exists()
    assert not _listening(port)
    assert _floatie(ws, "ui-stop").returncode == 0
    assert u.main(["log-path", "--workspace", str(ws), "--vm-name", "floatie-uitest"]) == 0


def test_ui_reports_port_conflict_without_touching_holder(lifecycle):
    u, ws, state, children = lifecycle
    # Socket is held by this fixture's child, unrelated to UI registrations.
    port = _free_port()
    holder = subprocess.Popen([sys.executable, "-u", "-c",
        "import socket,time; s=socket.socket(); s.bind(('127.0.0.1',int(__import__('sys').argv[1]))); "
        "s.listen(); print('ready',flush=True); time.sleep(30)", str(port)], stdout=subprocess.PIPE)
    assert holder.stdout.readline() == b"ready\n"
    try:
        result = _floatie(ws, "ui", str(port))
        assert result.returncode != 0
        assert "no live UI is recorded" in result.stderr and str(port) in result.stderr
        assert "Traceback" not in result.stderr
        assert holder.poll() is None and _listening(port)
        assert not u.registration_path(state, ws).exists()
    finally:
        holder.stdout.close()


def test_ui_surfaces_startup_failure_output(lifecycle):
    u, ws, state, _ = lifecycle
    with pytest.raises(RuntimeError, match="failed to start") as error:
        u.start_server(REPO, ws, "floatie-uitest", 999999)
    assert "Traceback" in str(error.value)
    assert not u.registration_path(state, ws).exists()


def test_same_vm_in_two_directories_is_independent(lifecycle):
    u, ws, state, _ = lifecycle
    other = ws.parent / "another workspace"
    other.mkdir()
    (other / "workspace.yml").write_text("name: floatie-uitest\n")
    first = u.start_server(REPO, ws, "floatie-uitest", _free_port())
    second = u.start_server(REPO, other, "floatie-uitest", _free_port())
    assert first["pid"] != second["pid"]
    assert first["instance_id"] != second["instance_id"]
    assert u.registration_path(state, ws) != u.registration_path(state, other)
    assert _floatie(ws, "ui-stop").returncode == 0
    assert u.matches_process(second)
    assert u.read_registration(state, other) == second
    assert _floatie(other, "ui-stop").returncode == 0


def test_interpreter_and_server_paths_with_spaces(lifecycle, monkeypatch):
    u, ws, state, _ = lifecycle
    repo = ws.parent / "server with spaces"
    repo.mkdir()
    for filename in ("ui_server.py", "ui_assets.py", "worker_inventory.py"):
        shutil.copy(REPO / filename, repo / filename)
    shutil.copytree(REPO / "ui/dist", repo / "ui/dist")
    python = ws.parent / "python with spaces"
    python.symlink_to(sys.executable)
    monkeypatch.setattr(u.sys, "executable", str(python))
    record = u.start_server(repo, ws, "floatie-uitest", _free_port())
    assert record["interpreter"] == str(python)
    assert record["server"] == str(repo / "ui_server.py")
    assert record["workspace"] == str(ws.resolve())
    assert u.matches_process(record)
    assert u.stop_registered(record)


def test_concurrent_starts_serialize_and_reuse_one_child(lifecycle):
    u, ws, state, children = lifecycle
    port = _free_port()
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(u.start_server, REPO, ws, "floatie-uitest", port) for _ in range(2)]
        first, second = [f.result() for f in futures]
    assert first == second
    assert len([p for p in children if "--instance-id" in p.args]) == 1
    assert u.read_registration(state, ws) == first


def test_identity_failure_never_registers_or_signals_unverified_child(lifecycle, monkeypatch):
    u, ws, state, children = lifecycle
    monkeypatch.setattr(u, "process_identity", lambda pid: None)
    monkeypatch.setattr(u, "START_TIMEOUT", 0.2)
    with pytest.raises(RuntimeError, match="verif"):
        u.start_server(REPO, ws, "floatie-uitest", _free_port())
    assert not u.registration_path(state, ws).exists()
    # Fixture cleanup owns the child even though lifecycle cannot identify it.
    assert any(p.poll() is None for p in children)


def test_foreground_recipe_remains_direct_and_workspace_bound(lifecycle):
    u, ws, _, _ = lifecycle
    port = _free_port()
    with (ws / "foreground.log").open("w+") as log:
        rendered = _floatie(ws, "--dry-run", "ui-fg", str(port))
        assert rendered.returncode == 0, rendered.stderr
        # Execute the rendered recipe directly so exec gives us the server handle.
        child = subprocess.Popen(["bash", "-c", rendered.stderr, "ui-fg", str(port)], cwd=ws, stdout=log, stderr=log)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not _listening(port):
            assert child.poll() is None
            time.sleep(0.05)
        assert _listening(port)
        log.seek(0)
        assert "/?t=" in log.read()
        child.terminate()
        child.wait(timeout=3)


def test_bound_url_alone_does_not_prove_startup(lifecycle, monkeypatch):
    u, ws, state, children = lifecycle
    repo = ws.parent / "not serving"
    repo.mkdir()
    (repo / "ui_server.py").write_text(
        "import sys,time\nport=sys.argv[sys.argv.index('--port')+1]\n"
        "print('http://127.0.0.1:'+port+'/?t=synthetic-token', flush=True)\n"
        "time.sleep(30)\n")
    monkeypatch.setattr(u, "START_TIMEOUT", 0.3)
    with pytest.raises(RuntimeError, match="failed to start"):
        u.start_server(repo, ws, "floatie-uitest", _free_port())
    assert not u.registration_path(state, ws).exists()
    assert all(p.poll() is not None for p in children)


def test_start_adapter_reports_real_server_and_log_tail(lifecycle, capsys):
    u, ws, state, children = lifecycle
    port = _free_port()
    assert u.main(["start", "--repo-dir", str(REPO), "--workspace", str(ws),
                   "--vm-name", "floatie-uitest", "--port", str(port)]) == 0
    assert "UI started" in capsys.readouterr().out
    record = u.read_registration(state, ws)
    assert u.matches_process(record)
    rendered = _floatie(ws, "--dry-run", "ui-logs")
    assert rendered.returncode == 0, rendered.stderr
    # Run the rendered exec recipe to retain the tail's own Popen handle.
    tail = subprocess.Popen(["bash", "-c", rendered.stderr], cwd=ws, stdout=subprocess.PIPE)
    try:
        assert b"/?t=" in tail.stdout.readline()
        tail.terminate()
        tail.wait(timeout=3)
        assert u.matches_process(record), "ending log tail must not stop UI"
    finally:
        tail.stdout.close()


def test_python39_host_launcher_can_start_and_stop():
    expected = (REPO / "ui/.python39-version").read_text().strip()
    executable = compatibility_interpreter(os.environ.get("FLOATIE_TEST_PYTHON39"), expected)
    result = subprocess.run(
        [executable, "-I", str(REPO / "tests/python39_runtime.py"), str(REPO)],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    receipt = json.loads(result.stdout)
    assert receipt["python"] == expected
    assert not receipt["node_on_path"] and not receipt["npm_on_path"]
    assert all(receipt[key] for key in ("owned_start", "ownership_verified", "entry_served", "owned_stop", "clean_log"))
    assert receipt["assets"]


def test_python39_probe_deadline_unwinds_owned_cleanup():
    """A stalled read must cancel inside the helper, before the outer watchdog.

    The child runs real Python 3.9 signals but owns only a fake process handle;
    a regression cannot leave a real detached server behind.
    """
    expected = (REPO / "ui/.python39-version").read_text().strip()
    executable = compatibility_interpreter(os.environ.get("FLOATIE_TEST_PYTHON39"), expected)
    source = r'''
import json, pathlib, sys, time, types
repo = pathlib.Path(sys.argv[1])
sys.path.insert(0, str(repo / 'tests'))
import python39_runtime as runtime

class OwnedChild:
    pid = 4242
    terminated = False
    waited = False
    def poll(self): return 0 if self.terminated else None
    def terminate(self): self.terminated = True
    def wait(self, timeout): self.waited = True; return 0

child = OwnedChild()
process = types.SimpleNamespace(subprocess=types.SimpleNamespace(Popen=lambda: child))
def start(*args):
    process.subprocess.Popen()
    return {'pid': child.pid, 'interpreter': sys.executable}
process.start_server = start
process.matches_process = lambda record: True
sys.modules['ui_process'] = process
runtime.get = lambda *args: time.sleep(5)
try:
    runtime.probe(repo, timeout=0.1)
except TimeoutError:
    assert child.terminated and child.waited
    print(json.dumps({'deadline': True, 'owned_cleanup': True}))
else:
    raise AssertionError('The deadline did not cancel the stalled read')
'''
    result = subprocess.run([executable, "-I", "-c", source, str(REPO)],
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"deadline": True, "owned_cleanup": True}


def test_python39_missing_configuration_is_an_error():
    with pytest.raises(RuntimeError, match="Set FLOATIE_TEST_PYTHON39"):
        compatibility_interpreter(None, "3.9.23")


def test_python39_nonexecutable_configuration_is_an_error(tmp_path):
    executable = tmp_path / "not executable"
    executable.write_text("not an interpreter\n")
    executable.chmod(0o600)
    with pytest.raises(RuntimeError, match="missing or not executable"):
        compatibility_interpreter(str(executable), "3.9.23")


def test_python39_relative_configuration_checks_the_requested_executable(tmp_path, monkeypatch):
    executable = tmp_path / "compatibility-python"
    executable.write_text("#!/bin/sh\nprintf '%s\\n' '[3, 9, 23]'\n")
    executable.chmod(0o700)
    monkeypatch.chdir(tmp_path)
    assert compatibility_interpreter(executable.name, "3.9.23") == str(executable)


@pytest.mark.parametrize("version", [[3, 10, 0], [3, 9, 0]])
def test_python39_wrong_version_is_an_error(tmp_path, version):
    executable = tmp_path / "wrong-version-python"
    executable.write_text("#!/bin/sh\nprintf '%s\\n' '" + json.dumps(version) + "'\n")
    executable.chmod(0o700)
    with pytest.raises(RuntimeError, match="must be Python 3.9.23"):
        compatibility_interpreter(str(executable), "3.9.23")


def test_nonce_generation_cannot_be_parsed_as_an_option(lifecycle, monkeypatch):
    u, ws, _, _ = lifecycle
    # URL-safe randomness may begin '-', which argparse treats as another option.
    monkeypatch.setattr(u.secrets, "token_urlsafe", lambda size: "-synthetic-nonce")
    record = u.start_server(REPO, ws, "floatie-uitest", _free_port())
    assert u.matches_process(record)
    assert u.stop_registered(record)


@pytest.mark.parametrize("recipe", ["ui", "ui-fg"])
def test_recipe_port_is_literal(lifecycle, recipe):
    _, ws, _, _ = lifecycle
    result = _floatie(ws, recipe, "$(touch task4-injected)invalid")
    assert result.returncode != 0
    assert not (ws / "task4-injected").exists()
