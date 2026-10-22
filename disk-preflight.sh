#!/usr/bin/env bash
# disk-preflight.sh [--warn-only] <path> — refuse to grow the VM's sparse disk into a
# nearly full host volume.
#
# The Lima disk image is sparse: the guest believes it has tens of GiB free while the host
# volume that backs it may have far less. When the host hits zero, the guest gets EIO on every
# file operation and every session drops. Check the volume holding <path> before any verb
# that starts, resumes or extends the VM.
#
#   exit 0  enough space (prints a WARN line on stdout below WARN_GIB)
#   exit 1  below MIN_GIB, df failed, or df output couldn't be parsed — the caller must
#           refuse; FLOATIE_SKIP_DISK_CHECK=1 overrides all three
#
# --warn-only: never refuse on a low reading — a below-MIN_GIB reading is printed as a
# WARN on stdout instead of an error on stderr, and the script exits 0. df failures and
# unparseable output still exit 1 regardless of this flag (there's no free-space number to
# warn with). Used by verbs (e.g. `attach`) that neither start nor grow the VM disk and are
# themselves the recovery path when the host is full.
#
# Runs on the HOST: macOS bash 3.2 compatible.
set -euo pipefail

WARN_GIB=20
MIN_GIB=10

WARN_ONLY=0
if [ "${1:-}" = "--warn-only" ]; then
  WARN_ONLY=1
  shift
fi

if [ $# -lt 1 ]; then
  echo "error: usage: disk-preflight.sh [--warn-only] <path>" >&2
  exit 1
fi

TARGET_ARG="$1"
TARGET="$TARGET_ARG"
# ~/.lima/<name> does not exist before `create`: ask df about the nearest existing ancestor,
# but keep TARGET_ARG for messages — report the path the caller actually asked about.
while [ ! -e "$TARGET" ]; do TARGET="$(dirname "$TARGET")"; done

# Check the override before calling df at all: df itself can fail (missing binary, odd
# mount), and under `set -euo pipefail` that would abort the script before the override
# ever got a chance to skip the check.
if [ "${FLOATIE_SKIP_DISK_CHECK:-}" = "1" ]; then
  echo "WARN: disk preflight skipped (FLOATIE_SKIP_DISK_CHECK=1) for the volume holding $TARGET_ARG"
  exit 0
fi

if ! df_out="$(df -Pk "$TARGET" 2>/dev/null)"; then
  echo "error: disk preflight could not read free space for $TARGET_ARG (df failed); set FLOATIE_SKIP_DISK_CHECK=1 to override" >&2
  exit 1
fi

# Parse the Available field via the Capacity field ("<digits>%"), the only column with an
# unambiguous shape in POSIX df -P output. A fixed offset from either end breaks once
# EITHER the device name (e.g. macOS's "map auto_home") OR the mount point (e.g.
# "/Volumes/My Disk" — common once LIMA_HOME is relocated onto a named macOS volume) has
# embedded spaces, since either shifts the column count unpredictably. Scan the second
# line backward for the Capacity marker and take the field immediately before it.
# 2>/dev/null on the df call above: the explicit exit-status check already covers real
# failures, so a df that exits 0 but writes a diagnostic to stderr must not pollute $df_out.
avail_kb="$(printf '%s\n' "$df_out" | awk 'NR==2 { for (i = NF; i > 1; i--) if ($i ~ /^[0-9]+%$/) { print $(i-1); exit } }')"
case "$avail_kb" in
  ''|*[!0-9]*)
    echo "error: disk preflight could not parse df output for $TARGET_ARG; set FLOATIE_SKIP_DISK_CHECK=1 to override" >&2
    exit 1
    ;;
esac
avail_gib=$((avail_kb / 1024 / 1024))

if [ "$avail_gib" -lt "$MIN_GIB" ]; then
  if [ "$WARN_ONLY" -eq 1 ]; then
    echo "WARN: only ${avail_gib} GiB free on the volume holding $TARGET_ARG (minimum ${MIN_GIB} GiB)."
    echo "      The VM disk is sparse and grows into this space; at zero the guest returns EIO on every"
    echo "      file operation. Free host space soon."
    exit 0
  fi
  echo "error: only ${avail_gib} GiB free on the volume holding $TARGET_ARG (minimum ${MIN_GIB} GiB)." >&2
  echo "       The VM disk is sparse and grows into this space; at zero the guest returns EIO on every" >&2
  echo "       file operation. Free host space first, or set FLOATIE_SKIP_DISK_CHECK=1 to override." >&2
  exit 1
fi

if [ "$avail_gib" -lt "$WARN_GIB" ]; then
  echo "WARN: ${avail_gib} GiB free on the volume holding $TARGET_ARG — below ${WARN_GIB} GiB; the VM disk grows into this space."
fi
