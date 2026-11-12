"""install-plugins.sh: unknown entries are added or installed, known ones are updated,
and a failed call never stops a reload. `claude` is a stub that logs its arguments."""
import os
import pathlib
import subprocess

SCRIPT = pathlib.Path(__file__).resolve().parents[1] / "install-plugins.sh"

STUB = r'''#!/bin/sh
echo "$*" >> "$STUB_LOG"
case "$*" in
  "plugin marketplace list") for m in $STUB_MARKETPLACES; do echo "  ❯ $m"; done ;;
  "plugin list") for p in $STUB_PLUGINS; do echo "  ❯ $p"; done ;;
  "plugin marketplace update "*|"plugin update "*) exit "${STUB_UPDATE_EXIT:-0}" ;;
  "plugin marketplace add "*|"plugin install "*) exit "${STUB_ADD_EXIT:-0}" ;;
esac
'''


def run(tmp_path, entries, marketplaces="", plugins="", update_exit="0", add_exit="0"):
    bindir = tmp_path / ".local" / "bin"
    bindir.mkdir(parents=True)
    stub = bindir / "claude"
    stub.write_text(STUB)
    stub.chmod(0o755)
    log = tmp_path / "claude.log"
    env = {**os.environ, "HOME": str(tmp_path), "STUB_LOG": str(log),
           "STUB_MARKETPLACES": marketplaces, "STUB_PLUGINS": plugins,
           "STUB_UPDATE_EXIT": update_exit, "STUB_ADD_EXIT": add_exit}
    result = subprocess.run(["bash", str(SCRIPT), *entries], capture_output=True, text=True, env=env)
    calls = log.read_text().splitlines() if log.exists() else []
    return result, calls


def test_unknown_entries_are_added_and_installed(tmp_path):
    result, calls = run(tmp_path, ["acme/team-skills", "tool@team-skills"])
    assert result.returncode == 0
    assert "plugin marketplace add acme/team-skills" in calls
    assert "plugin install tool@team-skills" in calls


def test_failed_add_or_install_does_not_stop_the_reload(tmp_path):
    result, _ = run(tmp_path, ["acme/team-skills", "tool@team-skills"], add_exit="1")
    assert result.returncode == 0
    assert result.stderr.count("WARN:") == 2


def test_known_marketplace_is_updated_not_added(tmp_path):
    result, calls = run(tmp_path, ["acme/team-skills"], marketplaces="team-skills")
    assert "plugin marketplace update team-skills" in calls
    assert not any(c.startswith("plugin marketplace add") for c in calls)
    assert "==>   running: claude plugin marketplace update team-skills" in result.stdout


def test_known_plugin_is_updated_not_installed(tmp_path):
    result, calls = run(tmp_path, ["tool@team-skills"], plugins="tool@team-skills")
    assert "plugin update tool@team-skills" in calls
    assert not any(c.startswith("plugin install") for c in calls)
    assert "==>   running: claude plugin update tool@team-skills" in result.stdout


def test_known_marketplace_with_a_missing_plugin_updates_one_and_installs_the_other(tmp_path):
    _, calls = run(tmp_path, ["acme/team-skills", "tool@team-skills"], marketplaces="team-skills")
    assert "plugin marketplace update team-skills" in calls
    assert "plugin install tool@team-skills" in calls


def test_marketplace_is_updated_before_its_plugin(tmp_path):
    _, calls = run(tmp_path, ["acme/team-skills", "tool@team-skills"],
                   marketplaces="team-skills", plugins="tool@team-skills")
    assert calls.index("plugin marketplace update team-skills") < calls.index("plugin update tool@team-skills")


def test_failed_update_warns_and_does_not_stop_the_reload(tmp_path):
    result, _ = run(tmp_path, ["acme/team-skills", "tool@team-skills"],
                    marketplaces="team-skills", plugins="tool@team-skills", update_exit="1")
    assert result.returncode == 0
    assert result.stderr.count("WARN:") == 2
