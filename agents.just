# Inside the VM: just --justfile ~/.local/share/floatie/agents.just <recipe>
# This file shares the host command names; it never invokes Lima.

# List guest recipes (also the bare guest invocation).
[private]
default:
    @just --justfile {{ quote(justfile()) }} --list

# Start/reuse the root agent (Codex by default), focus its pane and attach.
[positional-arguments]
orchestrate harness="codex":
    #!/usr/bin/env bash
    set -euo pipefail
    result=$(python3 "$HOME/.local/share/floatie/agent_tools.py" start main "$1")
    pane=$(printf '%s' "$result" | python3 -c 'import json,sys; print(json.load(sys.stdin)["pane"])')
    tmux select-window -t "$pane"
    tmux select-pane -t "$pane"
    exec tmux attach-session -t '=main'

# Start/reuse a primary agent in an existing session; main also accepted.
[positional-arguments]
agent-start target harness="codex":
    @python3 "$HOME/.local/share/floatie/agent_tools.py" start "$@"

# Discover agent processes/panes as JSON, including manual launches.
agent-list:
    @python3 "$HOME/.local/share/floatie/agent_tools.py" list

# Read terminal tail and cursor; optional --pane %ID selects an exact pane.
[positional-arguments]
agent-read target *options:
    @python3 "$HOME/.local/share/floatie/agent_tools.py" read "$@"

# Read literal prompt from stdin through EOF; optional --pane %ID.
[positional-arguments]
agent-send target *options:
    @python3 "$HOME/.local/share/floatie/agent_tools.py" send "$@"

# Observe changes: --pane %ID, repeated --target, --cursor, --seconds; not completion.
[positional-arguments]
agent-wait target *options:
    @python3 "$HOME/.local/share/floatie/agent_tools.py" wait "$@"

# Create/reconnect worktrees/session with an explicit exact repo set; attach via guest tmux.
[positional-arguments]
worker-create target +repos:
    @"$HOME/.local/bin/floatie-worker-init" "$@"
