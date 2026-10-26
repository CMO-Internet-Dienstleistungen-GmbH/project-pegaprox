#!/usr/bin/env bash
# Build the synthetic disk fixtures the Hyper-V migration tests read.
#
# Everything here is generated, never copied from a real machine: the images hold a known
# byte pattern and nothing else, so a test can prove the imported disk carries the same
# content it started with rather than merely that a file of the right size arrived.
#
# qemu-img runs in a container so the host needs nothing installed. It is the same tool
# the import path uses, which is the point: the fixtures are produced and read by the
# implementation's own dependency, not by a stand-in.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OUT="${HYPERV_FIXTURE_DIR:-$HERE/fixtures}"
IMAGE="${QEMU_IMAGE:-alpine:latest}"

# Small on purpose. These exercise format handling and content integrity, not throughput;
# a large-transfer test builds its own image and is marked slow.
DISK_MB="${HYPERV_FIXTURE_DISK_MB:-16}"

usage() {
    cat <<USAGE
Usage: ${0##*/} [--clean]

Builds synthetic VHDX fixtures into:
    $OUT

  --clean   Remove the fixture directory and exit.
  -h        This text.

Environment:
  HYPERV_FIXTURE_DIR       Where to write fixtures (default: <testbed>/fixtures)
  HYPERV_FIXTURE_DISK_MB   Virtual size of each disk in MB (default: 16)
  QEMU_IMAGE               Container image providing qemu-img (default: alpine:latest)
USAGE
}

case "${1:-}" in
    -h|--help) usage; exit 0 ;;
    --clean) rm -rf "$OUT"; echo "removed $OUT"; exit 0 ;;
    "") ;;
    *) usage; exit 2 ;;
esac

command -v docker >/dev/null 2>&1 || { echo "docker is required" >&2; exit 1; }

mkdir -p "$OUT"

docker run --rm -v "$OUT:/out" -w /out "$IMAGE" sh -eu -c "
    apk add --no-cache qemu-img >/dev/null 2>&1

    # A recognisable pattern, written at both ends and in the middle, so a truncated or
    # offset-shifted copy fails the comparison instead of passing on size alone.
    dd if=/dev/zero of=content.raw bs=1M count=${DISK_MB} status=none
    printf 'PEGAPROX-HYPERV-FIXTURE-HEAD' | dd of=content.raw bs=1 seek=0 conv=notrunc status=none
    printf 'PEGAPROX-HYPERV-FIXTURE-MID'  | dd of=content.raw bs=1 seek=\$(( ${DISK_MB} * 1048576 / 2 )) conv=notrunc status=none
    printf 'PEGAPROX-HYPERV-FIXTURE-TAIL' | dd of=content.raw bs=1 seek=\$(( ${DISK_MB} * 1048576 - 32 )) conv=notrunc status=none

    qemu-img convert -f raw -O vhdx content.raw dynamic.vhdx
    qemu-img convert -f raw -O vhdx -o subformat=fixed content.raw fixed.vhdx

    # A second disk with different content, so a multi-disk test can prove the mapping
    # rather than assume it: swapping the two would fail.
    dd if=/dev/zero of=second.raw bs=1M count=${DISK_MB} status=none
    printf 'PEGAPROX-HYPERV-FIXTURE-SECOND-DISK' | dd of=second.raw bs=1 seek=0 conv=notrunc status=none
    qemu-img convert -f raw -O vhdx second.raw second.vhdx

    sha256sum content.raw second.raw > checksums.txt
    rm -f content.raw second.raw
    chmod 0644 *.vhdx checksums.txt
"

echo "fixtures in $OUT:"
ls -l "$OUT"
