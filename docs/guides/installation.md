# Installation and workspace setup

Use macOS on Apple Silicon with Lima 2.1+, `just`, Git and host Python 3.9+ on
PATH. The VM template uses Apple's virtualization framework and Ubuntu 24.04.
Node, npm and test Python are contributor tools, not host UI runtime requirements.
Budget memory for the guest and services; Docker Desktop is not required.
With Homebrew, `brew install lima just` installs the first two; Git and Python 3
come with the Xcode Command Line Tools (`xcode-select --install`).

## Tool and workspace

From the tool checkout, run `just install`. It links `bin/floatie` into
`~/.local/bin`; put that directory on PATH. Set `FLOATIE_BIN_DIR` for a different
install directory. The wrapper resolves its own checkout and keeps the caller's
current directory as the workspace.

Copy `workspace-template` to a new workspace directory, including hidden files:

```sh
cp -R workspace-template ../my-workspace
cd ../my-workspace
cp .env.secrets.example .env.secrets
chmod 600 .env.secrets
```

Edit `workspace.yml` before creation. Set a unique VM `name`, `cpus`, `memory`,
`git_user_name`, a verified `git_user_email`, and your repository list. Use the
supported plain single-line scalars and block lists, not quoted or inline
nonempty lists. Repository entries accept `github.com/owner/repo` shorthand,
full clone URLs or guest-readable local paths; never embed credentials in URLs.
Replace all example values with your own. Existing clone directories are skipped
on create, so changing a URL does not replace an existing clone.

Describe repository roles, verification commands and authority boundaries in
workspace `AGENTS.md`. Keep `CLAUDE.md` as a reference to shared guidance. The
starter's shared orchestrate skill uses installed guest recipes and explicit
repository scope. Optional `stack/` content is placed at guest `~/work/stack`;
if it contains `.env.vm`, that file becomes `.env` there, and a `.floatie-vm`
marker file is added so stack scripts can tell they run in the VM.
Do not commit `.env.secrets`, private work boards or handoffs.

For private GitHub repositories, use a fine-grained `GITHUB_TOKEN` scoped to
selected repositories. Grant Contents write for pushes and Pull requests write
if agents should open PRs. The helper reads it from the guest environment;
`gh` also uses it without a separate login. Missing credentials warn rather than
preventing unrelated local work. A failed clone can be retried with `create`
after correcting access; completed clones are retained.

## Create, sign and log in

On the host, from the workspace directory:

```sh
floatie create
floatie signing-key
floatie codex-setup          # if using Codex
floatie attach
```

Create provisions the guest, copies secrets, clones missing repositories, places
context/tools and applies the firewall. It also configures Git identity and a
VM-resident SSH signing key. Register its **public** key on your GitHub account
as a **Signing Key**, not an authentication key. The configured email must be
verified on that account. Keep the private key in the VM. Stop/start retains it;
rebuild generates a replacement requiring registration.

Inside the VM, run `codex login --device-auth` and complete browser login, or run
`claude` and use `/login`. Detach with Ctrl-b d. Start your agent in that
terminal, or create workers with `floatie worker <name>` to run several agents in
parallel. Orchestration is optional: `floatie orchestrate` (Codex) or
`floatie orchestrate claude` starts a root agent that coordinates workers.
Authentication and conversation history belong to each harness; switching
harnesses needs a written handoff and must not replace an agent that is still
working.

Codex setup installs/checks the guest CLI and bubblewrap prerequisites. On Ubuntu
with restricted user namespaces it uses the packaged AppArmor policy and checks
sandbox startup without an AI request. It does not disable the VM-wide namespace
restriction or rewrite saved approval/sandbox preferences. Floatie's new Codex
launches deliberately pass full guest access and on-request approvals. Existing
agents are reused unchanged. See the [security model](security.md).

## Optional MCPs, plugins and skills

Copy `.floatie-mcp.json.example` to `.floatie-mcp.json` only if needed. Declare servers explicitly,
reference secrets as `${VAR}`, and allow required endpoint and authentication
domains in `egress`. Add secret values to the workspace's ignored `.env.secrets`.
Run `create` when changing secrets or missing clones; apply `firewall` when
changing network policy. `reload` alone does neither.

This is Floatie's only MCP source, with the existing `mcpServers` schema. Native
Claude project `.mcp.json` is independent and ignored by Floatie; keep any
host-specific project configuration there. Floatie does not copy it to guest
`~/work` or combine it with `.floatie-mcp.json`.

Upgrade the tool and workspace filename together: prepare `.floatie-mcp.json`
with the intended VM definitions before running the updated tool, then reload
(and run Codex setup if needed). For rollback, restore the reviewed older tool
revision together with its `.mcp.json` source before reapplying. Review any
existing native project file before changing it. Neither version falls back to
the other filename; an unpaired change can select unintended definitions or
leave integrations absent. Preserve guest customizations before reapplying.

Claude MCP configuration is placed at user scope in the guest. Authenticate OAuth
servers through Claude's `/mcp` flow. Plugins use the cached Claude login; before
login their installation can warn and be skipped. After login, `reload` retries
configured plugins. List marketplaces before plugins in `workspace.yml`.

If the Floatie source is missing or has no servers, Claude's existing entries
remain. Codex setup/reload removes its previously managed servers in that case,
preserving unmanaged personal servers. Removing a Claude server from the source
does not remove it from guest user scope; use `claude mcp remove <name>` there.

Codex setup/reload manages a block in its guest configuration. STDIO and
streamable HTTP are supported; legacy SSE needs a provider HTTP endpoint. Run
`codex mcp login <server-name>` for OAuth servers and inspect `/mcp`. Credentials
are separate from Claude's. Conflicting unmanaged server names fail rather than
being overwritten. Environment expansion can put STDIO secrets in a mode-0600
generated config; never commit it, and refresh setup/reload after changing them.
HTTP credential references resolve when connecting, so rotating their environment
values does not require embedding them in generated configuration. Refresh the
guest environment deliberately; reload does not copy `.env.secrets`.

Place shared skills in `.agents/skills/<skill>/SKILL.md`; existing
`.claude/skills` remains supported. Treat skills and plugins as executable trusted
inputs. Missing optional integration auth is not a reason to block unrelated
local work. Configured does not mean authenticated.

## Updates and rollback

Update the tool checkout to a reviewed revision. The installed symlink continues
to resolve it. Recipe/script changes apply on next invocation; restart only the
owned UI server with `ui-stop`, then `ui`, to load a new server/build. A missing
build returns 503 with repair guidance; users should restore a complete checkout,
not install Node to repair runtime files.

Changing which checkout the wrapper targets does not require guest placement.
Install from the replacement checkout with the same `FLOATIE_BIN_DIR`; keep the
old checkout for rollback. Reinstall from the old checkout to restore the wrapper,
and restart the owned UI if necessary. Existing VM disks, worktrees, live agents
and authentication remain in place.

`reload` can overwrite guest instructions, skills, MCP config and stack files.
Review and preserve guest customizations before adopting new workspace guidance;
existing workspaces opt in deliberately. `create` reapplies provisioning and
identity/secrets. Resource/template changes require planned destructive rebuild,
not a routine update. Read [command effects](commands.md) first.

## Troubleshooting

- **Host disk full / guest EIO:** the VM disk is sparse. The guest can show free
  space while its host volume is full. Free host space first; coordinate restart
  with owners of running work, then run `sudo fstrim -av` in the guest. Do not
  destroy the VM to diagnose I/O errors. Host disk checks use `$LIMA_HOME` or
  `~/.lima`: below 10 GiB lifecycle expansion refuses, 10–19 GiB warns. Bypassing
  with `FLOATIE_SKIP_DISK_CHECK=1` also disables failed-measurement protection.
- **Session dies after OOM:** inspect guest `journalctl`, including the period
  before the exit. A session dying roughly 90 seconds after an OOM kill can be
  systemd `OOMPolicy=stop`. Bound heavy jobs with
  `systemd-run --user --scope -p MemoryMax=… <command>` using an appropriate limit.
  Current provisioning does not add swap or an OOMPolicy override. Preserve work
  before changing session services or restarting the VM.
- **Stale secrets:** values persist in the secret file, shell startup files,
  running shells and tmux environment. Remove stale assignments without printing
  values; use `tmux set-environment -gu VAR` for the specific stale tmux variable,
  unset it in owned shells, and deliberately refresh owned consumers. Rotate a
  compromised value at its issuer. Replacing a file alone cannot revoke copies
  already inherited by a process.
- **Network/auth failure:** inspect the failure before changing policy. Reapply
  `firewall` if allowlisted DNS addresses rotated. OAuth may need both endpoint
  and auth domains. Check credentials independently for each harness/MCP. Avoid
  disabling egress as a permanent workaround.
- **Clipboard or scrollback:** tmux uses mouse support, vi copy mode (`v`, `y`),
  Ctrl-b u and a 50,000-line history for new panes. Shift-drag uses native terminal
  selection where supported. Nested terminal multiplexers can intercept clipboard
  escapes; try a plain terminal. Provisioning appends a marked block and sources
  it in a running tmux server; earlier settings can be overridden. Existing panes
  do not gain a larger history retroactively. Keep the marker in custom config to
  opt out of automatic block placement.
- **UI port occupied / stale registration:** inspect `ui-logs` or use `ui-fg`.
  Do not kill an unrelated process to free a port. An unknown outcome requires
  observation and explicit refresh before retrying. UI shutdown never stops the VM.

Use [the command reference](commands.md) for daily actions and
[architecture](architecture.md) for worker preservation and partial failures.
