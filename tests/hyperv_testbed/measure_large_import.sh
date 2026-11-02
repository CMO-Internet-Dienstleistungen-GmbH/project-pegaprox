#!/usr/bin/env bash
# Measure what a large Hyper-V import actually costs, and what it does when it runs out.
#
# The transfer's shape is the claim being checked: the data never passes through PegaProx,
# the target node converts the disk itself, and the only thing PegaProx reads is a progress
# percentage. If that is true, then the size of the disk changes the duration and nothing
# else — not the memory of the management server, not the size of a log.
#
# So this measures, on a deliberately large sparse disk:
#
#   1. virtual size against physical size, on the share and at the target
#   2. the peak resident memory of the process doing the conversion
#   3. what a target that runs out of space does, and what it leaves behind
#
# Everything is written down as measured, in docs/hyperv-transfer.md. There are no
# throughput promises here: a number from this machine describes this machine.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
IMAGE="${VERIFY_IMAGE:-alpine:3.20}"
SHARE_PORT="${HYPERV_SMB_PORT:-14450}"
SHARE_USER="${HYPERV_SMB_USER:-migrate}"
SHARE_PASSWORD="${HYPERV_SMB_PASSWORD:-migrate-testbed-only}"
PYTHON="${PYTHON:-python3}"

# Virtual size of the synthetic disk, and how much of it actually holds data. A real VM's
# disk is mostly holes too, which is the case worth measuring: a transfer that moved the
# virtual size would take twenty times as long as one that moves the physical size.
VIRTUAL_GB="${HYPERV_LARGE_VIRTUAL_GB:-64}"
CONTENT_MB="${HYPERV_LARGE_CONTENT_MB:-256}"

# How small the failing target is. Large enough that the conversion starts and gets some
# way in, small enough that it cannot finish.
FULL_TARGET_MB="${HYPERV_FULL_TARGET_MB:-64}"

usage() {
    cat <<USAGE
Usage: ${0##*/}

Measures a large import in the Docker testbed:

  1. builds a sparse ${VIRTUAL_GB} GiB VHDX holding ${CONTENT_MB} MiB of data
  2. converts it over the read-only share with the product's own command
  3. records sizes, peak memory and duration
  4. repeats onto a ${FULL_TARGET_MB} MiB target to measure what running out does

Environment:
  HYPERV_LARGE_VIRTUAL_GB   Virtual size of the test disk (default: 64)
  HYPERV_LARGE_CONTENT_MB   How much of it holds data (default: 256)
  HYPERV_FULL_TARGET_MB     Size of the deliberately too-small target (default: 64)
  HYPERV_SMB_PORT           Port the share listens on (default: 14450)
USAGE
}

[[ "${1:-}" == "-h" || "${1:-}" == "--help" ]] && { usage; exit 0; }

FIXTURES="$HERE/fixtures"
# The name carries the shape, so several measurement points can live side by side and a
# rerun with different numbers measures what it says rather than reusing the old disk.
FIXTURE_NAME="large-${VIRTUAL_GB}g-${CONTENT_MB}m"
LARGE="$FIXTURES/$FIXTURE_NAME.vhdx"

if [[ ! -f "$LARGE" ]]; then
    echo "Building a ${VIRTUAL_GB} GiB sparse VHDX with ${CONTENT_MB} MiB of content..."
    mkdir -p "$FIXTURES"
    docker run --rm -v "$FIXTURES:/out" -w /out "$IMAGE" sh -eu -c "
        apk add --no-cache qemu-img >/dev/null 2>&1
        qemu-img create -f raw large.raw ${VIRTUAL_GB}G >/dev/null
        # Data at both ends, so a copy that stops early is visibly short rather than
        # merely smaller, and the holes in between are what makes it sparse.
        dd if=/dev/urandom of=large.raw bs=1M count=\$(( ${CONTENT_MB} / 2 )) conv=notrunc status=none
        dd if=/dev/urandom of=large.raw bs=1M count=\$(( ${CONTENT_MB} / 2 )) \
           seek=\$(( ${VIRTUAL_GB} * 1024 - ${CONTENT_MB} / 2 )) conv=notrunc status=none
        qemu-img convert -f raw -O vhdx large.raw ${FIXTURE_NAME}.vhdx
        sha256sum large.raw > ${FIXTURE_NAME}.sha256
        rm -f large.raw
        chmod 0644 ${FIXTURE_NAME}.vhdx ${FIXTURE_NAME}.sha256
    "
fi

echo "Starting the read-only share..."
(cd "$HERE" && docker compose up -d hyperv-share >/dev/null)

echo "Asking the product for its commands..."
COMMANDS="$("$PYTHON" - <<PY
import sys
sys.path.insert(0, "$REPO")
from pegaprox.core import hyperv_transfer as t

mount_point = t.mount_point_for('measure01')
credentials = mount_point + '.credentials'
source = t.source_file_path(mount_point, '$FIXTURE_NAME.vhdx')

print(t.credentials_file_command(credentials))
print('---')
print(t.mount_command('127.0.0.1', 'vhdx', mount_point, credentials).replace(
    '-o ', '-o port=$SHARE_PORT,', 1))
print('---')
print(t.convert_command(source, '/target/disk.raw'))
print('---')
print(t.unmount_command(mount_point, credentials))
PY
)"

CREDENTIALS_CMD="$(echo "$COMMANDS" | sed -n '1p')"
MOUNT_CMD="$(echo "$COMMANDS" | sed -n '3p')"
CONVERT_CMD="$(echo "$COMMANDS" | sed -n '5p')"
UNMOUNT_CMD="$(echo "$COMMANDS" | sed -n '7p')"

run_measurement() {
    local label="$1" target_mb="$2"
    docker run --rm --privileged --network host \
        -e SHARE_USER="$SHARE_USER" -e SHARE_PASSWORD="$SHARE_PASSWORD" \
        -e CREDENTIALS_CMD="$CREDENTIALS_CMD" -e MOUNT_CMD="$MOUNT_CMD" \
        -e CONVERT_CMD="$CONVERT_CMD" -e UNMOUNT_CMD="$UNMOUNT_CMD" \
        -e TARGET_MB="$target_mb" -e LABEL="$label" -e FIXTURE_NAME="$FIXTURE_NAME" \
        "$IMAGE" sh -eu -c '
apk add --no-cache cifs-utils qemu-img >/dev/null 2>&1
mkdir -p /mnt/pegaprox-hyperv /target
printf "username=%s\npassword=%s\n" "$SHARE_USER" "$SHARE_PASSWORD" | sh -c "$CREDENTIALS_CMD"
sh -c "$MOUNT_CMD"

SOURCE=/mnt/pegaprox-hyperv/measure01/$FIXTURE_NAME.vhdx
# The format is named rather than probed, for the same reason the product names it in its
# convert command: a probe that guesses raw reports the file size as the virtual size,
# which is the one number this measurement exists to tell apart.
# The JSON nests the file under "children" and gives it a virtual-size of its own, which
# is the file size. The virtual size of the disk itself is the one at the top level, and
# it comes last: taking the first match reports 264 MiB for a 64 GiB disk.
echo "  source virtual bytes:  $(qemu-img info -f vhdx --output=json "$SOURCE" | sed -n "s/^    \"virtual-size\": \([0-9]*\).*/\1/p" | tail -1)"
echo "  source physical bytes: $(stat -c %s "$SOURCE")"

if [ "$TARGET_MB" != "0" ]; then
    # A target that cannot hold the disk, to measure what running out of room does.
    mount -t tmpfs -o size=${TARGET_MB}m tmpfs /target
fi

# Peak resident memory of the converting process, sampled while it runs. This is the
# number the claim rests on: it must not grow with the size of the disk.
# `set -e` is active and is inherited by this subshell, so a failing conversion would end
# it before the exit code could be written and the reader below would fall back to 1 --
# reporting a number it never measured. Capturing the status explicitly keeps the failure
# rows in docs/hyperv-transfer.md measurements rather than assumptions.
( rc=0
  sh -c "$CONVERT_CMD" >/tmp/convert.out 2>/tmp/convert.err || rc=$?
  echo "$rc" >/tmp/convert.rc ) &
CONVERT_SHELL=$!
PEAK=0
START=$(date +%s)
while kill -0 $CONVERT_SHELL 2>/dev/null; do
    for pid in $(pidof qemu-img 2>/dev/null || true); do
        HWM=$(sed -n "s/^VmHWM:[[:space:]]*\([0-9]*\).*/\1/p" /proc/$pid/status 2>/dev/null || echo 0)
        [ "${HWM:-0}" -gt "$PEAK" ] && PEAK=$HWM
    done
    # A conversion that fails early can be over in well under a second, and a sample that
    # misses it entirely would report a peak of zero and read as a memory measurement.
    sleep 0.05
done
wait $CONVERT_SHELL 2>/dev/null || true
END=$(date +%s)
# No file means the subshell never got to write one, which is a defect in this script
# rather than a result -- say so instead of inventing an exit code.
RC=$(cat /tmp/convert.rc 2>/dev/null || echo "not recorded")

echo "  qemu-img exit code:    $RC"
if [ "$PEAK" = "0" ]; then
    echo "  peak resident KiB:     not sampled (the run was shorter than the sampling interval)"
else
    echo "  peak resident KiB:     $PEAK"
fi
echo "  duration seconds:      $(( END - START ))"
if [ -f /target/disk.raw ]; then
    echo "  target apparent bytes: $(stat -c %s /target/disk.raw)"
    echo "  target allocated KiB:  $(du -k /target/disk.raw | cut -f1)"
fi
if [ "$RC" = "0" ] && [ -f /mnt/pegaprox-hyperv/measure01/$FIXTURE_NAME.sha256 ]; then
    EXPECTED=$(cut -d" " -f1 /mnt/pegaprox-hyperv/measure01/$FIXTURE_NAME.sha256)
    ACTUAL=$(sha256sum /target/disk.raw | cut -d" " -f1)
    if [ "$EXPECTED" = "$ACTUAL" ]; then
        echo "  content:               matches the fixture checksum"
    else
        echo "  content:               MISMATCH ($ACTUAL)"
        exit 1
    fi
fi
if [ "$RC" != "0" ]; then
    echo "  what it said:          $(tail -c 200 /tmp/convert.err | tr "\n" " ")"
fi

sh -c "$UNMOUNT_CMD"
'
}

echo
echo "=== A large sparse disk onto a target with room ==="
run_measurement roomy 0

echo
echo "=== The same disk onto a target that runs out ==="
run_measurement full "$FULL_TARGET_MB"

echo
echo "Measured. Record these in docs/hyperv-transfer.md rather than rounding them up."
