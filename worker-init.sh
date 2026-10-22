#!/bin/bash
# worker-init.sh — create a git worktree PER REPO + a named tmux session for a worker.
# Invoked as: limactl shell <vm> -- bash -s -- <name> <repo-name>... < worker-init.sh
#
# Arguments (positional):
#   $1        = worker name (e.g. "alpha")
#   $2,$3,... = optional repo NAMES; omitted means saved scope, or all actual clones
#
# What it does:
#   1. Resolve/validate the complete scope before mutation. Reconnect preserves the
#      exact saved set, branches and files. For NEW work, fast-forwards the
#      ~/work/<repo> clone to origin/<default> first, so
#      the worktree below branches from current code — but only when that is safe: clean,
#      on its default branch, no local commits. Anything else is reported and left alone,
#      and a failed fetch is a WARN, never an abort. See refresh_clone() below.
#   2. For each repo, creates a worktree at ~/work/.worktrees/<name>/<repo> on
#      branch worker/<name> (uniform nest, even for a single repo).
#   3. Starts a detached tmux session named <name> whose CWD is the PARENT
#      ~/work/.worktrees/<name>/ — so the agent sees every repo worktree side by
#      side, exactly as `main` sees ~/work. It exports COMPOSE_PROJECT_NAME=<name>.
#   4. A live session with the same verified scope offers attach; scope changes fail.
set -euo pipefail

WORKER_NAME="${1:-}"
[ -n "$WORKER_NAME" ] || { echo "error: worker name is required" >&2; exit 1; }
shift || true

# ── Worker-name validation (defense in depth — host-side guard is primary) ────
if ! [[ "$WORKER_NAME" =~ ^[a-z0-9][a-z0-9_-]{0,62}$ ]]; then
  echo "error: invalid worker name '$WORKER_NAME'" >&2
  echo "       Worker names must:" >&2
  echo "         * start with a lowercase letter or digit" >&2
  echo "         * contain only lowercase letters, digits, '-', or '_'" >&2
  echo "         * be at most 63 characters long" >&2
  echo "         * contain no uppercase letters, spaces, slashes, or quotes" >&2
  exit 1
fi

# These names belong to the root workspace or command-wide targets.
case "$WORKER_NAME" in
  stack|main|workspace|all) echo "error: '$WORKER_NAME' is a reserved worker name" >&2; exit 1 ;;
esac

WORK_ROOT=$(cd "$HOME/work" && pwd -P) || exit 1
WORKTREE_BASE="$WORK_ROOT/.worktrees"
WORKER_DIR="$WORKTREE_BASE/$WORKER_NAME"
BRANCH="worker/$WORKER_NAME"
REPOS=()
RECONNECT=false
shopt -s nullglob dotglob

scope_error() {
  echo "error: $*. Inspect the worker and repository scope before retrying." >&2
  return 1
}

valid_repo_name() {
  [[ "$1" =~ ^[A-Za-z0-9._-]+$ ]] && [ "$1" != . ] && [ "$1" != .. ]
}

contains() {
  local needle="$1" item
  shift
  for item in "$@"; do [ "$item" != "$needle" ] || return 0; done
  return 1
}

# Inspect identity only: reconnect must accept dirty files, local commits and custom
# branches. A listed path and shared common-dir alone cannot identify a worktree:
# a corrupt .git file could point at a sibling's HEAD/index in that same clone.
inspect_repository() {
  local path="$1" top listing matches common gitdir backlink backlink_dir
  [ -d "$path" ] && [ ! -L "$path" ] && [ ! -L "$path/.git" ] || return 1
  [ -d "$path/.git" ] || [ -f "$path/.git" ] || return 1
  top=$(git -C "$path" rev-parse --show-toplevel 2>/dev/null) || return 1
  [ "$top" = "$path" ] || return 1
  listing=$(git -C "$path" worktree list --porcelain 2>/dev/null) || return 1
  matches=$(printf '%s\n' "$listing" | grep -cxF "worktree $path") || return 1
  [ "$matches" = 1 ] || return 1
  common=$(git -C "$path" rev-parse --git-common-dir 2>/dev/null) || return 1
  common=$(cd "$path" && cd "$common" && pwd -P) || return 1
  gitdir=$(git -C "$path" rev-parse --absolute-git-dir 2>/dev/null) || return 1
  gitdir=$(cd "$gitdir" && pwd -P) || return 1
  if [ "$gitdir" != "$common" ]; then
    [ "${gitdir%/*}" = "$common/worktrees" ] || return 1
    [ -f "$gitdir/gitdir" ] && [ ! -L "$gitdir/gitdir" ] || return 1
    backlink=$(cat "$gitdir/gitdir") || return 1
    case "$backlink" in /*) ;; *) backlink="$gitdir/$backlink" ;; esac
    backlink_dir=$(cd "${backlink%/*}" && pwd -P) || return 1
    [ "$backlink_dir/${backlink##*/}" = "$path/.git" ] || return 1
  fi
  REPO_COMMON="$common" REPO_GITDIR="$gitdir" REPO_LISTING="$listing"
}

resolve_scope() {
  local path name common line registered listing
  local saved=() saved_common=() requested=() requested_common=()
  [ ! -L "$WORKTREE_BASE" ] && [ ! -L "$WORKER_DIR" ] || { scope_error "symlinked worker path"; return 1; }
  if [ -e "$WORKER_DIR" ]; then
    [ -d "$WORKER_DIR" ] && [ -r "$WORKER_DIR" ] && [ -x "$WORKER_DIR" ] || { scope_error "unreadable worker '$WORKER_NAME'"; return 1; }
    RECONNECT=true
    for path in "$WORKER_DIR"/*; do
      name="${path##*/}"
      valid_repo_name "$name" && inspect_repository "$WORK_ROOT/$name" || { scope_error "unknown saved repo '$name'"; return 1; }
      common="$REPO_COMMON"
      [ -f "$path/.git" ] && inspect_repository "$path" &&
        [ "$REPO_COMMON" = "$common" ] && [ "$REPO_GITDIR" != "$common" ] || { scope_error "unregistered or invalid saved worktree '$name'"; return 1; }
      if contains "$common" "${saved_common[@]+"${saved_common[@]}"}"; then
        scope_error "saved repositories share Git storage"; return 1
      fi
      saved+=("$name") saved_common+=("$common")
    done
    [ "${#saved[@]}" -gt 0 ] || { scope_error "empty saved scope for '$WORKER_NAME'"; return 1; }
  fi

  # A missing registered directory must not silently shrink a saved set. Scan
  # actual root clones, including ones not explicitly requested, before mutation.
  for path in "$WORK_ROOT"/*; do
    name="${path##*/}"
    valid_repo_name "$name" && inspect_repository "$path" || continue
    listing="$REPO_LISTING" common="$REPO_COMMON"
    while IFS= read -r line; do
      case "$line" in
        "worktree $WORKER_DIR"|"worktree $WORKER_DIR/"*)
          registered="${line#worktree }"
          if [ "${registered%/*}" != "$WORKER_DIR" ] ||
             ! contains "${registered##*/}" "${saved[@]+"${saved[@]}"}" ||
             ! inspect_repository "$registered" || [ "$REPO_COMMON" != "$common" ]; then
            scope_error "missing or unexpected registered worktree '$registered'"; return 1
          fi ;;
      esac
    done <<< "$listing"
  done

  # Validate every explicit repository before refreshing even the first clone.
  for name in "$@"; do
    valid_repo_name "$name" || { scope_error "invalid repo name '$name'"; return 1; }
    contains "$name" "${requested[@]+"${requested[@]}"}" && continue
    inspect_repository "$WORK_ROOT/$name" || { scope_error "repo '$name' is not an available clone"; return 1; }
    if contains "$REPO_COMMON" "${requested_common[@]+"${requested_common[@]}"}"; then
      scope_error "requested repositories share Git storage"; return 1
    fi
    requested+=("$name") requested_common+=("$REPO_COMMON")
  done
  if [ "$RECONNECT" = true ]; then
    if [ "$#" -gt 0 ]; then
      [ "${#requested[@]}" -eq "${#saved[@]}" ] || { scope_error "requested scope differs from saved scope: ${saved[*]}"; return 1; }
      for name in "${requested[@]}"; do
        contains "$name" "${saved[@]}" || { scope_error "requested scope differs from saved scope: ${saved[*]}"; return 1; }
      done
    fi
    REPOS=("${saved[@]}")
  elif [ "$#" -gt 0 ]; then
    REPOS=("${requested[@]}")
  else
    # Omitted NEW scope: ignore non-clones and retain one name per common-dir.
    for path in "$WORK_ROOT"/*; do
      name="${path##*/}"
      valid_repo_name "$name" && inspect_repository "$path" || continue
      contains "$REPO_COMMON" "${requested_common[@]+"${requested_common[@]}"}" && continue
      REPOS+=("$name") requested_common+=("$REPO_COMMON")
    done
  fi
  [ "${#REPOS[@]}" -gt 0 ] || { scope_error "no available repositories"; return 1; }
}

resolve_scope "$@"
if tmux has-session -t "=$WORKER_NAME" 2>/dev/null; then
  [ "$RECONNECT" = true ] || { scope_error "live session '$WORKER_NAME' has no saved scope"; exit 1; }
  echo "==> Already exists — in VM: tmux attach-session -t '=$WORKER_NAME'"
  exit 0
fi

# ── Fresh code: fast-forward a clean default-branch clone before branching from it ─
# Worktrees branch from the clone's CURRENT HEAD, and clones only move when someone
# pulls them — so without this a new worker starts weeks behind origin. Only a clone
# that is clean, on its default branch and strictly behind origin is moved, and only by
# fast-forward. Everything else — a detached HEAD, another branch, a dirty tree, local
# commits origin does not have, an unreachable origin — is reported and left alone. An
# existing worker/<name> branch is reused below, unchanged.

# WARN goes to stdout, not stderr: the worker still comes up, so this is a progress note
# on a succeeding path (same convention as disk-preflight.sh).
# $2 is git's own stderr — its first line says WHY.
refresh_warn() {  # $1 = repo name, $2 = git's stderr
  local reason
  reason="$(printf '%s\n' "$2" | sed -n '1p')"
  [ -n "$reason" ] || reason="git exited non-zero"
  echo "WARN: [$1] refresh failed — branching from current HEAD: $reason"
}

refresh_clone() {  # $1 = repo path, $2 = repo name (for messages)
  local repo="$1" name="$2" default current head_sha origin_sha out
  if ! git -C "$repo" remote get-url origin >/dev/null 2>&1; then
    echo "==> [$name] not refreshed: no 'origin' remote"; return 0
  fi
  current="$(git -C "$repo" symbolic-ref --quiet --short HEAD 2>/dev/null || true)"
  if [ -z "$current" ]; then
    echo "==> [$name] not refreshed: detached HEAD"; return 0
  fi
  # origin/HEAD is unset in a clone built by `remote add` + `fetch` rather than by
  # `git clone`; `|| true` keeps that failure from taking the script down under
  # `set -euo pipefail` before the fallback runs. Fall back to the branch we are on
  # when origin has it (that is what a fetch here tracks), else main.
  default="$(git -C "$repo" symbolic-ref --quiet --short refs/remotes/origin/HEAD 2>/dev/null | sed 's#^origin/##' || true)"
  if [ -z "$default" ]; then
    if git -C "$repo" show-ref --verify --quiet "refs/remotes/origin/$current" 2>/dev/null; then
      default="$current"
    else
      default=main
    fi
  fi
  if [ "$current" != "$default" ]; then
    echo "==> [$name] not refreshed: on branch '$current', not '$default'"; return 0
  fi
  if [ -n "$(git -C "$repo" status --porcelain 2>/dev/null)" ]; then
    echo "==> [$name] not refreshed: clone has uncommitted changes"; return 0
  fi
  # Never block a worker create on a tty prompt (a 401 on a private HTTPS remote asks for
  # a username) and never stall indefinitely on a dead network. `timeout` is not portable
  # to macOS, so bound it with git's own low-speed abort instead.
  if ! out="$(GIT_TERMINAL_PROMPT=0 GIT_SSH_COMMAND='ssh -o BatchMode=yes' \
       git -C "$repo" -c http.lowSpeedLimit=1000 -c http.lowSpeedTime=30 \
       fetch --quiet --prune origin 2>&1)"; then
    refresh_warn "$name" "$out"; return 0
  fi
  origin_sha="$(git -C "$repo" rev-parse --verify --quiet "refs/remotes/origin/$default" 2>/dev/null || true)"
  if [ -z "$origin_sha" ]; then
    echo "==> [$name] not refreshed: origin has no '$default' branch"; return 0
  fi
  # An UNBORN HEAD — a clone built by `init` + `remote add` + `fetch`, whose branch has no
  # commit yet — falls straight through to the fast-forward below, which handles that case
  # itself and populates HEAD from origin. Skipping the refresh here instead is what breaks
  # it: `git worktree add` then dies on `fatal: not a valid object name: 'HEAD'` and takes
  # the whole worker down under `set -e`. There is also nothing to lose — no local commits,
  # no checked-out tree. Both comparisons below need a HEAD, so they are the guarded part.
  head_sha="$(git -C "$repo" rev-parse --verify --quiet HEAD 2>/dev/null || true)"
  if [ -n "$head_sha" ]; then
    if [ "$head_sha" = "$origin_sha" ]; then
      echo "==> [$name] already at origin/$default ($(git -C "$repo" rev-parse --short HEAD))"; return 0
    fi
    # Ahead of, or diverged from, origin: the clone holds commits origin does not. A
    # fast-forward is impossible and anything else would move someone's local work, so
    # say so plainly rather than reporting a refresh that did not happen.
    if ! git -C "$repo" merge-base --is-ancestor HEAD "refs/remotes/origin/$default" 2>/dev/null; then
      echo "==> [$name] not refreshed: local commits on '$default' are not on origin"; return 0
    fi
  fi
  if out="$(git -C "$repo" merge --quiet --ff-only "origin/$default" 2>&1)"; then
    echo "==> [$name] refreshed to origin/$default ($(git -C "$repo" rev-parse --short HEAD))"
  else
    refresh_warn "$name" "$out"  # behind origin or unborn HEAD — both fast-forward cleanly, so a failure here is unexpected
  fi
}

# Reconnect only recreates the terminal. Never refresh clones, add repositories or
# reset branches/indexes in an existing saved scope.
if [ "$RECONNECT" = false ]; then
  # Fetch credentials are needed only when creating new work.
  if [ -f "$HOME/.env.secrets" ]; then set -a; . "$HOME/.env.secrets"; set +a; fi
  mkdir -p "$WORKER_DIR"
  CREATED_REPOS=()
  for REPO_NAME in "${REPOS[@]}"; do
    REPO_PATH="$WORK_ROOT/$REPO_NAME"
    WT="$WORKER_DIR/$REPO_NAME"
    refresh_clone "$REPO_PATH" "$REPO_NAME" || true
    ADD_ARGS=()
    if ! git -C "$REPO_PATH" show-ref --verify --quiet "refs/heads/$BRANCH" 2>/dev/null; then
      ADD_ARGS=(-b)
    fi
    echo "==> [$REPO_NAME] creating worktree at $WT on branch $BRANCH"
    if ! git -C "$REPO_PATH" worktree add "$WT" "${ADD_ARGS[@]+"${ADD_ARGS[@]}"}" "$BRANCH"; then
      echo "error: worker creation incomplete; no session started." >&2
      echo "       requested: ${REPOS[*]}" >&2
      echo "       created: ${CREATED_REPOS[*]:-none}" >&2
      echo "       missing: ${REPOS[*]:${#CREATED_REPOS[@]}}" >&2
      echo "       Saved work was kept. Inspect it and reconnect with that exact reduced scope." >&2
      exit 1
    fi
    CREATED_REPOS+=("$REPO_NAME")
  done
fi

# ── CWD-derive COMPOSE_PROJECT_NAME hook in ~/.bashrc (unchanged — handles nesting) ─
# Derives COMPOSE_PROJECT_NAME=<name> from CWD when inside ~/work/.worktrees/<name>/*,
# covering new tmux panes/splits. `${_w%%/*}` takes the first component, so both the
# parent (.worktrees/<name>) and a repo worktree (.worktrees/<name>/<repo>) yield <name>.
BASHRC_WORKTREE_MARKER="# floatie: derive COMPOSE_PROJECT_NAME from worktree CWD"
if ! grep -qF "$BASHRC_WORKTREE_MARKER" "$HOME/.bashrc" 2>/dev/null; then
  cat >> "$HOME/.bashrc" <<'BASHRCEOF'

# floatie: derive COMPOSE_PROJECT_NAME from worktree CWD
# When the shell starts inside ~/work/.worktrees/<name>/*, export
# COMPOSE_PROJECT_NAME=<name> so `docker compose` is automatically namespaced.
case "$PWD/" in
  "$HOME/work/.worktrees/"*) _w="${PWD#$HOME/work/.worktrees/}"; export COMPOSE_PROJECT_NAME="${_w%%/*}"; unset _w;;
esac
BASHRCEOF
  echo "==> added COMPOSE_PROJECT_NAME CWD-derive hook to ~/.bashrc"
else
  echo "==> COMPOSE_PROJECT_NAME CWD-derive hook already in ~/.bashrc"
fi

# ── Launch tmux session, rooted at the worker dir (parent of all repo worktrees) ─
LAUNCH_CMD="bash --login -c 'set -a; [ -f \"\$HOME/.env.secrets\" ] && . \"\$HOME/.env.secrets\"; set +a; export COMPOSE_PROJECT_NAME=$WORKER_NAME; exec bash'"
tmux new-session -d -s "$WORKER_NAME" -c "$WORKER_DIR" "$LAUNCH_CMD"

echo "==> tmux session '$WORKER_NAME' started (CWD: $WORKER_DIR, COMPOSE_PROJECT_NAME=$WORKER_NAME, ${#REPOS[@]} repo worktree(s))"
echo "==> in VM: tmux attach-session -t '=$WORKER_NAME'"
