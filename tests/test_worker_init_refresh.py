"""worker-init.sh branches a worker's worktree from the main clone's CURRENT HEAD. Clones only
move when someone pulls them, so without a refresh a new worker starts weeks behind origin.

Rule under test — six states a clone can be in, and what the refresh does with each:

  1. clean, on its default branch, behind origin   → fast-forwarded ("refreshed to origin/<d>")
  2. the same, but already at origin               → nothing to do ("already at origin/<d>")
  3. unborn HEAD (init + remote add + fetch)       → populated from origin by that same
                                                     fast-forward; skipping it breaks the worker
  4. commits origin does not have (ahead/diverged) → refused ("local commits … not on origin")
  5. not eligible — dirty, another branch, detached, no origin, no such branch on origin
                                                   → reported and left alone
  6. the fetch itself fails                        → WARN on stdout; the worker still comes up
                                                     from the clone's current HEAD

Every one of them exits 0 with a worktree: the refresh is best-effort and must never be what
stops a worker starting. tmux is stubbed.
"""
import os
import pathlib
import shutil
import subprocess

import pytest

WORKER_INIT = pathlib.Path(__file__).resolve().parents[1] / "worker-init.sh"
GIT_ENV = {
    "GIT_AUTHOR_NAME": "T", "GIT_AUTHOR_EMAIL": "t@e.com",
    "GIT_COMMITTER_NAME": "T", "GIT_COMMITTER_EMAIL": "t@e.com",
    # /dev/null as the global config: fixture commits must not inherit this machine's
    # ~/.gitconfig (commit.gpgsign=true fails wherever the signing key is absent).
    "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null",
}


def _git(*args, cwd):
    r = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True,
                       env={**os.environ, **GIT_ENV})
    assert r.returncode == 0, f"git {' '.join(args)} failed:\n{r.stderr}"
    return r.stdout.strip()


def tmux_log(tmp_path):
    return tmp_path / "tmux.log"


def tmux_calls(tmp_path):
    log = tmux_log(tmp_path)
    return log.read_text().splitlines() if log.exists() else []


def make_stubs(tmp_path):
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    # has-session must say "no such session" so init proceeds; new-session must succeed.
    # argv is logged too, so a test can assert init never reached tmux at all.
    (bindir / "tmux").write_text(
        f'#!/bin/sh\necho "$*" >> "{tmux_log(tmp_path)}"\n'
        'case "$1" in has-session) exit 1;; *) exit 0;; esac\n')
    (bindir / "tmux").chmod(0o755)
    return bindir


def make_origin_and_clone(tmp_path, repo="repo-a", branch="main"):
    """Bare origin with one commit on <branch>; ~/work/<repo> cloned from it; a `seed` clone to push from."""
    origin = tmp_path / "origin" / f"{repo}.git"
    origin.mkdir(parents=True)
    _git("init", "-q", "--bare", "-b", branch, cwd=origin)
    seed = tmp_path / "seed" / repo
    seed.mkdir(parents=True)
    _git("init", "-q", "-b", branch, cwd=seed)
    _git("remote", "add", "origin", str(origin), cwd=seed)
    (seed / "f.txt").write_text("1\n")
    _git("add", "f.txt", cwd=seed)
    _git("commit", "-qm", "c1", cwd=seed)
    _git("push", "-q", "-u", "origin", branch, cwd=seed)
    home = tmp_path / "home"
    (home / "work").mkdir(parents=True, exist_ok=True)
    _git("clone", "-q", str(origin), str(home / "work" / repo), cwd=tmp_path)
    return home, seed


def advance_origin(seed, msg="c2", branch="main"):
    (seed / "f.txt").write_text(f"{msg}\n")
    _git("add", "f.txt", cwd=seed)
    _git("commit", "-qm", msg, cwd=seed)
    _git("push", "-q", "origin", branch, cwd=seed)
    return _git("rev-parse", "HEAD", cwd=seed)


def commit_locally(clone, msg="local"):
    """A commit in the ~/work clone that origin does not have."""
    (clone / "local.txt").write_text(f"{msg}\n")
    _git("add", "local.txt", cwd=clone)
    _git("commit", "-qm", msg, cwd=clone)
    return _git("rev-parse", "HEAD", cwd=clone)


def run_init(home, bindir, worker="w1", *repos):
    env = {**os.environ, **GIT_ENV, "HOME": str(home),
           "PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}"}
    return subprocess.run(["bash", str(WORKER_INIT), worker, *repos],
                          capture_output=True, text=True, env=env)


def worktree_head(home, worker, repo):
    return _git("rev-parse", "HEAD", cwd=home / "work" / ".worktrees" / worker / repo)


def test_clean_clone_on_default_branch_is_fast_forwarded(tmp_path):
    bindir = make_stubs(tmp_path)
    home, seed = make_origin_and_clone(tmp_path)
    new_head = advance_origin(seed)
    r = run_init(home, bindir, "w1", "repo-a")
    assert r.returncode == 0, r.stderr
    assert worktree_head(home, "w1", "repo-a") == new_head, "worker started on stale code"
    assert "refreshed to origin/main" in r.stdout


def test_dirty_clone_is_left_alone_and_reported(tmp_path):
    bindir = make_stubs(tmp_path)
    home, seed = make_origin_and_clone(tmp_path)
    old_head = _git("rev-parse", "HEAD", cwd=home / "work" / "repo-a")
    advance_origin(seed)
    (home / "work" / "repo-a" / "f.txt").write_text("local edit\n")
    r = run_init(home, bindir, "w1", "repo-a")
    assert r.returncode == 0, r.stderr
    assert worktree_head(home, "w1", "repo-a") == old_head
    assert "not refreshed" in r.stdout and "uncommitted" in r.stdout


def test_clone_on_another_branch_is_left_alone_and_reported(tmp_path):
    bindir = make_stubs(tmp_path)
    home, seed = make_origin_and_clone(tmp_path)
    _git("checkout", "-q", "-b", "fix/something", cwd=home / "work" / "repo-a")
    old_head = _git("rev-parse", "HEAD", cwd=home / "work" / "repo-a")
    advance_origin(seed)
    r = run_init(home, bindir, "w1", "repo-a")
    assert r.returncode == 0, r.stderr
    assert worktree_head(home, "w1", "repo-a") == old_head
    assert "not refreshed" in r.stdout and "fix/something" in r.stdout


def test_clone_without_origin_still_gets_a_worktree(tmp_path):
    bindir = make_stubs(tmp_path)
    home = tmp_path / "home"
    rp = home / "work" / "repo-b"
    rp.mkdir(parents=True)
    _git("init", "-q", "-b", "main", cwd=rp)
    (rp / "f.txt").write_text("x\n")
    _git("add", "f.txt", cwd=rp)
    _git("commit", "-qm", "init", cwd=rp)
    r = run_init(home, bindir, "w1", "repo-b")
    assert r.returncode == 0, r.stderr
    assert (home / "work" / ".worktrees" / "w1" / "repo-b").is_dir()
    assert "no 'origin' remote" in r.stdout


def test_existing_worker_branch_is_reused_not_reset(tmp_path):
    """worker-init reuses an existing worker/<name>; refresh must not move that branch."""
    bindir = make_stubs(tmp_path)
    home, seed = make_origin_and_clone(tmp_path)
    clone = home / "work" / "repo-a"
    _git("branch", "worker/w1", cwd=clone)
    branch_head = _git("rev-parse", "worker/w1", cwd=clone)
    advance_origin(seed)
    r = run_init(home, bindir, "w1", "repo-a")
    assert r.returncode == 0, r.stderr
    assert worktree_head(home, "w1", "repo-a") == branch_head


def test_clone_without_origin_head_ref_falls_back_to_main(tmp_path):
    """A clone whose refs/remotes/origin/HEAD was never set must not abort the worker.

    `git remote add origin … && git fetch` leaves origin/HEAD unset, and under
    `set -euo pipefail` a failing `symbolic-ref … | sed` kills the whole script
    before the `default=main` fallback can run — silently, with no worktree.
    """
    bindir = make_stubs(tmp_path)
    home, seed = make_origin_and_clone(tmp_path)
    _git("symbolic-ref", "--delete", "refs/remotes/origin/HEAD", cwd=home / "work" / "repo-a")
    new_head = advance_origin(seed)
    r = run_init(home, bindir, "w1", "repo-a")
    assert r.returncode == 0, r.stderr
    assert worktree_head(home, "w1", "repo-a") == new_head


def test_unreachable_origin_warns_with_gits_reason_and_still_creates_the_worktree(tmp_path):
    """A refresh is best-effort: an unreachable origin must not stop a worker starting.

    The WARN has to carry git's own first error line — "refresh failed" alone leaves the
    user guessing between a dead network, a bad remote and a missing credential. It goes
    to stdout, not stderr: the worker came up, so this is a progress note, not an error.
    """
    bindir = make_stubs(tmp_path)
    home, _seed = make_origin_and_clone(tmp_path)
    clone = home / "work" / "repo-a"
    _git("remote", "set-url", "origin", str(tmp_path / "origin" / "gone.git"), cwd=clone)
    r = run_init(home, bindir, "w1", "repo-a")
    assert r.returncode == 0, r.stderr
    assert (home / "work" / ".worktrees" / "w1" / "repo-a").is_dir()
    warn = [ln for ln in r.stdout.splitlines() if ln.startswith("WARN: [repo-a]")]
    assert warn, f"no WARN on stdout:\n{r.stdout}"
    assert "refresh failed" in warn[0]
    reason = warn[0].split("branching from current HEAD:", 1)[1].strip()
    assert reason, f"WARN carries no reason from git: {warn[0]!r}"
    assert "refresh failed" not in r.stderr, "WARN belongs on stdout"


def test_clone_ahead_of_origin_is_reported_as_local_commits_not_as_a_refresh(tmp_path):
    """Ahead of origin: nothing to fast-forward, and claiming "refreshed" would be a lie."""
    bindir = make_stubs(tmp_path)
    home, _seed = make_origin_and_clone(tmp_path)
    local_head = commit_locally(home / "work" / "repo-a")
    r = run_init(home, bindir, "w1", "repo-a")
    assert r.returncode == 0, r.stderr
    assert worktree_head(home, "w1", "repo-a") == local_head
    assert "not refreshed: local commits" in r.stdout
    assert "refreshed to" not in r.stdout


def test_diverged_clone_is_reported_as_local_commits(tmp_path):
    bindir = make_stubs(tmp_path)
    home, seed = make_origin_and_clone(tmp_path)
    local_head = commit_locally(home / "work" / "repo-a")
    advance_origin(seed)
    r = run_init(home, bindir, "w1", "repo-a")
    assert r.returncode == 0, r.stderr
    assert worktree_head(home, "w1", "repo-a") == local_head
    assert "not refreshed: local commits" in r.stdout
    assert "refreshed to" not in r.stdout


def test_clone_already_at_origin_says_so(tmp_path):
    bindir = make_stubs(tmp_path)
    home, _seed = make_origin_and_clone(tmp_path)
    head = _git("rev-parse", "HEAD", cwd=home / "work" / "repo-a")
    r = run_init(home, bindir, "w1", "repo-a")
    assert r.returncode == 0, r.stderr
    assert worktree_head(home, "w1", "repo-a") == head
    assert "already at origin/main" in r.stdout


def test_detached_head_clone_is_left_alone_and_reported(tmp_path):
    bindir = make_stubs(tmp_path)
    home, seed = make_origin_and_clone(tmp_path)
    clone = home / "work" / "repo-a"
    old_head = _git("rev-parse", "HEAD", cwd=clone)
    _git("checkout", "-q", "--detach", "HEAD", cwd=clone)
    advance_origin(seed)
    r = run_init(home, bindir, "w1", "repo-a")
    assert r.returncode == 0, r.stderr
    assert worktree_head(home, "w1", "repo-a") == old_head
    assert "not refreshed: detached HEAD" in r.stdout


def test_unborn_clone_is_populated_from_origin(tmp_path):
    """A clone with an unborn HEAD must be populated by the refresh, not skipped.

    `git init` + `remote add` + `fetch` leaves the branch unborn: origin's commits are
    fetched, but HEAD points at a branch with no commit. Skipping the refresh there leaves
    `git worktree add` to fail with "not a valid object name: 'HEAD'", which under `set -e`
    kills the whole worker. `merge --ff-only` handles the unborn case itself, so the state
    that looks like "nothing to compare" is exactly the one that most needs the refresh.
    """
    bindir = make_stubs(tmp_path)
    home, seed = make_origin_and_clone(tmp_path)
    origin = tmp_path / "origin" / "repo-a.git"
    clone = home / "work" / "repo-a"
    # Rebuild ~/work/repo-a the `init` + `remote add` + `fetch` way: refs, no commits.
    shutil.rmtree(clone)
    clone.mkdir(parents=True)
    _git("init", "-q", "-b", "main", cwd=clone)
    _git("remote", "add", "origin", str(origin), cwd=clone)
    _git("fetch", "-q", "origin", cwd=clone)
    unborn = subprocess.run(["git", "rev-parse", "--verify", "--quiet", "HEAD"], cwd=str(clone),
                            capture_output=True, text=True, env={**os.environ, **GIT_ENV})
    assert unborn.returncode != 0, f"fixture is not unborn: {unborn.stdout!r}"
    origin_head = _git("rev-parse", "refs/remotes/origin/main", cwd=clone)

    r = run_init(home, bindir, "w1", "repo-a")
    assert r.returncode == 0, f"{r.stdout}\n{r.stderr}"
    assert (home / "work" / ".worktrees" / "w1" / "repo-a").is_dir(), r.stdout
    assert worktree_head(home, "w1", "repo-a") == origin_head
    assert "refreshed to origin/main" in r.stdout, r.stdout


def test_default_branch_is_not_assumed_to_be_main(tmp_path):
    """origin/HEAD missing must not fall back to `main` when the clone tracks `master`."""
    bindir = make_stubs(tmp_path)
    home, seed = make_origin_and_clone(tmp_path, branch="master")
    _git("symbolic-ref", "--delete", "refs/remotes/origin/HEAD", cwd=home / "work" / "repo-a")
    new_head = advance_origin(seed, branch="master")
    r = run_init(home, bindir, "w1", "repo-a")
    assert r.returncode == 0, r.stderr
    assert worktree_head(home, "w1", "repo-a") == new_head
    assert "refreshed to origin/master" in r.stdout


def test_fetch_runs_with_the_environment_from_env_secrets(tmp_path):
    """The fetch needs GITHUB_TOKEN: every workspace repo is a private HTTPS remote.

    worker-init.sh is streamed in as a bare `bash -s` — no login shell, no profile — so
    nothing puts the token in the environment unless the script sources ~/.env.secrets
    itself (as clone-repos.sh and install-plugins.sh already do). Stand-in for the token
    here: a url.<real>.insteadOf rewrite passed through git's own GIT_CONFIG_* env vars.
    The remote is unreachable unless that file reached the fetch's environment.
    """
    bindir = make_stubs(tmp_path)
    home, seed = make_origin_and_clone(tmp_path)
    real_origin = tmp_path / "origin" / "repo-a.git"
    _git("remote", "set-url", "origin", "https://example.invalid/repo-a.git",
         cwd=home / "work" / "repo-a")
    (home / ".env.secrets").write_text(
        "GIT_CONFIG_COUNT=1\n"
        f"GIT_CONFIG_KEY_0=url.{real_origin}.insteadOf\n"
        "GIT_CONFIG_VALUE_0=https://example.invalid/repo-a.git\n"
    )
    new_head = advance_origin(seed)
    r = run_init(home, bindir, "w1", "repo-a")
    assert r.returncode == 0, r.stderr
    assert "refreshed to origin/main" in r.stdout, r.stdout
    assert worktree_head(home, "w1", "repo-a") == new_head


@pytest.mark.parametrize("name", ["stack", "main"])
def test_reserved_worker_names_are_refused(tmp_path, name):
    """Two names belong to the main session and must never become workers.

    `stack` is the workspace stack's own compose project: the main session exports no
    COMPOSE_PROJECT_NAME, so compose names its project after ~/work/stack's basename, and
    worker-rm.sh treats a project of the worker's name running from that dir as the worker's
    own. `main` is the main tmux session's own name, so `floatie worker main` would hit the
    "session already exists" path and attach you to the main session instead of creating a
    worker (and a workspace may key its own per-worker state on the name too). Both pass the
    character rules, so the cheapest place to close them is here: refuse before anything is
    created.
    """
    bindir = make_stubs(tmp_path)
    home, _seed = make_origin_and_clone(tmp_path)
    r = run_init(home, bindir, name, "repo-a")
    assert r.returncode != 0
    assert "reserved worker name" in r.stderr, r.stderr
    assert not (home / "work" / ".worktrees" / name).exists(), "created the worker anyway"
    assert tmux_calls(tmp_path) == [], "got as far as tmux"


def test_script_is_valid_bash():
    r = subprocess.run(["bash", "-n", str(WORKER_INIT)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
