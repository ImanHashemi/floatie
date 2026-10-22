"""worker-rm.sh runs a workspace-provided hook, ~/work/stack/hooks/worker-rm <name>, after the
preservation gate and before anything is removed. floatie does not interpret it; a failing
hook aborts unless force=true. Reuses the docker/tmux stubs from test_worker_rm.
"""
import subprocess
import pathlib
import shlex

import pytest

# Sibling module, imported the way pytest's prepend import mode exposes it (tests/ is on
# sys.path). A `tests.` prefix would rely on tests/ resolving as a namespace package, which
# any installed distribution shipping a top-level `tests` package silently shadows.
from test_worker_rm import (WORKER_RM, GIT_ENV, make_stubs, make_workspace,
                            safe_path, branches, log_lines, _git, stub_git, stub_tmux)


def install_hook(home, log, exit_code=0):
    hooks = home / "work" / "stack" / "hooks"
    hooks.mkdir(parents=True, exist_ok=True)
    hook = hooks / "worker-rm"
    hook.write_text(f"""#!/bin/sh
echo "hook worker=$1 cwd=$(pwd)" >> "{log}"
[ -n "$WT_PROBE" ] && [ -d "$WT_PROBE" ] && echo "hook-sees-worktree" >> "{log}"
exit {exit_code}
""")
    hook.chmod(0o755)
    return hook


def run_rm(home, bindir, log, worker="w1", force=None, probe=None, streamed=False, **stub):
    env = {**GIT_ENV, "HOME": str(home), "PATH": safe_path(bindir), "DOCKER_LOG": str(log)}
    if probe:
        env["WT_PROBE"] = str(probe)
    env.update({k: v for k, v in stub.items() if v})
    args = ["bash", "-s", "--"] if streamed else ["bash", str(WORKER_RM)]
    args += [worker] + ([force] if force else [])
    return subprocess.run(args, input=WORKER_RM.read_text() if streamed else None,
                          capture_output=True, text=True, env=env)


def test_no_hook_behaves_as_before(tmp_path):
    log = tmp_path / "d.log"; bindir = make_stubs(tmp_path, log)
    home = make_workspace(tmp_path, {"repo-a": "worker/w1"})
    r = run_rm(home, bindir, log)
    assert r.returncode == 0, r.stderr
    assert "worker/w1" not in branches(home, "repo-a")
    assert not any(l.startswith("hook ") for l in log_lines(log))


def test_hook_runs_with_worker_name_from_the_stack_dir(tmp_path):
    log = tmp_path / "d.log"; bindir = make_stubs(tmp_path, log)
    home = make_workspace(tmp_path, {"repo-a": "worker/w1"})
    install_hook(home, log)
    r = run_rm(home, bindir, log)
    assert r.returncode == 0, r.stderr
    hooks = [l for l in log_lines(log) if l.startswith("hook ")]
    assert hooks == [f"hook worker=w1 cwd={home / 'work' / 'stack'}"]


def test_hook_runs_before_docker_and_while_worktrees_exist(tmp_path):
    log = tmp_path / "d.log"; bindir = make_stubs(tmp_path, log)
    home = make_workspace(tmp_path, {"repo-a": "worker/w1"})
    install_hook(home, log)
    wt = home / "work" / ".worktrees" / "w1" / "repo-a"
    r = run_rm(home, bindir, log, probe=wt)
    assert r.returncode == 0, r.stderr
    lines = log_lines(log)
    first_hook = next(i for i, l in enumerate(lines) if l.startswith("hook "))
    first_docker = next((i for i, l in enumerate(lines) if l.startswith(("ps ", "compose "))), len(lines))
    assert first_hook < first_docker, f"hook ran after docker: {lines}"
    assert "hook-sees-worktree" in lines


def test_failing_hook_aborts_and_removes_nothing(tmp_path):
    log = tmp_path / "d.log"; bindir = make_stubs(tmp_path, log)
    home = make_workspace(tmp_path, {"repo-a": "worker/w1"})
    install_hook(home, log, exit_code=3)
    r = run_rm(home, bindir, log)
    assert r.returncode != 0
    assert "hook" in r.stderr and "worker-rm w1 true" in r.stderr
    assert (home / "work" / ".worktrees" / "w1" / "repo-a").exists()
    assert "worker/w1" in branches(home, "repo-a")
    assert not any(" down" in l for l in log_lines(log)), "issued compose down after a failed hook"


def test_force_continues_past_a_failing_hook(tmp_path):
    log = tmp_path / "d.log"; bindir = make_stubs(tmp_path, log)
    home = make_workspace(tmp_path, {"repo-a": "worker/w1"})
    install_hook(home, log, exit_code=3)
    r = run_rm(home, bindir, log, force="true")
    assert r.returncode == 0, r.stderr
    assert "WARN" in r.stderr
    assert not (home / "work" / ".worktrees" / "w1").exists()


def test_dirty_gate_still_comes_before_the_hook(tmp_path):
    log = tmp_path / "d.log"; bindir = make_stubs(tmp_path, log)
    home = make_workspace(tmp_path, {"repo-a": "worker/w1"})
    install_hook(home, log)
    (home / "work" / ".worktrees" / "w1" / "repo-a" / "f.txt").write_text("dirty\n")
    r = run_rm(home, bindir, log)
    assert r.returncode != 0
    assert not log.exists(), "hook ran before the safety gate"


def bare_home(tmp_path):
    """A HOME with a preserved owned branch but no remaining worker worktrees.

    Not make_workspace: that always creates ~/work/.worktrees/<worker>, which is the
    opposite of the case under test.
    """
    home = make_workspace(tmp_path, {"repo-a": "worker/w1"})
    wt = home / "work/.worktrees/w1/repo-a"
    _git("worktree", "remove", str(wt), cwd=home / "work/repo-a")
    wt.parent.rmdir()  # preserved owned branch identifies a known orphan
    return home


@pytest.mark.parametrize("streamed", [False, True], ids=["direct", "streamed"])
@pytest.mark.parametrize("force", ["false", "true"])
@pytest.mark.parametrize("clones", [False, True], ids=["no-clones", "no-owned-branch"])
def test_empty_unknown_parent_never_authorizes_teardown(tmp_path, streamed, force, clones):
    log = tmp_path / "calls"; bindir = make_stubs(tmp_path, log)
    if clones:
        home = bare_home(tmp_path)
        _git("branch", "-d", "worker/w1", cwd=home / "work/repo-a")
        parent = home / "work/.worktrees/w1"
        parent.mkdir()
    else:
        home = make_workspace(tmp_path, {})
        parent = home / "work/.worktrees/w1"
    install_hook(home, log); stub_tmux(bindir, log, ["w1", "w1-other"])
    result = run_rm(home, bindir, log, force=force, streamed=streamed)
    assert (result.returncode != 0, parent.is_dir(), log_lines(log)) == (True, True, []), (
        result.stdout + result.stderr
    )
    assert list(parent.iterdir()) == []


@pytest.mark.parametrize("streamed", [False, True], ids=["direct", "streamed"])
@pytest.mark.parametrize("force", ["false", "true"])
@pytest.mark.parametrize("empty_parent", [False, True], ids=["absent-parent", "empty-parent"])
def test_preserved_owned_branch_authorizes_only_orphan_cleanup(tmp_path, streamed, force, empty_parent):
    log = tmp_path / "calls"; bindir = make_stubs(tmp_path, log)
    home = bare_home(tmp_path)
    parent = home / "work/.worktrees/w1"
    if empty_parent:
        parent.mkdir()
    tip = _git("rev-parse", "worker/w1", cwd=home / "work/repo-a")
    install_hook(home, log); stub_tmux(bindir, log, ["w1-other", "w1"])
    result = run_rm(home, bindir, log, force=force, streamed=streamed)
    assert result.returncode == 0, result.stdout + result.stderr
    assert log_lines(log) == [f"hook worker=w1 cwd={home / 'work/stack'}", "tmux-kill=w1"]
    assert _git("rev-parse", "worker/w1", cwd=home / "work/repo-a") == tip
    assert parent.exists() == empty_parent


def test_hook_runs_even_without_worktrees(tmp_path):
    """A worker's stack outlives a half-finished removal, or a hand-deleted
    ~/work/.worktrees/<name>. Exiting early there would leave the stack up forever.
    """
    log = tmp_path / "d.log"; bindir = make_stubs(tmp_path, log)
    home = bare_home(tmp_path)
    install_hook(home, log)
    assert not (home / "work" / ".worktrees" / "w1").exists()
    r = run_rm(home, bindir, log)
    assert r.returncode == 0, r.stderr
    hooks = [l for l in log_lines(log) if l.startswith("hook ")]
    assert hooks == [f"hook worker=w1 cwd={home / 'work' / 'stack'}"]


def test_failing_hook_without_worktrees_aborts(tmp_path):
    log = tmp_path / "d.log"; bindir = make_stubs(tmp_path, log)
    home = bare_home(tmp_path)
    install_hook(home, log, exit_code=3)
    r = run_rm(home, bindir, log)
    assert r.returncode != 0
    assert "hook" in r.stderr and "worker-rm w1 true" in r.stderr


def test_non_executable_hook_is_skipped_with_a_warning(tmp_path):
    """Skipping in silence is indistinguishable from having no hook at all.

    A hook that lost its +x — a fresh checkout, a copy, an editor — would leave the stack it
    was meant to tear down standing, with nothing in the output to explain why. Removal still
    proceeds; the WARN goes to stdout, like the other notes on a succeeding path.
    """
    log = tmp_path / "d.log"; bindir = make_stubs(tmp_path, log)
    home = make_workspace(tmp_path, {"repo-a": "worker/w1"})
    hook = install_hook(home, log)
    hook.chmod(0o644)
    r = run_rm(home, bindir, log)
    assert r.returncode == 0, r.stderr
    assert not any(l.startswith("hook ") for l in log_lines(log)), "ran a non-executable hook"
    assert f"WARN: {hook} exists but is not executable" in r.stdout, r.stdout
    assert not (home / "work" / ".worktrees" / "w1").exists(), "removal did not proceed"


def test_a_stdin_reading_hook_does_not_truncate_the_script(tmp_path):
    """floatie feeds this script to `bash -s` on stdin; a hook that reads stdin
    would swallow the rest of it and exit 0 having removed nothing."""
    log = tmp_path / "d.log"; bindir = make_stubs(tmp_path, log)
    home = make_workspace(tmp_path, {"repo-a": "worker/w1"})
    hook = install_hook(home, log)
    hook.write_text('#!/bin/sh\ncat > /dev/null\nexit 0\n'); hook.chmod(0o755)
    env = {**GIT_ENV, "HOME": str(home), "PATH": safe_path(bindir), "DOCKER_LOG": str(log)}
    with open(WORKER_RM) as f:
        r = subprocess.run(["bash", "-s", "--", "w1", "false"], stdin=f, capture_output=True, text=True, env=env)
    assert r.returncode == 0, r.stderr
    assert not (home / "work" / ".worktrees" / "w1").exists()
    assert "worker/w1" not in branches(home, "repo-a")


def test_force_reaches_teardown_when_the_stack_is_the_workspace_stack(tmp_path):
    """A failed hook leaves the worker's containers up, and a workspace stack runs
    from ~/work/stack — where floatie itself puts it — under the project name floatie
    exported. Refusing that as foreign dead-ends force=true: the hook did not tear the
    project down, floatie then declines to either, and the advice is to go stop a stack
    that is this worker's own.
    """
    log = tmp_path / "d.log"; bindir = make_stubs(tmp_path, log)
    home = make_workspace(tmp_path, {"repo-a": "worker/w1"})
    install_hook(home, log, exit_code=3)
    r = run_rm(home, bindir, log, force="true",
               STUB_OWN_DIR=str(home / "work" / "stack"))
    assert r.returncode == 0, r.stderr
    assert any(" down" in l for l in log_lines(log)), "refused this worker's own stack as foreign"
    assert not (home / "work" / ".worktrees" / "w1").exists()


@pytest.mark.parametrize("name", ["stack", "main"])
def test_reserved_worker_names_are_refused(tmp_path, name):
    """Removal is the dangerous half: both reserved names name the main session's own things.

    The main session exports no COMPOSE_PROJECT_NAME, so compose names its project after
    ~/work/stack's basename — `stack`. Phase 2b accepts a project of the worker's name running
    from that dir as the worker's own, so `worker-rm stack` would bring the main session's
    stack down. `main` is the main tmux session's own name, so `worker-rm main` would kill the
    session you are working in. Refuse both before the gate, the hook or docker — a worker of
    either name should not exist to be removed.
    """
    log = tmp_path / "d.log"; bindir = make_stubs(tmp_path, log)
    home = make_workspace(tmp_path, {"repo-a": f"worker/{name}"}, worker=name)
    install_hook(home, log)
    r = run_rm(home, bindir, log, worker=name)
    assert r.returncode != 0
    assert "reserved worker name" in r.stderr, r.stderr
    assert (home / "work" / ".worktrees" / name / "repo-a").exists(), "removed the worktree"
    assert f"worker/{name}" in branches(home, "repo-a"), "deleted the branch"
    assert log_lines(log) == [], "ran the hook or touched docker"


def test_force_continues_on_the_no_worktrees_path(tmp_path):
    log = tmp_path / "d.log"; bindir = make_stubs(tmp_path, log)
    home = bare_home(tmp_path)
    install_hook(home, log, exit_code=3)
    r = run_rm(home, bindir, log, force="true")
    assert r.returncode == 0, r.stderr
    assert "WARN" in r.stderr


@pytest.mark.parametrize("force", ["false", "true"])
@pytest.mark.parametrize("unsafe", ["dirty", "unpushed-head", "unpushed-owned", "no-remote",
                                     "status", "ls-files", "rev-list", "for-each-ref", "worktree list",
                                     "corrupt", "unregistered", "parent-file", "parent-hidden",
                                     "parent-symlink", "repo-symlink", "parent-as-file"])
def test_preservation_blocks_before_all_mutations(tmp_path, force, unsafe):
    log = tmp_path / "calls"; bindir = make_stubs(tmp_path, log)
    home = make_workspace(tmp_path, {"repo-a": "worker/w1", "repo-b": "worker/w1"})
    wt = home / "work/.worktrees/w1/repo-b"; repo = home / "work/repo-b"
    install_hook(home, log)
    stub_tmux(bindir, log, ["w1"])
    if unsafe == "dirty":
        (wt / "new.txt").write_text("unsaved")
    elif unsafe.startswith("unpushed"):
        (wt / "f.txt").write_text("local only")
        _git("commit", "-qam", "local", cwd=wt)
        if unsafe == "unpushed-owned":
            _git("checkout", "-qb", "custom", "origin/main", cwd=wt)
        else:
            _git("branch", "--set-upstream-to=origin/main", cwd=wt)
    elif unsafe == "no-remote":
        _git("remote", "remove", "origin", cwd=repo)
    elif unsafe in ("status", "ls-files", "rev-list", "for-each-ref", "worktree list"):
        stub_git(bindir, f'case " $* " in *" {unsafe} "*) exit 71;; esac')
    elif unsafe == "corrupt":
        (wt / ".git").write_text("gitdir: /nonexistent/task1-gitdir\n")
    elif unsafe == "unregistered":
        _git("worktree", "remove", str(wt), cwd=repo)
        wt.mkdir(); (wt / "kept.txt").write_text("unknown")
    elif unsafe in ("parent-file", "parent-hidden"):
        (wt.parent / ("notes" if unsafe == "parent-file" else ".notes")).write_text("keep")
    elif unsafe == "parent-symlink":
        (wt.parent / "external").symlink_to(repo, target_is_directory=True)
    elif unsafe == "repo-symlink":
        _git("worktree", "move", str(wt), str(tmp_path / "moved"), cwd=repo)
        wt.symlink_to(tmp_path / "moved", target_is_directory=True)
    else:
        # A missing directory must not route an existing file through orphan teardown.
        for name in ("repo-a", "repo-b"):
            _git("worktree", "remove", str(wt.parent / name), cwd=home / "work" / name)
        wt.parent.rmdir(); wt.parent.write_text("keep")
    result = run_rm(home, bindir, log, force=force)
    assert result.returncode != 0, result.stdout + result.stderr
    assert log_lines(log) == [], "hook/session/docker ran before preservation gate"
    assert wt.parent.exists() and "worker/w1" in branches(home, "repo-b")


@pytest.mark.parametrize("change", ["dirty", "owned-tip", "remote-evidence", "parent-extra"])
def test_hook_changes_are_rechecked_before_deletion(tmp_path, change):
    log = tmp_path / "calls"; bindir = make_stubs(tmp_path, log)
    home = make_workspace(tmp_path, {"repo-a": "custom"})
    wt = home / "work/.worktrees/w1/repo-a"; repo = home / "work/repo-a"
    hook = install_hook(home, log)
    if change == "dirty":
        command = f'echo dirty > {shlex.quote(str(wt / "f.txt"))}'
    elif change == "parent-extra":
        command = f'echo keep > {shlex.quote(str(wt.parent / "notes"))}'
    else:
        _git("checkout", "-qb", "another", cwd=repo)
        (repo / "f.txt").write_text("second")
        _git("commit", "-qam", "second", cwd=repo)
        _git("push", "-q", "origin", "another", cwd=repo)
        ref = "refs/heads/worker/w1" if change == "owned-tip" else "refs/remotes/origin/main"
        command = f'git -C {shlex.quote(str(repo))} update-ref {ref} HEAD'
    hook.write_text(f"#!/bin/sh\n{command}\n")
    result = run_rm(home, bindir, log)
    assert result.returncode != 0, result.stdout + result.stderr
    output = result.stdout + result.stderr
    assert "remaining" in output.lower() and "Nothing has been removed" not in output
    if change == "parent-extra":
        assert (wt.parent / "notes").exists()
    else:
        assert wt.exists() and "worker/w1" in branches(home, "repo-a")


@pytest.mark.parametrize("failure", ["hook", "docker", "foreign"])
def test_post_hook_failure_reports_possible_effects(tmp_path, failure):
    log = tmp_path / "calls"; bindir = make_stubs(tmp_path, log)
    home = make_workspace(tmp_path, {"repo-a": "worker/w1"})
    install_hook(home, log, exit_code=3 if failure == "hook" else 0)
    stub_tmux(bindir, log, ["w1"])
    result = run_rm(home, bindir, log,
                    STUB_DOWN_FAILS="1" if failure == "docker" else "",
                    STUB_FOREIGN_DIR=str(tmp_path) if failure == "foreign" else "")
    assert result.returncode != 0
    output = result.stdout + result.stderr
    assert "completed" in output.lower() and "remaining" in output.lower()
    assert "Nothing has been removed" not in output


@pytest.mark.parametrize("unsafe", ["missing-registered", "unregistered-clean", "enumeration-failure"])
def test_incomplete_inventory_blocks_all_mutations(tmp_path, unsafe):
    log = tmp_path / "calls"; bindir = make_stubs(tmp_path, log)
    home = make_workspace(tmp_path, {"repo-a": "worker/w1", "repo-b": "worker/w1"})
    wt = home / "work/.worktrees/w1/repo-b"; repo = home / "work/repo-b"
    install_hook(home, log); stub_tmux(bindir, log, ["w1"])
    if unsafe == "missing-registered":
        wt.rename(tmp_path / "moved-without-git")
    elif unsafe == "unregistered-clean":
        _git("worktree", "remove", str(wt), cwd=repo)
        _git("clone", "-q", str(repo), str(wt), cwd=tmp_path)
    else:
        (bindir / "find").write_text("#!/bin/sh\nexit 71\n")
        (bindir / "find").chmod(0o755)
    result = run_rm(home, bindir, log)
    assert result.returncode != 0
    assert log_lines(log) == []
    assert "worker/w1" in branches(home, "repo-a")
    assert "worker/w1" in branches(home, "repo-b")


@pytest.mark.parametrize("changed", ["owned-tip", "backup-tip"])
def test_branch_rechecked_after_successful_worktree_removal(tmp_path, changed):
    log = tmp_path / "calls"; bindir = make_stubs(tmp_path, log)
    home = make_workspace(tmp_path, {"repo-a": "worker/w1"})
    wt = home / "work/.worktrees/w1/repo-a"; repo = home / "work/repo-a"
    (repo / "f.txt").write_text("next")
    _git("commit", "-qam", "next", cwd=repo)
    _git("push", "-q", "origin", "main:next", cwd=repo)
    new_tip = _git("rev-parse", "HEAD", cwd=repo)
    ref = "refs/heads/worker/w1" if changed == "owned-tip" else "refs/remotes/origin/main"
    stub_git(bindir, f'''if [ "$3 $4" = "worktree remove" ]; then
  "$REAL_GIT" "$@" || exit $?
  "$REAL_GIT" -C {shlex.quote(str(repo))} update-ref {ref} {new_tip}
  exit $?
fi''')
    result = run_rm(home, bindir, log)
    assert result.returncode != 0, result.stdout + result.stderr
    assert not wt.exists() and "worker/w1" in branches(home, "repo-a")
    assert "remaining" in (result.stdout + result.stderr).lower()


@pytest.mark.parametrize("head_kind", ["custom", "detached"])
def test_no_upstream_unreachable_head_blocks_even_when_owned_branch_is_backed_up(tmp_path, head_kind):
    log = tmp_path / "calls"; bindir = make_stubs(tmp_path, log)
    home = make_workspace(tmp_path, {"repo-a": "custom" if head_kind == "custom" else None})
    wt = home / "work/.worktrees/w1/repo-a"
    (wt / "f.txt").write_text("unpushed")
    _git("commit", "-qam", "unpushed", cwd=wt)
    install_hook(home, log); stub_tmux(bindir, log, ["w1"])
    result = run_rm(home, bindir, log)
    assert result.returncode != 0 and log_lines(log) == []
    assert wt.exists() and "worker/w1" in branches(home, "repo-a")


def test_docker_provenance_probe_failure_never_authorizes_teardown(tmp_path):
    log = tmp_path / "calls"; bindir = make_stubs(tmp_path, log)
    home = make_workspace(tmp_path, {"repo-a": "worker/w1"})
    install_hook(home, log)
    docker = bindir / "docker"
    docker.write_text('#!/bin/sh\nif [ "$1" = ps ]; then exit 71; fi\necho "unexpected down" >> "$DOCKER_LOG"\n')
    result = run_rm(home, bindir, log)
    assert result.returncode != 0
    assert "unexpected down" not in log_lines(log)
    assert (home / "work/.worktrees/w1/repo-a").exists()


@pytest.mark.parametrize("invocation", ["installed", "streamed"])
@pytest.mark.parametrize("orphan", [False, True])
def test_exact_session_removed_in_both_invocation_shapes(tmp_path, invocation, orphan):
    log = tmp_path / "calls"; bindir = make_stubs(tmp_path, log)
    home = bare_home(tmp_path) if orphan else make_workspace(tmp_path, {"repo-a": "worker/w1"})
    install_hook(home, log)
    stub_tmux(bindir, log, ["w1-other", "w1"])
    env = {**GIT_ENV, "HOME": str(home), "PATH": safe_path(bindir), "DOCKER_LOG": str(log)}
    if invocation == "installed":
        installed = bindir / "floatie-worker-rm"
        installed.write_bytes(WORKER_RM.read_bytes()); installed.chmod(0o755)
        result = subprocess.run([str(installed), "w1", "false"], capture_output=True, text=True, env=env)
    else:
        result = subprocess.run(["bash", "-s", "--", "w1", "false"], input=WORKER_RM.read_text(),
                                capture_output=True, text=True, env=env)
    assert result.returncode == 0, result.stdout + result.stderr
    assert [line for line in log_lines(log) if line.startswith("tmux-kill=")] == ["tmux-kill=w1"]
    assert not (home / "work/.worktrees/w1").exists()
    if not orphan:
        assert "worker/w1" not in branches(home, "repo-a")


@pytest.mark.parametrize("flag", ["--assume-unchanged", "--skip-worktree"])
@pytest.mark.parametrize("force", ["false", "true"])
def test_hidden_index_flags_refuse_removal_without_changing_index(tmp_path, flag, force):
    log = tmp_path / "calls"; bindir = make_stubs(tmp_path, log)
    home = make_workspace(tmp_path, {"repo-a": "worker/w1"})
    wt = home / "work/.worktrees/w1/repo-a"
    _git("update-index", flag, "f.txt", cwd=wt)
    (wt / "f.txt").write_text("hidden local content\n")
    assert _git("status", "--porcelain", cwd=wt) == "", "fixture must hide the edit"
    index = pathlib.Path(_git("rev-parse", "--git-path", "index", cwd=wt))
    before = index.read_bytes()
    install_hook(home, log); stub_tmux(bindir, log, ["w1"])
    result = run_rm(home, bindir, log, force=force)
    assert result.returncode != 0, result.stdout + result.stderr
    assert log_lines(log) == [], "hidden work reached hook/session/stack mutation"
    assert (wt / "f.txt").read_text() == "hidden local content\n"
    assert "worker/w1" in branches(home, "repo-a")
    assert index.read_bytes() == before, "preservation probe changed the user's index"


@pytest.mark.parametrize("force", ["false", "true"])
@pytest.mark.parametrize("corruption", ["sibling-pointer", "duplicate-backlink", "missing-backlink", "wrong-backlink"])
def test_worktree_metadata_identity_blocks_before_mutations(tmp_path, force, corruption):
    log = tmp_path / "calls"; bindir = make_stubs(tmp_path, log)
    home = make_workspace(tmp_path, {"repo-a": "worker/w1"})
    wt = home / "work/.worktrees/w1/repo-a"; repo = home / "work/repo-a"
    _git("checkout", "-q", "--detach", cwd=wt)
    (wt / "f.txt").write_text("unpublished detached content\n")
    _git("commit", "-qam", "detached unpublished work", cwd=wt)
    detached_tip = _git("rev-parse", "HEAD", cwd=wt)
    own_metadata = pathlib.Path(_git("rev-parse", "--absolute-git-dir", cwd=wt))
    sibling = tmp_path / "sibling"
    _git("worktree", "add", "-q", "--detach", str(sibling), "origin/main", cwd=repo)
    if corruption in ("sibling-pointer", "duplicate-backlink"):
        sibling_metadata = pathlib.Path(_git("rev-parse", "--absolute-git-dir", cwd=sibling))
        (wt / ".git").write_bytes((sibling / ".git").read_bytes())
        if corruption == "duplicate-backlink":
            (sibling_metadata / "gitdir").write_text(str(wt / ".git") + "\n")
        # Match the sibling index deterministically; the target's original
        # index and detached unpublished HEAD remain different and must survive.
        (wt / "f.txt").write_bytes((sibling / "f.txt").read_bytes())
        # The old top-level/common-dir/status checks all pass while reading the
        # sibling's index and HEAD, hiding the target's unpublished detached commit.
        assert _git("rev-parse", "--show-toplevel", cwd=wt) == str(wt)
        assert _git("status", "--porcelain", cwd=wt) == ""
    elif corruption == "missing-backlink":
        (own_metadata / "gitdir").unlink()
    else:
        (own_metadata / "gitdir").write_text(str(sibling / ".git") + "\n")
    original_index = (own_metadata / "index").read_bytes()
    pointer = (wt / ".git").read_bytes()
    install_hook(home, log); stub_tmux(bindir, log, ["w1"])
    result = run_rm(home, bindir, log, force=force)
    assert result.returncode != 0
    assert log_lines(log) == [], "corrupt metadata reached hook/session/stack mutation"
    assert wt.exists() and sibling.exists() and "worker/w1" in branches(home, "repo-a")
    assert (wt / ".git").read_bytes() == pointer
    assert (own_metadata / "index").read_bytes() == original_index
    assert (own_metadata / "HEAD").read_text().strip() == detached_tip
