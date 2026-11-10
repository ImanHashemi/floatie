# Architecture

Floatie separates a reusable **tool checkout** from a **workspace directory**.
The host wrapper locates its own justfile and scripts but uses the caller's
current directory for `workspace.yml`. It does not search parent directories.
A workspace names one Lima VM, its repositories, resources, agent instructions,
optional stack, credentials and integrations.

`.floatie-mcp.json` is the sole Floatie MCP source, using the `mcpServers` schema.
Create/reload merges its entries into guest Claude user scope (`~/.claude.json`);
Codex setup/reload replaces the managed block in guest `config.toml`. The UI reads
the same source to list credential states. Native Claude project `.mcp.json`
belongs to the project's Claude configuration and is ignored by these readers.
Floatie does not place a project `.mcp.json` in guest `~/work`.

Missing or empty sources leave existing Claude entries in place, but remove
Floatie-managed Codex servers while preserving unmanaged personal entries.
OAuth state and harness login remain separate. Reload does not copy secrets;
HTTP credential references resolve at connection time, while STDIO expansion can
write secrets into Codex's mode-0600 generated configuration.

| Location | Responsibility |
|---|---|
| Host tool checkout | Wrapper, recipes, provisioning, Python UI server and built UI |
| Host workspace | Configuration and policy selected by the user |
| Guest `~/work/<repo>` | Working clones on the VM disk |
| Guest `~/work` / main terminal | Main terminal across all repositories; home of the optional root agent |
| Guest `~/work/.worktrees/<worker>/<repo>` | Worker worktrees and branch `worker/<worker>` |
| Guest `~/work/stack` | Optional workspace-supplied stack and hooks |

Lima uses the macOS virtualization backend and an Ubuntu guest. The guest runs
rootless Docker. Worktrees and Compose project names separate ordinary work;
workers still share one kernel, user, credentials and Docker daemon. Published
host ports must be allocated by the workspace: Compose project names do not
prevent port collisions. Floatie does not start a service stack automatically.

## Guidance and agent communication

Workspace `AGENTS.md` and `CLAUDE.md` are placed at the guest work root. The starter
keeps shared policy in `AGENTS.md` with a Claude reference to it. Shared skills in
`.agents/skills` reach the work root and Claude's user scope; existing Claude-only
skills remain supported. Codex setup also manages a global guidance block using
workspace `AGENTS.md`, falling back to `CLAUDE.md`. Unrelated global guidance is
preserved; a Codex `AGENTS.override.md` can take precedence.

The root prompt points to the shared orchestrate skill. The root uses the guest
[agent recipes](commands.md#guest-commands) to create workers with explicit repo
scope, start or reuse their primary agent, discover panes, send literal prompts,
and read or wait for output. Native harness subagents are separate from Floatie
workers. One primary agent per worker avoids competing writers.

Discovery examines live tmux panes and processes, including supported manual
launches. Use the returned pane identities, especially after restart; never
assume a pane number. Send transports stdin literally. Wait observes bounded
output/process changes; it cannot establish task completion or approve a command.
The caller evaluates evidence and the workspace's authorization rules. There is
no hidden supervisor continuing after the caller's turn ends.

`orchestrate` defaults to Codex. New Floatie-launched Codex processes use full
guest access with on-request approvals. An already-running agent is reused with
its existing settings; changing the requested harness does not convert it or
transfer its conversation. A harness change requires a deliberate handoff with
task, branch, decisions, validation and remaining work. `reload` places files;
it does not switch harnesses or import history.

## Worker preservation

A new worker gets worktrees from actual root clones. Before creation, safe clean
clones on their default branch may fast-forward to the remote default. Dirty,
detached, locally advanced or otherwise unsafe clones are retained with a reason;
fetch failures warn and creation can proceed from current HEAD. An existing worker
branch is reused unchanged, so inspect the resulting base when freshness matters.

Reconnect uses the saved repository set without refresh or branch reset. A
request for different explicit scope fails even if a terminal is live. Partial
creation leaves surviving work in place: inspect created/missing repositories,
reconnect the surviving set, or preserve it and use guarded removal before a
fresh creation. Repeating creation cannot silently expand scope.

Removal checks dirty/untracked files, unpublished commits, hidden-change flags,
owned branches and Compose provenance. Unknown or failed Git probes are not clean
state. Workspace hooks run before built-in teardown, with worker name as `$1`
and `FLOATIE_WORKER`, force as `FLOATIE_FORCE`, cwd `~/work/stack`, stdin closed,
and secrets not automatically sourced. Hook failures can leave partial effects.
`worker-rm true` bypasses only hook failure. Built-in teardown omits volume
removal, but a workspace hook may remove volumes. Unrelated branches remain.

Stopping the VM preserves disk but ends processes. Starting does not resume
agents. Destroy/rebuild delete disk after a preservation guard; their `true`
argument bypasses that guard. These operations are not worker removal or a tool
upgrade. See [commands](commands.md) before choosing a lifecycle action.

## Local UI

The host Python server serves checked-in `ui/dist` and the workspace API. Users
do not need Node. Workspace displays observations and generates terminal commands;
Settings covers resources, repositories, network, plugins and MCP configuration.
Saving configuration is separate from applying it to the VM.

Drafts and view state live in memory. Conflicting file edits require review and
explicit saving; an external editor can still race after the final byte check.
Failed observations mean unknown or stale state. After an unknown action outcome,
inspect and refresh before retrying. If the workspace VM identity changes, close
and reopen the UI from the intended workspace. UI process registration and logs
live under `${XDG_STATE_HOME:-~/.local/state}/floatie/`; stopping the UI verifies
its process identity and does not stop the VM.

See [UI contributor instructions](../../ui/README.md) for source/build parity and
[security](security.md) for the loopback/token boundary.
