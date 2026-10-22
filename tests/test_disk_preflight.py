"""disk-preflight.sh [--warn-only] <path>: refuse to grow the sparse VM disk into a nearly
full host volume.

`df` is stubbed on PATH so the thresholds are exercised without a real volume. The stub
records the path it was asked about, which pins the walk-up-to-an-existing-ancestor rule
(~/.lima/<name> does not exist before `create`).
"""
import os
import pathlib
import subprocess

SCRIPT = pathlib.Path(__file__).resolve().parents[1] / "disk-preflight.sh"
GIB_KB = 1024 * 1024


def make_df(tmp_path, avail_gib=None, log=None):
    """Write a stub `df` onto PATH; every invocation logs its argv to `log`.

    avail_gib=None: the stub logs argv and exits 1 — proves df-failure paths, and that
    the FLOATIE_SKIP_DISK_CHECK override never invokes df at all (log stays empty).
    avail_gib=<int>: the stub logs argv, prints a POSIX `df -P` row reporting that many
    GiB available, and exits 0.

    Returns (bindir, log).
    """
    if log is None:
        log = tmp_path / "df.log"
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    if avail_gib is None:
        (bindir / "df").write_text(f"""#!/bin/sh
echo "$@" >> "{log}"
exit 1
""")
    else:
        (bindir / "df").write_text(f"""#!/bin/sh
echo "$@" >> "{log}"
printf 'Filesystem 1024-blocks Used Available Capacity Mounted on\\n'
printf '/dev/fake 1000000000 1 {avail_gib * GIB_KB} 1%% /\\n'
""")
    (bindir / "df").chmod(0o755)
    return bindir, log


def last_df_arg(log):
    """The path argument of the last recorded `df -Pk [flags...] <path>` call (stub logs `$@`)."""
    lines = [l for l in log.read_text().splitlines() if l.strip()]
    assert lines, "df was never called"
    return lines[-1].split()[-1]


def run(bindir, target, args=(), **env):
    return subprocess.run(
        ["bash", str(SCRIPT), *args, str(target)],
        capture_output=True, text=True,
        env={**os.environ, "PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}", **env},
    )


def test_refuses_below_10_gib(tmp_path):
    bindir, log = make_df(tmp_path, 8)
    r = run(bindir, tmp_path)
    assert log.exists()
    assert r.returncode == 1
    assert "error" in r.stderr and "8 GiB" in r.stderr and "minimum 10 GiB" in r.stderr


def test_warns_between_10_and_20_gib(tmp_path):
    bindir, log = make_df(tmp_path, 15)
    r = run(bindir, tmp_path)
    assert log.exists()
    assert r.returncode == 0
    assert "WARN" in r.stdout and "15 GiB" in r.stdout


def test_silent_above_20_gib(tmp_path):
    bindir, log = make_df(tmp_path, 30)
    r = run(bindir, tmp_path)
    assert log.exists()
    assert r.returncode == 0
    assert "WARN" not in r.stdout and r.stderr == ""


def test_boundary_9_gib_errors(tmp_path):
    bindir, log = make_df(tmp_path, 9)
    r = run(bindir, tmp_path)
    assert log.exists()
    assert r.returncode == 1
    assert "error" in r.stderr and "9 GiB" in r.stderr


def test_boundary_10_gib_warns(tmp_path):
    bindir, log = make_df(tmp_path, 10)
    r = run(bindir, tmp_path)
    assert log.exists()
    assert r.returncode == 0
    assert "WARN" in r.stdout and "10 GiB" in r.stdout


def test_boundary_19_gib_warns(tmp_path):
    bindir, log = make_df(tmp_path, 19)
    r = run(bindir, tmp_path)
    assert log.exists()
    assert r.returncode == 0
    assert "WARN" in r.stdout and "19 GiB" in r.stdout


def test_boundary_20_gib_silent(tmp_path):
    bindir, log = make_df(tmp_path, 20)
    r = run(bindir, tmp_path)
    assert log.exists()
    assert r.returncode == 0
    assert "WARN" not in r.stdout and r.stderr == ""


def test_override_skips_the_refusal_but_says_so(tmp_path):
    bindir, log = make_df(tmp_path, 8)
    r = run(bindir, tmp_path, FLOATIE_SKIP_DISK_CHECK="1")
    assert r.returncode == 0
    assert "skipped" in r.stdout
    assert not log.exists(), "df should never be invoked once the override applies"


def test_override_skips_even_when_df_fails(tmp_path):
    # The override must short-circuit before df is ever invoked: under `set -euo
    # pipefail`, a failing df would otherwise abort the script despite the override.
    bindir, log = make_df(tmp_path, None)
    r = run(bindir, tmp_path, FLOATIE_SKIP_DISK_CHECK="1")
    assert r.returncode == 0
    assert "skipped" in r.stdout
    assert not log.exists(), "df was invoked despite the override"


def test_warn_only_prints_warn_instead_of_refusing(tmp_path):
    bindir, log = make_df(tmp_path, 8)
    r = run(bindir, tmp_path, args=("--warn-only",))
    assert log.exists()
    assert r.returncode == 0
    assert "WARN" in r.stdout and "8 GiB" in r.stdout
    assert r.stderr == ""


def test_df_failure_without_override_refuses(tmp_path):
    bindir, log = make_df(tmp_path, None)
    r = run(bindir, tmp_path)
    assert log.exists()
    assert r.returncode == 1
    assert "FLOATIE_SKIP_DISK_CHECK" in r.stderr


def test_unparseable_df_output_refuses(tmp_path):
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    (bindir / "df").write_text("""#!/bin/sh
printf 'Filesystem 1024-blocks Used Available Capacity Mounted on\\n'
printf 'map auto_home 0 0 abc 1%% /x\\n'
""")
    (bindir / "df").chmod(0o755)
    r = run(bindir, tmp_path)
    assert r.returncode == 1
    assert "could not parse" in r.stderr
    assert "FLOATIE_SKIP_DISK_CHECK" in r.stderr


def test_device_name_with_spaces_parses_correctly(tmp_path):
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    avail_kb = 30 * GIB_KB
    (bindir / "df").write_text(f"""#!/bin/sh
printf 'Filesystem 1024-blocks Used Available Capacity Mounted on\\n'
printf 'map auto_home 0 0 {avail_kb} 1%% /mnt\\n'
""")
    (bindir / "df").chmod(0o755)
    r = run(bindir, tmp_path)
    assert r.returncode == 0
    assert "WARN" not in r.stdout
    assert r.stderr == ""


def test_mount_point_with_spaces_parses_correctly(tmp_path):
    # LIMA_HOME commonly relocates onto a named macOS volume (e.g. /Volumes/My Disk),
    # which — like a spaced device name — shifts the column count from the mount-point end.
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    avail_kb = 50 * GIB_KB
    (bindir / "df").write_text(f"""#!/bin/sh
printf 'Filesystem 1024-blocks Used Available Capacity Mounted on\\n'
printf '/dev/disk4s1 500000000 10000000 {avail_kb} 3%% /Volumes/My Disk\\n'
""")
    (bindir / "df").chmod(0o755)
    r = run(bindir, tmp_path)
    assert r.returncode == 0
    assert "WARN" not in r.stdout
    assert r.stderr == ""


def test_spaced_device_and_mount_point_parse_correctly(tmp_path):
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    avail_kb = 50 * GIB_KB
    (bindir / "df").write_text(f"""#!/bin/sh
printf 'Filesystem 1024-blocks Used Available Capacity Mounted on\\n'
printf 'map auto_home 0 0 {avail_kb} 3%% /Volumes/My Disk\\n'
""")
    (bindir / "df").chmod(0o755)
    r = run(bindir, tmp_path)
    assert r.returncode == 0
    assert "WARN" not in r.stdout
    assert r.stderr == ""


def test_missing_argument_refuses():
    r = subprocess.run(["bash", str(SCRIPT)], capture_output=True, text=True)
    assert r.returncode == 1
    assert "usage" in r.stderr


def test_nonexistent_target_walks_up_to_an_existing_ancestor(tmp_path):
    missing = tmp_path / "no" / "such" / "vm"
    bindir, log = make_df(tmp_path, 30)
    r = run(bindir, missing)
    assert r.returncode == 0
    assert last_df_arg(log) == str(tmp_path), "df was not asked about the nearest existing ancestor"


def test_script_is_valid_bash():
    r = subprocess.run(["bash", "-n", str(SCRIPT)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
