# A Floatie workspace

This repository holds configuration and shared context for one Floatie VM.
Install Floatie separately; copy this starter into a new private workspace and
replace the examples before creating the VM.

| File | Purpose |
|---|---|
| `workspace.yml` | VM resources, repository scope, Git identity, network policy and optional plugins |
| `AGENTS.md` | Shared repository map, verification commands and working agreements; placed at `~/work/AGENTS.md` |
| `CLAUDE.md` | Imports `AGENTS.md`; placed at `~/work/CLAUDE.md` |
| `.agents/skills/orchestrate/` | Shared coordination skill and worker brief |
| `.mcp.json.example` | Optional integration examples; copy to `.mcp.json` only when needed |
| `.env.secrets.example` | Optional credential placeholders; keep actual `.env.secrets` private |
| `.floatie-local/` | Ignored local work board and handoffs; never export |

## Setup

Edit `workspace.yml` and `AGENTS.md` to name your repositories, roles, interfaces
and verification commands. Configure Git signing and verify signed commits before
agent delivery. If credentials are required, copy `.env.secrets.example` to
`.env.secrets`, restrict it with `chmod 600 .env.secrets`, and fill in only the
required values. Reference secrets as `${VAR}` in optional `.mcp.json` entries;
never put secret values in tracked files.

On the host, from this workspace directory:

```sh
floatie create
# For Codex, after create:
floatie codex-setup
floatie attach
```

Inside the VM, authenticate the harness you intend to use: `codex login
--device-auth`, or start `claude` and use `/login`. Login is separate from the
workspace's secrets file. Detach from tmux with `Ctrl-b d`. For configured Claude
plugins, run `floatie reload` on the host after login to retry installation.
Missing optional integration authentication should not block unrelated local work.

Start or reuse the root from the host with `floatie orchestrate codex` or
`floatie orchestrate claude`. The root uses the shared orchestrate skill and
installed guest commands to discover, create and communicate with workers.
Use `floatie worker review repo-a` to create/reconnect a worker with that exact
scope, and `floatie attach review` to attach its terminal. Substitute your actual
repository and worker names. Each worker has one primary agent; changing the
harness argument does not replace an already-running agent.

## Shared guidance and skills

`create` and `reload` copy each present instruction file literally into `~/work/`.
Missing source files leave existing guest guidance intact. Each transferred file
is checked before replacement; the two files are not a single transaction.
Edit `AGENTS.md` as the source of shared policy; keep `CLAUDE.md` as its import.
Codex's managed global guidance remains separate.

Shared skills under `.agents/skills/` are copied to both `~/work/.agents/skills/`
and `~/.claude/skills/` in the guest. Use real directories where possible;
symlinks are materialized during placement. Treat skill contents as trusted code.
Existing custom `.claude/skills/` sources remain supported, with shared sources
applied afterwards for matching names. Unrelated installed skills are preserved;
removing a source skill does not remove its guest copy.

An existing workspace adopts this starter by explicitly reviewing and merging
its guidance, preserving custom policy first. `reload` can overwrite guest
instructions, skills and stack configuration. Do not use it just to switch
harnesses or tool checkouts, and do not restart live agents or rebuild a VM to
adopt these files. Save a handoff for any authorized harness change.

## Local UI and lifecycle

Run `floatie ui` on the host from this directory. Workspace shows the root and
workers; Settings edits resources, repositories, agents/Git, network and
credentials. Saving does not apply changes: follow the displayed guidance.
Repository removal from configuration does not delete existing clones, and
removing plugins does not uninstall them. Keep credentials in `.env.secrets`;
running processes may need a deliberate restart or login after credential changes.

`floatie stop` shuts down the VM; `floatie start` does not resume agents. Worker
removal is a separate guarded action. Before cleanup, inspect all scoped repos
for dirty/untracked files, unpushed commits and dependent work. Preserve running
sessions and unfinished work. The VM shares credentials and can read the mounted
host home; it is not a per-worker security boundary.
