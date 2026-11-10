#!/bin/bash
# provision.sh — floatie guest toolchain setup.
# Runs inside the VM via `limactl shell <name> -- bash -s < provision.sh` (piped over stdin).
# Idempotent: safe to re-run; each step checks before acting.

set -euo pipefail

echo "==> floatie provision: start"

# ── apt packages (tmux + git + ipset + dnsutils + nodejs + npm) ──────────────
# ipset    — kernel IP set management, used by firewall.sh for the allowlist.
# dnsutils — provides `dig`, used by firewall.sh to resolve allowlist domains.
# nodejs   — JavaScript runtime; required for stdio MCPs launched via `npx`.
# npm      — Node package manager; ships with nodejs; required for `npx`.
NEED_APT=0
command -v tmux  >/dev/null 2>&1 || NEED_APT=1
command -v git   >/dev/null 2>&1 || NEED_APT=1
command -v ipset >/dev/null 2>&1 || NEED_APT=1
command -v dig   >/dev/null 2>&1 || NEED_APT=1
command -v node  >/dev/null 2>&1 || NEED_APT=1
command -v npm   >/dev/null 2>&1 || NEED_APT=1

if [ "$NEED_APT" = "1" ]; then
  echo "==> installing apt packages (tmux, git, ipset, dnsutils, nodejs, npm)"
  sudo apt-get update -qq
  sudo apt-get install -y --no-install-recommends tmux git ipset dnsutils nodejs npm
else
  echo "==> tmux already installed ($(tmux -V))"
  echo "==> git already installed ($(git --version))"
  echo "==> ipset already installed ($(ipset --version | head -1))"
  echo "==> dig already installed ($(dig -v 2>&1 | head -1))"
  echo "==> node already installed ($(node --version))"
  echo "==> npm already installed ($(npm --version))"
fi

# ── GitHub CLI (gh) ───────────────────────────────────────────────────────────
# Lets in-VM agents open PRs (`gh pr create`). gh auto-authenticates from
# GITHUB_TOKEN / GH_TOKEN in the environment (sourced from ~/.env.secrets), so no
# `gh auth login` is needed. Installed from GitHub's official apt repo
# (cli.github.com is in the firewall base allowlist). Idempotent.
if command -v gh >/dev/null 2>&1; then
  echo "==> gh already installed ($(gh --version | head -1))"
else
  echo "==> installing GitHub CLI (gh) from cli.github.com"
  sudo mkdir -p -m 755 /etc/apt/keyrings
  curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg | sudo tee /etc/apt/keyrings/githubcli-archive-keyring.gpg >/dev/null
  sudo chmod go+r /etc/apt/keyrings/githubcli-archive-keyring.gpg
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
    | sudo tee /etc/apt/sources.list.d/github-cli.list >/dev/null
  sudo apt-get update -qq
  sudo apt-get install -y gh
  echo "==> gh installed ($(gh --version | head -1))"
fi

# ── Claude Code ───────────────────────────────────────────────────────────────
# Installer lands at ~/.local/bin/claude (claude.ai is in the firewall allowlist).
# Idempotent: skip if already present.
if command -v claude >/dev/null 2>&1 || [ -f "$HOME/.local/bin/claude" ]; then
  CLAUDE_VER=$(~/.local/bin/claude --version 2>/dev/null || claude --version 2>/dev/null || echo "unknown")
  echo "==> claude already installed ($CLAUDE_VER)"
else
  echo "==> installing Claude Code via claude.ai installer"
  curl -fsSL https://claude.ai/install.sh | bash
  echo "==> Claude Code installed ($(~/.local/bin/claude --version 2>/dev/null || echo 'version unknown'))"
fi

# Ensure ~/.local/bin is on PATH for both interactive and headless shells.
# `limactl shell <name>` (interactive) sources ~/.profile (login shell).
# `limactl shell <name> -- cmd` (headless) does NOT source ~/.profile or ~/.bashrc;
# bash invokes it as a non-interactive non-login shell.
# Solution: /etc/profile.d/ scripts are sourced for all login shells AND for bash -l;
# we use bash -l in any recipe that needs claude on PATH (e.g. headless verification).
# Belt-and-suspenders: also add to ~/.profile for interactive `floatie attach` sessions.

# /etc/profile.d/floatie-path.sh — login-shell PATH injection.
# Note: /etc/profile.d scripts are sourced with dot, so $HOME expands at runtime.
if [ ! -f /etc/profile.d/floatie-path.sh ]; then
  printf '# floatie: add ~/.local/bin to PATH (Claude Code installs here).\nexport PATH="$HOME/.local/bin:$PATH"\n' \
    | sudo tee /etc/profile.d/floatie-path.sh >/dev/null
  sudo chmod 644 /etc/profile.d/floatie-path.sh
  echo "==> created /etc/profile.d/floatie-path.sh"
else
  echo "==> /etc/profile.d/floatie-path.sh already present"
fi

# ~/.profile belt-and-suspenders for interactive login shells.
# Guard on a unique marker string (same discipline as the ~/.bashrc hook above)
# to avoid false-positive matches on unrelated PATH lines that happen to contain '.local/bin'.
PROFILE_MARKER="# floatie: ensure ~/.local/bin is on PATH"
if ! grep -qF "$PROFILE_MARKER" "$HOME/.profile" 2>/dev/null; then
  printf '\n# floatie: ensure ~/.local/bin is on PATH (Claude Code lives here)\nexport PATH="$HOME/.local/bin:$PATH"\n' >> "$HOME/.profile"
  echo "==> added ~/.local/bin to ~/.profile PATH"
else
  echo "==> ~/.local/bin already in ~/.profile PATH"
fi

# ── Pre-create ~/.claude/skills so workspace skills (placed by `floatie create`/
# `reload`) live-reload without a Claude restart (Claude watches an existing dir;
# creating the top-level dir mid-session would need a restart). ────────────────
mkdir -p "$HOME/.claude/skills"

# ── Seed hasCompletedOnboarding in ~/.claude.json ────────────────────────────
# Claude Code #8938: interactive first-run shows an onboarding wizard until this
# key is set. We MERGE into any existing ~/.claude.json to avoid clobbering MCP
# state or settings added by later slices. python3 is expected on Ubuntu; guard
# against the (unlikely) case where it is absent so provision does not hard-fail.
if command -v python3 >/dev/null 2>&1; then
  echo "==> seeding hasCompletedOnboarding in ~/.claude.json"
  python3 - <<'PYEOF'
import json, os, sys

path = os.path.expanduser("~/.claude.json")
try:
    with open(path) as f:
        data = json.load(f)
except FileNotFoundError:
    data = {}
except json.JSONDecodeError as e:
    print(f"WARN: ~/.claude.json is not valid JSON ({e}); overwriting with minimal seed", file=sys.stderr)
    data = {}

if data.get("hasCompletedOnboarding") is True:
    print("==> hasCompletedOnboarding already set — no change")
else:
    data["hasCompletedOnboarding"] = True
    with open(path, "w") as f:
        json.dump(data, f, indent=2)
    print("==> ~/.claude.json updated")
PYEOF
else
  echo "WARN: python3 absent — skipping onboarding seed (interactive claude may show the wizard)" >&2
fi

# ── Secrets in shells that early-return (~/.bashrc, at the TOP) ──────────────
# Ubuntu's ~/.bashrc returns early for non-interactive shells, so a hook
# APPENDED to it only ever fires interactively. That matters because
# `limactl shell <vm> -- bash -c ...` — half of how floatie talks to the guest
# (the other half is `bash -s`, which reads none of these files and sources
# ~/.env.secrets explicitly instead; see clone-repos.sh) — is a non-interactive
# shell over SSH, and bash has a special case for exactly that shape: it sources
# ~/.bashrc and returns WITHOUT consulting $BASH_ENV. Measured in the guest.
#
# In practice secrets already reach those shells by INHERITANCE from the login
# environment limactl establishes. What this hook adds is the shell whose
# environment was scrubbed or partially overridden — the case issue #28
# reproduces, and the only one not already covered.
#
# Sourced exactly the way /etc/profile.d and the older appended hook do it, so
# every hook agrees on values; a hand-rolled parser would disagree with real
# shell sourcing on quoted entries, `export K=V` lines and trailing whitespace.
#
# INVARIANT: nothing above Ubuntu's early return may write to stdout. scp, sftp,
# rsync and git-over-ssh all arrive as a non-interactive `bash -c`, and a stray
# echo there corrupts their wire protocol.
#
# An older appended hook from a previous provision is deliberately left in
# place. It is idempotent and harmless, and editing a user's ~/.bashrc to remove
# it cannot be done safely by pattern matching — the block may be commented out,
# indented, wrapped in a conditional or absent while its marker text appears in
# a comment, and guessing wrong deletes unrelated configuration.
BASHRC_MARKER='# floatie:secrets'
BASHRC_HOOK='if [ -f "$HOME/.env.secrets" ]; then set -a; . "$HOME/.env.secrets"; set +a; fi  # floatie:secrets'

BASHRC_PATH="$HOME/.bashrc"
[ -e "$BASHRC_PATH" ] || : > "$BASHRC_PATH"
# Follow a symlink (dotfile managers) rather than replacing the link with a
# regular file, which would silently detach ~/.bashrc from its manager.
BASHRC_REAL=$(readlink -f "$BASHRC_PATH" 2>/dev/null || echo "$BASHRC_PATH")

if ! grep -qF "$BASHRC_MARKER" "$BASHRC_REAL" 2>/dev/null; then
  BASHRC_TMP="${BASHRC_REAL}.floatie-new"
  { printf '%s\n' "$BASHRC_HOOK"; cat "$BASHRC_REAL"; } > "$BASHRC_TMP"
  # Keep the original mode: the redirect above creates 0644, so a deliberately
  # restricted ~/.bashrc would otherwise be widened.
  # GNU stat first (the guest is Ubuntu), BSD stat second so this is also
  # exercisable on a macOS host, then a safe default.
  BASHRC_MODE=$(stat -c '%a' "$BASHRC_REAL" 2>/dev/null \
    || stat -f '%A' "$BASHRC_REAL" 2>/dev/null || echo 644)
  chmod "$BASHRC_MODE" "$BASHRC_TMP"
  # rename(2): an interruption leaves either the old file or the new one.
  mv "$BASHRC_TMP" "$BASHRC_REAL"
  echo "==> placed the secrets hook at the top of $BASHRC_REAL"
else
  echo "==> secrets hook already at the top of $BASHRC_REAL"
fi

# ── Source ~/.env.secrets in LOGIN shells too (non-interactive coverage) ──────
# The ~/.bashrc hook above only fires for INTERACTIVE shells (Ubuntu's ~/.bashrc
# returns early for non-interactive ones). Login shells — including headless
# `bash -lc` and `limactl shell <vm> -- ...` — read /etc/profile.d/*.sh, so this
# hook makes secrets (GITHUB_TOKEN, MCP keys) available to non-interactive login
# shells too (e.g. headless `claude -p`, `claude mcp list`).
# The script only SOURCES ~/.env.secrets (mode 0600); it holds no secret values.
if [ ! -f /etc/profile.d/floatie-secrets.sh ]; then
  printf '# floatie: load ~/.env.secrets into login shells (the secrets file itself is 0600).\nif [ -f "$HOME/.env.secrets" ]; then set -a; . "$HOME/.env.secrets"; set +a; fi\n' \
    | sudo tee /etc/profile.d/floatie-secrets.sh >/dev/null
  sudo chmod 644 /etc/profile.d/floatie-secrets.sh
  echo "==> created /etc/profile.d/floatie-secrets.sh"
else
  echo "==> /etc/profile.d/floatie-secrets.sh already present"
fi

# ── Git identity (user.name / user.email) ────────────────────────────────────
# Read git_user_name and git_user_email written to ~/.floatie-git-identity by
# `floatie create` (which parses workspace.yml on the host and passes them in).
# Both are required for commits and for the SSH signing key's allowed-signers
# file (the signing email must match a verified email on the GitHub account).
GIT_IDENT_FILE="$HOME/.floatie-git-identity"
GIT_USER_NAME=""
GIT_USER_EMAIL=""
if [ -f "$GIT_IDENT_FILE" ]; then
  GIT_USER_NAME=$(grep -m1 '^GIT_USER_NAME=' "$GIT_IDENT_FILE" | cut -d= -f2- | tr -d '\r' || true)
  GIT_USER_EMAIL=$(grep -m1 '^GIT_USER_EMAIL=' "$GIT_IDENT_FILE" | cut -d= -f2- | tr -d '\r' || true)
fi

if [ -n "$GIT_USER_NAME" ]; then
  git config --global user.name "$GIT_USER_NAME"
  echo "==> git user.name = $GIT_USER_NAME"
else
  echo "WARN: git_user_name not set in workspace.yml — commits will lack an author name." >&2
  echo "WARN: Set git_user_name: in workspace.yml and re-run 'floatie create'." >&2
fi

if [ -n "$GIT_USER_EMAIL" ]; then
  git config --global user.email "$GIT_USER_EMAIL"
  echo "==> git user.email = $GIT_USER_EMAIL"
else
  echo "WARN: git_user_email not set in workspace.yml — commits and signing will not work." >&2
  echo "WARN: Set git_user_email: to a VERIFIED email on your GitHub account and re-run 'floatie create'." >&2
  echo "WARN: GitHub shows 'Unverified' if the commit email is not verified on your account." >&2
fi

# ── HTTPS credential helper (reads GITHUB_TOKEN; token never appears in URLs) ─
# Headless git operations pick up GITHUB_TOKEN from ~/.env.secrets (sourced by
# the worker/launch mechanism). Interactive shells inherit it from ~/.bashrc.
# The credential helper is a shell function: it emits "password=$GITHUB_TOKEN"
# so the token stays out of process argv and git's own history.
CRED_HELPER='!f() { echo username=x-access-token; echo "password=$GITHUB_TOKEN"; }; f'
# Use --get-all so a re-provision correctly detects the helper even when multiple
# entries exist (git config without --get-all returns only the last value, which
# can miss an earlier identical entry and re-add the helper unnecessarily).
if git config --global --get-all credential.helper 2>/dev/null | grep -qF "$CRED_HELPER"; then
  echo "==> git credential.helper already configured"
else
  git config --global credential.helper "$CRED_HELPER"
  echo "==> git credential.helper configured (reads GITHUB_TOKEN from env)"
fi

# ── SSH signing key (dedicated, VM-resident; no passphrase for headless signing) ─
# Generated once; subsequent provision runs are no-ops (key is on persistent VM disk).
# Reject host SSH-agent forwarding: a VM-resident key is robust (no stale socket
# across sessions/tmux/workers) and safer (does not expose real auth keys via
# SSH_AUTH_SOCK). See docs/guides/installation.md#create-sign-and-log-in.
SSH_SIGNING_KEY="$HOME/.ssh/git_signing"
# Tracks whether THIS run created the key. Only a newly generated key needs
# registering on GitHub, so this gates the ACTION REQUIRED banner below (#30).
KEY_GENERATED=false
if [ -f "$SSH_SIGNING_KEY" ]; then
  echo "==> SSH signing key already exists at $SSH_SIGNING_KEY — skipping keygen (idempotent)"
else
  echo "==> generating SSH signing key at $SSH_SIGNING_KEY"
  mkdir -p "$HOME/.ssh"
  ssh-keygen -t ed25519 -C "floatie-signing" -f "$SSH_SIGNING_KEY" -N ""
  chmod 600 "$SSH_SIGNING_KEY"
  echo "==> SSH signing key generated"
  KEY_GENERATED=true
fi

# ── Repair a missing public key ──────────────────────────────────────────────
# Keygen above is guarded on the PRIVATE key, so a deleted .pub was never
# regenerated — while `user.signingkey` below points at it with commit.gpgsign
# on, which fails every commit with "Couldn't load public key".
#
# The private key is the source of truth, so re-derive the public half with
# `ssh-keygen -y`. Do NOT widen the keygen guard to test the .pub instead:
# ssh-keygen prompts "Overwrite (y/n)?" when the private key exists, and
# provision.sh IS this shell's stdin, so the prompt would eat the next line of
# the script. A derivation failure (corrupt or passphrase-protected key) is
# reported and left for the banner block to describe honestly — it must not
# abort the unrelated toolchain setup below.
if [ -f "$SSH_SIGNING_KEY" ] && [ ! -f "$SSH_SIGNING_KEY.pub" ]; then
  echo "WARN: $SSH_SIGNING_KEY.pub is missing — re-deriving it from the private key." >&2
  if ssh-keygen -y -f "$SSH_SIGNING_KEY" > "$SSH_SIGNING_KEY.pub" 2>/dev/null; then
    chmod 644 "$SSH_SIGNING_KEY.pub"
    echo "==> public key restored at $SSH_SIGNING_KEY.pub"
  else
    # The redirect creates the file even when ssh-keygen fails — clear it so
    # the checks below see the key as genuinely absent rather than empty.
    rm -f "$SSH_SIGNING_KEY.pub"
    echo "WARN: could not derive a public key from $SSH_SIGNING_KEY (corrupt, or passphrase-protected)." >&2
    echo "WARN: commit signing is configured but will fail. Remove $SSH_SIGNING_KEY and re-run to generate a fresh pair." >&2
  fi
fi

# ── Git SSH-signing config ────────────────────────────────────────────────────
git config --global gpg.format ssh
git config --global user.signingkey "$SSH_SIGNING_KEY.pub"
git config --global commit.gpgsign true

# ── allowed-signers file (enables `git verify-commit` locally) ───────────────
# Format: "<email> namespaces="git" <pubkey-line>"
# Only written when we have both the email and the public key.
ALLOWED_SIGNERS_FILE="$HOME/.config/git/allowed_signers"
if [ -n "$GIT_USER_EMAIL" ] && [ -f "$SSH_SIGNING_KEY.pub" ]; then
  PUBKEY=$(cat "$SSH_SIGNING_KEY.pub")
  mkdir -p "$(dirname "$ALLOWED_SIGNERS_FILE")"
  # Write fresh each time (email may have changed on re-provision).
  printf '%s namespaces="git" %s\n' "$GIT_USER_EMAIL" "$PUBKEY" > "$ALLOWED_SIGNERS_FILE"
  git config --global gpg.ssh.allowedSignersFile "$ALLOWED_SIGNERS_FILE"
  echo "==> allowed-signers file written at $ALLOWED_SIGNERS_FILE"
else
  echo "WARN: skipping allowed-signers file (git_user_email or signing key missing) — 'git verify-commit' won't work locally." >&2
fi

# ── Print signing public key + registration instructions ─────────────────────
# Three distinct states, and conflating any two of them misleads the operator:
#   1. no usable public key  → commits WILL fail; say so, claim nothing
#   2. key generated on THIS run → registration genuinely required; full banner
#   3. key already present   → registration was a one-time action, so stay
#      quiet — but name the key and how to re-read it, because suppressing the
#      banner also removes the only way to recover one that was missed in a
#      long create log or removed on the GitHub side. See issue #30.
if [ ! -f "$SSH_SIGNING_KEY.pub" ]; then
  echo "WARN: no signing public key at $SSH_SIGNING_KEY.pub — commit.gpgsign is on, so commits will fail." >&2
elif [ "$KEY_GENERATED" = true ]; then
  PUBKEY_LINE=$(cat "$SSH_SIGNING_KEY.pub")
  echo ""
  echo "╔══════════════════════════════════════════════════════════════════════╗"
  echo "║  ACTION REQUIRED — Register the signing key once on GitHub           ║"
  echo "╠══════════════════════════════════════════════════════════════════════╣"
  echo "║  1. Copy the public key below.                                        ║"
  echo "║  2. Go to: https://github.com/settings/ssh/new                        ║"
  echo "║  3. Title: anything (e.g. 'floatie-$(hostname)')                      ║"
  echo "║  4. Key type: SIGNING KEY  <── CRITICAL: NOT 'Authentication key'     ║"
  echo "║     (Authentication key → verified=false, reason=unknown_key)         ║"
  echo "║  5. Paste the key and save.                                            ║"
  echo "║                                                                        ║"
  echo "║  This is a ONE-TIME registration; it covers all repos on your account. ║"
  echo "╚══════════════════════════════════════════════════════════════════════╝"
  echo ""
  echo "  $PUBKEY_LINE"
  echo ""
else
  echo "==> signing key present at $SSH_SIGNING_KEY.pub (registration prompt shows only on the run that generates it)"
  echo "==>   commits showing 'Unverified'? Re-read the key with 'floatie signing-key' and add it at"
  echo "==>   https://github.com/settings/ssh/new as a SIGNING key (not an Authentication key)."
fi

# ── uv (Python package manager + runner) ─────────────────────────────────────
# Required for Python-based stdio MCPs launched via `uvx`.
# The official installer writes `uv` to ~/.local/bin (already on PATH above).
# Runs AFTER PATH setup so the idempotency check finds uv via PATH.
# Note: astral.sh is called here during provision, which runs BEFORE the egress
# firewall is applied (firewall.sh runs after provision in `floatie create`), so
# the astral.sh fetch succeeds unconditionally. On a re-provision of a running
# VM the firewall is already up — astral.sh is in the base allowlist in
# firewall.sh, so re-running provision with the firewall up is also safe.
if command -v uv >/dev/null 2>&1 || [ -f "$HOME/.local/bin/uv" ]; then
  UV_VER=$("$HOME/.local/bin/uv" --version 2>/dev/null || uv --version 2>/dev/null || echo "unknown")
  echo "==> uv already installed ($UV_VER)"
else
  echo "==> installing uv via official installer (astral.sh)"
  curl -LsSf https://astral.sh/uv/install.sh | sh
  echo "==> uv installed ($("$HOME/.local/bin/uv" --version 2>/dev/null || echo 'version unknown'))"
fi

# ── tmux config (session ergonomics) ─────────────────────────────────────────
# floatie installs tmux but shipped no config until now, leaving attached
# sessions on stock defaults: mouse off, 2000-line scrollback, emacs copy-mode,
# no focus-events. This writes a config with sane defaults.
#
# Idempotency mirrors the ~/.bashrc hooks above: guard on a marker string.
#   absent             -> write the file
#   present, no marker -> APPEND + warn (tmux is last-wins, so we override)
#   marker present     -> skip
TMUX_MARKER="# ── floatie: session ergonomics ──"
if ! grep -qF "$TMUX_MARKER" "$HOME/.tmux.conf" 2>/dev/null; then
  if [ -s "$HOME/.tmux.conf" ]; then
    echo "WARN: ~/.tmux.conf already exists — floatie appended its block at the end." >&2
    echo "WARN: tmux is last-wins, so floatie's settings override conflicting ones above." >&2
    echo "WARN: To opt out, add the marker line '$TMUX_MARKER' yourself; provision will then skip." >&2
  fi
  cat >> "$HOME/.tmux.conf" <<'TMUXEOF'

# ── floatie: session ergonomics ──────────────────────────────────────
# scrolling
set  -g mouse on                  # wheel scrolls; matches host-multiplexer habits
set  -g history-limit 50000       # tmux default is 2000 — one tool call overruns it
                                  # applies to panes created AFTER this loads

# copy mode
setw -g mode-keys vi
bind -T copy-mode-vi v send -X begin-selection
bind -T copy-mode-vi y send -X copy-selection-and-cancel
bind -T copy-mode-vi MouseDragEnd1Pane send -X copy-selection-no-clear
bind      u copy-mode             # prefix+u enters scroll mode (prefix+[ still works)

# clipboard — tmux emits OSC 52 correctly, but Zellij swallows it upstream.
# No terminal-features override needed: the attached client reports
# xterm-256color, which already matches tmux's built-in xterm*:clipboard.
set  -g set-clipboard on

# Claude Code quality of life
set  -g focus-events on           # Claude Code prints a startup hint asking for this
set -sg escape-time 10            # removes ESC lag in the TUI
set -ga terminal-features ',xterm*:RGB'   # 24-bit colour in the TUI
TMUXEOF
  echo "==> wrote tmux config to ~/.tmux.conf"
else
  echo "==> ~/.tmux.conf already has the floatie block — skipping"
fi

# A tmux server that is already running never re-reads ~/.tmux.conf — tmux only
# sources it at server start. So on a re-provision of a live VM the file above
# would be written and change nothing. Push it into the running server if there
# is one. Harmless when no server exists (the next `floatie attach` reads the
# file itself). Existing panes keep their old scrollback buffer; new panes and
# sessions pick up the new history-limit.
if command -v tmux >/dev/null 2>&1 && tmux info >/dev/null 2>&1; then
  if tmux source-file "$HOME/.tmux.conf" 2>/dev/null; then
    echo "==> sourced ~/.tmux.conf into the running tmux server"
  else
    echo "WARN: could not source ~/.tmux.conf into the running tmux server — new sessions will pick it up after the server restarts." >&2
  fi
fi

echo "==> floatie provision: done"
