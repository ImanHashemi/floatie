#!/usr/bin/env bash
# Run inside an already-provisioned VM. Never restarts sessions or changes repos.
set -euo pipefail
export PATH="$HOME/.local/bin:$PATH"

configure_apparmor() {
    # Ubuntu 24.04 ships bubblewrap's policy as an optional extra profile.
    # Keep the system-wide namespace restriction enabled and use that policy.
    [[ -r /etc/os-release ]] || return 0
    local ID VERSION_ID
    source /etc/os-release
    [[ ${ID:-} == ubuntu && ${VERSION_ID:-} == 24.04 ]] || return 0
    [[ $(sysctl -n kernel.apparmor_restrict_unprivileged_userns 2>/dev/null || true) == 1 ]] || return 0

    local profile=/etc/apparmor.d/bwrap-userns-restrict
    local packaged=/usr/share/apparmor/extra-profiles/bwrap-userns-restrict
    local attached
    # Multiple profiles attached to the same executable can prevent attachment.
    # Never remove or overwrite another application's policy automatically.
    attached=$(sudo grep -rlE '^[[:space:]]*(profile[[:space:]]+[^[:space:]]+[[:space:]]+)?/usr/bin/bwrap([[:space:]]|\{)' /etc/apparmor.d || true)
    while IFS= read -r attached; do
        [[ -z $attached || $attached == "$profile" ]] && continue
        echo "error: existing bubblewrap AppArmor policy at $attached; have an administrator reconcile it with $profile, then rerun setup" >&2
        return 1
    done <<< "$attached"
    if [[ -e /etc/apparmor.d/disable/bwrap-userns-restrict || -L /etc/apparmor.d/disable/bwrap-userns-restrict ]]; then
        echo "error: $profile was explicitly disabled; have an administrator review it before rerunning setup" >&2
        return 1
    fi
    if sudo grep -qx 'bwrap (enforce)' /sys/kernel/security/apparmor/profiles &&
       sudo grep -qx 'unpriv_bwrap (enforce)' /sys/kernel/security/apparmor/profiles; then
        return 0
    fi
    if [[ ! -f $profile ]]; then
        if [[ ! -f $packaged ]]; then
            sudo apt-get install -y --no-install-recommends apparmor-profiles apparmor-utils
        fi
        [[ -f $packaged ]] || {
            echo "error: Ubuntu's bubblewrap AppArmor profile is missing at $packaged; update the VM's AppArmor packages and rerun setup" >&2
            return 1
        }
        sudo install -m 0644 "$packaged" "$profile"
    fi
    sudo apparmor_parser -r "$profile"
}

if ! command -v bwrap >/dev/null 2>&1; then
    sudo apt-get install -y --no-install-recommends bubblewrap
fi
if ! command -v codex >/dev/null 2>&1; then
    command -v npm >/dev/null || { echo 'error: npm missing; provision the VM first' >&2; exit 1; }
    npm install --global --prefix "$HOME/.local" @openai/codex
fi
configure_apparmor
codex --version
if ! codex sandbox -- /bin/true; then
    echo 'error: Codex sandbox verification failed; inspect the VM AppArmor audit log and run codex sandbox -- /bin/true after resolving it' >&2
    exit 1
fi
