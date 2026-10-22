"""Behavioural tests over worker-rm.sh.

worker-rm.sh removes a worker: its tmux session, its Compose project, its
per-repo git worktrees under ~/work/.worktrees/<name>/<repo>, and the branch
floatie created for it.

The branch rule is the subtle part. worker-init.sh creates exactly one branch
per repo, `worker/<name>` — that is the only branch floatie owns. An agent
working in a worktree may switch it onto its own topic branch or detach HEAD, so
"the branch this worktree is on" and "the branch floatie created" are different
questions, and conflating them fails in both directions:

  * deleting only the branch HEAD points at leaks `worker/<name>`, and
    worker-init REUSES an existing `worker/<name>`, so the next worker of that
    name silently starts on the previous one's stale branch;
  * claiming a whole `<name>/*` namespace force-deletes human branches, since
    `<initials>/<topic>` is a common convention and worker names are arbitrary.

So: `worker/<name>` is deleted only after preservation checks; nothing else
is ever deleted, only reported. These tests pin both halves.

Compose teardown must also establish provenance. `docker compose -p <name>`
targets a project by name alone, and a stack started outside a worker takes its
project name from its directory basename — so a worker whose name collides with
one would tear down an unrelated stack.

docker and tmux are stubbed on PATH. The docker stub records its argv, and its
behaviour is steered by STUB_* env vars so the failure paths are reachable.
"""
import os
import pathlib
import subprocess
import shlex
import shutil

import pytest

WORKER_RM = pathlib.Path(__file__).resolve().parents[1] / "worker-rm.sh"

GIT_ENV = {
    "GIT_AUTHOR_NAME": "T", "GIT_AUTHOR_EMAIL": "t@e.com",
    "GIT_COMMITTER_NAME": "T", "GIT_COMMITTER_EMAIL": "t@e.com",
    # /dev/null as the global config: fixture commits must not inherit this machine's
    # ~/.gitconfig (commit.gpgsign=true fails wherever the signing key is absent).
    "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null",
}


def safe_path(bindir):
    """Keep stubs first, followed only by utilities needed by Git and hooks.

    Docker can share a directory with Bash/coreutils, so excluding whole PATH
    directories loses required tools. Never append the host PATH: removing a
    docker or tmux stub must not expose a real installation.
    """
    utilities = bindir.parent / "utility-bin"
    utilities.mkdir(exist_ok=True)
    for name in (
        "bash", "sh", "env", "git", "basename", "dirname", "cat", "find",
        "grep", "sed", "sort", "tr", "wc", "mkdir", "rm", "rmdir",
    ):
        link = utilities / name
        if not link.exists():
            executable = shutil.which(name)
            assert executable is not None, f"missing test utility: {name}"
            link.symlink_to(executable)
    return os.pathsep.join((str(bindir), str(utilities)))


def _git(*args, cwd):
    r = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True,
                       env={**os.environ, **GIT_ENV})
    assert r.returncode == 0, f"git {' '.join(args)} failed:\n{r.stderr}"
    return r.stdout.strip()


def make_stubs(tmp_path, log):
    """Stub docker + tmux on PATH.

    The docker stub records argv, notes whether the probed worktree still exists
    (for ordering assertions), answers the provenance query from STUB_*_DIR, and
    fails `down` when STUB_DOWN_FAILS is set.
    """
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    (bindir / "docker").write_text(f"""#!/bin/sh
echo "$*" >> "{log}"
if [ -n "$WT_PROBE" ]; then
  if [ -d "$WT_PROBE" ]; then echo "probe=worktree-present" >> "{log}"
  else echo "probe=worktree-gone" >> "{log}"; fi
fi
case "$*" in
  *working_dir*)
    [ -n "$STUB_OWN_DIR" ] && echo "$STUB_OWN_DIR"
    [ -n "$STUB_FOREIGN_DIR" ] && echo "$STUB_FOREIGN_DIR"
    exit 0 ;;
  *down*)
    [ -n "$STUB_DOWN_FAILS" ] && exit 1
    exit 0 ;;
esac
exit 0
""")
    (bindir / "tmux").write_text("#!/bin/sh\nexit 1\n")
    for f in bindir.iterdir():
        f.chmod(0o755)
    return bindir


def make_workspace(tmp_path, repos_branches, worker="w1", pre_branches=None):
    """Fake HOME with ~/work/<repo> repos and the worker's worktrees.

    repos_branches maps repo -> the branch the WORKTREE ends up on. Every repo
    also gets `worker/<worker>` created first, the way worker-init.sh does, so
    the two questions stay distinguishable.
    """
    home = tmp_path / "home"
    (home / "work").mkdir(parents=True, exist_ok=True)
    wdir = home / "work" / ".worktrees" / worker
    wdir.mkdir(parents=True, exist_ok=True)

    for repo, branch in repos_branches.items():
        rp = home / "work" / repo
        rp.mkdir()
        _git("init", "-q", "-b", "main", cwd=rp)
        (rp / "f.txt").write_text("x\n")
        _git("add", "f.txt", cwd=rp)
        _git("commit", "-qm", "init", cwd=rp)
        # Real, local remote backup; worker branches deliberately have no upstream.
        remote = tmp_path / f"{repo}.git"
        _git("init", "--bare", "-q", str(remote), cwd=tmp_path)
        _git("remote", "add", "origin", str(remote), cwd=rp)
        _git("push", "-q", "-u", "origin", "main", cwd=rp)
        for b in (pre_branches or {}).get(repo, []):
            _git("branch", b, cwd=rp)
        # worker-init.sh always creates worker/<name> and checks it out.
        _git("worktree", "add", "-q", str(wdir / repo), "-b", f"worker/{worker}", cwd=rp)
        if branch is None:                      # agent detached HEAD
            _git("checkout", "-q", "--detach", cwd=wdir / repo)
        elif branch != f"worker/{worker}":      # agent moved to another branch
            if branch in (pre_branches or {}).get(repo, []):
                _git("checkout", "-q", branch, cwd=wdir / repo)
            else:
                _git("checkout", "-q", "-b", branch, cwd=wdir / repo)
    return home


def run_worker_rm(home, bindir, log, worker="w1", probe=None, **stub):
    env = {**GIT_ENV, "HOME": str(home),
           "PATH": safe_path(bindir), "DOCKER_LOG": str(log)}
    if probe:
        env["WT_PROBE"] = str(probe)
    env.update({k: v for k, v in stub.items() if v})
    return subprocess.run(["bash", str(WORKER_RM), worker],
                          capture_output=True, text=True, env=env)


def branches(home, repo):
    return set(_git("branch", "--format=%(refname:short)",
                    cwd=home / "work" / repo).split())


def log_lines(log):
    return log.read_text().splitlines() if log.exists() else []


# ── the preserved branch floatie owns is deleted ────────────────────────────────

def test_floatie_branch_deleted_when_worktree_is_still_on_it(tmp_path):
    log = tmp_path / "d.log"; bindir = make_stubs(tmp_path, log)
    home = make_workspace(tmp_path, {"repo-a": "worker/w1"})
    r = run_worker_rm(home, bindir, log)
    assert r.returncode == 0, r.stderr
    assert "worker/w1" not in branches(home, "repo-a")


def test_floatie_branch_deleted_even_when_the_agent_switched_branches(tmp_path):
    """The regression that deleting-only-HEAD introduced.

    worker-init.sh reuses an existing worker/<name>, so leaking it means the
    next worker of that name starts on this worker's stale branch.
    """
    log = tmp_path / "d.log"; bindir = make_stubs(tmp_path, log)
    home = make_workspace(tmp_path, {"repo-a": "w1/agent-topic"})
    r = run_worker_rm(home, bindir, log)
    assert r.returncode == 0, r.stderr
    assert "worker/w1" not in branches(home, "repo-a"), (
        "leaked the branch floatie created; worker-init will reuse it"
    )


def test_floatie_branch_deleted_even_when_the_worktree_is_detached(tmp_path):
    log = tmp_path / "d.log"; bindir = make_stubs(tmp_path, log)
    home = make_workspace(tmp_path, {"repo-a": None})
    r = run_worker_rm(home, bindir, log)
    assert r.returncode == 0, r.stderr
    assert "worker/w1" not in branches(home, "repo-a")


# ── nothing else is ever deleted ─────────────────────────────────────────────

def test_agent_topic_branch_is_never_deleted(tmp_path):
    """It holds committed work floatie did not create. Report, don't delete."""
    log = tmp_path / "d.log"; bindir = make_stubs(tmp_path, log)
    home = make_workspace(tmp_path, {"repo-a": "w1/agent-topic"})
    r = run_worker_rm(home, bindir, log)
    assert "w1/agent-topic" in branches(home, "repo-a"), "destroyed the agent's branch"
    assert "w1/agent-topic" in (r.stdout + r.stderr), "left it behind without saying so"


def test_worker_name_prefixed_human_branch_is_never_deleted(tmp_path):
    """`just worker iman` must not force-delete a pre-existing `iman/*` branch.

    `<initials>/<topic>` is a common convention and worker names are arbitrary,
    so a `<name>/*` ownership rule would destroy months-old pushed work.
    """
    log = tmp_path / "d.log"; bindir = make_stubs(tmp_path, log)
    home = make_workspace(tmp_path, {"repo-a": "iman/big-refactor"},
                          worker="iman", pre_branches={"repo-a": ["iman/big-refactor"]})
    r = run_worker_rm(home, bindir, log, worker="iman")
    assert r.returncode == 0, r.stderr
    assert "iman/big-refactor" in branches(home, "repo-a"), (
        "force-deleted a human branch that merely shared the worker's prefix"
    )
    assert "worker/iman" not in branches(home, "repo-a")


def test_unrelated_branch_is_never_deleted(tmp_path):
    log = tmp_path / "d.log"; bindir = make_stubs(tmp_path, log)
    home = make_workspace(tmp_path, {"repo-a": "shared-feature"})
    r = run_worker_rm(home, bindir, log)
    assert "shared-feature" in branches(home, "repo-a")
    assert "shared-feature" in (r.stdout + r.stderr)


def test_mixed_repos(tmp_path):
    log = tmp_path / "d.log"; bindir = make_stubs(tmp_path, log)
    home = make_workspace(tmp_path, {
        "repo-a": "worker/w1",
        "repo-b": "w1/agent-topic",
        "repo-c": None,
    })
    r = run_worker_rm(home, bindir, log)
    assert r.returncode == 0, r.stderr
    for repo in ("repo-a", "repo-b", "repo-c"):
        assert "worker/w1" not in branches(home, repo), f"{repo} leaked worker/w1"
    assert "w1/agent-topic" in branches(home, "repo-b")
    assert not (home / "work" / ".worktrees" / "w1").exists()


# ── branch failure reports partial results while processing remaining repos ───────────────────

def test_undeletable_branch_does_not_strand_the_other_repos(tmp_path):
    """`git branch -D` fails if the branch is checked out in another worktree.

    Unguarded under `set -e` that aborts mid-loop, leaving later repos with
    their worktrees and branches intact and no indication which were processed.
    """
    log = tmp_path / "d.log"; bindir = make_stubs(tmp_path, log)
    home = make_workspace(tmp_path, {"repo-a": "worker/w1", "repo-b": "worker/w1"})
    # Something else has worker/w1 checked out, so -D will refuse in repo-a.
    _git("worktree", "add", "--force", "-q", str(tmp_path / "scratch"), "worker/w1",
         cwd=home / "work" / "repo-a")

    r = run_worker_rm(home, bindir, log)
    assert r.returncode != 0, "incomplete branch cleanup must report partial failure"
    assert "worker/w1" not in branches(home, "repo-b"), "repo-b was stranded"
    assert not (home / "work" / ".worktrees" / "w1").exists()
    assert "remaining" in (r.stdout + r.stderr).lower()


# ── compose teardown ─────────────────────────────────────────────────────────

def test_compose_down_is_invoked_with_remove_orphans(tmp_path):
    log = tmp_path / "d.log"; bindir = make_stubs(tmp_path, log)
    home = make_workspace(tmp_path, {"repo-a": "worker/w1"})
    run_worker_rm(home, bindir, log)
    downs = [l for l in log_lines(log) if " down" in l]
    assert downs, "compose project was never torn down"
    assert any("--remove-orphans" in l for l in downs), (
        f"down without --remove-orphans leaves stray containers: {downs}"
    )


def test_compose_down_never_destroys_volumes(tmp_path):
    log = tmp_path / "d.log"; bindir = make_stubs(tmp_path, log)
    home = make_workspace(tmp_path, {"repo-a": "worker/w1"})
    run_worker_rm(home, bindir, log)
    for l in log_lines(log):
        if "down" in l:
            assert " -v" not in l and "--volumes" not in l, f"would destroy volumes: {l!r}"


def test_compose_down_runs_even_when_nothing_is_running(tmp_path):
    """`down` needs neither a compose file nor running containers, and it also
    removes the project network. Gating it on a running container would skip a
    stopped project, leaking its containers and network.
    """
    log = tmp_path / "d.log"; bindir = make_stubs(tmp_path, log)
    home = make_workspace(tmp_path, {"repo-a": "worker/w1"})
    run_worker_rm(home, bindir, log)
    assert any(" down" in l for l in log_lines(log)), (
        "teardown was skipped when no container was running"
    )


def test_compose_down_runs_while_the_worktree_still_exists(tmp_path):
    """Containers can bind-mount the worktree, so ordering matters.

    Asserts the probe recorded immediately after the `down` invocation — not the
    first probe in the log, which belongs to an earlier query.
    """
    log = tmp_path / "d.log"; bindir = make_stubs(tmp_path, log)
    home = make_workspace(tmp_path, {"repo-a": "worker/w1"})
    wt = home / "work" / ".worktrees" / "w1" / "repo-a"
    run_worker_rm(home, bindir, log, probe=wt)

    lines = log_lines(log)
    idx = [i for i, l in enumerate(lines) if " down" in l]
    assert idx, "no down invocation recorded"
    after = lines[idx[0] + 1]
    assert after == "probe=worktree-present", (
        f"teardown ran after the worktree was deleted: {lines}"
    )


def test_foreign_compose_project_is_refused(tmp_path):
    """A stack started outside a worker takes its project name from its
    directory basename, so a worker whose name collides would tear down an
    unrelated stack. Nothing may be removed in that case.
    """
    log = tmp_path / "d.log"; bindir = make_stubs(tmp_path, log)
    home = make_workspace(tmp_path, {"repo-a": "worker/w1"})
    r = run_worker_rm(home, bindir, log,
                      STUB_FOREIGN_DIR=str(home / "work" / "repo-a"))

    assert r.returncode != 0, "tore down a project it does not own"
    assert not any(" down" in l for l in log_lines(log)), "issued down anyway"
    assert (home / "work" / ".worktrees" / "w1" / "repo-a").exists(), "removed worktrees anyway"
    assert "worker/w1" in branches(home, "repo-a")


def test_own_compose_project_is_torn_down(tmp_path):
    log = tmp_path / "d.log"; bindir = make_stubs(tmp_path, log)
    home = make_workspace(tmp_path, {"repo-a": "worker/w1"})
    wt = home / "work" / ".worktrees" / "w1" / "repo-a"
    r = run_worker_rm(home, bindir, log, STUB_OWN_DIR=str(wt))
    assert r.returncode == 0, r.stderr
    assert any(" down" in l for l in log_lines(log))


def test_teardown_failure_is_fatal_and_keeps_the_worktrees(tmp_path):
    """If the stack cannot be brought down, removing the worktrees pulls the
    bind-mount source out from under live containers and leaves them holding
    published host ports — a worse end state than doing nothing. Leaving the
    worktrees is recoverable by re-running; deleting them is not.
    """
    log = tmp_path / "d.log"; bindir = make_stubs(tmp_path, log)
    home = make_workspace(tmp_path, {"repo-a": "worker/w1"})
    r = run_worker_rm(home, bindir, log, STUB_DOWN_FAILS="1")

    assert r.returncode != 0, "reported success after teardown failed"
    assert (home / "work" / ".worktrees" / "w1" / "repo-a").exists(), (
        "deleted the worktree despite live containers bind-mounting it"
    )
    assert "worker/w1" in branches(home, "repo-a")


def test_missing_docker_does_not_break_removal(tmp_path):
    log = tmp_path / "d.log"; bindir = make_stubs(tmp_path, log)
    (bindir / "docker").unlink()
    home = make_workspace(tmp_path, {"repo-a": "worker/w1"})

    # Guard the premise: if a real docker were still reachable this test would
    # silently exercise the daemon instead of the no-docker path.
    probe = subprocess.run(["bash", "-c", "command -v docker"], capture_output=True,
                           text=True, env={"PATH": safe_path(bindir)})
    assert probe.returncode != 0, f"docker still on PATH at {probe.stdout.strip()}"

    r = run_worker_rm(home, bindir, log)
    assert r.returncode == 0, r.stderr
    assert not (home / "work" / ".worktrees" / "w1").exists()


# ── the dirty gate still comes first ─────────────────────────────────────────

def test_dirty_worktree_blocks_everything(tmp_path):
    log = tmp_path / "d.log"; bindir = make_stubs(tmp_path, log)
    home = make_workspace(tmp_path, {"repo-a": "worker/w1"})
    (home / "work" / ".worktrees" / "w1" / "repo-a" / "f.txt").write_text("dirty\n")

    r = run_worker_rm(home, bindir, log)
    assert r.returncode != 0, "dirty worktree did not block removal"
    assert (home / "work" / ".worktrees" / "w1" / "repo-a").exists()
    assert "worker/w1" in branches(home, "repo-a")
    assert not log.exists(), "invoked docker before the safety gate"


def test_script_is_valid_bash():
    r = subprocess.run(["bash", "-n", str(WORKER_RM)], capture_output=True, text=True)
    assert r.returncode == 0, f"worker-rm.sh failed to parse:\n{r.stderr}"


# Failure injection delegates every other operation to real temporary Git repos.
def stub_git(bindir, body):
    real_git = shlex.quote(shutil.which("git"))
    script = bindir / "git"
    script.write_text(f'#!/bin/bash\nREAL_GIT={real_git}\n{body}\nexec "$REAL_GIT" "$@"\n')
    script.chmod(0o755)


def stub_tmux(bindir, log, sessions):
    # tmux normally accepts prefixes: only a leading '=' requests an exact name.
    state = bindir / "sessions"
    state.write_text("\n".join(sessions) + "\n")
    script = bindir / "tmux"
    script.write_text(f"""#!/bin/bash
target="$3"
# An exact name wins before tmux tries prefix matching.
if grep -qxF -- "$target" {shlex.quote(str(state))}; then target="=$target"; fi
while IFS= read -r session; do
  [ -n "$session" ] || continue
  if [[ "$target" == =* ]]; then
    [ "$session" = "${{target#=}}" ] || continue
  else
    [[ "$session" == "$target"* ]] || continue
  fi
  if [ "$1" = kill-session ]; then
    echo "tmux-kill=$session" >> {shlex.quote(str(log))}
  fi
  exit 0
done < {shlex.quote(str(state))}
exit 1
""")
    script.chmod(0o755)


@pytest.mark.parametrize("locked_repo", ["repo-a", "repo-b"])
def test_locked_remove_preserves_path_and_branch_and_reports_partial(tmp_path, locked_repo):
    log = tmp_path / "calls"; bindir = make_stubs(tmp_path, log)
    home = make_workspace(tmp_path, {"repo-a": "worker/w1", "repo-b": "worker/w1"})
    wt = home / "work/.worktrees/w1" / locked_repo
    _git("worktree", "lock", str(wt), cwd=home / "work" / locked_repo)
    result = run_worker_rm(home, bindir, log)
    output = result.stdout + result.stderr
    assert result.returncode != 0
    assert wt.exists() and "worker/w1" in branches(home, locked_repo)
    assert "completed" in output.lower() and "remaining" in output.lower()
    assert "Nothing has been removed" not in output
    other = "repo-b" if locked_repo == "repo-a" else "repo-a"
    assert not (home / "work/.worktrees/w1" / other).exists()


@pytest.mark.parametrize("saved", [True, False])
def test_prefix_session_is_never_killed(tmp_path, saved):
    log = tmp_path / "calls"; bindir = make_stubs(tmp_path, log)
    home = make_workspace(tmp_path, {"repo-a": "worker/review"}, worker="review")
    if not saved:
        wt = home / "work/.worktrees/review/repo-a"
        _git("worktree", "remove", str(wt), cwd=home / "work/repo-a")
        wt.parent.rmdir()  # known orphan: backed-up owned branch still exists
    stub_tmux(bindir, log, ["review-other"])
    result = run_worker_rm(home, bindir, log, worker="review")
    assert result.returncode == 0, result.stderr
    assert not any("tmux-kill" in line for line in log_lines(log))


def test_unknown_session_alone_is_not_removal_authority(tmp_path):
    log = tmp_path / "calls"; bindir = make_stubs(tmp_path, log)
    home = tmp_path / "home"; (home / "work").mkdir(parents=True)
    stub_tmux(bindir, log, ["w1"])
    result = run_worker_rm(home, bindir, log)
    assert result.returncode != 0
    assert log_lines(log) == []


def test_backed_up_custom_head_without_owned_branch_can_be_removed(tmp_path):
    log = tmp_path / "calls"; bindir = make_stubs(tmp_path, log)
    home = make_workspace(tmp_path, {"repo-a": "custom"})
    _git("branch", "-D", "worker/w1", cwd=home / "work/repo-a")
    result = run_worker_rm(home, bindir, log)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "custom" in branches(home, "repo-a")
    assert not (home / "work/.worktrees/w1").exists()
