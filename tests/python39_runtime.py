"""Stdlib-only compatibility probe, executed by the separately pinned Python 3.9.

The pytest process may use a newer interpreter. This child runs the actual
launcher and server, and cleans up only Popen handles it created itself.
"""
import hashlib
from html.parser import HTMLParser
import http.client
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import subprocess
import sys
import tempfile


def compatibility_interpreter(value, expected):
    guidance = "Set FLOATIE_TEST_PYTHON39 to the executable installed from ui/.python39-version."
    if not value:
        raise RuntimeError(guidance)
    executable = Path(value).expanduser().resolve()
    if not executable.is_file() or not os.access(executable, os.X_OK):
        raise RuntimeError("The compatibility interpreter is missing or not executable. " + guidance)
    try:
        result = subprocess.run(
            [str(executable), "-I", "-c", "import json,sys; print(json.dumps(list(sys.version_info[:3])))"],
            capture_output=True, text=True, timeout=10, check=True,
        )
        version = json.loads(result.stdout)
    except (OSError, subprocess.SubprocessError, ValueError) as error:
        raise RuntimeError("Cannot verify the compatibility interpreter. " + guidance) from error
    if (not isinstance(version, list) or len(version) != 3
            or any(type(part) is not int for part in version)
            or version[:2] != [3, 9] or ".".join(map(str, version)) != expected):
        raise RuntimeError("The compatibility interpreter must be Python " + expected + ". " + guidance)
    return str(executable.resolve())


class Assets(HTMLParser):
    def __init__(self):
        super().__init__()
        self.paths = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "script" and attrs.get("src"):
            self.paths.append(attrs["src"])
        if tag == "link" and attrs.get("rel") in ("stylesheet", "modulepreload", "icon"):
            self.paths.append(attrs["href"])


def get(port, path):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
    try:
        connection.request("GET", path)
        response = connection.getresponse()
        data = response.read()
        assert response.status == 200, (path, response.status)
        assert response.getheader("Cache-Control") == "no-store"
        return data
    finally:
        connection.close()


def probe(repo, timeout=20):
    expected = (repo / "ui/.python39-version").read_text().strip()
    version = ".".join(map(str, sys.version_info[:3]))
    assert sys.version_info[:2] == (3, 9) and version == expected, (version, expected)
    ps = shutil.which("ps")
    assert ps, "The lifecycle runtime requires ps."
    sys.path.insert(0, str(repo))
    import ui_process as process

    children = []
    original_popen = process.subprocess.Popen

    def owned_popen(*args, **kwargs):
        child = original_popen(*args, **kwargs)
        children.append(child)
        return child

    process.subprocess.Popen = owned_popen
    with tempfile.TemporaryDirectory(prefix="floatie-python39-") as directory:
        root = Path(directory)
        bindir = root / "runtime-bin"
        bindir.mkdir()
        (bindir / "ps").symlink_to(ps)
        os.environ["PATH"] = str(bindir)
        assert shutil.which("node") is None and shutil.which("npm") is None
        workspace = root / "workspace with spaces"
        workspace.mkdir()
        (workspace / "workspace.yml").write_text("name: floatie-uitest\n")
        os.environ["XDG_STATE_HOME"] = str(root / "state")
        os.environ["FLOATIE_UI_FIXTURES"] = str(repo / "tests/fixtures/state_running.json")
        # Cancel inside this interpreter so the owned-child finally block runs.
        # The caller's 30-second watchdog leaves time for the bounded cleanup.
        def expired(signum, frame):
            raise TimeoutError("Python 3.9 compatibility probe exceeded its deadline")

        previous_handler = signal.signal(signal.SIGALRM, expired)
        signal.setitimer(signal.ITIMER_REAL, timeout)
        try:
            with socket.socket() as sock:
                sock.bind(("127.0.0.1", 0))
                port = sock.getsockname()[1]
            record = process.start_server(repo, workspace, "floatie-uitest", port)
            assert record["pid"] in [child.pid for child in children]
            assert Path(record["interpreter"]).resolve() == Path(sys.executable).resolve()
            assert process.matches_process(record)
            entry = get(port, "/")
            assert entry == (repo / "ui/dist/index.html").read_bytes()
            assets = Assets()
            assets.feed(entry.decode())
            assert assets.paths
            hashes = {}
            for path in assets.paths:
                assert path.startswith("/assets/") and "?" not in path
                data = get(port, path)
                assert data == (repo / "ui/dist" / path.lstrip("/")).read_bytes()
                hashes[path] = hashlib.sha256(data).hexdigest()
            assert "Traceback" not in Path(record["log_path"]).read_text()
            assert process.stop_registered(record)
            assert not process.matches_process(record)
            return dict(python=version, node_on_path=False, npm_on_path=False,
                        owned_start=True, ownership_verified=True, entry_served=True,
                        assets=hashes, owned_stop=True, clean_log=True)
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, previous_handler)
            for child in reversed(children):
                if child.poll() is None:
                    child.terminate()
                    try:
                        child.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        child.kill()
                        child.wait(timeout=3)
            process.subprocess.Popen = original_popen


if __name__ == "__main__":
    print(json.dumps(probe(Path(sys.argv[1]).resolve())))
