# Installation and setup

This guide covers installing Floatie, describing a workspace, creating the VM
and keeping everything up to date. The [README](../../README.md) has the short
version.

## What you need

- macOS on Apple Silicon.
- [Lima](https://lima-vm.io) 2.1 or newer and
  [`just`](https://github.com/casey/just): `brew install lima just`.
- Git and Python 3.9 or newer. Both come with the Xcode Command Line Tools
  (`xcode-select --install`).
- Free memory and disk for the VM and the services you run in it.

You do not need Docker Desktop. Node is only needed to work on the UI's source;
the built UI is checked in. The VM runs Ubuntu 24.04 on Apple's virtualization
framework.

## Install the `floatie` command

From your clone of Floatie:

```sh
just install
```

This links `bin/floatie` into `~/.local/bin`. Put that directory on your PATH, or
set `FLOATIE_BIN_DIR` to link the command somewhere else. The command always
runs the clone it was installed from, and treats the directory you run it in as
the workspace.

## Set up a workspace

A workspace is a directory that describes one VM. Copy the starter, hidden files
included:

```sh
cp -R workspace-template ../my-workspace
cd ../my-workspace
cp .env.secrets.example .env.secrets
chmod 600 .env.secrets
```

Every name in the starter is an example. Replace them with your own.

### `workspace.yml`

| Key | Meaning |
|---|---|
| `name` | Name of the VM. It must be unique on your Mac. |
| `cpus`, `memory` | Size of the VM, for example `4` and `8GiB`. |
| `git_user_name`, `git_user_email` | Author of the commits made in the VM. The email must be verified on your GitHub account, or GitHub shows the commits as unverified. |
| `repos` | Repositories to clone to `~/work/<repo>`. |
| `egress` | Extra domains the VM may reach. Anthropic, OpenAI, GitHub, npm, PyPI, Docker registries and Ubuntu packages are allowed already. |
| `plugins` | Claude Code plugin marketplaces and plugins to install. |
| `permissive` | `true` turns the network allowlist off. |

Things to know:

- Write plain values on one line and lists as `- item` lines. Quoted values and
  inline lists such as `[a, b]` are not read.
- A repository can be `github.com/owner/repo`, a full clone URL, or a local path
  the VM can read. Never put credentials in a URL.
- A repository that is already cloned is skipped. Changing its URL later does
  not replace the clone.
- List a plugin marketplace before the plugins that come from it.

### Instructions for the agents

`AGENTS.md` tells the agents what each repository is for, how to test it, and
what they may and may not do. `CLAUDE.md` is one line that imports `AGENTS.md`,
so Claude Code and Codex read the same text. Floatie copies both files to
`~/work` in the VM.

### Secrets

`.env.secrets` holds tokens. Floatie copies it into the VM. Never commit it, and
never commit private notes or handoffs either.

For private GitHub repositories, create a fine-grained token and store it as
`GITHUB_TOKEN`:

- Select only the repositories this workspace uses.
- Grant **Contents: read and write**, so agents can push.
- Grant **Pull requests: read and write** if agents should open pull requests.

Git and the GitHub CLI in the VM both use this token. If it is missing, Floatie
warns and carries on, so work that does not need GitHub still runs. If a clone
fails, fix the access and run `floatie create` again; finished clones are kept.

### Your stack (optional)

Put the files your services need to start, such as a Compose file or scripts,
in `stack/`. Floatie copies the directory to `~/work/stack` in the VM.

- If `stack/.env.vm` exists, it becomes `.env` in the VM copy.
- Floatie adds an empty `.floatie-vm` file there, so your scripts can tell they
  run in the VM.
- An optional `stack/hooks/worker-rm` script runs when a worker is removed. See
  [removing a worker](architecture.md#removing-a-worker).

## Create, sign and log in

Run these on your Mac, from the workspace directory.

1. **Create the VM.**

   ```sh
   floatie create
   ```

   This builds the VM, copies your secrets, clones the repositories, copies your
   instructions and skills, sets up Git and applies the network allowlist. It is
   safe to run again: it adds what is missing and applies your settings again.

2. **Register the signing key.** Floatie creates an SSH key in the VM and signs
   every commit with it.

   ```sh
   floatie signing-key
   ```

   Add the printed **public** key to your GitHub account as a **Signing Key**,
   not as an authentication key. The private key stays in the VM. Stopping and
   starting the VM keeps the key. Rebuilding the VM makes a new one, which you
   register again.

3. **Log in to your agent.**

   ```sh
   floatie attach
   ```

   - Claude Code: run `claude`, then `/login`.
   - Codex: run `floatie codex-setup` on your Mac first. Then run
     `codex login --device-auth` in the VM and finish the login in your browser.

   Detach with Ctrl-b d. The terminal keeps running.

4. **Start working.** Run your agent in that terminal, or create workers with
   `floatie worker <name>` to run several agents in parallel. Orchestration is
   optional: `floatie orchestrate` starts a root agent that coordinates the
   workers. It uses Codex unless you run `floatie orchestrate claude`.

Each agent keeps its own login and its own conversation history. To move a task
from one agent to the other, write down the state of the work first. Never
replace an agent that is still working.

### What `codex-setup` does

- It installs Codex in the VM and what its sandbox needs: bubblewrap, and the
  packaged AppArmor profile on Ubuntu versions that restrict user namespaces.
- It checks that the sandbox starts. This makes no AI request.
- It applies your MCP servers and instructions to Codex, and the network
  allowlist.

It does not turn off the VM-wide namespace restriction, and it does not change
approval or sandbox preferences you saved. Codex agents that Floatie starts get
full access inside the VM and ask for approval on request. Agents that are
already running are left as they are. See the [security model](security.md).

## MCP servers, plugins and skills (optional)

### MCP servers

Copy `.floatie-mcp.json.example` to `.floatie-mcp.json` and keep only the servers
you need. The file uses the usual `mcpServers` format.

- Refer to secrets as `${VAR}` and put the values in `.env.secrets`.
- Add each server's domain to `egress`. A server that logs in with OAuth usually
  needs its login domain as well.
- `.floatie-mcp.json` is the only MCP file Floatie reads. A `.mcp.json` in the
  same directory belongs to Claude Code on your Mac. Floatie ignores it and never
  copies it into the VM.

How the servers reach each agent:

| | Claude Code | Codex |
|---|---|---|
| Where they go | User settings in the VM | A block that Floatie manages in Codex's configuration |
| Applied by | `floatie create`, `floatie reload` | `floatie codex-setup`, `floatie reload` |
| OAuth login | `/mcp` inside Claude | `codex mcp login <server-name>` |
| Server removed from the file | Stays in the VM; remove it with `claude mcp remove <name>` | Removed |
| File missing or empty | Existing servers stay | Servers Floatie manages are removed; your own stay |

More to know:

- Each agent has its own MCP logins. A configured server is not a logged-in
  server.
- Codex supports stdio and streamable HTTP servers. An older SSE server needs an
  HTTP endpoint from its provider.
- If a server name clashes with one you added to Codex yourself, Floatie stops
  instead of overwriting it.
- For stdio servers, the configuration Floatie generates for Codex can contain
  the secret values. Only the VM user can read that file. Never commit it, and
  run `floatie reload` after you change those secrets.
- HTTP servers read their secrets when they connect, so a rotated value needs no
  new configuration. The new value still has to reach the VM: `floatie create`
  copies `.env.secrets`, `floatie reload` does not.
- Older versions of Floatie read `.mcp.json` instead. Neither version falls back
  to the other name. When you update across that change, rename the file in the
  same step, then run `floatie reload`. To roll back, restore the older Floatie
  together with its `.mcp.json`.

### Claude Code plugins

List them under `plugins` in `workspace.yml`. They install with your Claude
login, so before you log in Floatie warns and skips them. After `/login`, run
`floatie reload` to install them.

### Skills

Put shared skills in `.agents/skills/<skill>/SKILL.md`. Both agents get them.
Skills in `.claude/skills` still work.

Skills, plugins and MCP servers run with the agent's access. Only add ones you
trust. A missing login for an optional integration is no reason to stop work
that does not need it.

## Apply a change

| You changed | Run |
|---|---|
| `AGENTS.md`, `CLAUDE.md`, skills, `.floatie-mcp.json`, `stack/` | `floatie reload` |
| `.env.secrets`, Git name or email, the repository list | `floatie create` |
| `egress` or `permissive` | `floatie firewall` |
| `plugins` | `floatie reload`, after you logged in to Claude |
| `cpus` or `memory` | `floatie rebuild`, which deletes the VM disk |

`floatie reload` replaces the copies in the VM. If you or an agent edited those
files inside the VM, save the edits first. Programs that are already running
keep the secrets they started with; new shells get the new values. Removing a
repository from `workspace.yml` does not delete its clone. The
[command reference](commands.md) lists what every command does.

## Update Floatie

1. Update your Floatie clone to a version you have reviewed.
2. Changed scripts and commands take effect the next time you run `floatie`.
3. To load a new UI, restart it: `floatie ui-stop`, then `floatie ui`.

Updating does not touch the VM, your worktrees, running agents or logins. If the
UI answers with error 503 and says the build is missing, the clone is
incomplete. Restore the full checkout; you do not need Node.

### Use another clone, or roll back

`floatie` runs the clone it was installed from. To switch, run `just install` in
the other clone, with the same `FLOATIE_BIN_DIR` if you set one. Keep the old
clone: running `just install` there switches back. Restart the UI afterwards if
it is running. Nothing in the VM changes, so you do not need `floatie reload` or
`floatie create`.

## Troubleshooting

### The VM reports I/O errors, or your Mac's disk is full

The VM's disk is a file on your Mac that grows as it is used. The VM can show
free space while your Mac's disk is full, and then it gets I/O errors.

1. Free space on your Mac first.
2. Agree a restart with whoever has work running in the VM, then restart it.
3. In the VM, run `sudo fstrim -av` to hand unused space back.

Do not destroy the VM to investigate. Uncommitted work lives on its disk.

Floatie checks the free space where Lima keeps its VMs: `$LIMA_HOME`, or
`~/.lima`. Below 10 GiB, `create`, `start`, `reload` and `worker` refuse to run.
From 10 to 19 GiB they warn. `FLOATIE_SKIP_DISK_CHECK=1` skips the check, also
when the free space cannot be measured. It does not fix a full disk.

### A terminal dies about 90 seconds after a process ran out of memory

When the VM kills a process for using too much memory, systemd can stop the
whole terminal session a little later (`OOMPolicy=stop`).

- Read `journalctl` in the VM, including the minutes before the terminal died.
- Run heavy jobs with a memory limit:
  `systemd-run --user --scope -p MemoryMax=<limit> <command>`.
- Floatie adds no swap and does not change this policy.
- Save your work before you change session services or restart the VM.

### A removed or changed secret is still in use

A secret lives in four places: `.env.secrets`, shell startup files, running
shells and tmux's environment. Changing the file does not change the others.

- Remove the old value in each place, without printing it.
- For tmux, run `tmux set-environment -gu VAR`.
- Unset it in your own shells and restart the programs that use it.
- If a secret leaked, rotate it where it was issued. Deleting your copy does not
  take it away from a program that already has it.

### A service or login cannot be reached

- Read the error before you change network settings.
- Run `floatie firewall` again. The allowlist stores IP addresses, and those
  change.
- For OAuth, allow both the service's domain and its login domain.
- Check the login of each agent and each MCP server separately.
- Do not leave the allowlist off as the fix.

### Copying text or scrolling in the terminal

- The mouse wheel scrolls. Ctrl-b u enters scroll mode; `v` selects and `y`
  copies.
- New terminal panes keep 50,000 lines of history. Panes that were already open
  keep their old limit.
- Shift-drag uses your terminal's own selection, where it supports that.
- Another terminal multiplexer on your Mac can swallow clipboard copies. Try a
  plain terminal window.
- Floatie adds its tmux settings as one marked block in `~/.tmux.conf` and loads
  it into a running tmux. Settings above the block can be overridden by it. Keep
  the marker line in your own configuration to stop Floatie from adding the
  block.

### The UI port is in use, or the UI does not start

- Read `floatie ui-logs`, or run `floatie ui-fg` to see errors directly.
- Do not kill a process you do not know to free the port. Pick another port,
  for example `floatie ui 8790`.
- If you cannot tell whether an action worked, look at the state and refresh
  before you try again.
- Stopping the UI never stops the VM.
