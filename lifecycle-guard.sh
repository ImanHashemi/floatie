#!/bin/bash
# lifecycle-guard.sh — pre-destroy/rebuild push-guard.
# Enumerates ALL git worktrees in the VM (main repos + worker worktrees) and
# aborts if any has uncommitted changes OR unpushed commits.
#
# Invoked as: limactl shell <vm> -- bash -s < lifecycle-guard.sh
#
# Exit codes:
#   0 — all clean; caller may proceed with destroy/rebuild
#   1 — one or more worktrees are unsafe; human must push/commit or pass --force
#
# Output (stdout): human-readable per-worktree report.
# The caller is responsible for the --force override (this script always enforces).

set -euo pipefail

UNSAFE=0
REPORT=""

# Output mode: default human report (+ exit code); "--json" emits a structured
# array of EVERY git worktree (clean + unsafe) to stdout and always exits 0.
MODE="human"
[ "${1:-}" = "--json" ] && MODE="json"

# Base work dir (overridable for host-side testing; defaults to the in-VM path).
WORK_BASE="${FLOATIE_WORK_DIR:-$HOME/work}"

JSON_ITEMS=""   # comma-joined JSON objects, built as we traverse

# JSON-escape a string (handles backslash and doublequote; paths/labels never
# contain raw newlines or tabs in practice).
jescape() {
  printf '%s' "$1" | sed -e 's/\\/\\\\/g' -e 's/"/\\"/g'
}

# ── Helper: check one worktree ─────────────────────────────────────────────────
# Args: $1 = path to the worktree, $2 = display label (e.g. "main repo" or "worker alpha")
check_worktree() {
  local path="$1" label="$2" problems="" error="" dirty="" refs="" branch="" upstream=""
  local ref="" value="" tip="" head="" uncommitted=null unpushed=null clean=false item=""
  local index_entries="" entry=""
  local excludes=()
  [ -d "$path" ] || return 0

  if ! git -C "$path" rev-parse --git-dir >/dev/null 2>&1; then
    # Ordinary non-repo directories are not worktrees. A broken marker is unknown.
    [ -e "$path/.git" ] || [ -L "$path/.git" ] || return 0
    error="Git metadata unavailable"
  elif ! index_entries=$(git -C "$path" ls-files -v 2>/dev/null); then
    error="Git index flags unavailable"
  else
    # Match the removal gate: status cannot prove cleanliness with either bit.
    # Observe flags without clearing them or rewriting the index.
    while IFS= read -r entry; do
      # Letters spelled out: under bash 3.2 with a UTF-8 locale, [a-z] also matches uppercase.
      case "$entry" in [abcdefghijklmnopqrstuvwxyzS]" "*)
        error="Index flags can hide tracked changes"
        break ;;
      esac
    done <<< "$index_entries"
    if [ -z "$error" ] && ! dirty=$(GIT_OPTIONAL_LOCKS=0 git -C "$path" status --porcelain --untracked-files=all 2>/dev/null); then
      error="Git status unavailable"
    fi
  fi
  if [ -z "$error" ]; then
    uncommitted=0
    if [ -n "$dirty" ]; then
      uncommitted=$(printf '%s\n' "$dirty" | grep -c .)
      problems="${problems}    uncommitted changes:\n$dirty\n"
    fi
    if ! head=$(git -C "$path" rev-parse --verify 'HEAD^{commit}' 2>/dev/null); then
      error="HEAD unavailable"
    elif branch=$(git -C "$path" symbolic-ref --quiet HEAD 2>/dev/null); then
      if refs=$(git -C "$path" for-each-ref --format='%(refname) %(upstream)' "$branch" 2>/dev/null); then
        while read -r ref value; do
          [ "$ref" != "$branch" ] || upstream="$value"
        done <<< "$refs"
      else
        error="Branch enumeration unavailable"
      fi
    elif [ "$?" -ne 1 ]; then
      error="HEAD branch unavailable"
    fi
    if [ -z "$error" ]; then
      if [ -n "$upstream" ]; then
        refs="$upstream"
      elif ! refs=$(git -C "$path" for-each-ref --format='%(refname)' refs/remotes/ 2>/dev/null); then
        error="Remote ref enumeration unavailable"
      fi
      if [ -z "$error" ]; then
        while IFS= read -r ref; do
          [ -n "$ref" ] || continue
          if ! tip=$(git -C "$path" rev-parse --verify "$ref^{commit}" 2>/dev/null); then
            error="Remote reachability evidence unavailable"
            break
          fi
          excludes+=("^$tip")
        done <<< "$refs"
        if [ -z "$error" ]; then
          if [ "${#excludes[@]}" -gt 0 ]; then
            unpushed=$(git -C "$path" rev-list --count "$head" "${excludes[@]}" -- 2>/dev/null) || error="Commit reachability unavailable"
          else
            unpushed=$(git -C "$path" rev-list --count "$head" -- 2>/dev/null) || error="Commit count unavailable"
          fi
          case "$unpushed" in ''|*[!0-9]*) error="Commit count unavailable" ;; esac
        fi
      fi
    fi
  fi

  if [ -n "$error" ]; then
    # Incomplete observations never masquerade as known-zero preservation counts.
    uncommitted=null unpushed=null
    problems="${problems}    $error — cannot confirm clean\n"
  else
    if [ "$unpushed" -gt 0 ]; then
      if [ -z "$refs" ]; then
        problems="${problems}    $unpushed commit(s) with no remote configured\n"
      else
        problems="${problems}    $unpushed unpushed commit(s) not reachable from backup refs\n"
      fi
    fi
    [ "$uncommitted" -ne 0 ] || [ "$unpushed" -ne 0 ] || clean=true
  fi
  item=$(printf '{"label":"%s","path":"%s","clean":%s,"uncommitted":%s,"unpushed":%s' \
    "$(jescape "$label")" "$(jescape "$path")" "$clean" "$uncommitted" "$unpushed")
  [ -z "$error" ] || item="$item,\"error\":\"$(jescape "$error")\""
  item="$item}"
  if [ -z "$JSON_ITEMS" ]; then JSON_ITEMS="$item"; else JSON_ITEMS="$JSON_ITEMS,$item"; fi
  if [ -n "$problems" ]; then
    UNSAFE=1
    REPORT="${REPORT}  [UNSAFE] $label ($path):\n${problems}"
  fi
}

# ── Enumerate main repos under ~/work/ (excluding .worktrees) ─────────────────
if [ -d "$WORK_BASE" ]; then
  # Use a while-read loop (bash 3.2-compat; no mapfile).
  while IFS= read -r repo_path; do
    [ -n "$repo_path" ] || continue
    repo_name=$(basename "$repo_path")
    check_worktree "$repo_path" "main repo '$repo_name'"
  done <<< "$(find "$WORK_BASE" -maxdepth 1 -mindepth 1 -type d ! -name '.worktrees' 2>/dev/null || true)"
fi

# ── Enumerate worker worktrees under ~/work/.worktrees/ ───────────────────────
# Multi-repo workers nest one level: .worktrees/<name>/<repo>. Descend to depth 2
# so each per-repo worktree is checked. check_worktree skips non-worktree dirs
# (the <name> parent has no .git), so this also covers any legacy flat
# .worktrees/<name> worktree without special-casing.
WORKTREES_DIR="$WORK_BASE/.worktrees"
if [ -d "$WORKTREES_DIR" ]; then
  while IFS= read -r wt_path; do
    [ -n "$wt_path" ] || continue
    rel="${wt_path#$WORKTREES_DIR/}"
    check_worktree "$wt_path" "worker '$rel'"
  done <<< "$(find "$WORKTREES_DIR" -mindepth 1 -maxdepth 2 -type d 2>/dev/null || true)"
fi

# ── JSON mode: emit the structured array and exit 0 (data, not a gate) ─────────
if [ "$MODE" = "json" ]; then
  printf '[%s]\n' "$JSON_ITEMS"
  exit 0
fi

# ── Report ─────────────────────────────────────────────────────────────────────
if [ "$UNSAFE" -eq 0 ]; then
  echo "==> lifecycle-guard: all worktrees are clean — safe to proceed"
  exit 0
else
  echo "error: refusing to destroy — unsafe worktrees detected:" >&2
  printf '%b' "$REPORT" >&2
  echo "" >&2
  echo "  Push or commit your work, then re-run." >&2
  echo "  To override (DATA LOSS — unpushed work will be wiped), pass 'true' as the force arg:" >&2
  echo "    floatie destroy true" >&2
  echo "    floatie rebuild true" >&2
  exit 1
fi
