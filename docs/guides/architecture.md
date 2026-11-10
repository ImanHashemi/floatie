# How Floatie works

Floatie keeps two things on your Mac and one VM that does the work.

```text
Your Mac                              The VM (Lima, Ubuntu)
────────                              ─────────────────────
Floatie clone                         ~/work/                      main terminal
  the `floatie` command   ─ runs ─▶   ├── api/  web/               your repositories
                                      ├── AGENTS.md  CLAUDE.md     copied from the workspace
Workspace directory                   ├── stack/                   copied from the workspace
  workspace.yml           ─ copy ─▶   └── .worktrees/
  AGENTS.md, stack/, …                    ├── fix-login/api, web   a worker and its terminal
                                          └── docs/api             another worker
Browser ◀─ `floatie ui`
                                      Docker, with one Compose project per worker
```

## On your Mac

- **The Floatie clone** holds the `floatie` command, the scripts that build the
  VM, and the web UI.
- **A workspace directory** describes one VM: its name and size, the
  repositories, the instructions for the agents, secrets, optional MCP servers
  and an optional stack.

`floatie` always runs the clone it was installed from. It uses the directory you
run it in as the workspace and does not search parent directories. One Floatie
clone can serve several workspaces.

## Inside the VM

Lima runs the VM with Apple's virtualization framework. The VM runs Ubuntu and
rootless Docker.

| Place in the VM | What it is |
|---|---|
| `~/work/<repo>` | Your repositories, cloned onto the VM's disk |
| `~/work` | The main terminal. It sees every repository. The optional root agent runs here |
| `~/work/.worktrees/<worker>/<repo>` | A worker's worktrees, on branch `worker/<worker>` |
| `~/work/stack` | Your optional stack files and hooks |

Floatie never starts your services. In a worker's terminal Docker Compose uses
the worker's name as its project name, so workers can run copies of the stack
side by side. The names do not keep ports apart: give each copy its own ports.

Workers keep work apart. They are not a security boundary: every worker shares
one kernel, one user, the same credentials and one Docker. See the
[security model](security.md).

## Workers

A worker is a set of Git worktrees plus a terminal.

**Creating one.** Floatie makes a worktree of each repository on branch
`worker/<name>` and opens a terminal next to them.

- Before it does, it updates each clone to the latest default branch, but only
  when that is safe: the clone is clean, on its default branch and has no local
  commits.
- Any other clone is left alone, and Floatie says why.
- If fetching fails, Floatie warns and creates the worker from the code it has.
- If the branch `worker/<name>` already exists, it is reused as it is. Check
  where it starts when fresh code matters.

**Reconnecting.** Running `floatie worker <name>` again uses the repositories
the worker already has. Nothing is updated and no branch is reset. Asking for a
different set of repositories fails, even when the terminal is live.

**When creation stops halfway.** What was created stays. Look at which
repositories exist and which are missing. Then reconnect with the ones that
exist, or save the work, remove the worker and create it again. Creating it
again never adds repositories on its own.

## Removing a worker

`floatie worker-rm <name>` checks every worktree before it removes anything. It
refuses when it finds:

- uncommitted or untracked files,
- commits that are not pushed,
- Git flags that hide changes to tracked files,
- a Git state it cannot read. Unknown is never treated as clean.

Then it runs, in this order:

1. Your `stack/hooks/worker-rm` script, if the workspace has one.
2. Floatie's own teardown: the terminal, the worker's Compose project, the
   worktrees and the branch `worker/<name>`.

About the hook:

- It gets the worker's name as its first argument and as `FLOATIE_WORKER`.
  `FLOATIE_FORCE` tells it whether removal was forced.
- It runs in `~/work/stack`, with no input, and without your secrets loaded.
- If it fails, removal stops, and what it already did stays done.
  `floatie worker-rm <name> true` continues past a failing hook. It skips
  nothing else.

Floatie only takes down a Compose project it can tie to this worker. Its
teardown keeps volumes; your hook may delete them. Other branches are kept.

## Stopping and deleting the VM

- `floatie stop` shuts the VM down. The disk is kept. Terminals and agents end.
- `floatie start` boots it again. It does not restart agents.
- `floatie destroy` and `floatie rebuild` delete the disk. They first check for
  uncommitted or unpushed work; adding `true` skips that check.

None of these removes a single worker, and none updates Floatie. Read the
[command reference](commands.md) before you pick one.

## Instructions, skills and MCP servers

`floatie create` and `floatie reload` copy these from the workspace into the VM:

| From the workspace | To the VM |
|---|---|
| `AGENTS.md`, `CLAUDE.md` | `~/work` |
| `.agents/skills/` | `~/work/.agents/skills` and Claude Code's own skills folder |
| `.claude/skills/` | Claude Code's own skills folder |
| `.floatie-mcp.json` | Claude Code's user settings, and a block Floatie manages in Codex's configuration |
| `stack/` | `~/work/stack` |

The starter keeps the shared rules in `AGENTS.md`. `CLAUDE.md` only imports it.

For Codex, Floatie also keeps a block in Codex's global instructions. It takes
the text from `AGENTS.md`, or from `CLAUDE.md` when there is no `AGENTS.md`.
Other global instructions are kept. A Codex `AGENTS.override.md` can take
precedence over the block.

`.floatie-mcp.json` is the only MCP file Floatie reads. The web UI reads it too,
to show which credentials are set. A `.mcp.json` belongs to Claude Code on your
Mac; Floatie ignores it. [Installation](installation.md#mcp-servers) has the
details for each agent.

## Orchestration (optional)

`floatie orchestrate` starts a root agent in the main terminal. Its first prompt
points it to the `orchestrate` skill. With the
[commands inside the VM](commands.md#commands-inside-the-vm) it can:

- create a worker with the repositories you name,
- start an agent in that worker, or reuse the one running there,
- find agents and their terminal panes,
- send an agent a prompt, and read or wait for its output.

How this behaves:

- **Workers are not subagents.** An agent's own subagents are separate from
  Floatie workers. Run one main agent per worker, so two agents never write to
  the same files.
- **Finding agents.** Floatie looks at the live terminals and processes,
  including agents you started by hand. Use the pane IDs it returns, especially
  after a restart. Do not guess them.
- **Sending and waiting.** A prompt is sent exactly as written. Waiting watches
  output for a limited time. It cannot tell that a task is done, and it approves
  nothing. The agent that asked judges the result, within the rules of your
  workspace.
- **No supervisor.** The root agent only works during its own turn. Nothing
  keeps watching afterwards.
- **Codex or Claude.** `orchestrate` uses Codex unless you say `claude`. A Codex
  agent that Floatie starts gets full access inside the VM and asks for approval
  on request. An agent that is already running is reused with the settings it
  has. Asking for the other agent does not convert it or move its conversation.
  To change agents, write a handoff first: the task, the branch, the decisions,
  what was tested and what is left. `floatie reload` copies files; it does not
  change agents or move history.

## The web UI

The UI is a small Python server on your Mac. It serves the built UI from the
Floatie clone, so you do not need Node.

- **Workspace** shows what Floatie can see in the VM and gives you the terminal
  commands to run.
- **Settings** edits resources, repositories, agents and Git identity, the
  network allowlist and credentials. Saving a setting does not apply it to the
  VM.

Things to know:

- Unsaved edits and the view you are on live in memory only.
- If a file changed on disk while you edited it, the UI asks you to review
  before saving. An editor that writes at the same moment can still win.
- When the UI cannot read the VM, treat what it shows as unknown or out of date.
- When you cannot tell whether an action worked, look at the state and refresh
  before you try again.
- If the workspace starts pointing at another VM, close the UI and open it again
  from the right workspace.
- The UI's registration and logs live under
  `${XDG_STATE_HOME:-~/.local/state}/floatie/`. Stopping the UI checks that the
  process really is this workspace's UI, and never stops the VM.

See the [UI contributor notes](../../ui/README.md) for how the UI is built, and
the [security model](security.md) for who can reach it.
