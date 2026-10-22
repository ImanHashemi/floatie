#!/bin/bash
# clone-repos.sh — clone workspace repos onto VM disk.
# Invoked as: limactl shell <name> -- bash -s -- <repo>... < clone-repos.sh
# Each positional argument is one repo source (already normalized by the justfile):
#   - full URL   → passed verbatim to git clone
#   - local path → passed verbatim to git clone
#
# Clone destination: ~/work/<repo-name>  (VM disk, NOT the read-only host mount).
# Idempotent: if the directory already exists, skip with a message.
# A failed clone aborts the script; fix the entry and re-run `just create` —
# already-cloned repos are skipped.
#
# GITHUB_TOKEN availability:
#   Interactive shells get GITHUB_TOKEN via the ~/.bashrc hook (slice #3 mechanism).
#   Headless worker launches get it via the explicit source in their launch command.
#   Bare headless `bash -s` invocations (this script) receive no login/profile sourcing,
#   so we source ~/.env.secrets explicitly here to ensure GITHUB_TOKEN reaches the
#   credential helper for private-repo clones.

set -euo pipefail

# Source secrets so GITHUB_TOKEN is available in this non-interactive non-login shell.
# ~/.env.secrets is copied to the guest by `just create` before the clone step runs.
if [ -f "$HOME/.env.secrets" ]; then set -a; . "$HOME/.env.secrets"; set +a; fi

mkdir -p "$HOME/work"

for SRC in "$@"; do
  # Strip trailing slash so the repo-name derivation and idempotency check work correctly.
  SRC="${SRC%/}"

  # Derive repo name: last path component, strip trailing .git
  REPO_NAME="${SRC##*/}"
  REPO_NAME="${REPO_NAME%.git}"

  DEST="$HOME/work/$REPO_NAME"

  if [ -d "$DEST" ]; then
    EXISTING_ORIGIN=$(git -C "$DEST" remote get-url origin 2>/dev/null || true)
    # Normalise both sides by stripping a trailing .git so that shorthand and
    # verbatim references to the same repo compare equal.
    if [ "${EXISTING_ORIGIN%.git}" = "${SRC%.git}" ]; then
      echo "==> skip (already cloned): $DEST"
    else
      echo "WARN: $DEST exists but origin is '$EXISTING_ORIGIN'; skipping '$SRC' — rename one repo or remove the dir" >&2
    fi
  else
    echo "==> cloning $SRC -> $DEST"
    git clone "$SRC" "$DEST"
    echo "==> cloned: $DEST"
  fi
done

echo "==> clone-repos: done"
