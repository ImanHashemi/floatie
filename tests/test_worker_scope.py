"""Real temporary Git repositories; terminal, agent and stack boundaries are stubs."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from test_worker_init_refresh import (
    GIT_ENV, _git, advance_origin, commit_locally, make_origin_and_clone, run_init,
)


@pytest.fixture
def boundary(tmp_path):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    live = tmp_path / "sessions.json"
    live.write_text("[]")
    calls = tmp_path / "calls.jsonl"
    tmux = bindir / "tmux"
    tmux.write_text(f"#!{sys.executable}\n" + f'''
import json, sys
from pathlib import Path
live, log = Path({str(live)!r}), Path({str(calls)!r})
args = sys.argv[1:]
with log.open('a') as out:
    out.write(json.dumps(args) + '\\n')
sessions = json.loads(live.read_text())
if args[0] == 'has-session':
    target = args[args.index('-t') + 1]
    matched = target[1:] in sessions if target.startswith('=') else any(s.startswith(target) for s in sessions)
    sys.exit(0 if matched else 1)
if args[0] == 'new-session':
    sessions.append(args[args.index('-s') + 1])
    live.write_text(json.dumps(sessions))
    sys.exit(0)
sys.exit('unexpected tmux command: ' + repr(args))
''')
    tmux.chmod(0o755)
    # A regression must fail safely, never launch a real agent or stack.
    launches = tmp_path / "launches"
    for name in ("codex", "claude", "docker", "just", "limactl"):
        command = bindir / name
        command.write_text(f"#!/bin/sh\necho {name} >> '{launches}'\nexit 99\n")
        command.chmod(0o755)
    return bindir, live, calls, launches


def calls_since(log, start=0):
    return [json.loads(line) for line in log.read_text().splitlines()[start:]] if log.exists() else []


def saved_worker(home, repo="api", worker="review"):
    wt = home / "work/.worktrees" / worker / repo
    wt.parent.mkdir(parents=True, exist_ok=True)
    _git("worktree", "add", "-b", f"worker/{worker}", str(wt), cwd=home / "work" / repo)
    return wt


def saved_repos(home, worker="review"):
    parent = home / "work/.worktrees" / worker
    return {p.name for p in parent.iterdir()} if parent.exists() else set()


def test_reconnect_omitted_scope_preserves_saved_set(tmp_path, boundary):
    bindir, live, calls, launches = boundary
    home, seed = make_origin_and_clone(tmp_path, "api")
    make_origin_and_clone(tmp_path, "web")
    wt = saved_worker(home)
    _git("checkout", "-b", "custom-work", cwd=wt)
    tip = commit_locally(wt)
    (wt / "f.txt").write_text("unsaved edit\n")
    (wt / "notes").write_text("untracked work\n")
    clone_tip = _git("rev-parse", "HEAD", cwd=home / "work/api")
    advance_origin(seed)
    result = run_init(home, bindir, "review")
    assert result.returncode == 0, result.stdout + result.stderr
    assert saved_repos(home) == {"api"}
    assert _git("rev-parse", "HEAD", cwd=wt) == tip
    assert _git("branch", "--show-current", cwd=wt) == "custom-work"
    assert (wt / "f.txt").read_text() == "unsaved edit\n"
    assert (wt / "notes").read_text() == "untracked work\n"
    assert _git("rev-parse", "HEAD", cwd=home / "work/api") == clone_tip
    assert json.loads(live.read_text()) == ["review"]
    assert "tmux attach-session -t '=review'" in result.stdout
    assert not launches.exists()
    assert [c[0] for c in calls_since(calls)] == ["has-session", "new-session"]


@pytest.mark.parametrize("live_session", [False, True])
@pytest.mark.parametrize("requested", [("web",), ("api", "web")])
def test_differing_scope_refused_even_with_live_session(tmp_path, boundary, live_session, requested):
    bindir, live, calls, _ = boundary
    home, _ = make_origin_and_clone(tmp_path, "api")
    make_origin_and_clone(tmp_path, "web")
    wt = saved_worker(home)
    (wt / "notes").write_text("keep me")
    live.write_text(json.dumps(["review"] if live_session else []))
    result = run_init(home, bindir, "review", *requested)
    assert result.returncode != 0
    assert "scope" in result.stderr and "api" in result.stderr
    assert saved_repos(home) == {"api"}
    assert (wt / "notes").read_text() == "keep me"
    assert not any(c[0] == "new-session" for c in calls_since(calls))


@pytest.mark.parametrize("requested", [(), ("api",), ("api", "api")])
def test_duplicate_live_same_scope_only_offers_attach(tmp_path, boundary, requested):
    bindir, live, calls, launches = boundary
    home, _ = make_origin_and_clone(tmp_path, "api")
    saved_worker(home)
    live.write_text('["review"]')
    result = run_init(home, bindir, "review", *requested)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Already exists — in VM:" in result.stdout
    assert "tmux attach-session -t '=review'" in result.stdout
    assert calls_since(calls) == [["has-session", "-t", "=review"]]
    assert not launches.exists()


def test_prefix_collision_does_not_reuse_or_attach_other_worker(tmp_path, boundary):
    bindir, live, calls, _ = boundary
    home, _ = make_origin_and_clone(tmp_path, "api")
    saved_worker(home)
    live.write_text('["review-other"]')
    result = run_init(home, bindir, "review", "api")
    assert result.returncode == 0, result.stderr
    assert json.loads(live.read_text()) == ["review-other", "review"]
    assert calls_since(calls)[0] == ["has-session", "-t", "=review"]


@pytest.mark.parametrize("name", ["main", "stack", "workspace", "all", "Upper", "x" * 64,
                                  "../escape", "bad name", "ok\nbad", "-option"])
def test_invalid_new_names_fail_before_any_mutation(tmp_path, boundary, name):
    bindir, _, calls, _ = boundary
    home, _ = make_origin_and_clone(tmp_path, "api")
    result = run_init(home, bindir, name, "api")
    assert result.returncode != 0
    assert not (home / "work/.worktrees").exists()
    assert calls_since(calls) == []


def test_new_omitted_scope_discovers_only_actual_clones(tmp_path, boundary):
    bindir, _, _, launches = boundary
    home, _ = make_origin_and_clone(tmp_path, "api")
    make_origin_and_clone(tmp_path, "web")
    (home / "work/stack").mkdir()
    (home / "work/CLAUDE.md").write_text("workspace context")
    (home / "work/broken").mkdir()
    (home / "work/broken/.git").write_text("gitdir: /missing/metadata\n")
    # Git would find this ancestor for plain subdirectories unless .git is required.
    _git("init", "-q", cwd=home / "work")
    result = run_init(home, bindir, "review")
    assert result.returncode == 0, result.stdout + result.stderr
    assert saved_repos(home) == {"api", "web"}
    assert not launches.exists()


@pytest.mark.parametrize("bad_repo", ["missing", "plain", "broken", ".", "..", "api\nplain", "bad/name"])
def test_all_explicit_repos_validated_before_refresh_or_creation(tmp_path, boundary, bad_repo):
    bindir, _, calls, _ = boundary
    home, seed = make_origin_and_clone(tmp_path, "api")
    clone = home / "work/api"
    before = _git("rev-parse", "HEAD", cwd=clone)
    advance_origin(seed)
    (home / "work/plain").mkdir()
    (home / "work/broken").mkdir()
    (home / "work/broken/.git").write_text("gitdir: /missing/metadata\n")
    _git("init", "-q", cwd=home / "work")
    result = run_init(home, bindir, "review", "api", bad_repo)
    assert result.returncode != 0
    assert not (home / "work/.worktrees").exists()
    assert _git("rev-parse", "HEAD", cwd=clone) == before
    assert calls_since(calls) == []


@pytest.mark.parametrize("explicit", [False, True])
def test_shared_common_dir_cannot_be_two_available_clones(tmp_path, boundary, explicit):
    bindir, _, _, _ = boundary
    home, _ = make_origin_and_clone(tmp_path, "api")
    _git("worktree", "add", "-b", "alias", str(home / "work/z-alias"), cwd=home / "work/api")
    result = run_init(home, bindir, "review", *(('api', 'z-alias') if explicit else ()))
    if explicit:
        assert result.returncode != 0
        assert not (home / "work/.worktrees").exists()
    else:
        assert result.returncode == 0, result.stdout + result.stderr
        assert len(saved_repos(home)) == 1


def test_root_alias_does_not_prevent_reconnecting_saved_scope(tmp_path, boundary):
    bindir, _, _, _ = boundary
    home, _ = make_origin_and_clone(tmp_path, "api")
    _git("worktree", "add", "-b", "alias", str(home / "work/z-alias"), cwd=home / "work/api")
    saved_worker(home)
    result = run_init(home, bindir, "review")
    assert result.returncode == 0, result.stdout + result.stderr
    assert saved_repos(home) == {"api"}


@pytest.mark.parametrize("repos", [(), ("web", "api", "web")])
def test_multi_repo_reconnect_accepts_same_set_in_any_order(tmp_path, boundary, repos):
    bindir, live, calls, _ = boundary
    home, _ = make_origin_and_clone(tmp_path, "api")
    make_origin_and_clone(tmp_path, "web")
    api = saved_worker(home)
    web = saved_worker(home, "web")
    _git("checkout", "--detach", cwd=api)
    _git("checkout", "-b", "custom-web", cwd=web)
    live.write_text('["review"]')
    result = run_init(home, bindir, "review", *repos)
    assert result.returncode == 0, result.stdout + result.stderr
    assert saved_repos(home) == {"api", "web"}
    assert _git("branch", "--show-current", cwd=api) == ""
    assert _git("branch", "--show-current", cwd=web) == "custom-web"
    assert calls_since(calls) == [["has-session", "-t", "=review"]]


@pytest.mark.parametrize("scope", [(), ("plain",)])
def test_no_actual_clones_never_starts_empty_worker(tmp_path, boundary, scope):
    bindir, _, calls, _ = boundary
    home = tmp_path / "home"
    (home / "work/plain").mkdir(parents=True)
    result = run_init(home, bindir, "review", *scope)
    assert result.returncode != 0
    assert not (home / "work/.worktrees").exists()
    assert calls_since(calls) == []


@pytest.mark.parametrize("symlink", ["worker", "repo", "gitfile", "backlink"])
def test_saved_scope_rejects_symlinked_identity_without_mutation(tmp_path, boundary, symlink):
    bindir, _, calls, _ = boundary
    home, _ = make_origin_and_clone(tmp_path, "api")
    wt = saved_worker(home)
    selected = {"worker": wt.parent, "repo": wt, "gitfile": wt / ".git",
                "backlink": Path(_git("rev-parse", "--absolute-git-dir", cwd=wt)) / "gitdir"}[symlink]
    moved = tmp_path / "preserved"
    selected.rename(moved)
    selected.symlink_to(moved)
    result = run_init(home, bindir, "review", "api")
    assert result.returncode != 0
    assert "inspect" in result.stderr.lower()
    assert selected.is_symlink() and moved.exists()
    assert not any(c[0] == "new-session" for c in calls_since(calls))


@pytest.mark.parametrize("state", ["empty", "plain", "broken", "unregistered", "hidden", "missing-registration"])
def test_unknown_saved_scope_requires_inspection(tmp_path, boundary, state):
    bindir, live, calls, _ = boundary
    home, _ = make_origin_and_clone(tmp_path, "api")
    wt = saved_worker(home)
    if state == "empty":
        _git("worktree", "remove", str(wt), cwd=home / "work/api")
    elif state == "plain":
        (wt / ".git").unlink()
    elif state == "broken":
        (wt / ".git").write_text("gitdir: /missing/metadata\n")
    elif state == "unregistered":
        metadata = Path(_git("rev-parse", "--absolute-git-dir", cwd=wt))
        (metadata / "gitdir").write_text(str(wt.parent / "api-other/.git") + "\n")
    elif state == "hidden":
        (wt.parent / ".unknown").write_text("keep me")
    else:
        make_origin_and_clone(tmp_path, "web")
        missing = saved_worker(home, "web")
        shutil.rmtree(missing)
    before = sorted(str(p) for p in wt.parent.rglob("*"))
    live.write_text('["review"]')
    result = run_init(home, bindir, "review", "api")
    assert result.returncode != 0
    assert "inspect" in result.stderr.lower()
    assert sorted(str(p) for p in wt.parent.rglob("*")) == before
    assert not any(c[0] == "new-session" for c in calls_since(calls))


def test_sibling_gitdir_cannot_impersonate_registered_saved_worktree(tmp_path, boundary):
    bindir, _, calls, _ = boundary
    home, _ = make_origin_and_clone(tmp_path, "api")
    wt = saved_worker(home)
    sibling = saved_worker(home, worker="review-other")
    (wt / "f.txt").write_text("keep this work")
    (wt / ".git").write_bytes((sibling / ".git").read_bytes())
    # Both registration and common-dir checks still pass for this corrupt gitfile.
    assert f"worktree {wt}\n" in _git("worktree", "list", "--porcelain", cwd=home / "work/api") + "\n"
    result = run_init(home, bindir, "review", "api")
    assert result.returncode != 0
    assert "inspect" in result.stderr.lower()
    assert (wt / "f.txt").read_text() == "keep this work"
    assert not any(c[0] == "new-session" for c in calls_since(calls))


def test_live_session_without_saved_scope_is_not_blessed(tmp_path, boundary):
    bindir, live, calls, _ = boundary
    home, _ = make_origin_and_clone(tmp_path, "api")
    live.write_text('["review"]')
    result = run_init(home, bindir, "review", "api")
    assert result.returncode != 0
    assert "inspect" in result.stderr.lower()
    assert not (home / "work/.worktrees").exists()
    assert calls_since(calls) == [["has-session", "-t", "=review"]]


def test_second_repo_failure_retains_first_and_rejects_expanding_retry(tmp_path, boundary):
    bindir, _, calls, _ = boundary
    home, _ = make_origin_and_clone(tmp_path, "api")
    make_origin_and_clone(tmp_path, "web")
    # A valid repo can still fail at add: the worker branch is checked out elsewhere.
    _git("worktree", "add", "-b", "worker/review", str(tmp_path / "occupied"), cwd=home / "work/web")
    result = run_init(home, bindir, "review", "api", "web")
    assert result.returncode != 0
    assert saved_repos(home) == {"api"}
    diagnostic = (result.stdout + result.stderr).lower()
    assert "requested: api web" in diagnostic
    assert "created: api" in diagnostic and "missing: web" in diagnostic
    assert not any(c[0] == "new-session" for c in calls_since(calls))
    wt = home / "work/.worktrees/review/api"
    (wt / "notes").write_text("survivor")
    retry = run_init(home, bindir, "review", "api", "web")
    assert retry.returncode != 0
    assert "scope" in retry.stderr
    accepted = run_init(home, bindir, "review", "api")
    assert accepted.returncode == 0, accepted.stdout + accepted.stderr
    assert saved_repos(home) == {"api"}
    assert (wt / "notes").read_text() == "survivor"


def test_guest_worker_create_forwards_explicit_multi_repo_scope(tmp_path, boundary):
    bindir, _, _, _ = boundary
    home, _ = make_origin_and_clone(tmp_path, "api")
    make_origin_and_clone(tmp_path, "web")
    installed = home / ".local/bin/floatie-worker-init"
    installed.parent.mkdir(parents=True)
    shutil.copyfile(Path(__file__).resolve().parents[1] / "worker-init.sh", installed)
    installed.chmod(0o755)
    real_just = shutil.which("just")
    result = subprocess.run([real_just, "--justfile", str(Path(__file__).resolve().parents[1] / "agents.just"),
                             "worker-create", "review", "api", "web"], capture_output=True, text=True,
                            env={**os.environ, **GIT_ENV, "HOME": str(home), "PATH": f"{bindir}:{os.environ['PATH']}"})
    assert result.returncode == 0, result.stdout + result.stderr
    assert saved_repos(home) == {"api", "web"}
