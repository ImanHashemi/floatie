# Floatie 🛟

Run coding agents and your development stack in a local Lima VM. Floatie provides
repository clones, Git worktrees, tmux terminals, agent communication commands,
and an optional Workspace / Settings UI. Use Codex or Claude Code with your own
accounts and workspace policy.

Supported host: **macOS on Apple Silicon**, with Lima **2.1 or newer**, `just`,
Git, and Python **3.9 or newer** on PATH. Node is needed only to contribute to the
UI; users run the checked-in build. Leave enough host memory and disk for the VM
and your services; running Docker Desktop alongside the VM uses extra memory.

The VM has its own kernel, but the host home is mounted **read-only and readable**
inside it. Agents share guest credentials and have passwordless sudo. Read the
[security model](docs/guides/security.md) before using sensitive files or tokens.

## Start here

From a downloaded or cloned tool checkout:

```sh
just install
cp -R workspace-template ../my-workspace
cd ../my-workspace
cp .env.secrets.example .env.secrets
chmod 600 .env.secrets
```

Add the installed wrapper directory (default `~/.local/bin`) to PATH. Edit
`workspace.yml` with a unique VM name, Git identity and repositories. Edit
`AGENTS.md` with repository roles and verification commands, and configure only
needed credentials in `.env.secrets`. All example names are placeholders.

```sh
# Host, from the workspace directory:
floatie create
floatie codex-setup          # if using Codex
floatie attach
```

Inside the VM, run `codex login --device-auth`; for Claude Code, run `claude`
and use `/login`. Register the VM's public Git signing key as described in
[installation](docs/guides/installation.md). Detach with **Ctrl-b d**.

```sh
# Host, from the same workspace directory:
floatie orchestrate         # start/reuse Codex root and attach
# Or: floatie orchestrate claude
floatie workers
floatie ui                  # optional local Workspace / Settings UI
```

`attach` reconnects a terminal without starting an agent. `stop` shuts down the
whole VM: disk persists, processes end. `start` boots it without resuming agents.
`reload` places workspace files and tools and can overwrite guest customizations;
it is not a harness switch. Preserve existing work before lifecycle changes.

- [Installation, configuration, upgrades and troubleshooting](docs/guides/installation.md)
- [Host and guest command reference](docs/guides/commands.md)
- [Architecture and worker lifecycle](docs/guides/architecture.md)
- [Security model](docs/guides/security.md) and [vulnerability reporting](SECURITY.md)
- [Contributing and verification](CONTRIBUTING.md)
- [Bundled third-party notices](ui/THIRD_PARTY_NOTICES.txt)

Floatie's original code is licensed under the [MIT License](LICENSE). Bundled
third-party code remains subject to
[its own licenses and notices](ui/THIRD_PARTY_NOTICES.txt).
