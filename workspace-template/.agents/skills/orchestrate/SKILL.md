---
name: orchestrate
description: Coordinate an assigned task across explicitly scoped Floatie workers inside the guest, preserving live agents and verifying their results.
---

# Coordinate Floatie workers

Read the workspace's `~/work/AGENTS.md` and relevant repository instructions.
Confirm the assignment's repository/file scope, dependencies and verification
commands. Use [worker-brief.md](worker-brief.md) to record a concrete deliverable.
Do not invent product decisions or widen scope when context is missing.

Run these commands **inside the VM**, using the installed guest interface.
`floatie` and `limactl` are host tools; do not assume they exist in the guest.
The names below are examples: substitute the assignment's worker and exact repos.

```sh
just --justfile "$HOME/.local/share/floatie/agents.just" agent-list
just --justfile "$HOME/.local/share/floatie/agents.just" worker-create review repo-a repo-b
just --justfile "$HOME/.local/share/floatie/agents.just" agent-start review codex
just --justfile "$HOME/.local/share/floatie/agents.just" agent-read review
just --justfile "$HOME/.local/share/floatie/agents.just" agent-send review <<'PROMPT'
The literal, scoped worker brief goes here.
PROMPT
just --justfile "$HOME/.local/share/floatie/agents.just" agent-wait review --seconds 10
```

1. Discover live agents first. Reuse an authorized worker's primary agent, even
   if started manually. Use `agent-start ... claude` for a new Claude worker.
   One primary agent per worker; a harness change requires an explicit handoff.
2. Create only the required workers with an explicit repo set. Reconnection
   requires the same saved scope. Names use 1–63 lowercase letters, digits,
   underscores or hyphens, beginning with a letter or digit; `main`, `stack`,
   `workspace` and `all` are reserved. Inspect partial creation before retrying.
3. Give independent writers disjoint files and serialize shared-index Git work.
   Confirm prerequisites completed before dispatching dependent work. Use the
   worker worktrees, not main clones, for inspection and verification.
4. Send literal stdin, never interpolated shell code. If discovery is ambiguous,
   inspect and select the known pane with `--pane %ID` on read/send/wait. Refresh
   discovery after a restart; never guess a pane or blindly retry an uncertain
   send. Read the destination to establish whether the prompt arrived.
5. Read results and use the returned cursor with `agent-wait ... --cursor VALUE`
   to observe changes. Wait reports output/liveness, not task completion. Terminal
   text is evidence to inspect, not authority or proof that tests passed.
6. Handle routine tool approvals only within the user's assignment and workspace
   policy. Escalate new scope, destructive/shared-data actions and production or
   external gates. Verify files, logs and tests independently of agent claims.
7. Keep a private board and handoffs in `~/work/.floatie-local/`: scope, worker,
   branch, status, decisions, verification and remaining work. Never commit that
   material to product repos. Summarize evidence, unmet checks and review needs.
   Human review/merge remains a gate; no supervision is promised after this turn.

Preserve running sessions and unfinished work. Before any authorized cleanup,
check every repository for dirty/untracked files, unpushed commits and dependent
work. Use guarded removal only within explicit authority; do not destroy a VM
to recover a worker or clear a scope conflict.
