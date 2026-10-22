"""Behavioural tests over the SSH-signing-key section of provision.sh.

See issue #30 and the adversarial review on PR #49.

Keygen is idempotent, but the "ACTION REQUIRED — register the signing key"
banner was gated only on the *public key file existing*, which is always true
once keygen has run. So every re-provision re-printed a first-run banner and the
operator had no way to tell real action from noise.

The signing region contains no `sudo` and makes no network calls, so unlike the
rest of provision.sh it can be executed directly against a throwaway HOME. That
makes these real behavioural tests rather than substring assertions.

Two states matter beyond the happy path, because in both of them git is
configured with `commit.gpgsign=true` pointing at `user.signingkey`, so getting
them wrong breaks every commit in the VM:

  * the public key is missing while the private key remains (it is re-derived)
  * the public key cannot be derived at all (must be reported, never papered over)

Neither may produce a message claiming the key is present or registered.
"""
import pathlib
import re
import subprocess

PROVISION = pathlib.Path(__file__).resolve().parents[1] / "provision.sh"
START = "# ── SSH signing key (dedicated"
END = "# ── uv (Python package manager"

BANNER = "ACTION REQUIRED"
# The quiet line on the re-provision path. Asserted against provision.sh itself
# by test_quiet_note_constant_matches_provision so the two cannot drift — the
# earlier version of this file matched a *pre-existing* keygen line instead and
# therefore asserted nothing about the change under test.
QUIET_NOTE = "signing key present at"
RESTORE_NOTE = "public key restored at"


def signing_region() -> str:
    """The signing-key section of provision.sh, sliced by its section headers.

    Renaming or moving either header fails loudly rather than silently changing
    the slice. Note this does NOT protect against a new section being inserted
    between the two headers — that would join what these tests execute, though
    loudly (a `sudo` or `curl` in it would trip `set -e`).
    """
    t = PROVISION.read_text()
    start = t.find(START)
    assert start != -1, f"section header not found: {START!r} — was it renamed?"
    end = t.find(END, start)
    assert end != -1, f"section header not found: {END!r} — was it renamed?"
    return t[start:end]


def banner_else_branch() -> str:
    """The final `else` arm of the banner conditional — the re-provision path."""
    r = signing_region()
    start = r.find("# ── Print signing public key")
    assert start != -1, "banner section header not found — was it renamed?"
    block = r[start:]
    i = block.rfind("\nelse\n")
    assert i != -1, "no else arm in the banner conditional"
    j = block.find("\nfi\n", i)
    assert j != -1, "no closing fi after the banner's else arm"
    return block[i:j]


def run_region(home: pathlib.Path, email: str = "test@example.com"):
    """Execute the signing region with HOME pointed at a sandbox.

    GIT_USER_EMAIL is a parameter because provision.sh initialises it to "" and
    leaves it empty whenever ~/.floatie-git-identity is absent — which the
    justfile produces when workspace.yml leaves both identity fields blank.
    """
    script = (
        "set -euo pipefail\n"
        f'GIT_USER_EMAIL="{email}"\n' + signing_region()
    )
    return subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        env={
            "HOME": str(home),
            "PATH": "/usr/bin:/bin:/usr/local/bin",
            "GIT_CONFIG_NOSYSTEM": "1",
        },
    )


def git_config(home: pathlib.Path, key: str) -> str:
    r = subprocess.run(
        ["git", "config", "--global", "--get", key],
        capture_output=True, text=True,
        env={"HOME": str(home), "PATH": "/usr/bin:/bin:/usr/local/bin",
             "GIT_CONFIG_NOSYSTEM": "1"},
    )
    return r.stdout.strip()


# ── extraction guards ────────────────────────────────────────────────────────

def test_region_is_extractable():
    assert "ssh-keygen" in signing_region()


def test_quiet_note_constant_matches_provision():
    """Keeps this file's copy of the quiet-note text honest.

    Without this, rewording the shell line would leave the assertions below
    passing against a string that no longer appears anywhere.
    """
    assert QUIET_NOTE in banner_else_branch()


# ── the happy paths ──────────────────────────────────────────────────────────

def test_first_run_prints_the_banner(tmp_path):
    """A fresh VM genuinely needs the key registered — the banner must print."""
    r = run_region(tmp_path)
    assert r.returncode == 0, f"region failed:\n{r.stderr}"
    assert BANNER in r.stdout
    assert "generating SSH signing key" in r.stdout


def test_rerun_does_not_print_the_banner(tmp_path):
    """Issue #30: the key already exists and was registered on a prior run, so
    the ACTION REQUIRED box is pure noise. This is the original regression.
    """
    first = run_region(tmp_path)
    assert first.returncode == 0, f"first run failed:\n{first.stderr}"
    assert BANNER in first.stdout, "precondition: first run should print the banner"

    second = run_region(tmp_path)
    assert second.returncode == 0, f"second run failed:\n{second.stderr}"
    assert "skipping keygen (idempotent)" in second.stdout, "keygen should be skipped"
    assert BANNER not in second.stdout, (
        "re-provision re-printed the first-run ACTION REQUIRED banner:\n" + second.stdout
    )


def test_rerun_prints_the_quiet_note(tmp_path):
    """Suppressing the banner must not make the run silent about signing.

    Asserts the note from the else arm specifically, not the pre-existing
    "already exists" keygen line — deleting the else arm must turn this red.
    """
    run_region(tmp_path)
    second = run_region(tmp_path)
    assert QUIET_NOTE in second.stdout, (
        "re-run printed no quiet note about the signing key:\n" + second.stdout
    )


def test_rerun_points_at_a_recovery_path(tmp_path):
    """The old code reprinted the public key every run, which was the de-facto
    way to recover a banner you missed or a key removed on GitHub's side.
    Suppressing the banner removes that, so the quiet note must say how to
    re-read the key rather than leaving the operator with nothing.
    """
    run_region(tmp_path)
    second = run_region(tmp_path)
    assert "git_signing.pub" in second.stdout, "quiet note does not name the public key path"
    assert "github.com/settings/ssh" in second.stdout, "quiet note gives no registration URL"


def test_signing_config_survives_a_rerun(tmp_path):
    """Acceptance criterion: no regression to commit signing. The git config and
    allowed-signers file must be written on EVERY run, banner or not.
    """
    run_region(tmp_path)
    run_region(tmp_path)
    assert git_config(tmp_path, "user.signingkey").endswith("git_signing.pub")
    assert git_config(tmp_path, "gpg.format") == "ssh"
    assert git_config(tmp_path, "commit.gpgsign") == "true"
    assert git_config(tmp_path, "gpg.ssh.allowedSignersFile")
    assert (tmp_path / ".config/git/allowed_signers").is_file()


# ── the states that break every commit in the VM ─────────────────────────────

def test_missing_pubkey_is_restored(tmp_path):
    """Private key present, public key gone.

    Keygen is guarded on the *private* key, so it is skipped and the public key
    was never regenerated: `user.signingkey` then pointed at a nonexistent file
    with commit.gpgsign=true, and every `git commit` failed with
    "Couldn't load public key". The private key is the source of truth, so the
    public half is re-derived from it.

    Note the fix is `ssh-keygen -y` (derive), NOT widening the keygen guard to
    test the .pub: ssh-keygen prompts "Overwrite (y/n)?" when the private key
    exists, and provision.sh IS the shell's stdin, so that prompt would consume
    the next line of the script.
    """
    first = run_region(tmp_path)
    assert first.returncode == 0, f"first run failed:\n{first.stderr}"
    pub = tmp_path / ".ssh/git_signing.pub"
    original = pub.read_text()
    pub.unlink()

    second = run_region(tmp_path)
    assert second.returncode == 0, f"second run failed:\n{second.stderr}"
    assert pub.is_file(), "public key was not restored — commits will fail"
    assert pub.read_text().split()[:2] == original.split()[:2], (
        "restored public key does not match the private key it came from"
    )
    assert RESTORE_NOTE in second.stdout


def test_missing_pubkey_does_not_claim_the_key_was_present(tmp_path):
    """The regression the review caught: this state printed
    "signing key present (registered once on GitHub — skipping prompt)" and
    exited 0, while commits were broken. Nothing may assert registration here —
    the script cannot know it, and the run was not clean.
    """
    run_region(tmp_path)
    (tmp_path / ".ssh/git_signing.pub").unlink()
    r = run_region(tmp_path)
    assert "registered once on GitHub" not in r.stdout, (
        "run asserted the key was registered on GitHub, which it cannot know:\n" + r.stdout
    )
    assert r.stderr, "a repaired public key should be reported on stderr, not silently"


def test_missing_pubkey_leaves_signing_actually_usable(tmp_path):
    """End-to-end: after the repair, the file git is configured to sign with
    must exist. This is the assertion that would have caught the rc-128 commit
    failure that the previous version of this suite was green through.
    """
    run_region(tmp_path)
    (tmp_path / ".ssh/git_signing.pub").unlink()
    run_region(tmp_path)
    configured = git_config(tmp_path, "user.signingkey")
    assert configured, "user.signingkey not set"
    assert pathlib.Path(configured).is_file(), (
        f"git is configured to sign with {configured}, which does not exist — "
        "every commit in the VM will fail with 'Couldn't load public key'"
    )


def test_underivable_pubkey_is_reported_and_never_claimed_present(tmp_path):
    """Public key gone AND the private key unusable, so derivation cannot help.

    The run must say so rather than claim the key is present, and must not abort
    the rest of provision (the toolchain install below it is unrelated to
    signing).
    """
    run_region(tmp_path)
    (tmp_path / ".ssh/git_signing.pub").unlink()
    (tmp_path / ".ssh/git_signing").write_text("not a key\n")

    r = run_region(tmp_path)
    assert r.returncode == 0, f"provision aborted over a signing problem:\n{r.stderr}"
    assert QUIET_NOTE not in r.stdout, (
        "claimed the signing key is present when it could not be derived:\n" + r.stdout
    )
    assert BANNER not in r.stdout
    assert re.search(r"WARN.*(derive|public key)", r.stderr), (
        "no warning about the unusable signing key:\n" + r.stderr
    )
    assert not (tmp_path / ".ssh/git_signing.pub").exists(), (
        "left an empty .pub behind — the redirect creates the file even when "
        "ssh-keygen fails, so it must be cleaned up"
    )


def test_no_email_warns_and_still_reports_the_key_state(tmp_path):
    """GIT_USER_EMAIL empty — what the justfile produces when workspace.yml
    leaves both identity fields blank. allowed-signers is skipped, so this must
    warn; the key itself really is present, so the quiet note is still correct.
    """
    run_region(tmp_path, email="")
    r = run_region(tmp_path, email="")
    assert r.returncode == 0, f"region failed:\n{r.stderr}"
    assert "skipping allowed-signers" in r.stderr, (
        "no warning that verify-commit will not work:\n" + r.stderr
    )
    assert QUIET_NOTE in r.stdout
    assert BANNER not in r.stdout


def test_provision_is_valid_bash():
    """Substring assertions cannot catch an unbalanced quote or a missing `fi`."""
    r = subprocess.run(["bash", "-n", str(PROVISION)], capture_output=True, text=True)
    assert r.returncode == 0, f"provision.sh failed to parse:\n{r.stderr}"
