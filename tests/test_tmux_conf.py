"""Structural tests over the tmux config block in provision.sh.

provision.sh is piped to the guest over stdin by the create recipe, so the tmux
config must live inside it as a heredoc. These tests assert the heredoc's
content directly — no VM required.
"""
import pathlib
import re
import subprocess

PROVISION = pathlib.Path(__file__).resolve().parents[1] / "provision.sh"
MARKER = "# ── floatie: session ergonomics ──"


def provision_text() -> str:
    return PROVISION.read_text()


def tmux_block() -> str:
    """Return the body of the TMUXEOF heredoc, or '' if absent."""
    m = re.search(r"<<'TMUXEOF'\n(.*?)\nTMUXEOF", provision_text(), re.S)
    return m.group(1) if m else ""


def test_provision_has_tmux_heredoc():
    assert tmux_block(), "no TMUXEOF heredoc found in provision.sh"


def test_marker_present_in_block():
    assert MARKER in tmux_block()


def test_scrolling_settings():
    b = tmux_block()
    assert "set  -g mouse on" in b
    assert "set  -g history-limit 50000" in b


def test_vi_copy_mode_bindings():
    b = tmux_block()
    assert "setw -g mode-keys vi" in b
    assert "bind -T copy-mode-vi v send -X begin-selection" in b
    assert "bind -T copy-mode-vi y send -X copy-selection-and-cancel" in b
    assert "bind -T copy-mode-vi MouseDragEnd1Pane send -X copy-selection-no-clear" in b
    assert "bind      u copy-mode" in b


def test_claude_code_quality_of_life():
    b = tmux_block()
    assert "set  -g focus-events on" in b
    assert "set -sg escape-time 10" in b


def test_clipboard_enabled():
    assert "set  -g set-clipboard on" in tmux_block()


def test_terminal_features_are_targeted_not_wildcard():
    """Keep clipboard capability declarations targeted to supported terminals.

    `terminal-features ',*:clipboard'` is redundant (tmux ships xterm*:clipboard
    and the attached client reports xterm-256color) and harmful in general: `*`
    asserts OSC 52 support for terminals that lack it, which renders as raw
    escape garbage. Only xterm*-targeted features are allowed.
    """
    b = tmux_block()
    assert "'*:clipboard'" not in b and ",*:clipboard" not in b
    assert "set -ga terminal-features ',xterm*:RGB'" in b
    # The legacy Tc override was replaced by terminal-features; keep one mechanism.
    assert "terminal-overrides" not in b


def test_provision_is_valid_bash():
    """Substring assertions cannot catch an unbalanced quote or a missing `fi`.

    Hand the whole script to bash for a parse check — cheap, and it catches a
    class of error nothing else here can.
    """
    r = subprocess.run(["bash", "-n", str(PROVISION)], capture_output=True, text=True)
    assert r.returncode == 0, f"provision.sh failed to parse:\n{r.stderr}"


def marker_assignment_value() -> str:
    """The literal string assigned to TMUX_MARKER in provision.sh."""
    m = re.search(r'TMUX_MARKER="([^"]*)"', provision_text())
    assert m, "TMUX_MARKER assignment not found in provision.sh — was it renamed?"
    return m.group(1)


def test_marker_value_appears_in_what_the_heredoc_writes():
    """The guard greps for TMUX_MARKER; the heredoc writes the banner line.

    If the two drift apart, grep never matches, the guard never fires, and every
    re-provision appends the block again — the exact failure the guard exists to
    prevent, and one that no substring assertion elsewhere in this file catches.
    """
    assert marker_assignment_value() in tmux_block()


def test_python_marker_constant_matches_provision():
    """Keeps this test file's own copy of the marker honest."""
    assert marker_assignment_value() == MARKER


def guard_section() -> str:
    """The shell guarding the heredoc: from the TMUX_MARKER assignment through
    the `fi` that closes it.

    Bounded by the heredoc itself rather than by an unrelated later line, so
    adding new sections to provision.sh cannot silently widen what these tests
    inspect.
    """
    t = provision_text()
    start = t.find('TMUX_MARKER="')
    assert start != -1, "TMUX_MARKER assignment not found in provision.sh — was it renamed?"
    end = t.find("\nTMUXEOF\n", start)
    assert end != -1, "TMUXEOF heredoc terminator not found after TMUX_MARKER"
    close = t.find("\nfi\n", end)
    assert close != -1, "no closing `fi` found after the tmux heredoc"
    return t[start:close]


def test_guard_greps_for_marker_before_writing():
    g = guard_section()
    assert 'grep -qF "$TMUX_MARKER" "$HOME/.tmux.conf"' in g


def test_guard_skips_when_marker_present():
    assert "already has the floatie block — skipping" in guard_section()


def test_guard_appends_never_truncates():
    """Must use >> so an existing user config is preserved, never > which clobbers."""
    g = guard_section()
    assert 'cat >> "$HOME/.tmux.conf"' in g
    assert 'cat > "$HOME/.tmux.conf"' not in g


def test_guard_warns_when_appending_to_existing_file():
    """Appending overrides earlier settings, so warn before changing behavior."""
    g = guard_section()
    assert '[ -s "$HOME/.tmux.conf" ]' in g
    assert "last-wins" in g
    assert "To opt out" in g


def test_sources_config_into_a_running_tmux_server():
    """A running tmux server never re-reads ~/.tmux.conf — tmux only sources it at
    server start. Without this, a re-provision of a live VM writes the file and
    changes nothing, which is exactly what happened during live verification.
    """
    t = provision_text()
    assert "tmux source-file" in t
    assert "tmux info" in t


def test_source_step_runs_outside_the_write_guard():
    """It must run on BOTH the write path and the skip path. If it sat inside the
    guard, a VM whose config was already present would never get it applied.
    """
    t = provision_text()
    source_at = t.index("tmux source-file")
    guard_end = t.index("\nfi\n", t.index("\nTMUXEOF\n"))
    assert source_at > guard_end, "source-file must come after the write/skip guard closes"
