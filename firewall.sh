#!/usr/bin/env bash
# firewall.sh — default-deny egress + allowlist + permissive relax.
# Invoked as: limactl shell <name> -- bash -s -- [domain...] < firewall.sh
#             limactl shell <name> -- bash -s -- --off       < firewall.sh
#
# Positional args are EXTRA domains to allowlist (from workspace.yml egress:).
# "--off" as the sole first argument disables the firewall (permissive mode).
#
# Idempotent: re-apply uses an atomic ipset swap so the live allowlist is never
# empty during DNS resolution — no reject window on re-apply, and a DNS failure
# during re-apply leaves the PREVIOUS allowlist intact.
# The ESTABLISHED rule keeps any active limactl/SSH session alive.
# DNS (udp+tcp :53 to gateway only) and local/RFC-1918 traffic are always allowed.

set -euo pipefail

# ── Base allowlist ────────────────────────────────────────────────────────────
# Anthropic — Claude Code API + installer
# GitHub — repos + API + raw content + code archives
# npm, PyPI, Docker registries — package pulls during agent work
# Docker CDN — docker pull layer fetch uses Amazon CloudFront as the CDN backend;
#   required even with registry domains — without it layers download from a blocked IP.
# Ubuntu apt — package installation in the guest (amd64 + arm64 ports)
# Claude.ai — Claude Code installer download
# GitHub CLI — gh apt repo + keyring (lets in-VM agents open PRs)
BASE="
api.anthropic.com
api.openai.com
auth.openai.com
chatgpt.com
claude.ai
github.com
api.github.com
codeload.github.com
objects.githubusercontent.com
raw.githubusercontent.com
registry.npmjs.org
pypi.org
files.pythonhosted.org
registry-1.docker.io
auth.docker.io
production.cloudfront.docker.com
archive.ubuntu.com
security.ubuntu.com
ports.ubuntu.com
astral.sh
cli.github.com
"

# Read-only host query: exit before dependency checks, DNS or any mutation.
if [ "${1:-}" = "--print-base-domains" ] && [ "$#" -eq 1 ]; then
    for domain in $BASE; do printf '%s\n' "$domain"; done
    exit 0
fi

# ── Permissive (--off) ────────────────────────────────────────────────────────
if [ "${1:-}" = "--off" ]; then
    sudo iptables -F OUTPUT
    sudo iptables -P OUTPUT ACCEPT
    # Mirror off in ip6tables if available (prevent dual-stack bypass).
    if command -v ip6tables >/dev/null 2>&1; then
        sudo ip6tables -F OUTPUT
        sudo ip6tables -P OUTPUT ACCEPT
    fi
    echo "==> firewall: OFF (permissive — all egress allowed)"
    exit 0
fi

# ── Dependency guard ─────────────────────────────────────────────────────────
# Placed AFTER the --off early-exit: the permissive escape hatch must work even
# on a guest that is missing dig/ipset (e.g. before first provision completes).
command -v dig >/dev/null || { echo "error: dig missing — re-run provision (just create)" >&2; exit 1; }
command -v ipset >/dev/null || { echo "error: ipset missing — re-run provision (just create)" >&2; exit 1; }
command -v ip >/dev/null || { echo "error: 'ip' (iproute2) missing — re-run provision (just create)" >&2; exit 1; }

# ── Gateway ───────────────────────────────────────────────────────────────────
GW="$(ip route 2>/dev/null | awk '/^default/{print $3; exit}')"
if [ -z "$GW" ]; then
    echo "warn: could not determine default gateway — DNS and GW rules will be skipped" >&2
fi

# ── Atomic ipset refill via swap ──────────────────────────────────────────────
# Strategy: resolve ALL domains into a TEMP set (allowed-new), then atomically
# swap it into place.  The live `allowed` set (referenced by the iptables rule)
# is NEVER flushed before the new IPs are ready, so:
#   • Re-apply has NO reject window — allowlisted connections are uninterrupted.
#   • A DNS failure (0 IPs resolved) aborts BEFORE touching the live set —
#     the previous allowlist stays intact and egress to allowed domains continues.

# Step 1: destroy any stale temp set, then create fresh.
sudo ipset destroy allowed-new 2>/dev/null || true
sudo ipset create allowed-new hash:ip family inet

# Step 2: resolve all domains into allowed-new.
IP_COUNT=0
for domain in $BASE "$@"; do
    # Shape guard: skip args that don't look like valid domain names.
    [[ "$domain" =~ ^[A-Za-z0-9.-]+$ ]] || { echo "warn: skipping invalid domain '$domain'" >&2; continue; }
    # Collect A records; may be empty for a domain not yet in DNS or offline.
    DOMAIN_IPS=$(dig +short A "$domain" 2>/dev/null | grep -E '^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$' || true)
    if [ -z "$DOMAIN_IPS" ]; then
        echo "warn: $domain resolved to 0 IPs — not allowlisted" >&2
        continue
    fi
    for ip in $DOMAIN_IPS; do
        # -exist: tolerates duplicates; real errors (bad IP format etc.) still surface.
        sudo ipset -exist add allowed-new "$ip"
        IP_COUNT=$((IP_COUNT + 1))
    done
done

# Step 3: abort BEFORE touching the live set if nothing resolved.
# A 0-IP default-deny would brick all egress including the Anthropic endpoint.
if [ "$IP_COUNT" -eq 0 ]; then
    sudo ipset destroy allowed-new
    echo "error: 0 A-records resolved across all domains — aborting; previous allowlist is intact" >&2
    exit 1
fi

# Step 4: ensure the live `allowed` set EXISTS (required target for swap on first run).
if ! sudo ipset list allowed >/dev/null 2>&1; then
    sudo ipset create allowed hash:ip family inet
fi

# Step 5: atomic swap — kernel swaps the backing data structures in one operation.
# The iptables rule (-m set --match-set allowed) instantly sees the new contents.
sudo ipset swap allowed-new allowed

# Step 6: destroy allowed-new (now holds the OLD contents — no longer needed).
sudo ipset destroy allowed-new

# ── Flush existing rules and rebuild iptables OUTPUT chain ────────────────────
# On a FIRST apply, the chain is empty → flush is a no-op, then rules are added.
# On a RE-apply, the rules already reference `allowed` (which was just swapped);
# we flush and re-add to keep rule ordering correct and idempotent.
#
# REJECT-rule-over-DROP-policy is deliberate: a DROP default policy combined with
# a half-applied rule set would silently lock out SSH; REJECT gives callers an
# immediate ICMP error and keeps the chain traversal predictable.
sudo iptables -F OUTPUT

# ── iptables OUTPUT chain ─────────────────────────────────────────────────────
# 1. Loopback always allowed.
sudo iptables -A OUTPUT -o lo -j ACCEPT
# 2. Keep any existing SSH/limactl sessions alive (critical — prevents lock-out).
sudo iptables -A OUTPUT -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT
# 3. DNS resolution — restricted to gateway only (prevents DNS-over-port-53 exfil).
#    Flows via systemd-resolved (127.0.0.53, covered by lo rule) → upstream GW.
[ -n "$GW" ] && sudo iptables -A OUTPUT -p udp --dport 53 -d "$GW" -j ACCEPT
[ -n "$GW" ] && sudo iptables -A OUTPUT -p tcp --dport 53 -d "$GW" -j ACCEPT
# 4. Default gateway (DHCP renewals, Lima host agent).
[ -n "$GW" ] && sudo iptables -A OUTPUT -d "$GW" -j ACCEPT
# 5. RFC-1918 private ranges (Docker bridge, VM internal networking).
#    NOTE: this exposes the host LAN — see README caveats.
sudo iptables -A OUTPUT -d 10.0.0.0/8     -j ACCEPT
sudo iptables -A OUTPUT -d 172.16.0.0/12  -j ACCEPT
sudo iptables -A OUTPUT -d 192.168.0.0/16 -j ACCEPT
# 6. Allowlisted IPs.
sudo iptables -A OUTPUT -m set --match-set allowed dst -j ACCEPT
# 7. Default deny — reject with ICMP so callers get an immediate error.
sudo iptables -A OUTPUT -j REJECT --reject-with icmp-port-unreachable

# ── IPv6 rules (mirror minimal set to prevent dual-stack bypass) ──────────────
# If the guest gains an IPv6 address, an absent ip6tables config would bypass the
# firewall entirely. We mirror lo/ESTABLISHED/REJECT as a catch-all.
if command -v ip6tables >/dev/null 2>&1; then
    sudo ip6tables -F OUTPUT
    sudo ip6tables -A OUTPUT -o lo -j ACCEPT
    sudo ip6tables -A OUTPUT -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT
    sudo ip6tables -A OUTPUT -j REJECT --reject-with icmp6-port-unreachable
else
    echo "warn: ip6tables not found — IPv6 egress is unfiltered (skip is safe if guest has no IPv6)" >&2
fi

echo "==> firewall: ON ($IP_COUNT A-records resolved, gw=${GW:-none})"
