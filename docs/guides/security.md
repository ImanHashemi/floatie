# Security model

Floatie is built for trusted development work with coding agents that can make
mistakes. It is not a jail for hostile code, and it does not guarantee that data
cannot leave the VM. Before you use it with sensitive material, check what the
VM can read, which credentials it holds and which services it can reach.

## Your Mac and the VM

- The VM has its own kernel and its own disk.
- Your Mac's home directory is mounted in the VM **read-only**, at the same
  path. Read-only stops writes, not reads: an agent can read every file your
  user can read there, including secrets and private source code.
- A new VM name or a different `HOME` does not hide your files. To test with
  sensitive material, use a separate Mac or a clean user account, and check what
  is mounted before you start an agent.

## Inside the VM

- Agents can use sudo without a password.
- Every worker shares one user, one disk, the same credentials, the signing key
  and one Docker. Worktrees and Compose project names organise the work. They do
  not separate agents from each other.
- An agent with full access can change the VM's settings, its software and other
  workers. Codex agents that Floatie starts get that access. Settings you saved
  and agents that already run are not changed.
- Your instructions and your approvals still matter, but the operating system
  does not enforce them.

## Credentials

- Give the GitHub token access to the repositories you chose and nothing more.
- Git in the VM reads `GITHUB_TOKEN` from the environment. Floatie copies
  `.env.secrets` to `~/.env.secrets` in the VM, readable only by the VM user, and
  shells load it when they start. Every agent runs as that user and can read it.
- Do not put credentials in repository URLs, logs or prompts.
- The signing key has no passphrase. It signs commits and is not used to log in
  to GitHub. Register only its public half, as a signing key.
- Agent logins, MCP tokens and environment values can all be read inside the VM.
- MCP servers, plugin marketplaces and skills are code and services you trust
  with that access. Each optional integration needs its own login; a configured
  server is not a logged-in server.
- Taking a secret out of a file does not take it away from programs that already
  loaded it. See
  [troubleshooting](installation.md#a-removed-or-changed-secret-is-still-in-use).
  If a secret leaked, rotate it where it was issued.
- Never commit generated login configuration, private workspaces, terminal
  captures or real personal or customer data.

## Network

The firewall allows outbound traffic to the IPv4 addresses that the allowed
domains resolve to at the moment it is applied.

- These always stay reachable: the VM itself, connections that are already open,
  the gateway and DNS, and private network ranges. Private ranges can include
  services on your Mac and on your local network.
- An address shared by a CDN can let other domains through.
- When a service's addresses change, it stops working until you run
  `floatie firewall` again.
- It does not filter by hostname inside TLS, and it does not stop data from
  leaving. A service you allowed can receive sensitive data.
- IPv6 is filtered when `ip6tables` is available. Without it, IPv6 traffic is
  not filtered if the VM has IPv6.
- An agent with sudo can change the rules.
- `permissive: true` turns filtering off when it is applied.
  `floatie firewall off` allows everything until the allowlist is applied again.
- Saving a network setting does not apply it. Run `floatie firewall` and check
  the result; a failed or old reading is no proof that filtering is on.
- The firewall limits traffic from the VM. It does not limit the web UI or other
  programs on your Mac.

## The web UI

- The UI listens on `127.0.0.1` only.
- The page and its bundled files can be loaded by anything that reaches that
  address with an allowed Host header. API calls also need a token that is new
  on every start.
- The address you open contains the token, so reloading and going back keep
  working. Treat that address, your browser history and the UI's log as private.
- The page keeps the token in memory. It uses no cookies and no browser storage,
  and it does not put the token in file addresses or referrers.
- Listening on `127.0.0.1` does not protect against other programs that run as
  your user. Do not put the UI behind a proxy, a public address or a tunnel.
- Stopping the UI checks the recorded process and workspace, not just a process
  number. If the record is old or the port is taken, look into it instead of
  killing a process you do not know.

## Reporting a vulnerability

Follow [SECURITY.md](../../SECURITY.md). A verified private reporting route is
not available yet, so do not publish sensitive details.
