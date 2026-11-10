# Command reference

Run `floatie` on your Mac, from the workspace directory: the one that contains
`workspace.yml`. Floatie does not search parent directories. `floatie help`
prints a short version of this page.

## Setup

| Command | What it does |
|---|---|
| `just install` | Run in your Floatie clone. Links the `floatie` command into `~/.local/bin`, or into `FLOATIE_BIN_DIR`. |
| `floatie create` | Creates the VM, or starts it, and brings it in line with the workspace: tools, Git identity, secrets, plugins, missing clones, instructions and the network allowlist. Safe to run again. |
| `floatie codex-setup` | After `create`: installs Codex, checks its sandbox, and applies its configuration and the allowlist. |
| `floatie signing-key` | Prints the VM's public signing key and how to register it. |

## Daily use

| Command | What it does |
|---|---|
| `floatie attach [worker]` | Opens the main terminal, or a worker's terminal, in a running VM. Does not start an agent. |
| `floatie worker <name> [repo]` | Creates a worker with every repository, or with one, and attaches to it. If the worker exists, it reconnects with the repositories the worker already has. |
| `floatie workers` | Lists workers and whether their terminal is live. Does not start the VM. |
| `floatie start` | Boots a stopped VM and applies the allowlist. Does not restart agents. |
| `floatie stop` | Shuts the whole VM down. The disk is kept; terminals and agents end. |
| `floatie reload` | Copies instructions, MCP servers, `stack/`, tools and skills into a running VM. Also refreshes Codex's configuration and retries Claude plugins. |
| `floatie firewall [off]` | Applies the allowlist from `workspace.yml`. `off` allows all traffic until the allowlist is applied again. |
| `floatie help` | Shows short help. So do `floatie`, `floatie --help` and `floatie -h`. |

`floatie reload` needs a running VM and replaces the copies in the VM. It does
not apply secrets, Git identity, new clones, VM size or the allowlist.
[Apply a change](installation.md#apply-a-change) lists which command applies
what.

When the disk that holds Lima's VMs has less than 10 GiB free, `create`,
`start`, `reload` and `worker` refuse to run. From 10 to 19 GiB they warn.
`attach` only warns. `FLOATIE_SKIP_DISK_CHECK=1` skips the check, also when the
free space cannot be measured. It does not fix a full disk.

## Web UI

| Command | What it does |
|---|---|
| `floatie ui [port]` | Starts the local UI in the background, or reuses the one that runs. The port is 8787 unless you give another. |
| `floatie ui-stop` | Stops this workspace's UI. Does not stop the VM. |
| `floatie ui-logs` | Follows the UI's log. Ctrl-C stops watching. |
| `floatie ui-fg [port]` | Runs the UI in the foreground, for debugging. |

## Removing things

| Command | What it does |
|---|---|
| `floatie worker-rm <name> [true]` | Removes a worker: its terminal, its Compose project, its worktrees and the branch `worker/<name>`. |
| `floatie destroy [true]` | Deletes the VM and its disk. |
| `floatie rebuild [true]` | Deletes the VM and creates it again from the workspace. |

Floatie looks for unsaved work first:

- `worker-rm` refuses when a worktree has uncommitted changes, commits that are
  not pushed, or a Git state it cannot read. That includes Git flags that hide
  changes to tracked files. `true` only skips a failing workspace hook; it never
  skips these checks.
- `destroy` and `rebuild` refuse when any repository or worktree in the VM has
  uncommitted or unpushed work. `true` skips that check and can lose work.
- A stopped VM may be started to run the check. It stays running if the check
  refuses.
- A removal that fails halfway is not undone. The hook, the terminal or the
  Compose project may already be gone. Look at what is left before you retry.
- Floatie's own Compose teardown keeps volumes. Your `worker-rm` hook may delete
  them.
- Branches other than `worker/<name>` are kept.

[Removing a worker](architecture.md#removing-a-worker) explains the checks.

## Worker names

A new worker name starts with a lowercase letter or a digit. After that it may
contain lowercase letters, digits, `_` and `-`. It has at most 63 characters.
`main`, `stack`, `workspace` and `all` are reserved.

Reconnecting to a worker never adds repositories and never resets branches.
Asking for a different set of repositories fails. If creation stopped halfway,
look at what was created before you reconnect or remove the worker.

## Orchestration and automation

These commands let an agent, or a script, drive other agents.

| Command | What it does |
|---|---|
| `floatie orchestrate [codex\|claude]` | Starts the root agent in the main terminal, or reuses it, and attaches. Codex unless you say `claude`. |
| `floatie agent-start <target> [codex\|claude]` | Starts an agent in a worker, or in `main`, without attaching. If one is running, it is reused. Codex unless you say `claude`. |
| `floatie agent-list` | Lists running agents and their terminal panes as JSON, including agents you started by hand. |
| `floatie agent-read <target> [--pane %ID]` | Prints the end of an agent's terminal output, and a cursor. |
| `floatie agent-send <target> [--pane %ID]` | Sends the text on standard input to the agent as a prompt. |
| `floatie agent-wait <target> [--pane %ID] [--cursor <cursor>] [--seconds <n>]` | Waits up to `n` seconds for new output from one agent. |
| `floatie agent-wait <target> --target <other> [--target <other>]… [--cursor <multi_cursor>] [--seconds <n>]` | The same for several agents. `--pane` is not allowed here. |

```sh
floatie agent-list
floatie agent-read review --pane %42
# Take `cursor` from that response, then:
floatie agent-wait review --pane %42 --cursor "$cursor" --seconds 10
# For several agents, take a first snapshot with:
#   floatie agent-wait review --target other --seconds 0
# Take `multi_cursor` from that response, then:
floatie agent-wait review --target other --cursor "$multi_cursor" --seconds 10
printf '%s\n' 'Report progress.' | floatie agent-send review --pane %42
floatie agent-send review --pane %42 < prompt.txt
```

Rules:

- Use pane IDs that `agent-list` returned. `%42` above is only an example.
- A `cursor` comes from a read or wait of the same agent and pane. A
  `multi_cursor` comes from an earlier wait with the same agents in the same
  order. Leave `--cursor` out for the first snapshot.
- A prompt is 1 to 65,536 bytes of UTF-8. Blank prompts are rejected, and so are
  control characters other than newline and tab.
- Sending a prompt approves nothing. New output, a running process or a quiet
  terminal do not mean a task is finished.
- Nothing keeps watching after a command returns.
- A Codex agent that Floatie starts gets full access inside the VM. An agent
  that is already running keeps its settings.
- Older agent names with capital letters or 64 characters still work with these
  commands. New workers follow the naming rule above.

## Commands inside the VM

Inside the VM there is no `floatie` command. Agents use a small `just` file that
Floatie installs:

```sh
just --justfile "$HOME/.local/share/floatie/agents.just"
just --justfile "$HOME/.local/share/floatie/agents.just" worker-create review api web
just --justfile "$HOME/.local/share/floatie/agents.just" agent-start review codex
just --justfile "$HOME/.local/share/floatie/agents.just" agent-list
```

The first line lists the recipes: `orchestrate`, `worker-create`, `agent-start`,
`agent-list`, `agent-read`, `agent-send` and `agent-wait`. They take the same
arguments as the commands above. `worker-create` always needs the repositories
named; replace `api` and `web` with your own. To reconnect to a live terminal,
run `tmux attach-session -t '=review'`. Ctrl-b d detaches.

## The `just` runner underneath

`floatie` hands every other argument to `just`:

| Command | What it does |
|---|---|
| `floatie --list` | Lists the recipes. |
| `floatie --show <recipe>` | Shows a recipe's source. |
| `floatie --usage <recipe>` | Shows a recipe's arguments. |
| `floatie --version` | Prints the version of `just`, not of Floatie. |

Only `floatie`, `floatie help`, `floatie --help` and `floatie -h` show Floatie's
own help. Recipes whose name starts with `_` are internal and may change.
