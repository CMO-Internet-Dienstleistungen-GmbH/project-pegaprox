#!/usr/bin/env bash
# Prove the migration's own commands move every kind of VHDX correctly.
#
# The unit tests assert what the command builders produce. This asserts that what they
# produce works: a container stands in for the Proxmox node, mounts the read-only SMB
# export that stands in for the Hyper-V host's file share, and converts each fixture disk
# with the exact command string pegaprox/core/hyperv_transfer.py generates. Every result is
# compared against the checksum the fixtures were built with.
#
# Three disks rather than one, because the matrix in fork issue #37 asks for both VHDX
# subformats and for more than one disk: dynamic and fixed carry the same content and must
# both arrive as it, and the second disk carries different content, so a run that mixed the
# two up would fail here rather than pass on size alone. All three go through one mount,
# which is what the runner does.
#
# What this cannot stand in for is named in README.md. In short: the WinRM side and the
# Proxmox side are absent, so this proves the data path and nothing about either endpoint.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
IMAGE="${VERIFY_IMAGE:-alpine:3.20}"
SHARE_PORT="${HYPERV_SMB_PORT:-14450}"
SHARE_USER="${HYPERV_SMB_USER:-migrate}"
SHARE_PASSWORD="${HYPERV_SMB_PASSWORD:-migrate-testbed-only}"
PYTHON="${PYTHON:-python3}"

usage() {
    cat <<USAGE
Usage: ${0##*/}

Runs the data-path proof for the Hyper-V migration:

  1. builds the VHDX fixtures if they are missing
  2. starts the read-only SMB export
  3. mounts it once and converts each fixture disk using the product's own commands
  4. compares every result against the checksum its fixture was built with

Environment:
  HYPERV_SMB_PORT       Port the share listens on (default: 14450)
  HYPERV_SMB_USER       Share account (default: migrate)
  HYPERV_SMB_PASSWORD   Share password (testbed only, never a real credential)
  VERIFY_IMAGE          Image providing cifs-utils and qemu-img (default: alpine:3.20)
USAGE
}

[[ "${1:-}" == "-h" || "${1:-}" == "--help" ]] && { usage; exit 0; }

if [[ ! -f "$HERE/fixtures/checksums.txt" ]]; then
    echo "Building fixtures..."
    "$HERE/make_fixtures.sh"
fi

echo "Starting the read-only SMB export..."
(cd "$HERE" && docker compose up -d >/dev/null)

# The commands come from the product, not from this script. Retyping them here would prove
# that this script works, which is not the question.
# Each disk of the matrix, with the fixture checksum its content has to come back as.
# Dynamic and fixed are the two VHDX subformats a Hyper-V VM can hold; the second disk
# carries different content, so a multi-disk run that swapped the two would fail here
# rather than pass on size alone.
DISKS="dynamic.vhdx:content.raw fixed.vhdx:content.raw second.vhdx:second.raw"

COMMANDS="$("$PYTHON" - <<PY
import sys
sys.path.insert(0, "$REPO")
from pegaprox.core import hyperv_transfer as t

mount_point = t.mount_point_for('verify01')
credentials = mount_point + '.credentials'

print(t.credentials_file_command(credentials))
print('---')
# The share is reached on a high port here; a real host answers on 445 and needs no
# addition. Everything else is the product's own option set, unchanged.
print(t.mount_command('127.0.0.1', 'vhdx', mount_point, credentials).replace(
    '-o ', '-o port=$SHARE_PORT,', 1))
print('---')
# One mount, every disk: the share is mounted per share rather than per disk, which is
# what the runner does and therefore what this has to do.
for name in '$DISKS'.split():
    disk = name.split(':')[0]
    source = t.source_file_path(mount_point, disk)
    print(disk + '|||' + t.probe_command(source) + '|||'
          + t.convert_command(source, '/tmp/converted-' + disk + '.raw'))
print('---')
print(t.unmount_command(mount_point, credentials))
PY
)"

CREDENTIALS_CMD="$(echo "$COMMANDS" | sed -n '1p')"
MOUNT_CMD="$(echo "$COMMANDS" | sed -n '3p')"
DISK_COMMANDS="$(echo "$COMMANDS" | sed -n '/^---$/,$p' | sed -n '/|||/p')"
UNMOUNT_CMD="$(echo "$COMMANDS" | tail -1)"

CHECKSUMS="$(cat "$HERE/fixtures/checksums.txt")"

echo "Converting through the product's own commands..."
# --privileged is needed for mount(2) inside a container and for nothing else here. The
# host network reaches the share on its published port.
docker run --rm --privileged --network host \
    -e SHARE_USER="$SHARE_USER" -e SHARE_PASSWORD="$SHARE_PASSWORD" \
    -e DISKS="$DISKS" -e CHECKSUMS="$CHECKSUMS" \
    -e CREDENTIALS_CMD="$CREDENTIALS_CMD" -e MOUNT_CMD="$MOUNT_CMD" \
    -e DISK_COMMANDS="$DISK_COMMANDS" -e UNMOUNT_CMD="$UNMOUNT_CMD" \
    "$IMAGE" sh -eu -c '
apk add --no-cache cifs-utils qemu-img >/dev/null 2>&1
mkdir -p /mnt/pegaprox-hyperv

# The password reaches the file on stdin, exactly as the migration sends it over SSH.
printf "username=%s\npassword=%s\n" "$SHARE_USER" "$SHARE_PASSWORD" | sh -c "$CREDENTIALS_CMD"
sh -c "$MOUNT_CMD"

echo "  mounted; the share must refuse a write:"
if touch /mnt/pegaprox-hyperv/verify01/should-not-exist 2>/dev/null; then
    echo "  FAIL: the share accepted a write" >&2
    sh -c "$UNMOUNT_CMD"
    exit 1
fi
echo "  refused, as it must"

FAILED=0
echo "$DISK_COMMANDS" | while IFS= read -r line; do
    DISK=${line%%|||*}
    REST=${line#*|||}
    PROBE_CMD=${REST%%|||*}
    CONVERT_CMD=${REST#*|||}

    RAW=$(echo "$DISKS" | tr " " "\n" | sed -n "s/^${DISK}://p")
    EXPECTED=$(echo "$CHECKSUMS" | awk -v raw="$RAW" "\$2 == raw {print \$1}")
    [ -n "$EXPECTED" ] || { echo "  FAIL: no fixture checksum for $DISK" >&2; exit 1; }

    sh -c "$PROBE_CMD" >/dev/null
    sh -c "$CONVERT_CMD" >/dev/null
    ACTUAL=$(sha256sum /tmp/converted-$DISK.raw | cut -d" " -f1)
    if [ "$ACTUAL" != "$EXPECTED" ]; then
        echo "  FAIL: $DISK converted to $ACTUAL, fixtures say $EXPECTED" >&2
        exit 1
    fi
    echo "  $DISK: content matches the fixture checksum"
done || FAILED=1

sh -c "$UNMOUNT_CMD"
[ "$FAILED" = "0" ] || exit 1
'

echo "Data path verified."
