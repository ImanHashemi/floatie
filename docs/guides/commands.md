# Command reference

Run host commands from the intended workspace directory containing
`workspace.yml`. The wrapper uses that exact current directory; it does not
search ancestors. [Product help](../../help.txt) and the
[command contract fixture](../../tests/fixtures/command-entrypoints.json) track
the interface. Run `floatie help` for a terminal-sized reference.

## Host commands

| Command | Effect |
|---|---|
| `just install` | Run in the tool checkout; symlink its wrapper into `FLOATIE_BIN_DIR` or `~/.local/bin` |
| `floatie create` | Create/start VM and reconcile provisioning, identity, secrets, plugins, missing clones, context and firewall |
| `floatie codex-setup` | After create, install/check Codex and sandbox prerequisites; apply config and firewall |
| `floatie attach [worker]` | Attach main or a live worker terminal; does not launch an agent |
| `floatie orchestrate [codex\|claude]` | Start/reuse root agent, focus its pane and attach; defaults to Codex |
| `floatie worker <name> [repo]` | Create with all actual clones or one repo; reconnect exact saved scope |
| `floatie workers` | List saved workers and live session state, excluding root; does not start the VM |
| `floatie start` | Boot existing VM and apply firewall; does not resume agents |
| `floatie stop` | Shut down the entire VM; disk persists, processes end |
| `floatie reload` | Place workspace context/MCPs, stack, guest tools and skills; refresh installed Codex config and retry Claude plugins |
| `floatie firewall [off]` | Apply saved allowlist; exact `off` temporarily permits all egress |
| `floatie signing-key` | Print the public VM signing key and registration instructions |
| `floatie ui [port]` | Start/reuse detached local UI; default port 8787 |
| `floatie ui-stop` | Stop this workspace's owned UI process, not the VM |
| `floatie ui-logs` | Follow its log; Ctrl-C stops watching |
| `floatie ui-fg [port]` | Run UI in foreground; default port 8787 |
| `floatie help` | Show product help; also bare `floatie`, exact `--help` or `-h` |

`reload` requires a running VM and can overwrite placed files. It does not apply
secrets, Git identity, repository cloning, resource changes or firewall changes.
`create` reapplies provisioning and reconciles identity/secrets/missing clones;
fresh processes load new secrets. VM resource/template changes require a
planned rebuild. Neither command is a live harness switch.

Under 10 GiB free on the host Lima volume, create/start/reload/worker refuse;
10–19 GiB warns, and attach only warns. `FLOATIE_SKIP_DISK_CHECK=1` bypasses this
guard, including inability to measure free space; it does not fix disk pressure.

## Removal and destructive maintenance

| Command | Preservation and limits |
|---|---|
| `floatie worker-rm <name> [true\|false]` | Defaults to false; checks Git state, then hook, terminal, stack and worktrees/owned branch. `true` bypasses only a failed workspace hook |
| `floatie destroy [true\|false]` | Delete VM and disk after push guard; `true` overrides that guard and can lose data |
| `floatie rebuild [true\|false]` | Same deletion guard, then create from saved configuration |

Worker removal refuses dirty, unpublished or unknown Git state, including flags
that hide tracked changes. Hook/terminal/stack effects can remain after failure;
there is no rollback. Built-in Compose teardown omits `-v`; workspace hooks may
delete named volumes. Unrelated branches remain. Inspect all remaining work
before retrying. Destroy/rebuild can start a stopped VM for their guard and leave
it running if deletion is refused.

New worker names start with a lowercase letter or digit, contain only lowercase
letters, digits, `_` and `-`, and have at most 63 characters. `main`, `stack`,
`workspace` and `all` are reserved. Reconnect never adds repositories or resets
branches. A different explicit scope fails; partial creation requires inspecting
the surviving set before reconnect or guarded removal. See
[worker preservation](architecture.md#worker-preservation).

## Agent automation

| Host command | Contract |
|---|---|
| `floatie agent-start <target> [codex\|claude]` | Headless start/reuse; defaults to Codex, accepts `main`; new Codex agents have full guest access, reused agents keep their settings |
| `floatie agent-list` | Discover process/pane JSON, including supported manual launches |
| `floatie agent-read <target> [--pane %ID]` | Read terminal tail and cursor |
| `floatie agent-send <target> [--pane %ID]` | Send literal prompt from stdin through EOF |
| `floatie agent-wait <target> [--pane %ID] [--cursor <cursor>] [--seconds <n>]` | Bounded observation for one target |
| `floatie agent-wait <target> --target <other> [--target <other>]… [--cursor <multi_cursor>] [--seconds <n>]` | Bounded observation for multiple targets; no `--pane` |

Use observed pane IDs, not guessed numbers. The example `%42` below stands for
an ID returned by `agent-list`. Set `cursor` to the `cursor` JSON field from a
read/wait of that exact target and pane. Set `multi_cursor` from a prior wait
with the same targets in the same order. Omit `--cursor` for the initial snapshot.

```sh
floatie agent-list
floatie agent-read review --pane %42
# Set cursor from that response before running:
floatie agent-wait review --pane %42 --cursor "$cursor" --seconds 10
# First request a snapshot with:
# floatie agent-wait review --target other --seconds 0
# Set multi_cursor from that response before running:
floatie agent-wait review --target other --cursor "$multi_cursor" --seconds 10
printf '%s\n' 'Report progress.' | floatie agent-send review --pane %42
floatie agent-send review --pane %42 < prompt.txt
```

Send accepts 1–65536 UTF-8 bytes; whitespace-only text and control characters
other than newline/tab are rejected. A prompt is not an approval API. Output
changes, process presence and idle terminals do not establish task completion.
There is no hidden supervision scheduler. Explicit legacy uppercase and
64-character agent targets remain accessible under the existing pane/directory
checks; that compatibility does not change the new-worker naming rule.

## Guest commands

Inside the VM use the installed guest justfile, not the host wrapper or Lima:

```sh
just --justfile "$HOME/.local/share/floatie/agents.just"
just --justfile "$HOME/.local/share/floatie/agents.just" worker-create review api web
just --justfile "$HOME/.local/share/floatie/agents.just" agent-start review codex
just --justfile "$HOME/.local/share/floatie/agents.just" agent-list
```

Replace `api` and `web` with actual clone names. The eight guest recipes are
`default` (recipe list), `orchestrate [harness]`, `worker-create <target> <repos>…`,
`agent-start <target> [harness]`, `agent-list`, `agent-read`, `agent-send`, and
`agent-wait`. Read/send/wait take the same target/options/stdin contracts as above.
`worker-create` requires explicit repository scope. Reconnect to a live guest
terminal with `tmux attach-session -t '=review'`; Ctrl-b d detaches.

## Runner references

`floatie --list` lists public recipes; `floatie --show <recipe>` shows source and
`floatie --usage <recipe>` shows arguments. `floatie --version` is the just runner
version, not a Floatie release. Use `just --help` for runner options. Only zero
arguments or exact help/--help/-h select product help; other arguments, stdin and
current directory pass through. Hidden named `default` remains callable. Internal
helpers are implementation details, not an alternate lifecycle interface.
