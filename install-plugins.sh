#!/bin/bash
# install-plugins.sh — install Claude Code plugins from workspace.yml.
# Invoked as: limactl shell <name> -- bash -s -- <entry>... < install-plugins.sh
# Each positional argument is one plugins: entry from workspace.yml (already
# parsed by the justfile):
#   - "owner/repo" shape (no @)  → claude plugin marketplace add <entry>
#                                  (already added: claude plugin marketplace update <name>)
#   - "<plugin>@<marketplace>"   → claude plugin install <entry>
#                                  (already installed: claude plugin update <entry>)
#
# Auth: plugins use the cached ~/.claude login — run `floatie attach` → `claude`
# → `/login` once per VM (browser on the host via Lima port-forwarding; the token
# caches in ~/.claude and persists with the VM).  If not yet logged in, each
# per-entry call below fails and is skipped with a WARN (never a hard failure).
# Log in, then run `floatie reload` to retry.
#
# Idempotent: a known marketplace or installed plugin is updated, an unknown one
# is added or installed.  A failed call only warns; the script always exits 0.
# ~/.env.secrets is sourced at the top (for GITHUB_TOKEN used by marketplace clones)
# so this script works in a non-login, non-interactive bash (same pattern as
# clone-repos.sh).

set -euo pipefail

# Source secrets so GITHUB_TOKEN (needed for private marketplace clones) is
# available in this non-interactive non-login shell.  ~/.env.secrets is copied
# to the guest by `floatie create` before this script runs.
if [ -f "$HOME/.env.secrets" ]; then set -a; . "$HOME/.env.secrets"; set +a; fi

# Ensure ~/.local/bin (Claude Code) is on PATH for a non-login shell.
export PATH="$HOME/.local/bin:$PATH"

# ── No entries → clean no-op ──────────────────────────────────────────────────
if [ "$#" -eq 0 ]; then
  echo "==> install-plugins: no plugins configured — nothing to do"
  exit 0
fi

echo "==> install-plugins: starting (${#} entr$([ "$#" -eq 1 ] && echo 'y' || echo 'ies'))"

# ── Helper: check if marketplace is already added ─────────────────────────────
marketplace_known() {
  local entry="$1"
  # `claude plugin marketplace list` output format:
  #   Configured marketplaces:
  #
  #     ❯ claude-plugins-official
  #       Source: GitHub (anthropics/claude-plugins-official)
  #
  # The marketplace short-name (last path component of owner/repo) appears as the
  # sole token on lines matching "❯ <name>".  Anchor the match to "❯ <name>" to
  # avoid a substring hit where e.g. "fmt" would incorrectly match "gofmt".
  local mkt_name="${entry##*/}"   # e.g. "claude-plugins-official" from "anthropics/claude-plugins-official"
  claude plugin marketplace list 2>/dev/null | grep -qF "❯ $mkt_name" || return 1
}

# ── Helper: check if plugin is already installed ──────────────────────────────
plugin_known() {
  local entry="$1"
  # `claude plugin list` output format:
  #   ❯ superpowers@superpowers-marketplace
  #     Version: ...
  #
  # Entry is "<plugin>@<marketplace>".  Anchor match to "❯ <plugin>@" so that a
  # plugin named "fmt" does not false-match "gofmt@some-marketplace".
  local plugin_name="${entry%%@*}"
  claude plugin list 2>/dev/null | grep -qF "❯ ${plugin_name}@" || return 1
}

# ── Process each entry ────────────────────────────────────────────────────────
for ENTRY in "$@"; do
  if printf '%s' "$ENTRY" | grep -q '@'; then
    # ── Plugin install: <plugin>@<marketplace-name> ───────────────────────────
    echo "==> plugin: $ENTRY"
    if plugin_known "$ENTRY"; then
      echo "==>   running: claude plugin update $ENTRY"
      if claude plugin update "$ENTRY"; then
        echo "==>   updated"
      else
        echo "WARN: 'claude plugin update $ENTRY' failed (auth or network?) — skipping" >&2
      fi
    else
      echo "==>   running: claude plugin install $ENTRY"
      if claude plugin install "$ENTRY"; then
        echo "==>   installed"
      else
        echo "WARN: 'claude plugin install $ENTRY' failed (auth or network?) — skipping" >&2
      fi
    fi
  else
    # ── Marketplace add: owner/repo or URL ────────────────────────────────────
    echo "==> marketplace: $ENTRY"
    if marketplace_known "$ENTRY"; then
      echo "==>   running: claude plugin marketplace update ${ENTRY##*/}"
      if claude plugin marketplace update "${ENTRY##*/}"; then
        echo "==>   updated"
      else
        echo "WARN: 'claude plugin marketplace update ${ENTRY##*/}' failed (auth or network?) — skipping" >&2
      fi
    else
      echo "==>   running: claude plugin marketplace add $ENTRY"
      if claude plugin marketplace add "$ENTRY"; then
        echo "==>   added"
      else
        echo "WARN: 'claude plugin marketplace add $ENTRY' failed (auth or network?) — skipping" >&2
      fi
    fi
  fi
done

echo "==> install-plugins: done"
