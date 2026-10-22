"""PATH isolation must keep utilities even when Docker shares their directory."""
import os
import shutil
import subprocess

import pytest

from test_worker_rm import GIT_ENV, _git, make_stubs, safe_path


@pytest.fixture
def mixed_path(tmp_path, monkeypatch):
    mixed = tmp_path / "system-bin"
    mixed.mkdir()
    for name in (
        "bash", "sh", "env", "git", "basename", "dirname", "cat", "find",
        "grep", "sed", "sort", "tr", "wc", "mkdir", "rm", "rmdir",
    ):
        executable = shutil.which(name)
        assert executable is not None, f"missing test utility: {name}"
        (mixed / name).symlink_to(executable)
    (mixed / "docker").write_text("#!/bin/sh\nexit 99\n")
    (mixed / "docker").chmod(0o755)
    # Also cover a host tmux installation in a directory without Docker.
    other = tmp_path / "other-bin"
    other.mkdir()
    (other / "tmux").write_text("#!/bin/sh\nexit 99\n")
    (other / "tmux").chmod(0o755)
    monkeypatch.setenv("PATH", os.pathsep.join((str(mixed), str(other))))
    return make_stubs(tmp_path, tmp_path / "calls")


def test_safe_path_keeps_utilities_and_git_hooks(mixed_path, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git("init", "-q", cwd=repo)
    (repo / "f.txt").write_text("fixture\n")
    hook = repo / ".git/hooks/pre-commit"
    hook.write_text("""#!/usr/bin/env bash
set -euo pipefail
test "$(basename "$(dirname "$PWD/f.txt")")" = repo
mkdir .git/hook-check
cat f.txt | grep fixture | sed s/fixture/checked/ | sort | tr a-z A-Z > .git/hook-check/result
test "$(wc -l < .git/hook-check/result | tr -d ' ')" = 1
find .git/hook-check -type f | grep -q result
env sh -c 'rm .git/hook-check/result; rmdir .git/hook-check'
echo hook-ran
""")
    hook.chmod(0o755)
    result = subprocess.run(
        ["bash", "-ec", "git add f.txt; git commit -qm fixture"], cwd=repo,
        env={**GIT_ENV, "PATH": safe_path(mixed_path)}, capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "hook-ran" in result.stdout + result.stderr


@pytest.mark.parametrize("command", ["docker", "tmux"])
def test_safe_path_stubs_win_without_host_fallback(mixed_path, command):
    path = safe_path(mixed_path)
    assert shutil.which(command, path=path) == str(mixed_path / command)
    (mixed_path / command).unlink()
    assert shutil.which(command, path=path) is None
    assert shutil.which(command, path=safe_path(mixed_path)) is None
