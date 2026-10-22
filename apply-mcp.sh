#!/bin/bash
# apply-mcp.sh — merge a workspace .mcp.json's mcpServers into ~/.claude.json at
# USER scope, so every Claude Code process in the VM (orchestrator, workers,
# subagents) sees the same MCP servers regardless of working directory.
#
# Invoked in the guest as: bash -s -- <path-to-mcp-json> < apply-mcp.sh
# (the workspace .mcp.json is copied into the guest first by the _place-context recipe).
#
# floatie only APPLIES the hand-authored list — it never generates it (decision #18).
# Merge semantics (v1): workspace servers are added/overwritten by name; servers
# NOT in the workspace .mcp.json are LEFT IN PLACE (so interactively-added servers
# and cached OAuth state are preserved). Removing a server from .mcp.json does not
# auto-remove it from the VM — use `claude mcp remove <name>` or `just rebuild`.
set -euo pipefail

SRC="${1:-/tmp/floatie-mcp.json}"

if ! command -v python3 >/dev/null 2>&1; then
  echo "WARN: python3 absent in guest — cannot apply MCP servers" >&2
  exit 0
fi

python3 - "$SRC" <<'PYEOF'
import json, os, sys

src = sys.argv[1]
try:
    with open(src) as f:
        ws = json.load(f)
except FileNotFoundError:
    print(f"==> no MCP source at {src} — skipping"); sys.exit(0)
except json.JSONDecodeError as e:
    print(f"WARN: {src} is not valid JSON ({e}); skipping MCP apply", file=sys.stderr); sys.exit(0)

servers = ws.get("mcpServers", {})
if not isinstance(servers, dict) or not servers:
    print("==> .mcp.json has no mcpServers — nothing to apply"); sys.exit(0)

path = os.path.expanduser("~/.claude.json")
try:
    with open(path) as f:
        data = json.load(f)
except FileNotFoundError:
    data = {}
except json.JSONDecodeError:
    # Corrupt user config: re-seed the onboarding flag (provision.sh's job) so the
    # rewrite doesn't resurrect the first-run wizard, then merge servers in.
    print("WARN: ~/.claude.json was not valid JSON; re-seeding minimal config + merging servers", file=sys.stderr)
    data = {"hasCompletedOnboarding": True}

existing = data.get("mcpServers", {})
if not isinstance(existing, dict):
    existing = {}

for name, cfg in servers.items():
    existing[name] = cfg  # workspace definition wins on name conflict

data["mcpServers"] = existing
with open(path, "w") as f:
    json.dump(data, f, indent=2)

print(f"==> applied {len(servers)} MCP server(s) at user scope: {', '.join(sorted(servers))}")
PYEOF
