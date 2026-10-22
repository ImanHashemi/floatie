"""Identity regressions use synthetic OS observations; no signal reaches the OS."""
import importlib
import importlib.util
import json
import signal
import subprocess
from pathlib import Path

import pytest


@pytest.fixture
def u():
    assert importlib.util.find_spec("ui_process") is not None, "verified UI process lifecycle is missing"
    return importlib.import_module("ui_process")


@pytest.fixture
def record(tmp_path):
    ws = tmp_path / "workspace with spaces"
    ws.mkdir()
    return dict(version=1, workspace=str(ws.resolve()), interpreter="/opt/python bin/python3",
                server="/opt/server dir/ui_server.py", executable="/opt/python bin/python3", pid=12345, start_id="boot:43210",
                instance_id="nonce-unique-for-launch", log_path=str(tmp_path / "ui.log"), port=8787)


def observation(record):
    return dict(pid=record["pid"], executable=record["executable"], start_id=record["start_id"],
                command=[record["interpreter"], record["server"], "--port", "8787", "--workspace",
                         record["workspace"], "--instance-id", record["instance_id"]])


@pytest.mark.parametrize("damage", ["pid", "start_id", "executable", "command", "nonce", "workspace", "server", "missing"])
def test_reused_or_altered_process_is_not_signaled(u, record, monkeypatch, damage):
    observed = observation(record)
    if damage == "missing":
        observed = None
    elif damage in ("pid", "start_id", "executable"):
        observed[damage] = "different"
    elif damage == "command":
        observed["command"].append("--other")
    else:
        observed["command"][{"nonce": 7, "workspace": 5, "server": 1}[damage]] = "changed"
    signals = []
    monkeypatch.setattr(u, "process_identity", lambda pid: observed)
    monkeypatch.setattr(u.os, "kill", lambda *args: signals.append(args))
    assert not u.matches_process(record)
    assert not u.stop_registered(record)
    assert signals == []


@pytest.mark.parametrize("legacy", [12345, "12345", {"pid": 12345}, {}, None])
def test_legacy_or_incomplete_record_never_signals(u, legacy, monkeypatch):
    signals = []
    monkeypatch.setattr(u.os, "kill", lambda *args: signals.append(args))
    monkeypatch.setattr(u, "process_identity", lambda pid: pytest.fail("incomplete record was adopted"))
    assert not u.matches_process(legacy)
    assert not u.stop_registered(legacy)
    assert signals == []


def test_identity_is_rechecked_immediately_before_term(u, record, monkeypatch):
    original = observation(record)
    reused = {**original, "start_id": "new-start"}
    observations = iter([original, reused])
    signals = []
    monkeypatch.setattr(u, "process_identity", lambda pid: next(observations, reused))
    monkeypatch.setattr(u.os, "kill", lambda *args: signals.append(args))
    assert not u.stop_registered(record)
    assert signals == []


def test_identity_is_rechecked_before_kill(u, record, monkeypatch):
    original = observation(record)
    reused = {**original, "start_id": "new-start"}
    signals = []
    monkeypatch.setattr(u, "STOP_TIMEOUT", 0)
    monkeypatch.setattr(u, "_has_exited", lambda pid: False)
    observations = iter([original, original, original, reused])
    monkeypatch.setattr(u, "process_identity", lambda pid: next(observations, reused))
    monkeypatch.setattr(u.os, "kill", lambda *args: signals.append(args))
    assert not u.stop_registered(record)
    assert signals == [(12345, signal.SIGTERM)]


def test_verified_stubborn_instance_gets_term_then_kill(u, record, monkeypatch):
    signals = []
    monkeypatch.setattr(u, "STOP_TIMEOUT", 0)
    monkeypatch.setattr(u, "process_identity", lambda pid: observation(record) if len(signals) < 2 else None)
    monkeypatch.setattr(u, "_has_exited", lambda pid: len(signals) == 2)
    monkeypatch.setattr(u.os, "kill", lambda *args: signals.append(args))
    assert u.stop_registered(record)
    assert signals == [(12345, signal.SIGTERM), (12345, signal.SIGKILL)]


@pytest.mark.parametrize("confirmed_exit,identity_returns", [(True, False), (False, False), (False, True)])
def test_identity_loss_during_shutdown_waits_without_more_signals(
        u, record, monkeypatch, confirmed_exit, identity_returns):
    # Linux can clear cmdline/exe before stat changes to Z. Missing launch
    # identity is neither a confirmed exit nor permission to send another signal.
    signals = []
    clock = [0.0]
    monkeypatch.setattr(u, "STOP_TIMEOUT", 0.2)
    monkeypatch.setattr(u.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(u.time, "sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds))
    monkeypatch.setattr(u.os, "kill", lambda *args: signals.append(args))
    monkeypatch.setattr(u, "process_identity", lambda pid: observation(record)
                        if not signals or (identity_returns and clock[0] >= 0.1) else None)
    monkeypatch.setattr(u, "_has_exited", lambda pid: confirmed_exit and clock[0] >= 0.1)
    assert u.stop_registered(record) is confirmed_exit
    assert clock[0] >= (0.1 if confirmed_exit else 0.2)
    assert clock[0] <= u.STOP_TIMEOUT + 0.05
    assert signals == [(12345, signal.SIGTERM)]


def test_workspace_path_scopes_registration(u, tmp_path):
    state = tmp_path / "state"
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir(); b.mkdir()
    link = tmp_path / "link"
    link.symlink_to(a, target_is_directory=True)
    assert u.registration_path(state, a) != u.registration_path(state, b)
    assert u.registration_path(state, a) == u.registration_path(state, link)


@pytest.mark.parametrize("content", ["12345", "{broken", '{"pid": 12345}'])
def test_invalid_registration_refuses_with_inspection_guidance(u, tmp_path, content):
    path = u.registration_path(tmp_path, tmp_path)
    path.write_text(content)
    with pytest.raises(RuntimeError, match="[Ii]nspect"):
        u.read_registration(tmp_path, tmp_path)
    assert path.read_text() == content


def test_registration_for_different_workspace_is_not_adopted(u, tmp_path, record):
    path = u.registration_path(tmp_path, tmp_path)
    path.write_text(json.dumps(record))
    with pytest.raises(RuntimeError, match="[Ii]nspect"):
        u.read_registration(tmp_path, tmp_path)


def test_legacy_pid_file_refuses_start_and_stop(u, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    state = tmp_path / "floatie"
    state.mkdir()
    legacy = state / "ui-demo.pid"
    legacy.write_text("12345\n")
    monkeypatch.setattr(u.os, "kill", lambda *args: pytest.fail("legacy PID was signaled"))
    monkeypatch.setattr(u.subprocess, "Popen", lambda *args, **kwargs: pytest.fail("legacy PID was adopted"))
    for command in ("start", "stop"):
        assert u.main([command, "--workspace", str(tmp_path), "--vm-name", "demo"]) == 1
        assert "inspect" in capsys.readouterr().err.lower()
    assert legacy.read_text() == "12345\n"


def test_stop_never_removes_replacement_registration(u, tmp_path, record, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    state = tmp_path / "floatie"
    state.mkdir()
    ws = Path(record["workspace"])
    path = u.registration_path(state, ws)
    path.write_text(json.dumps(record))
    replacement = {**record, "instance_id": "replacement"}
    def stopped(_):
        path.write_text(json.dumps(replacement))
        return True
    monkeypatch.setattr(u, "stop_registered", stopped)
    assert u.main(["stop", "--workspace", str(ws), "--vm-name", "demo"]) == 0
    assert json.loads(path.read_text()) == replacement


def test_linux_proc_identity_parses_spaces_and_parentheses(u, tmp_path, monkeypatch):
    # Recorded /proc shapes: field 22 follows state + 18 fields, comm may include ')'.
    proc = tmp_path / "proc"
    entry = proc / "12345"
    entry.mkdir(parents=True)
    (entry / "stat").write_text("12345 (python (test) worker) S " + "0 " * 18 + "43210 0 0\n")
    argv = ["/opt/python bin/python3", "/opt/server dir/ui_server.py", "--instance-id", "nonce"]
    (entry / "cmdline").write_bytes(b"\0".join(arg.encode() for arg in argv) + b"\0")
    (entry / "exe").symlink_to("/opt/python bin/python3")
    boot = proc / "sys/kernel/random/boot_id"
    boot.parent.mkdir(parents=True)
    boot.write_text("synthetic-boot\n")
    monkeypatch.setattr(u, "PROC_ROOT", proc)
    monkeypatch.setattr(u.sys, "platform", "linux")
    assert u.process_identity(12345) == dict(pid=12345, executable=argv[0], command=argv,
                                           start_id="synthetic-boot:43210")
    (entry / "cmdline").write_bytes(b"")
    assert u.process_identity(12345) is None


def test_macos_wide_ps_preserves_paths_with_spaces(u, record, monkeypatch):
    observed = observation(record)
    command = " ".join(observed["command"])
    monkeypatch.setattr(u.sys, "platform", "darwin")
    def ps(argv, **kwargs):
        assert "-ww" in argv and str(record["pid"]) in argv
        field = argv[-1]
        values = {"lstart=": "Mon Oct 12 11:22:33 2026\n", "comm=": observed["executable"] + "\n",
                  "args=": command + "\n", "stat=": "S\n"}
        return subprocess.CompletedProcess(argv, 0, values[field], "")
    monkeypatch.setattr(u.subprocess, "run", ps)
    identity = u.process_identity(12345)
    assert identity["command"] == command
    assert identity["executable"] == "/opt/python bin/python3"
    assert identity["start_id"] == "Mon Oct 12 11:22:33 2026"
    record["start_id"] = identity["start_id"]
    assert u.matches_process(record)


def test_unavailable_os_observation_fails_closed(u, monkeypatch):
    monkeypatch.setattr(u.sys, "platform", "darwin")
    def denied(*args, **kwargs):
        raise PermissionError("denied")
    monkeypatch.setattr(u.subprocess, "run", denied)
    assert u.process_identity(12345) is None


def test_macos_framework_launcher_pins_observed_interpreter(u, record, monkeypatch):
    # Framework launchers replace argv[0] and exec Python.app; ps records that binary.
    record["executable"] = "/Library/Python Framework/Python.app/Contents/MacOS/Python"
    observed = observation(record)
    observed["command"][0] = record["executable"]
    observed["command"] = " ".join(observed["command"])
    monkeypatch.setattr(u, "process_identity", lambda pid: observed)
    assert u.matches_process(record)
    observed["executable"] = "/other/Python"
    assert not u.matches_process(record)


def test_stale_registration_is_preserved_for_inspection(u, tmp_path, record, monkeypatch, capsys):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    state = tmp_path / "floatie"
    state.mkdir()
    ws = Path(record["workspace"])
    path = u.registration_path(state, ws)
    path.write_text(json.dumps(record))
    monkeypatch.setattr(u, "process_identity", lambda pid: None)
    monkeypatch.setattr(u.os, "kill", lambda *args: pytest.fail("unverified process signaled"))
    for command in ("start", "stop"):
        assert u.main([command, "--workspace", str(ws), "--vm-name", "demo"]) == 1
        assert "inspect" in capsys.readouterr().err.lower()
    assert json.loads(path.read_text()) == record


def test_legacy_tmp_pid_is_not_adopted(u, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setattr(u, "LEGACY_DIR", tmp_path)
    legacy = tmp_path / "floatie-ui-demo.pid"
    legacy.write_text("12345\n")
    monkeypatch.setattr(u.os, "kill", lambda *args: pytest.fail("legacy process signaled"))
    assert u.main(["stop", "--workspace", str(tmp_path), "--vm-name", "demo"]) == 1
    assert "inspect" in capsys.readouterr().err.lower()
    assert legacy.read_text() == "12345\n"


def test_atomic_write_failure_preserves_previous_record(u, tmp_path, record, monkeypatch):
    path = tmp_path / "record.json"
    path.write_text("previous\n")
    def failed_replace(*args):
        raise OSError("synthetic rename failure")
    monkeypatch.setattr(u.os, "replace", failed_replace)
    with pytest.raises(OSError, match="rename"):
        u._write_registration(path, record)
    assert path.read_text() == "previous\n"
    assert list(tmp_path.glob("record.json.*")) == []


def test_zero_port_cannot_leave_an_unregistered_listener(u, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setattr(u.subprocess, "Popen", lambda *a, **k: pytest.fail("unregistrable listener launched"))
    with pytest.raises(RuntimeError, match="port"):
        u.start_server(tmp_path, tmp_path, "demo", 0)
