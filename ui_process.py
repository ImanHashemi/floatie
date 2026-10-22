#!/usr/bin/env python3
"""Verified ownership of the detached, workspace-scoped UI server (host only)."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import fcntl
import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import secrets
import signal
import socket
import subprocess
import sys
import tempfile
import time

PROC_ROOT = Path("/proc")
LEGACY_DIR = Path("/tmp")
START_TIMEOUT = 5
STOP_TIMEOUT = 3


def _ps(pid: int, field: str) -> str:
    result = subprocess.run(["ps", "-ww", "-p", str(pid), "-o", field + "="],
                            capture_output=True, text=True, timeout=2,
                            env={**os.environ, "LC_ALL": "C"})
    # ps exits 1 with empty output when the process has gone away.
    if result.returncode not in (0, 1) or result.stderr.strip():
        raise OSError("process observation failed")
    return result.stdout.strip()


def _linux_stat(pid: int) -> tuple[str, str]:
    text = (PROC_ROOT / str(pid) / "stat").read_text()
    if text.split(" ", 1)[0] != str(pid):
        raise ValueError("mismatched proc PID")
    # comm is parenthesized and can itself contain spaces and parentheses.
    fields = text[text.rindex(")") + 2:].split()
    return fields[0], fields[19]  # state (3), starttime (22)


def process_identity(pid: int) -> dict | None:
    """Observe a live process. Missing, malformed or inaccessible means unknown."""
    if type(pid) is not int or pid <= 1:
        return None
    try:
        if sys.platform == "linux":
            state, start = _linux_stat(pid)
            root = PROC_ROOT / str(pid)
            raw = (root / "cmdline").read_bytes()
            if not raw.endswith(b"\0"):
                return None
            command = [os.fsdecode(arg) for arg in raw[:-1].split(b"\0")]
            executable = os.readlink(root / "exe")
            boot = (PROC_ROOT / "sys/kernel/random/boot_id").read_text().strip()
            if state in ("Z", "X") or not boot or not command[0] or _linux_stat(pid)[1] != start:
                return None
            start_id = boot + ":" + start
        elif sys.platform == "darwin":
            start_id = _ps(pid, "lstart")
            executable = _ps(pid, "comm")
            command = _ps(pid, "args")
            state = _ps(pid, "stat")
            if not state or state.startswith("Z") or _ps(pid, "lstart") != start_id:
                return None
        else:
            return None
        if not start_id or not executable or not command:
            return None
        return dict(pid=pid, executable=executable, command=command, start_id=start_id)
    except (OSError, ValueError, IndexError, subprocess.SubprocessError):
        return None


def registration_path(state_dir: Path, workspace: Path) -> Path:
    key = hashlib.sha256(os.fsencode(workspace.resolve())).hexdigest()
    return state_dir / ("ui-" + key + ".json")


def _valid_record(record) -> bool:
    if not isinstance(record, dict) or record.get("version") != 1:
        return False
    if type(record.get("pid")) is not int or record["pid"] <= 1:
        return False
    if type(record.get("port")) is not int or not 0 < record["port"] < 65536:
        return False
    for key in ("workspace", "interpreter", "executable", "server", "log_path", "start_id", "instance_id"):
        value = record.get(key)
        if not isinstance(value, str) or not value or any(c in value for c in "\0\r\n"):
            return False
    return (all(Path(record[k]).is_absolute() for k in ("workspace", "interpreter", "executable", "server", "log_path"))
            and record["workspace"] == str(Path(record["workspace"]).resolve())
            and Path(record["server"]).name == "ui_server.py")


def _inspection(path: Path) -> RuntimeError:
    return RuntimeError(f"Cannot verify UI ownership. Inspect {path} and its process manually; "
                        "no further signals will be sent. Remove the record only after inspection.")


def read_registration(state_dir: Path, workspace: Path) -> dict | None:
    path = registration_path(state_dir, workspace)
    try:
        record = json.loads(path.read_text())
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        raise _inspection(path) from None
    if not _valid_record(record) or record["workspace"] != str(workspace.resolve()):
        raise _inspection(path)
    return record


def _command(record: dict) -> list[str]:
    return [record["interpreter"], record["server"], "--port", str(record["port"]),
            "--workspace", record["workspace"], "--instance-id", record["instance_id"]]


def matches_process(record: dict) -> bool:
    """Match the complete launch identity; a bare PID never confers ownership."""
    if not _valid_record(record):
        return False
    observed = process_identity(record["pid"])
    if not observed:
        return False
    command = _command(record)
    # macOS ps does not quote argument boundaries. Never shlex-split its output:
    # compare the complete expected command, plus the separately observed executable.
    if isinstance(observed.get("command"), str):
        # macOS framework Python launchers exec Python.app and rewrite argv[0].
        # Pin that observed executable on our own child, then require it forever.
        command[0] = record["executable"]
        expected = " ".join(command)
    else:
        expected = command
    return (observed.get("pid") == record["pid"]
            and observed.get("executable") == record["executable"]
            and observed.get("start_id") == record["start_id"]
            and observed.get("command") == expected)


def _has_exited(pid: int) -> bool:
    """Distinguish confirmed exit/zombie from unavailable identity information."""
    try:
        if sys.platform == "linux":
            try:
                state, _ = _linux_stat(pid)
                return state in ("Z", "X")
            except FileNotFoundError:
                return not (PROC_ROOT / str(pid)).exists()
        if sys.platform == "darwin":
            state = _ps(pid, "stat")
            return not state or state.startswith("Z")
    except (OSError, ValueError, IndexError, subprocess.SubprocessError):
        pass
    return False


def _signal(record: dict, sig: int) -> bool:
    # Keep this observation adjacent to EVERY signal, including escalation.
    if not matches_process(record):
        return False
    try:
        os.kill(record["pid"], sig)
        return True
    except OSError:
        return False


def stop_registered(record: dict) -> bool:
    if not matches_process(record) or not _signal(record, signal.SIGTERM):
        return False
    for sig in (signal.SIGTERM, signal.SIGKILL):
        deadline = time.monotonic() + STOP_TIMEOUT
        identity_lost = False
        while time.monotonic() < deadline:
            if _has_exited(record["pid"]):
                return True
            # Linux may clear cmdline/exe before reporting a zombie. Keep
            # waiting for confirmed exit, but never escalate after identity
            # was lost, even if a later observation appears to match again.
            if not identity_lost and not matches_process(record):
                identity_lost = True
            time.sleep(0.05)
        if _has_exited(record["pid"]):
            return True
        if identity_lost or sig == signal.SIGKILL or not matches_process(record) or not _signal(record, signal.SIGKILL):
            return False
    return False


def _state_dir() -> Path:
    return Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state") / "floatie"


@contextmanager
def _locked(state_dir: Path, workspace: Path):
    state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    state_dir.chmod(0o700)
    path = registration_path(state_dir, workspace).with_suffix(".lock")
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield
        # Closing releases the lock. Do not unlink: waiters share this inode.


def _check_legacy(state_dir: Path, vm_name: str):
    # VM names are filenames here, never paths supplied by a registration.
    if not vm_name or Path(vm_name).name != vm_name or vm_name in (".", ".."):
        raise RuntimeError("set a valid 'name:' in workspace.yml")
    for path in (state_dir / f"ui-{vm_name}.pid", LEGACY_DIR / f"floatie-ui-{vm_name}.pid"):
        if path.exists():
            raise _inspection(path)


def _write_registration(path: Path, record: dict):
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, prefix=path.name + ".", delete=False) as out:
        temp = Path(out.name)
        try:
            json.dump(record, out)
            out.flush()
            os.fsync(out.fileno())
            os.replace(temp, path)
        finally:
            temp.unlink(missing_ok=True)


def _url(log_path: Path) -> str | None:
    try:
        urls = re.findall(r"http://127\.0\.0\.1:[0-9]+/\?t=[A-Za-z0-9_-]+", log_path.read_text())
        return urls[-1] if urls else None
    except OSError:
        return None


def _serving(port: int) -> bool:
    # Fetch only the static page; readiness must not inspect or mutate a VM.
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=0.5)
    try:
        connection.request("GET", "/")
        response = connection.getresponse()
        response.read()
        return response.status == 200
    except (OSError, http.client.HTTPException):
        return False
    finally:
        connection.close()


def start_server(repo_dir: Path, workspace: Path, vm_name: str, port: int) -> dict:
    if port == 0:
        raise RuntimeError("floatie UI failed to start: choose an explicit port between 1 and 65535")
    workspace = workspace.resolve()
    state_dir = _state_dir()
    path = registration_path(state_dir, workspace)
    with _locked(state_dir, workspace):
        _check_legacy(state_dir, vm_name)
        record = read_registration(state_dir, workspace)
        if record:
            if matches_process(record):
                return record
            raise _inspection(path)
        # Probe only for a conflict; never turn a port into a process to signal.
        if 0 < port < 65536:
            with socket.socket() as probe:
                probe.settimeout(0.5)
                if probe.connect_ex(("127.0.0.1", port)) == 0:
                    raise RuntimeError(f"127.0.0.1:{port} is already in use, but no live UI is recorded "
                                       f"for '{vm_name}'. Inspect the listener or choose another port: floatie ui {port + 1}")
        log_path = path.with_suffix(".log")
        record = dict(version=1, workspace=str(workspace), interpreter=os.path.abspath(sys.executable),
                      server=str((repo_dir / "ui_server.py").resolve()), executable="", pid=0, start_id="",
                      instance_id=secrets.token_hex(24), log_path=str(log_path), port=port)
        fd = os.open(log_path, os.O_CREAT | os.O_WRONLY | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w") as log:
            os.fchmod(log.fileno(), 0o600)
            child = subprocess.Popen(_command(record), cwd=workspace,
                                     env={**os.environ, "FLOATIE_VM": vm_name}, stdin=subprocess.DEVNULL,
                                     stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        record["pid"] = child.pid
        deadline = time.monotonic() + START_TIMEOUT
        while time.monotonic() < deadline:
            if child.poll() is not None:
                break
            observed = process_identity(child.pid) if _url(log_path) else None
            if observed:
                if not record["start_id"]:
                    record["start_id"] = observed["start_id"]
                    record["executable"] = observed["executable"]
                # The server prints its URL only AFTER binding its loopback socket.
                if matches_process(record) and _serving(port) and matches_process(record):
                    try:
                        _write_registration(path, record)
                    except OSError:
                        stop_registered(record)
                        raise
                    return record
            time.sleep(0.05)
        # Only verified children may be signaled, even on failed startup. A child
        # that never printed its URL may still have an observable launch identity.
        if not record["start_id"] and child.poll() is None:
            observed = process_identity(child.pid)
            if observed:
                record["start_id"] = observed["start_id"]
                record["executable"] = observed["executable"]
        if matches_process(record):
            stop_registered(record)
            child.poll()
        detail = "\n".join(log_path.read_text(errors="replace").splitlines()[-20:])
        raise RuntimeError(f"floatie UI failed to start or verify ownership. Inspect PID {child.pid} and {log_path} "
                           "if it remains running — last log lines:\n" + detail)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="floatie UI lifecycle")
    parser.add_argument("command", choices=("start", "stop", "log-path"))
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    parser.add_argument("--repo-dir", type=Path, default=Path(__file__).resolve().parent)
    parser.add_argument("--vm-name", required=True)
    parser.add_argument("--port", type=int, default=8787)
    args = parser.parse_args(argv)
    state_dir = _state_dir()
    path = registration_path(state_dir, args.workspace)
    try:
        if args.command == "start":
            previous = read_registration(state_dir, args.workspace)
            record = start_server(args.repo_dir, args.workspace, args.vm_name, args.port)
            status = "already running" if previous == record else "started"
            url = _url(Path(record["log_path"])) or "see: floatie ui-logs"
            print(f"==> floatie UI {status} (pid {record['pid']}) — {url}")
            print("    logs: floatie ui-logs    stop: floatie ui-stop")
        elif args.command == "stop":
            with _locked(state_dir, args.workspace):
                _check_legacy(state_dir, args.vm_name)
                record = read_registration(state_dir, args.workspace)
                if not record:
                    print("==> floatie UI not running")
                elif stop_registered(record):
                    # Never erase a replacement instance, even if an external writer
                    # ignores our lock. Compare the complete record before unlinking.
                    if read_registration(state_dir, args.workspace) == record:
                        path.unlink()
                    print(f"==> floatie UI stopped (pid {record['pid']})")
                else:
                    raise _inspection(path)
        else:
            record = read_registration(state_dir, args.workspace)
            log = Path(record["log_path"]) if record else path.with_suffix(".log")
            if not log.exists():
                # Reading legacy logs is harmless; never adopt their bare PID.
                name = args.vm_name
                if Path(name).name == name:
                    for old in (state_dir / f"ui-{name}.log", LEGACY_DIR / f"floatie-ui-{name}.log"):
                        if old.exists():
                            log = old
                            break
            print(log)
        return 0
    except (OSError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
