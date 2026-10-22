"""How workspace secrets reach a shell that early-returns (issue #28).

Ubuntu's ~/.bashrc returns early for non-interactive shells, so a hook APPENDED
to it only ever fires interactively. That matters because a non-interactive
`bash -c` over SSH — which is what `limactl shell <vm> -- bash -c ...` is, about
half of how floatie talks to the guest — hits a bash special case: it sources
~/.bashrc and returns WITHOUT consulting $BASH_ENV. So the hook has to sit above
the early return.

Two things this deliberately does NOT do, both learned from review:

  * It does not try to remove an older appended hook. Pattern-matching a block
    that may be commented out, indented, wrapped in a conditional, or merely
    mentioned in a comment deletes unrelated user configuration. The old block
    is idempotent, so leaving it costs a duplicate source and nothing else.
  * It does not export $BASH_ENV. Every shell that could see it has already
    inherited the secrets from the login environment, so the hook would be inert
    while adding a system-wide code-execution surface.

The wiring section uses no sudo, so these tests execute the real thing.
"""
import os
import pathlib
import re
import stat as statmod
import subprocess

PROVISION = pathlib.Path(__file__).resolve().parents[1] / "provision.sh"
START = "# ── Secrets in shells that early-return (~/.bashrc, at the TOP) ─"
END = "# ── Source ~/.env.secrets in LOGIN shells too"

EARLY_RETURN = "case $- in *i*) ;; *) return;; esac"


def wiring_region() -> str:
    t = PROVISION.read_text()
    s = t.find(START)
    assert s != -1, f"section header not found: {START!r} — was it renamed?"
    e = t.find(END, s)
    assert e != -1, f"section header not found: {END!r} — was it renamed?"
    return t[s:e]


def hook_line() -> str:
    """The literal hook provision.sh prepends."""
    m = re.search(r"^BASHRC_HOOK='(.*)'$", PROVISION.read_text(), re.M)
    assert m, "BASHRC_HOOK assignment not found — was it renamed?"
    return m.group(1)


def run_wiring(home):
    return subprocess.run(
        ["bash", "-c", wiring_region()],
        capture_output=True, text=True,
        env={"HOME": str(home), "PATH": os.environ["PATH"]},
    )


def make_home(tmp_path, secrets="GITHUB_TOKEN=from-file\n"):
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    if secrets is not None:
        (home / ".env.secrets").write_text(secrets)
    return home


# ── placement ────────────────────────────────────────────────────────────────

def test_hook_lands_on_the_first_line_and_keeps_everything(tmp_path):
    home = make_home(tmp_path)
    (home / ".bashrc").write_text(f"# user config\n{EARLY_RETURN}\nalias ll='ls -l'\n")
    r = run_wiring(home)
    assert r.returncode == 0, r.stderr
    lines = (home / ".bashrc").read_text().splitlines()
    assert "floatie:secrets" in lines[0], f"not first: {lines[:2]}"
    body = "\n".join(lines)
    assert "# user config" in body and "alias ll='ls -l'" in body
    assert EARLY_RETURN in body, "removed Ubuntu's own guard"


def test_wiring_is_idempotent(tmp_path):
    home = make_home(tmp_path)
    (home / ".bashrc").write_text(f"{EARLY_RETURN}\n")
    run_wiring(home)
    once = (home / ".bashrc").read_text()
    run_wiring(home)
    assert (home / ".bashrc").read_text() == once
    assert once.count("floatie:secrets") == 1


def test_creates_bashrc_when_absent(tmp_path):
    home = make_home(tmp_path)
    r = run_wiring(home)
    assert r.returncode == 0, r.stderr
    assert "floatie:secrets" in (home / ".bashrc").read_text()


# ── the two things review said the old version got wrong ─────────────────────

def test_symlinked_bashrc_is_followed_not_replaced(tmp_path):
    """Dotfile managers symlink ~/.bashrc. Replacing the link with a regular
    file silently detaches it, so every later dotfiles update stops applying.
    """
    home = make_home(tmp_path)
    real = tmp_path / "dotfiles" / "bashrc"
    real.parent.mkdir()
    real.write_text(f"# managed\n{EARLY_RETURN}\n")
    (home / ".bashrc").symlink_to(real)

    r = run_wiring(home)
    assert r.returncode == 0, r.stderr
    assert (home / ".bashrc").is_symlink(), "replaced the symlink with a regular file"
    assert "floatie:secrets" in real.read_text().splitlines()[0], (
        "wrote past the symlink instead of into the managed file"
    )


def test_original_mode_is_preserved(tmp_path):
    """A shell redirect creates 0644, so a deliberately restricted ~/.bashrc
    would be silently widened.
    """
    home = make_home(tmp_path)
    rc = home / ".bashrc"
    rc.write_text(f"{EARLY_RETURN}\n")
    rc.chmod(0o600)
    r = run_wiring(home)
    assert r.returncode == 0, r.stderr
    mode = statmod.S_IMODE(rc.stat().st_mode)
    assert mode == 0o600, f"mode widened to {oct(mode)}"


def test_legacy_appended_hook_is_left_untouched(tmp_path):
    """Deliberate non-migration: the old block is idempotent, and removing it by
    pattern match destroys user configuration when the shape differs.
    """
    home = make_home(tmp_path)
    legacy = (
        "# user config\n"
        f"{EARLY_RETURN}\n"
        "\n"
        "# floatie: source ~/.env.secrets\n"
        'if [ -f "$HOME/.env.secrets" ]; then\n'
        "  set -a\n"
        '  . "$HOME/.env.secrets"\n'
        "  set +a\n"
        "fi\n"
        "alias keep=me\n"
    )
    (home / ".bashrc").write_text(legacy)
    r = run_wiring(home)
    assert r.returncode == 0, r.stderr
    out = (home / ".bashrc").read_text()
    assert "# floatie: source ~/.env.secrets" in out, "removed the legacy block"
    assert "alias keep=me" in out and "# user config" in out
    assert "floatie:secrets" in out.splitlines()[0]


# ── the property the SSH bash -c path depends on ─────────────────────────────

def test_hook_above_the_early_return_delivers_secrets(tmp_path):
    """Faithful simulation: ~/.bashrc sourced by a NON-interactive shell, so
    Ubuntu's `case $- in *i*)` guard returns early. A hook above it still runs.
    """
    home = make_home(tmp_path)
    rc = tmp_path / "rc-top"
    rc.write_text(f"{hook_line()}\n{EARLY_RETURN}\necho TAIL_RAN\n")
    r = subprocess.run(["bash", "-c", f'. "{rc}"; echo "V=${{GITHUB_TOKEN:-unset}}"'],
                       capture_output=True, text=True,
                       env={"HOME": str(home), "PATH": os.environ["PATH"]})
    assert "V=from-file" in r.stdout, f"hook did not run: {r.stdout}{r.stderr}"
    assert "TAIL_RAN" not in r.stdout, "early return did not fire — not simulating the real path"


def test_hook_below_the_early_return_does_not(tmp_path):
    """Negative control. If a hook below the guard did deliver secrets, the top
    placement would be unnecessary and this whole change pointless.
    """
    home = make_home(tmp_path)
    rc = tmp_path / "rc-bottom"
    rc.write_text(f"{EARLY_RETURN}\n{hook_line()}\n")
    r = subprocess.run(["bash", "-c", f'. "{rc}"; echo "V=${{GITHUB_TOKEN:-unset}}"'],
                       capture_output=True, text=True,
                       env={"HOME": str(home), "PATH": os.environ["PATH"]})
    assert "V=unset" in r.stdout


def test_hook_writes_nothing_to_stdout(tmp_path):
    """scp, sftp, rsync and git-over-ssh all arrive as a non-interactive
    `bash -c`, so anything above the early return that prints corrupts their
    wire protocol. This pins the invariant the placement creates.
    """
    home = make_home(tmp_path)
    rc = tmp_path / "rc-quiet"
    rc.write_text(f"{hook_line()}\n{EARLY_RETURN}\n")
    r = subprocess.run(["bash", "-c", f'. "{rc}"'], capture_output=True, text=True,
                       env={"HOME": str(home), "PATH": os.environ["PATH"]})
    assert r.stdout == "", f"hook wrote to stdout: {r.stdout!r}"
    assert r.stderr == "", f"hook wrote to stderr: {r.stderr!r}"


def test_hook_is_a_noop_without_a_secrets_file(tmp_path):
    home = make_home(tmp_path, secrets=None)
    rc = tmp_path / "rc"
    rc.write_text(f"{hook_line()}\n")
    r = subprocess.run(["bash", "-c", f'set -euo pipefail; . "{rc}"; echo OK'],
                       capture_output=True, text=True,
                       env={"HOME": str(home), "PATH": os.environ["PATH"]})
    assert r.returncode == 0, r.stderr
    assert "OK" in r.stdout


# ── guards against the two rejected designs coming back ──────────────────────

def test_provision_does_not_export_bash_env():
    """A system-wide BASH_ENV export is inert here — every shell that can see it
    has already inherited the secrets — while adding code-execution surface.
    """
    assert "export BASH_ENV" not in PROVISION.read_text()


def test_provision_does_not_pattern_edit_existing_bashrc_content():
    """No awk/sed rewrite of ~/.bashrc: the only mutation is a prepend."""
    region = wiring_region()
    for tool in ("awk", "sed "):
        assert tool not in region, f"{tool!r} is back in the ~/.bashrc wiring"


def test_hook_sources_the_file_the_same_way_profile_d_does():
    """One sourcing idiom across hooks. A hand-rolled parser disagreed with real
    shell sourcing on quoted values, `export K=V` lines and trailing whitespace,
    so the same file yielded different values per shell class.
    """
    t = PROVISION.read_text()
    idiom = 'set -a; . "$HOME/.env.secrets"; set +a'
    assert idiom in hook_line(), f"hook does not use the shared idiom: {hook_line()!r}"
    assert t.count(idiom) >= 2, "profile.d and the bashrc hook have diverged"


def test_provision_is_valid_bash():
    r = subprocess.run(["bash", "-n", str(PROVISION)], capture_output=True, text=True)
    assert r.returncode == 0, f"provision.sh failed to parse:\n{r.stderr}"
