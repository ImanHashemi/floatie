# Floatie 🛟

Floatie gives coding agents a sandbox on your Mac: one local Linux VM that holds
your repositories, your development stack and the agents that work on them.

Agents such as Claude Code and Codex run inside the VM, next to Docker and your
services. They build, start and test the real stack there, not on your Mac. Every
task can get its own **worker**: a separate checkout, terminal and copy of the
stack, so several agents work in parallel without getting in each other's way.

## What you get

- **One VM per workspace.** Ubuntu with Docker, Git, the GitHub CLI, Node, uv,
  `just`, tmux and Claude Code installed. Codex is one command away.
- **Your repositories inside it**, cloned to `~/work/<repo>`.
- **Parallel workers.** Each worker has a Git worktree of your repositories on
  its own branch, its own terminal and its own Docker Compose project name.
- **Guard rails.** An allowlist for outbound network traffic, a GitHub token
  limited to the repositories you choose, and commits signed with a key that
  stays in the VM.
- **Optional extras.** A local web UI, and an orchestrator agent that hands work
  to workers.

Floatie is for trusted development work, not a jail for hostile code. Read
[what the sandbox protects](#what-the-sandbox-protects) before you start.

## Requirements

macOS on Apple Silicon with [Lima](https://lima-vm.io) 2.1 or newer,
[`just`](https://github.com/casey/just), Git and Python 3.9 or newer.

```sh
brew install lima just        # Git and Python 3 come with: xcode-select --install
```

## Quick start

1. **Install Floatie.**

   ```sh
   git clone https://github.com/ImanHashemi/floatie.git
   cd floatie
   just install               # links `floatie` into ~/.local/bin; put that on your PATH
   ```

2. **Create a workspace.** A workspace is a small directory, usually its own
   private Git repository, that describes one VM.

   ```sh
   cp -R workspace-template ../my-workspace
   cd ../my-workspace
   cp .env.secrets.example .env.secrets && chmod 600 .env.secrets
   ```

   Edit `workspace.yml`: name the VM and list your repositories. Your Git name
   and email are optional: when `workspace.yml` leaves them out, Floatie uses the
   ones from your Mac's git config. For private repositories, put a GitHub token in `.env.secrets`.

3. **Create the VM.** Run every `floatie` command from the workspace directory.

   ```sh
   floatie create             # build the VM, clone the repositories, apply the firewall
   floatie signing-key        # prints a public key: add it to GitHub as a Signing Key
   ```

4. **Log in and start working.**

   ```sh
   floatie attach             # a terminal in the VM, at ~/work
   ```

   Inside, run `claude` and use `/login`. For Codex, run `floatie codex-setup` on
   the Mac first, then `codex login --device-auth` inside the VM. Detach with
   Ctrl-b d; the terminal and the agent keep running.

## Working in parallel

```sh
floatie worker fix-login      # new worker with every repository, then attach to it
floatie worker docs api       # new worker with only the `api` repository
floatie workers               # list workers and whether their terminal is live
floatie attach fix-login      # reconnect to a worker's terminal
floatie worker-rm fix-login   # remove it; refuses while work is uncommitted or unpushed
```

A worker lives at `~/work/.worktrees/<name>/<repo>` on branch `worker/<name>`.
Start an agent in its terminal and it works there, apart from the others.

`floatie stop` shuts the VM down and `floatie start` boots it again. The disk is
kept; running terminals and agents are not.

## Workspace layout

```text
my-workspace/
├── workspace.yml         # the VM: name, size, Git identity, repositories, allowed domains
├── AGENTS.md             # instructions for the agents: what each repository is, how to test it
├── CLAUDE.md             # one line that imports AGENTS.md for Claude Code
├── .env.secrets          # tokens; never committed
├── .floatie-mcp.json     # optional: MCP servers for the agents in the VM
├── .agents/skills/       # optional: skills shared by the agents
└── stack/                # optional: what your services need to start
```

Only `workspace.yml` is required. A small one looks like this:

```yaml
name: my-project
cpus: 4
memory: 8GiB
git_user_name: Jane Smith
git_user_email: jane@example.com
repos:
  - github.com/my-org/api
  - github.com/my-org/web
```

The two `git_user_*` lines are optional and override your Mac's git identity; leave them out to use it.

After you change workspace files, `floatie reload` copies instructions, skills,
MCP servers and `stack/` into the running VM. It replaces the copies in the VM.

## Running your stack

Docker runs inside the VM, so `docker compose up` in a repository works as it
does on your Mac. Ports your services publish are forwarded to `localhost` on
the Mac.

If your services need shared files to start, such as a Compose file or scripts,
put them in `stack/`. Floatie copies that directory to `~/work/stack`.

In a worker's terminal, Docker Compose uses the worker's name as its project
name, so each worker can run its own copy of the stack. Give each copy its own
ports. Floatie never starts your stack for you.

## Orchestration (optional)

You can drive every worker yourself. To hand that to an agent instead, run:

```sh
floatie orchestrate           # Codex; or: floatie orchestrate claude
```

This starts a root agent in the main terminal. With the `orchestrate` skill from
the starter workspace it can create workers, start an agent in each, send them
prompts and read their output. It only works while its own turn runs; nothing
supervises in the background.

## Web UI (optional)

`floatie ui` starts a page on `127.0.0.1`. **Workspace** shows the VM and your
workers, with the command to attach to each. **Settings** edits `workspace.yml`
and credentials. Saving a setting does not apply it; the page names the command
that does.

## What the sandbox protects

- The VM has its own kernel and disk. Agents have full access inside it,
  including sudo.
- Your Mac's home directory is mounted **read-only** in the VM. Agents cannot
  change your files, but they can read them, including secrets stored there.
- Workers share one VM user, its credentials and its Docker. They keep work
  apart; they are not a security boundary between agents.
- The network allowlist is best effort, and an agent with sudo can change it.

The [security model](docs/guides/security.md) has the details.

## More

- [Installation, configuration, upgrades and troubleshooting](docs/guides/installation.md)
- [Command reference](docs/guides/commands.md), or run `floatie help`
- [Architecture and worker lifecycle](docs/guides/architecture.md)
- [Vulnerability reporting](SECURITY.md) and [contributing](CONTRIBUTING.md)

Floatie's own code is under the [MIT License](LICENSE). Bundled third-party code
keeps [its own licenses and notices](ui/THIRD_PARTY_NOTICES.txt).
