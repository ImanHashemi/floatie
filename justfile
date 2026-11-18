# floatie justfile — workspace VM lifecycle.
# Reads name/cpus/memory/repos/egress/permissive from workspace.yml (no extra dependencies — grep/awk only).
# Accept LF and CRLF when reading; never rewrite workspace.yml line endings.

# Read the workspace name from workspace.yml.
name := `grep -m1 '^name:' workspace.yml 2>/dev/null | awk '{sub(/\r$/, ""); print $2}'`

# Read git_user_name from workspace.yml (everything after "git_user_name: ").
git_user_name := `grep -m1 '^git_user_name:' workspace.yml 2>/dev/null | sed 's/^git_user_name:[[:space:]]*//' | tr -d '\r'`

# Read git_user_email from workspace.yml (everything after "git_user_email: ").
git_user_email := `grep -m1 '^git_user_email:' workspace.yml 2>/dev/null | sed 's/^git_user_email:[[:space:]]*//' | tr -d '\r'`

# Read cpus from workspace.yml; default 4 if absent.
cpus := `grep -m1 '^cpus:' workspace.yml 2>/dev/null | awk '{sub(/\r$/, ""); print $2}' | grep -E '^[0-9]+$' || echo 4`

# Read memory from workspace.yml; strip trailing GiB suffix; default 4 if absent.
# Accepts integers and decimals (e.g. 1.5 for 1.5GiB).
memory := `grep -m1 '^memory:' workspace.yml 2>/dev/null | awk '{sub(/\r$/, ""); print $2}' | sed 's/[Gg][Ii][Bb]$//' | grep -E '^[0-9]+(\.[0-9]+)?$' || echo 4`

# Parse repos: list from workspace.yml.
# Extracts lines between "repos:" and the next top-level key (or EOF).
# Supports a block list of plain (unquoted) scalars only — no inline [a, b] lists, no quoted items.
# Each "- <value>" item is normalized: bare "github.com/owner/repo" → full https URL.
# Result is a newline-separated list of unquoted URLs/paths; entries must not contain whitespace.
# The create recipe converts lines to positional args via a bash 3.2-compatible while-read loop.
repos := ```
awk '
  { sub(/\r$/, "") }
  /^repos:/ { in_repos=1; next }
  in_repos && /^[^ \t]/ { in_repos=0 }
  in_repos && /^[ \t]*-[ \t]+/ {
    sub(/^[ \t]*-[ \t]+/, "")
    # Strip trailing inline comment (whitespace + # ...) so entries like
    # "github.com/org/repo  # comment" are handled correctly.
    sub(/[ \t]+#.*$/, "")
    # Strip any remaining trailing whitespace.
    sub(/[ \t]+$/, "")
    # Normalize bare github.com/owner/repo shorthand to full https URL.
    if ($0 ~ /^github\.com\//) {
      sub(/\.git$/, "")
      print "https://" $0 ".git"
    } else {
      print $0
    }
  }
' workspace.yml 2>/dev/null || true
```

# Parse egress: list from workspace.yml — extra domains to allowlist beyond the base list.
# Same block-list parsing style as repos: — plain unquoted scalars, one per line.
# Trailing inline comments (# ...) are stripped. Result is a newline-separated domain list.
egress := ```
awk '
  { sub(/\r$/, "") }
  /^egress:/ { in_egress=1; next }
  in_egress && /^[^ \t]/ { in_egress=0 }
  in_egress && /^[ \t]*-[ \t]+/ {
    sub(/^[ \t]*-[ \t]+/, "")
    sub(/[ \t]+#.*$/, "")
    sub(/[ \t]+$/, "")
    if (length($0) > 0) print $0
  }
' workspace.yml 2>/dev/null || true
```

# Parse plugins: list from workspace.yml — Claude Code plugins/marketplaces to install.
# Same block-list parsing style as repos: / egress: — plain unquoted scalars, one per line.
# Trailing inline comments (# ...) are stripped. Result is a newline-separated list.
# Two entry kinds:
#   owner/repo  (no @) → claude plugin marketplace add <entry>
#   plugin@marketplace → claude plugin install <entry>
plugins := ```
awk '
  { sub(/\r$/, "") }
  /^plugins:/ { in_plugins=1; next }
  in_plugins && /^[^ \t]/ { in_plugins=0 }
  in_plugins && /^[ \t]*-[ \t]+/ {
    sub(/^[ \t]*-[ \t]+/, "")
    sub(/[ \t]+#.*$/, "")
    sub(/[ \t]+$/, "")
    if (length($0) > 0) print $0
  }
' workspace.yml 2>/dev/null || true
```

# Read permissive flag from workspace.yml; true = disable firewall (allow-all).
# Matches "permissive: true" (case-insensitive value) on its own top-level line.
permissive := `grep -m1 '^permissive:' workspace.yml 2>/dev/null | awk '{sub(/\r$/, ""); print tolower($2)}' | grep -x 'true' || echo false`

# Absolute path to this repo's directory (works regardless of invocation cwd).
repo := justfile_directory()

# Where Lima keeps VM instance dirs (disk images, config). Lima itself honours LIMA_HOME
# to relocate this; the disk preflight must check the same directory Lima will actually use.
lima_dir := env_var_or_default("LIMA_HOME", home_directory() / ".lima")

# Keep the named default callable; bare host invocation shares product help.
[private]
default: help

# Product quick reference: daily commands, setup, maintenance and automation.
help:
    @cat {{ quote(justfile_directory()) }}/help.txt

# Symlink bin/floatie onto your PATH so you can run `floatie <recipe>` from any
# workspace directory. Idempotent. Override the target dir with FLOATIE_BIN_DIR.
# Updating floatie later = `git pull` in this repo; the symlink keeps working
# (it points at bin/floatie, which self-locates) — no need to re-run install.
# Symlink bin/floatie onto your PATH (run once, in the floatie repo).
install:
    #!/usr/bin/env bash
    set -euo pipefail
    REPO={{ quote(repo) }}
    WRAPPER="$REPO/bin/floatie"
    [ -f "$WRAPPER" ] || { echo "error: $WRAPPER not found" >&2; exit 1; }
    chmod +x "$WRAPPER"
    TARGET_DIR="${FLOATIE_BIN_DIR:-$HOME/.local/bin}"
    mkdir -p "$TARGET_DIR"
    ln -sf "$WRAPPER" "$TARGET_DIR/floatie"
    echo "==> linked $TARGET_DIR/floatie -> $WRAPPER"
    case ":$PATH:" in
        *":$TARGET_DIR:"*)
            echo "==> $TARGET_DIR is on PATH — run 'floatie <recipe>' from any workspace dir" ;;
        *)
            echo "WARN: $TARGET_DIR is not on your PATH. Add it, e.g.:"
            echo "      echo 'export PATH=\"$TARGET_DIR:\$PATH\"' >> ~/.zshrc && source ~/.zshrc" ;;
    esac

# Boot the Lima VM, run provision.sh, then clone repos listed in workspace.yml.
# Tolerates "already exists" and "already running" — safe to re-run.
# Create/start and reconcile provisioning, Git identity, secrets, plugins, missing clones, context and firewall.
create:
    #!/usr/bin/env bash
    set -euo pipefail
    NAME={{ quote(name) }}
    CPUS={{ quote(cpus) }}
    MEMORY={{ quote(memory) }}
    REPO={{ quote(repo) }}
    WSDIR="$PWD"   # workspace dir (just -d). Tool assets come from $REPO; config/context from $WSDIR.
    [ -n "$NAME" ] || { echo "error: set 'name:' in workspace.yml" >&2; exit 1; }
    bash "$REPO/disk-preflight.sh" {{ quote(lima_dir) }}/"$NAME"
    echo "==> floatie create: VM name = $NAME, cpus = $CPUS, memory = ${MEMORY} GiB"

    # Capture status once to avoid two limactl list calls.
    STATUS=$(limactl list "$NAME" --format '{{{{.Status}}' 2>/dev/null || true)

    # --tty=false is required in scripts; without it limactl opens an interactive editor.
    if [ "$STATUS" = "Running" ] || [ "$STATUS" = "Stopped" ]; then
        echo "==> VM '$NAME' already exists (status: $STATUS)"
        if [ "$STATUS" = "Stopped" ]; then
            echo "==> starting stopped VM"
            limactl start "$NAME" --tty=false
        fi
    elif [ -n "$STATUS" ]; then
        echo "error: VM '$NAME' is in state '$STATUS'; run 'floatie destroy' first" >&2
        exit 1
    else
        echo "==> creating VM from lima.yaml (cpus=$CPUS memory=${MEMORY}GiB)"
        # --cpus and --memory (in GiB) override the lima.yaml template values.
        limactl start --name="$NAME" --cpus="$CPUS" --memory="$MEMORY" "$REPO/lima.yaml" --tty=false
    fi

    # ── Write git identity file into guest ───────────────────────────────────
    # provision.sh reads ~/.floatie-git-identity (written here from workspace.yml)
    # to set git user.name/user.email and build the allowed-signers file.
    # Using an in-VM file avoids passing identity via argv (safe) and works
    # regardless of whether provision runs headless or interactively.
    # Only written when at least one field is non-empty — avoids the false
    # impression that identity was configured when both fields are blank.
    GIT_USER_NAME={{ quote(git_user_name) }}
    GIT_USER_EMAIL={{ quote(git_user_email) }}
    # Values missing from workspace.yml fall back to this Mac's git config, so a
    # shared workspace repo does not have to hold one person's identity.
    GIT_FROM_HOST=""
    if [ -z "$GIT_USER_NAME" ]; then
      GIT_USER_NAME=$(git config --get user.name 2>/dev/null || true)
      [ -n "$GIT_USER_NAME" ] && GIT_FROM_HOST=1
    fi
    if [ -z "$GIT_USER_EMAIL" ]; then
      GIT_USER_EMAIL=$(git config --get user.email 2>/dev/null || true)
      [ -n "$GIT_USER_EMAIL" ] && GIT_FROM_HOST=1
    fi
    if [ -n "$GIT_FROM_HOST" ]; then
      echo "==> git identity: from this Mac's git config ($GIT_USER_NAME <$GIT_USER_EMAIL>)"
    fi
    if [ -n "$GIT_USER_NAME" ] || [ -n "$GIT_USER_EMAIL" ]; then
      printf 'GIT_USER_NAME=%s\nGIT_USER_EMAIL=%s\n' "$GIT_USER_NAME" "$GIT_USER_EMAIL" \
        | limactl shell "$NAME" -- bash -c 'cat > ~/.floatie-git-identity; chmod 600 ~/.floatie-git-identity'
      echo "==> git identity file written to guest (~/.floatie-git-identity)"
    else
      echo "==> git identity: both git_user_name and git_user_email are blank — skipping ~/.floatie-git-identity (provision will WARN)"
    fi

    echo "==> running provision.sh in guest"
    # Pipe provision.sh over stdin so it works regardless of where the repo is cloned
    # (the host ~ mount is read-only and only covers paths under $HOME).
    limactl shell "$NAME" -- bash -s < "$REPO/provision.sh"

    # ── Copy .env.secrets into guest ──────────────────────────────────────────
    # .env.secrets holds GITHUB_TOKEN (private-repo clone/push) + optional MCP keys.
    # Claude Code auth is NOT in .env.secrets — log in interactively after create:
    #   floatie attach → claude → /login  (browser opens on the host via Lima forwarding;
    #   the session caches in ~/.claude in the VM and persists with the VM).
    # The host repo is mounted read-only in the guest, so we can't just chmod it.
    # Instead, copy to ~/.env.secrets (VM disk) with mode 0600 so agents can source it.
    # If the file is absent, warn clearly but do NOT fail — the VM is usable without it.
    if [ -f "$WSDIR/.env.secrets" ]; then
        # Warn if GITHUB_TOKEN is absent, empty, or still holds the example placeholder.
        # We grep for the key but never echo the value — keeps the token out of logs.
        GH_TOKEN_LINE=$(grep -m1 '^GITHUB_TOKEN=' "$WSDIR/.env.secrets" || true)
        GH_TOKEN_VAL="${GH_TOKEN_LINE#GITHUB_TOKEN=}"
        GH_TOKEN_VAL="${GH_TOKEN_VAL%$'\r'}"
        if [ -z "$GH_TOKEN_VAL" ] || [ "$GH_TOKEN_VAL" = "github_pat_DUMMY" ]; then
            echo "WARN: GITHUB_TOKEN is empty or still set to the example placeholder in .env.secrets — git push and private-repo clones won't work until it is set."
            echo "WARN: Mint a fine-grained PAT at https://github.com/settings/personal-access-tokens/new and re-run 'floatie create'."
        fi
        echo "==> copying .env.secrets to guest ~/.env.secrets (0600)"
        # Normalize before copy-in:
        #   tr  — strip CR so CRLF host-edited files don't corrupt the value.
        #   sed — strip whitespace right after `=` on KEY= lines; a stray space
        #         (`KEY= value`) makes the sourced file try to RUN the value as a
        #         command (cryptic exit 127). Values themselves are never printed.
        # umask 077 ensures the file is created 0600 with no world-readable window;
        # the trailing chmod 600 is belt-and-suspenders in case of unusual guest umask.
        tr -d '\r' < "$WSDIR/.env.secrets" \
          | sed -E 's/^([A-Za-z_][A-Za-z0-9_]*=)[[:space:]]+/\1/' \
          | limactl shell "$NAME" -- bash -c 'umask 077; cat > ~/.env.secrets; chmod 600 ~/.env.secrets'
        echo "==> .env.secrets in place"
    else
        echo "WARN: .env.secrets not found in workspace root — GITHUB_TOKEN and MCP keys will be unset."
        echo "WARN: Create it from .env.secrets.example and re-run 'floatie create'."
        echo "WARN: Claude Code auth is separate — run 'floatie attach' then 'claude' then '/login'."
    fi

    # ── Install Claude plugins ─────────────────────────────────────────────────
    # Plugins use the cached ~/.claude login (from claude → /login), not a token.
    # If not yet logged in, installs are skipped with a WARN — run `floatie reload`
    # after completing /login to install them.
    just --justfile "$REPO/justfile" -d "$PWD" --set name {{ quote(name) }} --set permissive {{ quote(permissive) }} --set egress {{ quote(egress) }} --set plugins {{ quote(plugins) }} _install-plugins

    # ── Clone repos ────────────────────────────────────────────────────────────
    # Build the list of repos to clone (may be empty if repos: is absent/empty).
    REPOS={{ quote(repos) }}
    if [ -n "$REPOS" ]; then
        echo "==> cloning repos into VM disk (~/work/)"
        # Convert the newline-separated list into an array (bash 3.2-compatible;
        # mapfile is bash 4+ only, unavailable on macOS /bin/bash).
        REPO_ARGS=()
        while IFS= read -r repo; do
            if [ -n "$repo" ]; then REPO_ARGS+=("$repo"); fi
        done <<< "$REPOS"
        limactl shell "$NAME" -- bash -s -- "${REPO_ARGS[@]}" < "$REPO/clone-repos.sh"
    else
        echo "==> no repos configured — skipping clone step"
    fi

    # ── Place workspace context (CLAUDE.md + user-scope MCPs) ───────────────────
    just --justfile "$REPO/justfile" -d "$PWD" --set name {{ quote(name) }} --set permissive {{ quote(permissive) }} --set egress {{ quote(egress) }} --set plugins {{ quote(plugins) }} _place-context

    # ── Apply egress firewall ──────────────────────────────────────────────────
    # Applied AFTER clone so that repo cloning (github.com is already allowlisted)
    # is unaffected; firewall then locks down all subsequent egress.
    just --justfile "$REPO/justfile" -d "$PWD" --set name {{ quote(name) }} --set permissive {{ quote(permissive) }} --set egress {{ quote(egress) }} --set plugins {{ quote(plugins) }} firewall

    echo "==> floatie create: done — VM '$NAME' is up and provisioned"

# Attach to a tmux session inside the VM.
# Usage:
#   floatie attach           — attach to the main session (slice-1 behaviour, unchanged)
#   floatie attach <worker>  — attach to a named worker session
# Use Ctrl-b d to detach without destroying the session.
# Enter the VM at ~/work in tmux (or reconnect to a named worker session).
[positional-arguments]
attach worker="":
    #!/usr/bin/env bash
    set -euo pipefail
    NAME={{ quote(name) }}
    WORKER="$1"
    REPO={{ quote(repo) }}
    [ -n "$NAME" ] || { echo "error: set 'name:' in workspace.yml" >&2; exit 1; }
    bash "$REPO/disk-preflight.sh" --warn-only {{ quote(lima_dir) }}/"$NAME"
    STATUS=$(limactl list "$NAME" --format '{{{{.Status}}' 2>/dev/null || true)
    if [ "$STATUS" != "Running" ]; then
        echo "error: VM '$NAME' is not running (status: ${STATUS:-absent})." >&2
        echo "       Run 'floatie create' (new) or 'floatie start' (stopped) first." >&2
        exit 1
    fi
    if [ -z "$WORKER" ]; then
        # Default: attach (or create) the main session, rooted at ~/work so the
        # env-map ~/work/CLAUDE.md loads top-down for the orchestrator.
        limactl shell "$NAME" -- bash -lc 'mkdir -p "$HOME/work"; if tmux has-session -t "=main" 2>/dev/null; then exec tmux attach-session -t "=main"; else exec tmux new-session -s main -c "$HOME/work"; fi'
    else
        # Worker mode: attach only — don't create; worker must already exist.
        if ! limactl shell "$NAME" -- tmux has-session -t "=$WORKER" 2>/dev/null; then
            echo "error: no tmux session '$WORKER' in VM '$NAME'" >&2
            printf '       Run floatie worker %q first, or floatie workers to list saved workers and live sessions.\n' "$WORKER" >&2
            exit 1
        fi
        limactl shell "$NAME" -- tmux attach-session -t "=$WORKER"
    fi

# Place optional workspace AGENTS.md and CLAUDE.md → ~/work/, and
# .floatie-mcp.json's mcpServers → user scope (~/.claude.json). Private: called by
# `create` and `reload`. Missing inputs leave existing guidance untouched.
_place-context:
    #!/usr/bin/env bash
    set -euo pipefail
    NAME={{ quote(name) }}
    REPO={{ quote(repo) }}
    WSDIR="$PWD"
    [ -n "$NAME" ] || { echo "error: set 'name:' in workspace.yml" >&2; exit 1; }

    if limactl shell "$NAME" -- bash -lc 'command -v codex >/dev/null'; then
        just --justfile "$REPO/justfile" -d "$WSDIR" --set name {{ quote(name) }} _place-codex
    fi

    for source in AGENTS.md CLAUDE.md; do
        if [ -f "$WSDIR/$source" ]; then
            echo "==> placing $source → ~/work/$source"
            digest=$(python3 -c 'import hashlib, pathlib, sys; print(hashlib.sha256(pathlib.Path(sys.argv[1]).read_bytes()).hexdigest())' "$WSDIR/$source")
            limactl shell "$NAME" -- bash -c '
                set -eu
                destination="$HOME/work/$1"
                mkdir -p "$(dirname "$destination")"
                temporary=$(mktemp "${destination}.XXXXXX")
                trap '\''rm -f "$temporary"'\'' EXIT
                cat > "$temporary"
                actual=$(python3 -c "import hashlib, pathlib, sys; print(hashlib.sha256(pathlib.Path(sys.argv[1]).read_bytes()).hexdigest())" "$temporary")
                [ "$actual" = "$2" ] || { echo "error: context transfer digest mismatch for $1" >&2; exit 1; }
                mv "$temporary" "$destination"
            ' -- "$source" "$digest" < "$WSDIR/$source"
        else
            echo "==> no $source in workspace — skipping context placement"
        fi
    done

    if [ -f "$WSDIR/.floatie-mcp.json" ]; then
        echo "==> applying .floatie-mcp.json at user scope in the VM"
        # Copy the file in first (CRLF-stripped), then run apply-mcp.sh (stdin = script).
        tr -d '\r' < "$WSDIR/.floatie-mcp.json" | limactl shell "$NAME" -- bash -c 'cat > /tmp/floatie-mcp.json'
        limactl shell "$NAME" -- bash -s -- /tmp/floatie-mcp.json < "$REPO/apply-mcp.sh"
        limactl shell "$NAME" -- rm -f /tmp/floatie-mcp.json
    else
        echo "==> no .floatie-mcp.json in workspace — skipping MCP apply"
    fi

    if [ -d "$WSDIR/.claude/skills" ]; then
        # Enumerate shipped skill dirs (top-level only) via a glob loop, NOT `ls */` —
        # a dir named like `-x` would be parsed by ls as an option and the skill
        # silently dropped. (bash 3.2 array; safe under set -u while guarded by count.)
        SKILL_NAMES=()
        for _d in "$WSDIR"/.claude/skills/*/; do
            [ -d "$_d" ] || continue
            SKILL_NAMES+=("$(basename "$_d")")
        done
        if [ "${#SKILL_NAMES[@]}" -gt 0 ]; then
            echo "==> placing workspace skills → ~/.claude/skills/ (user scope): ${SKILL_NAMES[*]}"
            # Clean-replace ONLY the shipped skills (leave plugin/other skills intact),
            # then copy the tree in. --dereference materializes symlinked skill dirs as
            # real content; COPYFILE_DISABLE + --no-xattrs/--no-mac-metadata keep macOS
            # AppleDouble (._*) files and xattrs out of the guest. (rm runs before extract:
            # a mid-stream tar failure leaves a skill briefly absent — re-run the
            # idempotent `floatie reload` to restore.)
            printf '%s\n' "${SKILL_NAMES[@]}" | limactl shell "$NAME" -- bash -c '
                mkdir -p ~/.claude/skills
                while IFS= read -r s; do [ -n "$s" ] && rm -rf ~/.claude/skills/"$s"; done'
            COPYFILE_DISABLE=1 tar --no-xattrs --no-mac-metadata --dereference \
                -C "$WSDIR/.claude/skills" -cf - . \
              | limactl shell "$NAME" -- bash -c 'tar -C ~/.claude/skills -xf -'
        else
            echo "==> .claude/skills has no skill dirs — nothing to place"
        fi
    else
        echo "==> no .claude/skills in workspace — skipping skills placement"
    fi

    just --justfile "$REPO/justfile" -d "$WSDIR" --set name {{ quote(name) }} _place-agent-tools

    # Place the workspace's local stack (stack/ → ~/work/stack), if present, so a compose stack
    # can run in the VM beside the cloned repos. Exclude host-only/runtime files. If the stack
    # ships a VM env file (stack/.env.vm), promote it to the active .env in the VM — a workspace
    # uses that to point its compose at the ~/work layout. Drop a .floatie-vm sentinel so the stack
    # can gate VM-only behaviour off it. floatie stays stack-agnostic: any workspace-specific
    # settings (env var names, compose filenames) live in the workspace's stack/.env.vm, not here.
    if [ -d "$WSDIR/stack" ]; then
        echo "==> placing stack/ → ~/work/stack"
        limactl shell "$NAME" -- bash -c 'mkdir -p "$HOME/work/stack"'
        COPYFILE_DISABLE=1 tar --no-xattrs --no-mac-metadata --dereference \
            --exclude='./.env' --exclude='./.workers' --exclude='./.pytest_cache' \
            --exclude='./tests/.venv' --exclude='./tests/__pycache__' \
            -C "$WSDIR/stack" -cf - . \
          | limactl shell "$NAME" -- bash -c 'tar -C "$HOME/work/stack" -xf -'
        limactl shell "$NAME" -- bash -c 'if [ -f "$HOME/work/stack/.env.vm" ]; then cp "$HOME/work/stack/.env.vm" "$HOME/work/stack/.env"; fi; touch "$HOME/work/stack/.floatie-vm"'
    else
        echo "==> no stack/ in workspace — skipping stack placement"
    fi

# Install the small shared agent interface, independently of workspace configuration.
_place-agent-tools:
    #!/usr/bin/env bash
    set -euo pipefail
    NAME={{ quote(name) }}
    REPO={{ quote(repo) }}
    [ -n "$NAME" ] || { echo "error: set 'name:' in workspace.yml" >&2; exit 1; }
    for source in agent_tools.py worker_inventory.py agents.just worker-init.sh worker-rm.sh; do
        case "$source" in
            worker-init.sh) destination=.local/bin/floatie-worker-init ;;
            worker-rm.sh) destination=.local/bin/floatie-worker-rm ;;
            *) destination=.local/share/floatie/"$source" ;;
        esac
        limactl shell "$NAME" -- bash -c '
            set -eu
            destination="$HOME/$1"
            mkdir -p "$(dirname "$destination")"
            temporary=$(mktemp "${destination}.XXXXXX")
            trap '\''rm -f "$temporary"'\'' EXIT
            cat > "$temporary"
            chmod 0755 "$temporary"
            mv "$temporary" "$destination"
        ' -- "$destination" < "$REPO/$source"
    done
    # Shared skill source: native Codex discovery at the work root, Claude at user scope.
    # Apply after legacy .claude/skills placement so this source wins for matching names.
    if [ -d "$PWD/.agents/skills" ]; then
        TAR_FLAGS=(--no-xattrs --dereference)
        if [ "$(uname -s)" = Darwin ]; then TAR_FLAGS+=(--no-mac-metadata); fi
        for destination in work/.agents/skills .claude/skills; do
            COPYFILE_DISABLE=1 tar "${TAR_FLAGS[@]}" \
                -C "$PWD/.agents/skills" -cf - . \
              | limactl shell "$NAME" -- bash -c 'set -eu; mkdir -p "$HOME/$1"; tar -C "$HOME/$1" -xf -' -- "$destination"
        done
    fi

# Forward literal argv/stdin to the installed VM helper; no host-side agent runtime.
[positional-arguments]
_agent action *args:
    #!/usr/bin/env bash
    set -euo pipefail
    NAME={{ quote(name) }}
    [ -n "$NAME" ] || { echo "error: set 'name:' in workspace.yml" >&2; exit 1; }
    exec limactl shell "$NAME" -- bash -lc 'exec python3 "$HOME/.local/share/floatie/agent_tools.py" "$@"' -- "$@"

# Start/reconnect to the root agent inside the VM, then attach to its tmux pane.
[positional-arguments]
orchestrate harness="codex":
    #!/usr/bin/env bash
    set -euo pipefail
    result=$(just --justfile {{ quote(repo) }}/justfile -d "$PWD" --set name {{ quote(name) }} _agent start main "$1")
    pane=$(printf '%s' "$result" | python3 -c 'import json,sys; print(json.load(sys.stdin)["pane"])')
    limactl shell {{ quote(name) }} -- tmux select-window -t "$pane"
    limactl shell {{ quote(name) }} -- tmux select-pane -t "$pane"
    exec limactl shell {{ quote(name) }} -- tmux attach-session -t '=main'

# Launch the primary agent in an existing worker, or report the one already running.
[positional-arguments]
agent-start target harness="codex":
    @just --justfile {{ quote(repo) }}/justfile -d "$PWD" --set name {{ quote(name) }} _agent start "$@"

# Discover agents, including manually launched Codex/Claude sessions.
agent-list:
    @just --justfile {{ quote(repo) }}/justfile -d "$PWD" --set name {{ quote(name) }} _agent list

# Read recent terminal output; optional --pane selects an ambiguous worker explicitly.
[positional-arguments]
agent-read target *options:
    @just --justfile {{ quote(repo) }}/justfile -d "$PWD" --set name {{ quote(name) }} _agent read "$@"

# Submit a literal prompt from stdin to the selected agent.
[positional-arguments]
agent-send target *options:
    @just --justfile {{ quote(repo) }}/justfile -d "$PWD" --set name {{ quote(name) }} _agent send "$@"

# Observe output/liveness changes; --pane %ID, --target, --cursor, --seconds (not task completion).
[positional-arguments]
agent-wait target *options:
    @just --justfile {{ quote(repo) }}/justfile -d "$PWD" --set name {{ quote(name) }} _agent wait "$@"

# Install Claude Code plugins listed in workspace.yml into the VM.
# Private: called by `create` and `reload`.
# Plugins use the cached ~/.claude login (floatie attach → claude → /login).
# If not yet logged in, each entry fails gracefully (WARN + skip, never hard-fail).
# Run `floatie reload` after /login to retry.
_install-plugins:
    #!/usr/bin/env bash
    set -euo pipefail
    NAME={{ quote(name) }}
    REPO={{ quote(repo) }}
    PLUGINS={{ quote(plugins) }}
    [ -n "$NAME" ] || { echo "error: set 'name:' in workspace.yml" >&2; exit 1; }
    if [ -z "$PLUGINS" ]; then
        echo "==> no plugins configured — skipping plugin install step"
        exit 0
    fi
    echo "==> installing Claude plugins into VM"
    PLUGIN_ARGS=()
    while IFS= read -r plugin; do
        if [ -n "$plugin" ]; then PLUGIN_ARGS+=("$plugin"); fi
    done <<< "$PLUGINS"
    limactl shell "$NAME" -- bash -s -- "${PLUGIN_ARGS[@]}" < "$REPO/install-plugins.sh"

# provision.sh shows the registration banner only on the run that GENERATES the
# key (issue #30), so this is how to re-read it afterwards: banner missed in a
# long `create` log, key removed on the GitHub side, or a restored VM disk.
# Read-only — prints the PUBLIC half; the private key never leaves the VM.
# Print the VM's SSH signing public key, to register at github.com/settings/ssh/new.
signing-key:
    #!/usr/bin/env bash
    set -euo pipefail
    NAME={{ quote(name) }}
    [ -n "$NAME" ] || { echo "error: set 'name:' in workspace.yml" >&2; exit 1; }

    # $HOME is single-quoted so it expands in the GUEST, not on the host.
    if ! limactl shell "$NAME" -- bash -c 'test -f "$HOME/.ssh/git_signing.pub"'; then
        echo "error: no signing public key in VM '$NAME' at ~/.ssh/git_signing.pub" >&2
        echo "       run 'floatie create' to provision it (generated on first create)." >&2
        exit 1
    fi
    echo "==> signing public key for VM '$NAME'"
    echo "==> register at https://github.com/settings/ssh/new as a SIGNING key (not Authentication)"
    echo ""
    limactl shell "$NAME" -- bash -c 'cat "$HOME/.ssh/git_signing.pub"'

# Applies context/MCPs (Codex if installed), stack, guest tools and shared skills;
# retries configured Claude plugins. No secrets, Git identity, clones or firewall.
# Apply workspace files and retry plugins in a running VM; no re-provisioning.
reload:
    #!/usr/bin/env bash
    set -euo pipefail
    NAME={{ quote(name) }}
    REPO={{ quote(repo) }}
    [ -n "$NAME" ] || { echo "error: set 'name:' in workspace.yml" >&2; exit 1; }
    bash "$REPO/disk-preflight.sh" {{ quote(lima_dir) }}/"$NAME"
    STATUS=$(limactl list "$NAME" --format '{{{{.Status}}' 2>/dev/null || true)
    if [ "$STATUS" != "Running" ]; then
        echo "error: VM '$NAME' is not running (status: ${STATUS:-absent})." >&2
        echo "       Run 'floatie create' (new) or 'floatie start' (stopped) first." >&2
        exit 1
    fi
    echo "==> floatie reload: re-placing context for VM '$NAME'"
    just --justfile "$REPO/justfile" -d "$PWD" --set name {{ quote(name) }} --set plugins {{ quote(plugins) }} _place-context
    just --justfile "$REPO/justfile" -d "$PWD" --set name {{ quote(name) }} --set plugins {{ quote(plugins) }} _install-plugins
    echo "==> floatie reload: done"

# The optional local web pane for THIS workspace: status + config + safe lifecycle actions, 127.0.0.1 only.
# Usage: floatie ui [port] (detached) · ui-logs (watch) · ui-stop (stop) · ui-fg (foreground).
# Launch the workspace web UI DETACHED — terminal stays free; prints the tokenized URL to open.
[positional-arguments]
ui port="8787":
    #!/usr/bin/env bash
    set -euo pipefail
    NAME={{ quote(name) }}
    REPO={{ quote(repo) }}
    PORT="$1"
    [ -n "$NAME" ] || { echo "error: set 'name:' in workspace.yml" >&2; exit 1; }
    command -v python3 >/dev/null 2>&1 || { echo "error: python3 not found on PATH" >&2; exit 1; }
    exec python3 "$REPO/ui_process.py" start --repo-dir "$REPO" --workspace "$PWD" --vm-name "$NAME" --port "$PORT"

# Stop the detached floatie UI for THIS workspace (idempotent).
ui-stop:
    #!/usr/bin/env bash
    set -euo pipefail
    NAME={{ quote(name) }}
    [ -n "$NAME" ] || { echo "error: set 'name:' in workspace.yml" >&2; exit 1; }
    exec python3 {{ quote(repo) }}/ui_process.py stop --workspace "$PWD" --vm-name "$NAME"

# Tail the detached floatie UI log (Ctrl-C stops watching; the UI keeps running).
ui-logs:
    #!/usr/bin/env bash
    set -euo pipefail
    NAME={{ quote(name) }}
    [ -n "$NAME" ] || { echo "error: set 'name:' in workspace.yml" >&2; exit 1; }
    LOGF="$(python3 {{ quote(repo) }}/ui_process.py log-path --workspace "$PWD" --vm-name "$NAME")"
    [ -f "$LOGF" ] || { echo "no UI log at $LOGF — is it running? ('floatie ui')"; exit 0; }
    exec tail -f "$LOGF"

# Run the floatie UI in the FOREGROUND (blocks; Ctrl-C to stop). For debugging.
[positional-arguments]
ui-fg port="8787":
    #!/usr/bin/env bash
    set -euo pipefail
    NAME={{ quote(name) }}
    REPO={{ quote(repo) }}
    [ -n "$NAME" ] || { echo "error: set 'name:' in workspace.yml" >&2; exit 1; }
    command -v python3 >/dev/null 2>&1 || { echo "error: python3 not found on PATH" >&2; exit 1; }
    FLOATIE_VM="$NAME" exec python3 "$REPO/ui_server.py" --port "$1" --workspace "$PWD"

# Create a worker (worktree(s) + tmux session) WITHOUT attaching. Private: used by
# `worker` (which then attaches) and by `floatie ui` (which can't attach — no TTY).
[positional-arguments]
_worker-create worker_name *repo_names:
    #!/usr/bin/env bash
    set -euo pipefail
    VM={{ quote(name) }}
    REPO={{ quote(repo) }}
    WORKER="$1"
    shift
    [ -n "$VM" ]     || { echo "error: set 'name:' in workspace.yml" >&2; exit 1; }
    [ -n "$WORKER" ] || { echo "error: worker name is required" >&2; exit 1; }

    if ! [[ "$WORKER" =~ ^[a-z0-9][a-z0-9_-]{0,62}$ ]]; then
        echo "error: invalid worker name '$WORKER'" >&2
        echo "       lowercase letter/digit start; [a-z0-9_-]; max 63 chars." >&2
        exit 1
    fi
    case "$WORKER" in
        stack|main|workspace|all) echo "error: '$WORKER' is a reserved worker name" >&2; exit 1 ;;
    esac
    for repo_name in "$@"; do
        if ! [[ "$repo_name" =~ ^[A-Za-z0-9._-]+$ ]] || [ "$repo_name" = . ] || [ "$repo_name" = .. ]; then
            echo "error: invalid repo name '$repo_name'" >&2; exit 1
        fi
    done

    bash "$REPO/disk-preflight.sh" {{ quote(lima_dir) }}/"$VM"
    # The guest resolves omitted scope from saved work, or actual clones for new work.
    # Explicit arguments are the caller's frozen set and must never be expanded here.
    limactl shell "$VM" -- bash -s -- "$WORKER" "$@" < "$REPO/worker-init.sh"
    printf '==> on host: cd %q && floatie attach %q\n' "$PWD" "$WORKER"

# Create a worker from all clones (or one); reconnect using its exact saved scope.
[positional-arguments]
worker worker_name repo_name="":
    #!/usr/bin/env bash
    set -euo pipefail
    VM={{ quote(name) }}
    REPO={{ quote(repo) }}
    WORKER="$1"
    REPO_ARG="$2"
    if [ -n "$REPO_ARG" ]; then
        just --justfile "$REPO/justfile" -d "$PWD" --set name {{ quote(name) }} _worker-create "$WORKER" "$REPO_ARG"
    else
        just --justfile "$REPO/justfile" -d "$PWD" --set name {{ quote(name) }} _worker-create "$WORKER"
    fi
    echo "==> attaching to worker session '$WORKER' (Ctrl-b d to detach)"
    limactl shell "$VM" -- tmux attach-session -t "=$WORKER"

# List saved workers and observed terminals without starting the VM.
workers:
    #!/usr/bin/env bash
    set -euo pipefail
    VM={{ quote(name) }}
    [ -n "$VM" ] || { echo "error: set 'name:' in workspace.yml" >&2; exit 1; }
    FILTER=$(python3 -c 'import json, sys; print(".name == " + json.dumps(sys.argv[1]))' "$VM")
    if ! STATUS=$(limactl list --filter "$FILTER" --format '{{ '{{.Status}}' }}'); then
        echo "Worker inventory unavailable: VM status query failed." >&2
        exit 1
    fi
    if [ "$STATUS" != "Running" ]; then
        echo "Worker inventory unavailable until the VM is running (status: ${STATUS:-absent})."
        exit 0
    fi
    if ! limactl shell "$VM" -- python3 - --human < {{ quote(repo) }}/worker_inventory.py; then
        echo "Worker inventory unavailable: VM inventory query failed." >&2
        exit 1
    fi

# Refuses dirty, unpublished or unknown Git state, including index flags hiding
# changes. Rechecks preservation before deletion; true bypasses only hook failure.
# Built-in Compose teardown omits -v; the workspace hook may delete named volumes.
# Failures can leave partial hook/terminal/stack effects; nothing is rolled back.
# Remove a preserved worker's session/stack/worktrees/owned branch; true bypasses only hook failure.
worker-rm worker_name force="false":
    #!/usr/bin/env bash
    set -euo pipefail
    VM={{ quote(name) }}
    WORKER={{ quote(worker_name) }}
    FORCE={{ quote(force) }}
    [ -n "$VM" ]     || { echo "error: set 'name:' in workspace.yml" >&2; exit 1; }
    [ -n "$WORKER" ] || { echo "error: worker name is required" >&2; exit 1; }

    if ! printf '%s' "$WORKER" | grep -qE '^[a-z0-9][a-z0-9_-]{0,62}$'; then
        echo "error: invalid worker name '$WORKER'" >&2
        exit 1
    fi
    # Reserved (see _worker-create): `stack` is the workspace's own stack, `main` the main
    # tmux session — removing either under a worker's name is exactly the bug.
    case "$WORKER" in
        stack|main) echo "error: '$WORKER' is a reserved worker name" >&2; exit 1 ;;
    esac

    echo "==> floatie worker-rm: removing worker '$WORKER' from VM '$VM'"
    limactl shell "$VM" -- bash -s -- "$WORKER" "$FORCE" < {{ quote(justfile_directory()) }}/worker-rm.sh

# Re-apply (or relax) the egress firewall from workspace.yml.
# Usage:
#   floatie firewall       — re-resolve DNS and re-apply allowlist per workspace.yml
#   floatie firewall off   — force permissive (allow-all), ignoring workspace.yml permissive flag
#
# Re-running is the recommended fix for CDN-backed domains that start failing due
# to IP rotation (iptables+ipset snapshots IPs at apply time; re-running re-resolves them).
# Re-apply the egress allowlist from workspace.yml (or 'off' to relax to permissive).
firewall mode="":
    #!/usr/bin/env bash
    set -euo pipefail
    NAME={{ quote(name) }}
    REPO={{ quote(repo) }}
    [ -n "$NAME" ] || { echo "error: set 'name:' in workspace.yml" >&2; exit 1; }
    MODE={{ quote(mode) }}
    case "$MODE" in
        ""|off) ;;
        *) echo "error: firewall mode must be empty or off" >&2; exit 1 ;;
    esac
    if [ "$MODE" = "off" ]; then
        echo "==> firewall off: relaxing to permissive (allow-all)"
        limactl shell "$NAME" -- bash -s -- --off < "$REPO/firewall.sh"
    else
        PERMISSIVE={{ quote(permissive) }}
        EGRESS={{ quote(egress) }}
        if [ "$PERMISSIVE" = "true" ]; then
            echo "==> WARNING: permissive: true — egress firewall DISABLED (allow-all)"
            limactl shell "$NAME" -- bash -s -- --off < "$REPO/firewall.sh"
        else
            echo "==> re-applying egress firewall (re-resolving DNS)"
            EGRESS_ARGS=()
            while IFS= read -r domain; do
                if [ -n "$domain" ]; then EGRESS_ARGS+=("$domain"); fi
            done <<< "$EGRESS"
            if [ "${#EGRESS_ARGS[@]}" -gt 0 ]; then
                limactl shell "$NAME" -- bash -s -- "${EGRESS_ARGS[@]}" < "$REPO/firewall.sh"
            else
                limactl shell "$NAME" -- bash -s < "$REPO/firewall.sh"
            fi
        fi
    fi

# Shut down the entire VM; this is not suspension.
# NOTE: iptables egress rules are in-memory only — they are cleared on guest reboot.
#       'floatie start' re-applies the firewall after the VM boots.
# NOTE: Worker tmux sessions do NOT survive a VM stop/start (the guest reboots).
#       Their worktrees and branches persist on disk — recreate sessions with 'floatie worker <name>'.
# Shut down the VM; disk persists, terminal and agent processes do not.
stop:
    #!/usr/bin/env bash
    set -euo pipefail
    NAME={{ quote(name) }}
    [ -n "$NAME" ] || { echo "error: set 'name:' in workspace.yml" >&2; exit 1; }
    STATUS=$(limactl list "$NAME" --format '{{{{.Status}}' 2>/dev/null || true)
    if [ -z "$STATUS" ]; then
        echo "error: VM '$NAME' does not exist — run 'floatie create' first" >&2; exit 1
    fi
    if [ "$STATUS" = "Stopped" ]; then
        echo "==> VM '$NAME' is already stopped — nothing to do"
        exit 0
    fi
    echo "==> floatie stop: shutting down VM '$NAME' (graceful)"
    echo "    NOTE: egress firewall rules are cleared on stop; 'floatie start' will re-apply them."
    echo "    NOTE: worker tmux sessions do not survive stop/start; worktrees/branches persist on disk."
    limactl stop "$NAME"
    echo "==> VM '$NAME' stopped"

# Boot an existing stopped VM and apply firewall; does not resume agents.
# Idempotent: starting an already-running VM prints a message and exits cleanly.
# NOTE: Worker tmux sessions must be re-created with 'floatie worker <name>' after start; agents must be launched separately.
#       Worktrees, branches, repos, and Docker image cache are all preserved.
# Boot an existing stopped VM and apply firewall; does not resume agents.
start:
    #!/usr/bin/env bash
    set -euo pipefail
    NAME={{ quote(name) }}
    REPO={{ quote(repo) }}
    [ -n "$NAME" ] || { echo "error: set 'name:' in workspace.yml" >&2; exit 1; }
    bash "$REPO/disk-preflight.sh" {{ quote(lima_dir) }}/"$NAME"
    STATUS=$(limactl list "$NAME" --format '{{{{.Status}}' 2>/dev/null || true)
    if [ -z "$STATUS" ]; then
        echo "error: VM '$NAME' does not exist — run 'floatie create' first" >&2; exit 1
    fi
    if [ "$STATUS" = "Running" ]; then
        echo "==> VM '$NAME' is already running — nothing to do"
        exit 0
    fi
    echo "==> floatie start: booting VM '$NAME'"
    limactl start "$NAME" --tty=false
    echo "==> VM '$NAME' started"

    # Re-apply the egress firewall (iptables rules are lost on guest reboot).
    echo "==> re-applying egress firewall (rules are not persisted across stop/start)"
    just --justfile "$REPO/justfile" -d "$PWD" --set name {{ quote(name) }} --set permissive {{ quote(permissive) }} --set egress {{ quote(egress) }} firewall

    echo "==> floatie start: done — VM '$NAME' is up"
    echo "    NOTE: worker tmux sessions did not survive the reboot."
    echo "          Worktrees and branches are intact — run 'floatie worker <name>' to recreate sessions; then launch your agent."

# Remove the VM (force stop + delete).
# Push-guarded: aborts if any worktree (main repos or worker worktrees) has uncommitted
# changes or unpushed commits. Pass 'true' as the force arg to override (DATA LOSS —
# unpushed work will be wiped along with the VM disk): floatie destroy true
# Remove the VM (push-guarded; pass 'true' to force despite unpushed/uncommitted work).
destroy force="false":
    #!/usr/bin/env bash
    set -euo pipefail
    NAME={{ quote(name) }}
    REPO={{ quote(repo) }}
    FORCE={{ quote(force) }}
    [ -n "$NAME" ] || { echo "error: set 'name:' in workspace.yml" >&2; exit 1; }
    echo "==> floatie destroy: removing VM '$NAME'"

    # ── Push-guard ─────────────────────────────────────────────────────────────
    # Run lifecycle-guard.sh inside the VM to enumerate ALL worktrees and check
    # for uncommitted changes or unpushed commits.
    # Guard runs whenever the VM exists (Running OR Stopped); a Stopped VM is
    # auto-started briefly so the check can run.
    STATUS=$(limactl list "$NAME" --format '{{{{.Status}}' 2>/dev/null || true)
    if [ "$STATUS" = "Running" ] || [ "$STATUS" = "Stopped" ]; then
        if [ "$STATUS" = "Stopped" ]; then
            echo "==> VM is stopped — starting briefly to run the push-guard"
            limactl start "$NAME" --tty=false
        fi
        if [ "$FORCE" = "true" ]; then
            echo "WARNING: --force override active — proceeding despite push-guard."
            echo "WARNING: The following worktrees have uncommitted changes or unpushed commits"
            echo "WARNING: that will be PERMANENTLY DESTROYED with the VM disk:"
            echo ""
            limactl shell "$NAME" -- bash -s < "$REPO/lifecycle-guard.sh" || true
            echo ""
            echo "WARNING: Proceeding with forced destroy — all listed work will be lost."
            echo ""
        else
            limactl shell "$NAME" -- bash -s < "$REPO/lifecycle-guard.sh" || exit 1
        fi
    fi

    limactl delete -f "$NAME"
    echo "==> VM '$NAME' removed"

# Recreate the VM from clean and re-provision.
# Push-guarded (same guard as destroy; pass 'true' as the force arg to override: floatie rebuild true).
# DATA LOSS WARNING: all VM disk state is wiped — including any worktrees, branches,
# Docker images, and other in-VM state not pushed to a remote. This is why the guard runs.
# Wipe + re-provision the VM from clean (push-guarded; pass 'true' to force).
rebuild force="false":
    #!/usr/bin/env bash
    set -euo pipefail
    NAME={{ quote(name) }}
    REPO={{ quote(repo) }}
    FORCE={{ quote(force) }}
    [ -n "$NAME" ] || { echo "error: set 'name:' in workspace.yml" >&2; exit 1; }
    echo "==> floatie rebuild: recreating VM '$NAME' from clean"

    # ── Push-guard ─────────────────────────────────────────────────────────────
    STATUS=$(limactl list "$NAME" --format '{{{{.Status}}' 2>/dev/null || true)
    if [ "$STATUS" = "Running" ] || [ "$STATUS" = "Stopped" ]; then
        if [ "$STATUS" = "Stopped" ]; then
            echo "==> VM is stopped — starting briefly to run the push-guard"
            limactl start "$NAME" --tty=false
        fi
        if [ "$FORCE" = "true" ]; then
            echo "WARNING: --force override active — proceeding despite push-guard."
            echo "WARNING: The following worktrees have uncommitted changes or unpushed commits"
            echo "WARNING: that will be PERMANENTLY DESTROYED with the VM disk:"
            echo ""
            limactl shell "$NAME" -- bash -s < "$REPO/lifecycle-guard.sh" || true
            echo ""
            echo "WARNING: Proceeding with forced rebuild — all listed work will be lost."
            echo ""
        else
            limactl shell "$NAME" -- bash -s < "$REPO/lifecycle-guard.sh" || exit 1
        fi
    fi

    # ── Destroy + create ───────────────────────────────────────────────────────
    echo "==> destroying existing VM (disk wiped)"
    limactl delete -f "$NAME"
    echo "==> VM '$NAME' deleted"

    echo "==> re-creating VM from clean"
    just --justfile "$REPO/justfile" -d "$PWD" --set name {{ quote(name) }} --set permissive {{ quote(permissive) }} --set egress {{ quote(egress) }} create

    echo "==> floatie rebuild: done — VM '$NAME' is fresh and provisioned"

# Install Codex in an existing VM; apply MCPs, instructions, and workspace firewall.
codex-setup:
    #!/usr/bin/env bash
    set -euo pipefail
    [ -n {{ quote(name) }} ] || { echo "error: set name in workspace.yml" >&2; exit 1; }
    limactl shell {{ quote(name) }} -- bash -s < {{ quote(repo) }}/install-codex.sh
    just --justfile {{ quote(repo) }}/justfile -d "$PWD" --set name {{ quote(name) }} --set permissive {{ quote(permissive) }} --set egress {{ quote(egress) }} _place-codex
    just --justfile {{ quote(repo) }}/justfile -d "$PWD" --set name {{ quote(name) }} --set permissive {{ quote(permissive) }} --set egress {{ quote(egress) }} firewall
    echo "==> Ready: floatie attach; then codex login --device-auth and codex"
    echo "==> OAuth MCPs need their own login: codex mcp login <name>"

# Host workspace is mounted read-only in the guest at the same absolute path.
_place-codex:
    #!/usr/bin/env bash
    set -euo pipefail
    limactl shell {{ quote(name) }} -- bash -lc 'python3 - "$1"' bash "$PWD" < {{ quote(repo) }}/apply-codex.py
