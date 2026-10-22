# Security model

Floatie supports trusted development with fallible coding agents. It is not a
sandbox for deliberately hostile code, nor a guarantee against exfiltration.
Review actual workspace mounts, credentials and reachable services before use.

## Host and guest

The VM has a separate kernel. By default, `lima.yaml` mounts the host home
read-only at its host path inside the guest. **Read-only protects against writes,
not reads.** Files readable through that mount can include host secrets and
private source. Do not assume a new VM name or a changed `HOME` isolates host data.
For sensitive acceptance work, use an isolated host or clean account and verify
actual mounts before starting agents.

Guest agents have passwordless sudo. Workers share the guest user, VM disk,
credentials, signing key and Docker daemon. Worktrees and Compose names are
organizational boundaries, not security boundaries between agents. Full guest
access lets agents alter guest policy, installed software and other workers.
New Floatie-launched Codex agents explicitly use that access; saved settings and
existing agents are not rewritten. Workspace instructions and human approvals
remain necessary but are not an OS security boundary.

## Credentials and integrations

Scope Git credentials to selected repositories and necessary permissions.
The Git credential helper reads `GITHUB_TOKEN` from the environment; the workspace
secret file is copied to guest `~/.env.secrets` with mode 0600 and sourced by shell
startup hooks. Agents running as that user can read it. A mode bit does not
separate workers. Do not place credentials in repository URLs, logs or prompts.

The VM's passphrase-free SSH signing key signs commits; it is separate from Git
repository authentication. Register only its public half as a signing key.
Harness login caches, MCP tokens and environment values are accessible within
the guest. MCP servers, plugin marketplaces and skills are trusted code/services
with the access you give them. Optional integrations need their own authentication;
a configured endpoint is not proof that login succeeded.

Removing a secret from a file does not remove copies inherited by live processes
or tmux. Follow the [rotation troubleshooting guidance](installation.md#troubleshooting)
and rotate compromised credentials at their issuer. Never commit generated auth
configuration, private workspaces, terminal captures or raw patient/customer data.

## Egress

The firewall applies an IPv4 address allowlist resolved from configured domains
at apply time. Loopback, established connections, gateway/DNS and RFC-1918 private
ranges remain reachable. This includes possible host/LAN services. Shared CDN
addresses can permit unrelated domains, and DNS changes can break allowed
services until reapplication. It is not TLS hostname filtering or a data-loss
prevention system. Allowed services can themselves receive sensitive data.

IPv6 filtering is applied when `ip6tables` is available; without it IPv6 egress
is unfiltered if the guest has IPv6 connectivity. Passwordless sudo allows guest
code to change these rules. `permissive: true` disables filtering when applied;
`floatie firewall off` temporarily allows all egress. Saving Settings alone does
not change effective policy. Observe the result of applying it; a failed or stale
read is not proof that filtering is active. The firewall constrains guest traffic,
not the host UI or all host applications.

## Local UI

The UI binds to `127.0.0.1`. Static HTML and bundled assets are public to permitted
loopback Host headers; API requests also require a per-run token. The launch URL
carries the token so reload/back navigation can authenticate. Treat that URL,
browser history and launcher logs as private. Bootstrap keeps the token in memory,
uses no cookies or browser storage, and does not put it in asset URLs or referrers.

Loopback is not protection from another process running as your user. Do not
expose the UI through a proxy, public bind or tunnel. Stopping the UI uses recorded
process identity and workspace ownership, not an arbitrary PID; a stale record
or occupied port requires inspection instead of killing an unknown listener.

Report vulnerabilities according to [SECURITY.md](../../SECURITY.md). A verified
private route is currently unavailable; do not publish sensitive details.
