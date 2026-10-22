import json, os, subprocess
from pathlib import Path

import pytest
from test_worker_rm import GIT_ENV, make_stubs, stub_git, safe_path

FLOATIE = Path(__file__).resolve().parent.parent
GUARD = FLOATIE / "lifecycle-guard.sh"


def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                   env={**os.environ, **GIT_ENV})


def _init_repo(path):
    """Init a repo with one commit and NO remote configured."""
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-q")
    _git(path, "config", "user.email", "t@t")
    _git(path, "config", "user.name", "t")
    (path / "f.txt").write_text("hello\n")
    _git(path, "add", "-A")
    _git(path, "commit", "-qm", "init")


def _add_remote_and_push(path, bare):
    subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
    _git(path, "remote", "add", "origin", str(bare))
    _git(path, "push", "-q", "-u", "origin", "HEAD")


def _run_json(work_dir, **extra):
    env = {**os.environ, **GIT_ENV, "FLOATIE_WORK_DIR": str(work_dir), **extra}
    out = subprocess.run(["bash", str(GUARD), "--json"], env=env, capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def test_json_reports_clean_and_dirty(tmp_path):
    work = tmp_path / "work"
    clean = work / "clean-repo"
    dirty = work / "dirty-repo"
    _init_repo(clean); _add_remote_and_push(clean, tmp_path / "clean.git")
    _init_repo(dirty); _add_remote_and_push(dirty, tmp_path / "dirty.git")
    (dirty / "f.txt").write_text("changed\n")          # uncommitted change

    items = _run_json(work)
    by_label = {i["label"]: i for i in items}
    c = by_label["main repo 'clean-repo'"]
    d = by_label["main repo 'dirty-repo'"]
    assert c["clean"] is True and c["uncommitted"] == 0 and c["unpushed"] == 0
    assert d["clean"] is False and d["uncommitted"] == 1


def test_json_counts_unpushed(tmp_path):
    work = tmp_path / "work"
    repo = work / "ahead-repo"
    _init_repo(repo); _add_remote_and_push(repo, tmp_path / "remote.git")
    (repo / "f.txt").write_text("v2\n")
    _git(repo, "commit", "-qam", "second")             # 1 commit ahead of upstream

    items = _run_json(work)
    ahead = next(i for i in items if i["label"] == "main repo 'ahead-repo'")
    assert ahead["unpushed"] == 1 and ahead["clean"] is False


def test_json_no_remote_commits_are_unpushed(tmp_path):
    """A repo with local commits and NO remote is at-risk -> not clean (guard protects it)."""
    work = tmp_path / "work"
    _init_repo(work / "local-only")
    items = _run_json(work)
    lo = next(i for i in items if i["label"] == "main repo 'local-only'")
    assert lo["clean"] is False and lo["unpushed"] >= 1


def test_default_mode_unchanged_clean(tmp_path):
    work = tmp_path / "work"
    clean = work / "clean-repo"
    _init_repo(clean); _add_remote_and_push(clean, tmp_path / "clean.git")
    env = {**os.environ, "FLOATIE_WORK_DIR": str(work)}
    out = subprocess.run(["bash", str(GUARD)], env=env, capture_output=True, text=True)
    assert out.returncode == 0
    assert "all worktrees are clean" in out.stdout


def test_default_mode_flags_no_remote_commits(tmp_path):
    """Default guard must still BLOCK (exit 1) on no-remote local commits."""
    work = tmp_path / "work"
    _init_repo(work / "local-only")
    env = {**os.environ, "FLOATIE_WORK_DIR": str(work)}
    out = subprocess.run(["bash", str(GUARD)], env=env, capture_output=True, text=True)
    assert out.returncode == 1
    assert "no remote configured" in (out.stdout + out.stderr)


def test_json_empty_when_no_repos(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    items = _run_json(work)
    assert items == []


def test_json_includes_worker_worktree(tmp_path):
    work = tmp_path / "work"
    wt = work / ".worktrees" / "alpha" / "api"
    _init_repo(wt); _add_remote_and_push(wt, tmp_path / "api.git")
    (wt / "f.txt").write_text("dirty\n")          # uncommitted in a worker worktree
    items = _run_json(work)
    labels = [i["label"] for i in items]
    assert any("worker" in lbl and "alpha" in lbl for lbl in labels), labels
    worker_item = next(i for i in items if "alpha" in i["label"])
    assert worker_item["clean"] is False and worker_item["uncommitted"] == 1


@pytest.mark.parametrize("failure", ["status", "ls-files", "rev-list", "for-each-ref", "corrupt"])
def test_guard_probe_error_is_unknown(tmp_path, failure):
    work = tmp_path / "work"; repo = work / "api"
    _init_repo(repo); _add_remote_and_push(repo, tmp_path / "remote.git")
    bindir = make_stubs(tmp_path, tmp_path / "calls")
    if failure == "corrupt":
        # Keep the .git marker but point it to unavailable metadata.
        (repo / ".git").rename(tmp_path / "metadata")
        (repo / ".git").write_text("gitdir: /nonexistent/task1-gitdir\n")
    else:
        if failure == "for-each-ref":
            _git(repo, "branch", "--unset-upstream")
        stub_git(bindir, f'case " $* " in *" {failure} "*) exit 71;; esac')
    item, = _run_json(work, PATH=safe_path(bindir))
    assert item["clean"] is not True and item["error"]
    if failure in ("status", "corrupt"):
        assert item["uncommitted"] is None
    if failure != "status":
        assert item["unpushed"] is None


def test_guard_no_upstream_with_remote_reachability_is_clean(tmp_path):
    work = tmp_path / "work"; repo = work / "api"
    _init_repo(repo); _add_remote_and_push(repo, tmp_path / "remote.git")
    _git(repo, "branch", "--unset-upstream")
    item, = _run_json(work)
    assert item["clean"] is True and item["unpushed"] == 0


@pytest.mark.parametrize("flag", ["--assume-unchanged", "--skip-worktree"])
@pytest.mark.parametrize("edited", [False, True])
def test_guard_hidden_index_flags_are_unknown_and_index_unchanged(tmp_path, flag, edited):
    work = tmp_path / "work"; repo = work / "api"
    _init_repo(repo); _add_remote_and_push(repo, tmp_path / "remote.git")
    _git(repo, "update-index", flag, "f.txt")
    if edited:
        (repo / "f.txt").write_text("hidden local content\n")
    index = repo / ".git/index"
    before = index.read_bytes()
    item, = _run_json(work)
    assert item["clean"] is False and item.get("error"), item
    assert item["uncommitted"] is None and item["unpushed"] is None
    assert index.read_bytes() == before, "observation changed the user's index"
    if edited:
        assert (repo / "f.txt").read_text() == "hidden local content\n"
    env = {**os.environ, **GIT_ENV, "FLOATIE_WORK_DIR": str(work)}
    human = subprocess.run(["bash", str(GUARD)], env=env, capture_output=True, text=True)
    assert human.returncode == 1, human.stdout + human.stderr
    assert index.read_bytes() == before
