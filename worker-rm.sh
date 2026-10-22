#!/bin/bash
# worker-rm.sh — remove a worker: kill its tmux session, bring its Compose
# project down, and remove EVERY per-repo worktree + branch under
# ~/work/.worktrees/<name>/.
# Invoked as: limactl shell <vm> -- bash -s -- <name> [force] < worker-rm.sh
#   $1 = worker name
#   $2 = "true" to continue past a failing workspace hook (default "false")
#
# A workspace may ship ~/work/stack/hooks/worker-rm. It runs after preservation
# preflight (Phase 1c), including for known orphans with preserved owned branches.
# A session name alone does not authorize orphan teardown.
#
# The worker's repos are enumerated from the filesystem; the main repo for each
# worktree is ~/work/<repo> (same basename). Refuses the WHOLE operation if ANY
# worktree is dirty, unpushed or unknown (guards all before removing any).
#
# Deletes exactly one branch per repo: worker/<name>, the branch worker-init.sh
# created. Once preserved, it is deleted even if the agent switched branches,
# since worker-init REUSES an existing worker/<name>. Any other branch a worktree ended
# up on is reported and kept. Compose teardown establishes provenance first and
# omits -v in built-in teardown; the workspace hook may delete named volumes.
set -euo pipefail

WORKER_NAME="${1:-}"
FORCE="${2:-false}"   # "true": continue past a failing workspace hook
[ -n "$WORKER_NAME" ] || { echo "error: worker name is required" >&2; exit 1; }

# ── Worker-name validation (defense in depth) ─────────────────────────────────
if ! printf '%s' "$WORKER_NAME" | grep -qE '^[a-z0-9][a-z0-9_-]{0,62}$'; then
  echo "error: invalid worker name '$WORKER_NAME'" >&2
  exit 1
fi

# Reserved worker names — both would make a worker collide with the main session:
#   stack  the workspace stack's own compose project. The main session exports no
#          COMPOSE_PROJECT_NAME, so compose derives the project from ~/work/stack's basename;
#          a worker of that name makes the ~/work/stack provenance exemption match the main
#          session's stack and tear it down.
#   main   the main tmux session's own name. `floatie worker main` would attach you to the
#          main session rather than create a worker, and `floatie worker-rm main` would kill
#          it. (A workspace may also key its own per-worker state on this name.)
case "$WORKER_NAME" in
  stack|main) echo "error: '$WORKER_NAME' is a reserved worker name" >&2; exit 1 ;;
esac

# ── The workspace teardown hook ───────────────────────────────────────────────
# A workspace may ship `stack/hooks/worker-rm`. It runs with the worker name, from
# ~/work/stack, so it can tear down whatever the worker's stack created that floatie
# deliberately does not touch (named volumes, built images, the workspace's own
# bookkeeping). floatie does not interpret it — it only decides WHEN it runs: after
# the safety gate, before anything is removed.
#
# The contract a workspace can rely on:
#   argv   $1 = worker name
#   cwd    ~/work/stack
#   stdin  is /dev/null — the hook is non-interactive and must never prompt
#   env    FLOATIE_WORKER, FLOATIE_FORCE, plus whatever worker-rm itself inherited.
#          ~/.env.secrets is NOT sourced: floatie cannot know what a hook needs and will
#          not hand every workspace secret to arbitrary code by default — a hook that
#          needs them sources the file itself
#   exit   non-zero aborts worker-rm unless force=true; hook effects may remain
run_workspace_hook() {
  local HOOK="$HOME/work/stack/hooks/worker-rm"
  [ -f "$HOOK" ] || { HOOK_PHASE="absent"; return 0; }
  # Say so rather than skipping in silence: a hook that lost its +x (a fresh checkout, a
  # copy, an editor) looks exactly like no hook at all, and the stack it was meant to tear
  # down then leaks with nothing in the output to explain why. Not fatal — removal proceeds.
  if [ ! -x "$HOOK" ]; then
    echo "WARN: $HOOK exists but is not executable — skipping"
    HOOK_PHASE="skipped (not executable)"
    return 0
  fi
  echo "==> running workspace hook: $HOOK $WORKER_NAME"
  # </dev/null is load-bearing: floatie streams THIS script into `bash -s` on stdin, so
  # an inherited stdin is the unread remainder of worker-rm.sh — a hook that reads it
  # (read, ssh, sudo, cat) consumes the rest of the script and worker-rm exits 0 having
  # removed nothing.
  HOOK_PHASE="attempted (effects possible)"
  if ! (cd "$HOME/work/stack" && \
        FLOATIE_WORKER="$WORKER_NAME" FLOATIE_FORCE="$FORCE" \
        "$HOOK" "$WORKER_NAME" </dev/null); then
    if [ "$FORCE" = "true" ]; then
      echo "WARN: workspace hook failed — continuing because force=true" >&2
    else
      echo "error: workspace hook '$HOOK $WORKER_NAME' failed." >&2
      echo "       Hook effects may remain. Fix the stack, or continue with: floatie worker-rm $WORKER_NAME true" >&2
      exit 1
    fi
  else
    HOOK_PHASE="completed"
  fi
}

WORKER_DIR="$HOME/work/.worktrees/$WORKER_NAME"

FLOATIE_BRANCH="worker/$WORKER_NAME"
WTS=() HEAD_TIPS=() HEAD_BACKUPS=() OWNED_TIPS=() OWNED_BACKUPS=()
HOOK_PHASE="pending" TERMINAL_PHASE="pending" STACK_PHASE="pending" PARENT_PHASE="pending"
COMPLETED="" REMAINING="" FAILED=0

report_phases() {
  local result=$?
  if [ "$result" -ne 0 ]; then
    echo "error: worker '$WORKER_NAME' removal incomplete; effects are not rolled back." >&2
  fi
  echo "==> completed/attempted phases: hook=$HOOK_PHASE; terminal=$TERMINAL_PHASE; stack=$STACK_PHASE; parent=$PARENT_PHASE"
  [ -z "$COMPLETED" ] || printf '==> completed: %s\n' "$COMPLETED"
  if [ "$result" -ne 0 ]; then
    echo "==> remaining: ${REMAINING:-unfinished phases above; inspect saved worktrees and owned branches before retrying}"
  fi
}
trap report_phases EXIT

# Resolve local preservation evidence without treating a failed probe as zero.
# Sets REF_TIP and REF_EVIDENCE; the latter records the exact backup refs/tips.
check_ref_preserved() {
  local repo="$1" ref="$2" branch="" refs="" upstream="" name="" value="" count="" tip=""
  local excludes=()
  REF_TIP="" REF_EVIDENCE=""
  REF_TIP=$(git -C "$repo" rev-parse --verify "$ref^{commit}" 2>/dev/null) || return 1
  if [ "$ref" = HEAD ]; then
    if branch=$(git -C "$repo" symbolic-ref --quiet HEAD 2>/dev/null); then
      :
    else
      [ "$?" -eq 1 ] || return 1  # 1 is detached; any other failure is unknown.
    fi
  else
    branch="$ref"
  fi
  if [ -n "$branch" ]; then
    refs=$(git -C "$repo" for-each-ref --format='%(refname) %(upstream)' "$branch" 2>/dev/null) || return 1
    while read -r name value; do
      [ "$name" != "$branch" ] || upstream="$value"
    done <<< "$refs"
  fi
  if [ -n "$upstream" ]; then
    refs="$upstream"
  else
    refs=$(git -C "$repo" for-each-ref --format='%(refname)' refs/remotes/ 2>/dev/null) || return 1
  fi
  [ -n "$refs" ] || return 1
  while IFS= read -r name; do
    [ -n "$name" ] || continue
    tip=$(git -C "$repo" rev-parse --verify "$name^{commit}" 2>/dev/null) || return 1
    excludes+=("^$tip")
    REF_EVIDENCE="${REF_EVIDENCE}${name}=${tip};"
  done <<< "$refs"
  [ "${#excludes[@]}" -gt 0 ] || return 1
  count=$(git -C "$repo" rev-list --count "$REF_TIP" "${excludes[@]}" -- 2>/dev/null) || return 1
  [ "$count" = 0 ]
}

# Enumerate rather than collapsing show-ref errors into "branch absent".
owned_branch_tip() {
  local repo="$1" branch="$2" refs="" ref=""
  OWNED_TIP="" OWNED_EVIDENCE=""
  refs=$(git -C "$repo" for-each-ref --format='%(refname)' "refs/heads/$branch" 2>/dev/null) || return 1
  while IFS= read -r ref; do
    [ "$ref" = "refs/heads/$branch" ] || continue
    check_ref_preserved "$repo" "$ref" || return 1
    OWNED_TIP="$REF_TIP" OWNED_EVIDENCE="$REF_EVIDENCE"
  done <<< "$refs"
}

# Confirm the exact registered worktree belongs to the expected main clone.
inspect_worktree() {
  local wt="$1" branch="$2" repo="$HOME/work/${1##*/}" listing="" status="" common="" main_common="" top=""
  local matches="" gitdir="" backlink="" backlink_dir="" index_entries="" entry=""
  [ -d "$wt" ] && [ ! -L "$wt" ] && [ -f "$wt/.git" ] && [ ! -L "$wt/.git" ] || return 1
  [ -d "$repo" ] && [ ! -L "$repo" ] || return 1
  listing=$(git -C "$repo" worktree list --porcelain 2>/dev/null) || return 1
  matches=$(printf '%s\n' "$listing" | grep -cxF "worktree $wt") || return 1
  [ "$matches" = 1 ] || return 1
  common=$(git -C "$wt" rev-parse --git-common-dir 2>/dev/null) || return 1
  main_common=$(git -C "$repo" rev-parse --git-common-dir 2>/dev/null) || return 1
  common=$(cd "$wt" && cd "$common" && pwd -P) || return 1
  main_common=$(cd "$repo" && cd "$main_common" && pwd -P) || return 1
  [ "$common" = "$main_common" ] || return 1
  # Common storage is shared by siblings; HEAD/index must come from this exact
  # registration, with a backlink to this worktree's .git file.
  gitdir=$(git -C "$wt" rev-parse --absolute-git-dir 2>/dev/null) || return 1
  gitdir=$(cd "$gitdir" && pwd -P) || return 1
  [ "${gitdir%/*}" = "$common/worktrees" ] || return 1
  [ -f "$gitdir/gitdir" ] && [ ! -L "$gitdir/gitdir" ] || return 1
  backlink=$(cat "$gitdir/gitdir") || return 1
  case "$backlink" in /*) ;; *) backlink="$gitdir/$backlink" ;; esac
  backlink_dir=$(cd "${backlink%/*}" && pwd -P) || return 1
  [ "$backlink_dir/${backlink##*/}" = "$wt/.git" ] || return 1
  top=$(git -C "$wt" rev-parse --show-toplevel 2>/dev/null) || return 1
  [ "$top" = "$wt" ] || return 1
  # -v uses lowercase tags for assume-unchanged and S/s for skip-worktree.
  # These can hide real edits from both status and removal. Refuse as unknown;
  # never clear the flags or otherwise rewrite the user's index to inspect it.
  index_entries=$(git -C "$wt" ls-files -v 2>/dev/null) || return 1
  while IFS= read -r entry; do
    # Letters spelled out: under bash 3.2 with a UTF-8 locale, [a-z] also matches uppercase.
    case "$entry" in [abcdefghijklmnopqrstuvwxyzS]" "*)
      echo "error: '$wt' has index flags that can hide tracked changes." >&2
      return 1 ;;
    esac
  done <<< "$index_entries"
  status=$(GIT_OPTIONAL_LOCKS=0 git -C "$wt" status --porcelain --untracked-files=all 2>/dev/null) || return 1
  [ -z "$status" ] || return 1
  check_ref_preserved "$wt" HEAD || return 1
  HEAD_TIP="$REF_TIP" HEAD_EVIDENCE="$REF_EVIDENCE"
  owned_branch_tip "$repo" "$branch" || return 1
}

# All filesystem candidates must be recognized, including hidden parent entries.
# These snapshots authorize only this invocation and are checked again at removal.
preflight_worker() {
  local worker_dir="$1" owned_branch="$2" entries="" wt=""
  [ ! -L "$HOME/work/.worktrees" ] && [ ! -L "$worker_dir" ] || return 1
  [ -d "$worker_dir" ] && [ -r "$worker_dir" ] && [ -x "$worker_dir" ] || return 1
  check_registered_inventory "$worker_dir" "$owned_branch" || return 1
  entries=$(find "$worker_dir" -mindepth 1 -maxdepth 1 -print 2>/dev/null) || return 1
  while IFS= read -r wt; do
    [ -n "$wt" ] || continue
    if ! inspect_worktree "$wt" "$owned_branch"; then
      echo "error: cannot preserve '$wt': dirty, unpushed, unregistered or unknown Git state." >&2
      return 1
    fi
    WTS+=("$wt")
    HEAD_TIPS+=("$HEAD_TIP") HEAD_BACKUPS+=("$HEAD_EVIDENCE")
    OWNED_TIPS+=("$OWNED_TIP") OWNED_BACKUPS+=("$OWNED_EVIDENCE")
  done <<< "$entries"
  # An empty directory is not ownership evidence; only a preserved owned branch
  # can authorize the known-orphan path when no recognized worktrees remain.
  [ "${#WTS[@]}" -gt 0 ] || [ "$KNOWN_ORPHAN" = true ]
}

# Check registrations as well as directories: a missing/unreadable worktree must
# not disappear from the inventory and let teardown proceed on a partial set.
check_registered_inventory() {
  local worker_dir="$1" owned_branch="$2" entries="" repo="" listing="" line="" registered=""
  KNOWN_ORPHAN=false
  entries=$(find "$HOME/work" -mindepth 1 -maxdepth 1 -type d ! -name '.worktrees' -print 2>/dev/null) || return 1
  while IFS= read -r repo; do
    [ -n "$repo" ] || continue
    [ -e "$repo/.git" ] || [ -L "$repo/.git" ] || continue
    listing=$(git -C "$repo" worktree list --porcelain 2>/dev/null) || return 1
    while IFS= read -r line; do
      case "$line" in
        "worktree $worker_dir"|"worktree $worker_dir/"*)
          registered="${line#worktree }"
          if [ "$registered" != "$worker_dir/${repo##*/}" ] ||
             [ ! -d "$registered" ] || [ -L "$registered" ]; then
            echo "error: missing or unexpected registered worktree: $registered" >&2
            return 1
          fi ;;
      esac
    done <<< "$listing"
    owned_branch_tip "$repo" "$owned_branch" || return 1
    [ -z "$OWNED_TIP" ] || KNOWN_ORPHAN=true
  done <<< "$entries"
}

# With no saved directory, a preserved owned branch identifies a known orphan.
# A terminal name alone never grants permission to stop an unrelated session.
preflight_orphan() {
  [ ! -e "$WORKER_DIR" ] && [ ! -L "$WORKER_DIR" ] && [ ! -L "$HOME/work/.worktrees" ] || return 1
  check_registered_inventory "$WORKER_DIR" "$FLOATIE_BRANCH" || return 1
  [ "$KNOWN_ORPHAN" = true ]
}

if [ -d "$WORKER_DIR" ]; then
  preflight_worker "$WORKER_DIR" "$FLOATIE_BRANCH" || {
    echo "error: preservation preflight failed; repair or preserve the worker before removal." >&2
    exit 1
  }
else
  preflight_orphan || {
    echo "error: unknown worker or unsafe orphan '$WORKER_NAME'; session name alone is not ownership." >&2
    exit 1
  }
fi

if [ "${#WTS[@]}" -eq 0 ]; then
  echo "==> known orphan '$WORKER_NAME'; no saved worktrees, owned branches retained"
  run_workspace_hook
  if tmux has-session -t "=$WORKER_NAME" 2>/dev/null; then
    TERMINAL_PHASE="attempted"
    tmux kill-session -t "=$WORKER_NAME"
  fi
  TERMINAL_PHASE="completed" STACK_PHASE="delegated to hook" PARENT_PHASE="absent"
  [ ! -d "$WORKER_DIR" ] || PARENT_PHASE="retained"
  exit 0
fi

# ── Phase 1c: workspace teardown hook ─────────────────────────────────────────
# Here — after the safety gate, before anything is removed. That ordering also matters
# for Phase 2b below: a hook that removes the project leaves provenance nothing to
# refuse. When it does not — it failed and force=true carried us past it, or it tore
# down something else — the project is still up under this worker's name from
# ~/work/stack, and Phase 2b recognises that dir as ours and brings it down itself.
run_workspace_hook

# ── Phase 2: kill the tmux session once ───────────────────────────────────────
if tmux has-session -t "=$WORKER_NAME" 2>/dev/null; then
  echo "==> killing tmux session '$WORKER_NAME'"
  TERMINAL_PHASE="attempted"
  tmux kill-session -t "=$WORKER_NAME"
fi

TERMINAL_PHASE="completed"

# ── Phase 2b: bring the worker's compose stack down ──────────────────────────
# worker-init.sh exports COMPOSE_PROJECT_NAME=<name>, so a worker's stack is its
# own compose project. Removing the worktrees without stopping it strands the
# containers, which go on holding their published host ports indefinitely — the
# very collision issue #33 is about, arriving via a cleanup gap.
#
# Provenance first. `docker compose -p <name>` targets a project by NAME alone,
# and a stack started outside a worker takes its project name from its
# directory basename — so without this check `worker-rm api` would tear down a
# stack brought up in ~/work/api. Every container in the project must live under
# this worker's directory or in ~/work/stack; anything else and we refuse
# Compose/worktree cleanup. Earlier hook and terminal effects may already remain.
#
# Ordering and fatality are the same argument: containers can bind-mount the
# worktree, so teardown runs first, and if it fails we stop. Leaving the
# worktrees is recoverable by re-running; deleting them out from under live
# containers is not, and would leave those containers holding host ports with a
# vanished mount source — worse than doing nothing.
#
# Built-in teardown omits -v. The earlier workspace hook may delete named
# volumes; this step alone does not promise their survival.
if command -v docker >/dev/null 2>&1; then
  FOREIGN=""
  if ! COMPOSE_DIRS=$(docker ps -a --filter "label=com.docker.compose.project=$WORKER_NAME" \
      --format '{{.Label "com.docker.compose.project.working_dir"}}' 2>/dev/null); then
    echo "error: compose ownership probe failed; stack and worktrees retained." >&2
    exit 1
  fi
  while IFS= read -r WD; do
    [ -n "$WD" ] || continue
    case "$WD" in
      "$WORKER_DIR"|"$WORKER_DIR"/*) ;;
      # floatie places the workspace stack at ~/work/stack itself, and the project
      # name is the worker name floatie exported — so a project of THIS name running
      # from THAT dir is this worker's by construction, not a stranger's. Without
      # this, force=true dead-ends: the hook failed, so the containers are still up,
      # and floatie would refuse to stop them while advising the operator to go stop
      # a stack that is the worker's own.
      "$HOME/work/stack") ;;
      *) FOREIGN="${FOREIGN}${FOREIGN:+, }$WD" ;;
    esac
  done <<< "$COMPOSE_DIRS"

  if [ -n "$FOREIGN" ]; then
    echo "error: compose project '$WORKER_NAME' has containers outside $WORKER_DIR and not in $HOME/work/stack:" >&2
    echo "         $FOREIGN" >&2
    echo "       That project was not created by this worker, so it is not ours to stop." >&2
    echo "       Worktrees are retained; hook and terminal effects may already have occurred." >&2
    exit 1
  fi

  # Unconditional: `down` needs neither a compose file nor a running container,
  # and it also removes the project network. Gating it on a running container
  # would skip a stopped or crashed project and leak both.
  echo "==> bringing down compose project '$WORKER_NAME'"
  STACK_PHASE="attempted (effects possible)"
  docker compose -p "$WORKER_NAME" down --remove-orphans || {
    echo "error: 'docker compose -p $WORKER_NAME down' failed." >&2
    echo "       Refusing to remove the worktrees — containers may still bind-mount them." >&2
    echo "       Hook, terminal and stack effects may remain. Fix the stack and re-run." >&2
    exit 1
  }
  STACK_PHASE="completed"
else
  STACK_PHASE="skipped (docker unavailable)"
  echo "==> docker not available — skipping compose teardown"
fi

# ── Phase 3: recheck each worktree immediately before removal ────────────────
for ((I=0; I<${#WTS[@]}; I++)); do
  WT="${WTS[$I]}"
  REPO_NAME="${WT##*/}"
  REPO_PATH="$HOME/work/$REPO_NAME"
  if ! inspect_worktree "$WT" "$FLOATIE_BRANCH" ||
     [ "$HEAD_TIP" != "${HEAD_TIPS[$I]}" ] || [ "$HEAD_EVIDENCE" != "${HEAD_BACKUPS[$I]}" ] ||
     [ "$OWNED_TIP" != "${OWNED_TIPS[$I]}" ] || [ "$OWNED_EVIDENCE" != "${OWNED_BACKUPS[$I]}" ]; then
    echo "error: [$REPO_NAME] preservation evidence changed or unavailable; worktree and branch retained." >&2
    REMAINING="$REMAINING [$REPO_NAME] worktree and branch;"
    FAILED=1
    continue
  fi
  WT_BRANCH=$(git -C "$WT" symbolic-ref --quiet --short HEAD 2>/dev/null) || WT_BRANCH=""
  echo "==> [$REPO_NAME] removing worktree $WT"
  if ! git -C "$REPO_PATH" worktree remove "$WT"; then
    echo "error: [$REPO_NAME] worktree removal failed; path and branch retained (check worktree locks)." >&2
    REMAINING="$REMAINING [$REPO_NAME] worktree and branch;"
    FAILED=1
    continue
  fi
  COMPLETED="$COMPLETED [$REPO_NAME] worktree;"

  # The hook, another process, or removal itself may have changed branch evidence.
  # Only delete after successful removal and another matching preservation probe.
  if ! owned_branch_tip "$REPO_PATH" "$FLOATIE_BRANCH" ||
     [ "$OWNED_TIP" != "${OWNED_TIPS[$I]}" ] || [ "$OWNED_EVIDENCE" != "${OWNED_BACKUPS[$I]}" ]; then
    echo "error: [$REPO_NAME] branch evidence changed or unavailable; retained '$FLOATIE_BRANCH'." >&2
    REMAINING="$REMAINING [$REPO_NAME] owned branch;"
    FAILED=1
  elif [ -n "$OWNED_TIP" ]; then
    if git -C "$REPO_PATH" branch -D "$FLOATIE_BRANCH" >/dev/null 2>&1; then
      echo "==> [$REPO_NAME] branch '$FLOATIE_BRANCH' deleted"
      COMPLETED="$COMPLETED [$REPO_NAME] owned branch;"
    else
      echo "error: [$REPO_NAME] could not delete '$FLOATIE_BRANCH'; retained (checked out elsewhere?)." >&2
      REMAINING="$REMAINING [$REPO_NAME] owned branch;"
      FAILED=1
    fi
  fi
  if [ -n "$WT_BRANCH" ] && [ "$WT_BRANCH" != "$FLOATIE_BRANCH" ]; then
    echo "==> [$REPO_NAME] worktree was on '$WT_BRANCH' — not floatie's branch, left in place"
  fi
done

# Never hide partial failure or delete unexpected parent files with recursive removal.
if rmdir "$WORKER_DIR" 2>/dev/null; then
  PARENT_PHASE="completed"
else
  PARENT_PHASE="retained"
  REMAINING="$REMAINING parent directory (not empty or unavailable);"
  FAILED=1
fi
[ "$FAILED" -eq 0 ] || exit 1
echo "==> worker '$WORKER_NAME' removed"
