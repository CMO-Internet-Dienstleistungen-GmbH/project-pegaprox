#!/usr/bin/env bash
# Prove the target half of a Hyper-V import against a real Proxmox node.
#
# verify_transfer.sh converts into a file inside a container. That proves the data path and
# says nothing about the two steps that only exist on a Proxmox node: asking `pvesm` for a
# volume, and writing into whatever `pvesm path` hands back. Those are the steps where a
# block device behaves differently from a file, and where a storage decides whether the
# target stays sparse.
#
# So this runs the product's own commands over SSH on a node somebody provides, converts
# into an allocated volume, reads the content back off that volume and compares it against
# the fixture checksum, and frees the volume again. With a second, deliberately small
# storage it also measures what running out of room does there — which is not what it does
# on a file.
#
# Nothing about any particular environment is written down here. Every address, name and id
# comes from the environment, and the script refuses to guess one.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
PYTHON="${PYTHON:-python3}"

usage() {
    cat <<USAGE
Usage: ${0##*/}

Converts a VHDX into a volume on a real Proxmox node, using the commands
pegaprox/core/hyperv_transfer.py and the import runner build.

Required environment (no defaults — the script will not guess):
  HYPERV_TARGET_SSH          ssh destination of the Proxmox node
  HYPERV_TARGET_STORAGE      storage to allocate the volume on
  HYPERV_TARGET_VMID         a free VMID to allocate under
  HYPERV_TARGET_SHARE_HOST   host serving the fixtures, as the node reaches it
  HYPERV_TARGET_SHARE_NAME   share name on that host
  HYPERV_TARGET_CREDENTIALS  file on the node holding the share credentials
  HYPERV_TARGET_FIXTURE      fixture file name inside the share
  HYPERV_TARGET_VIRTUAL_BYTES  virtual size of that fixture

Optional:
  HYPERV_TARGET_SHARE_PORT   port the share listens on (default: the SMB default)
  HYPERV_TARGET_STORAGE_SMALL  a storage too small to hold the disk, for the failure case
  HYPERV_TARGET_SUDO         prefix for privileged commands on the node (default: sudo)

The node needs cifs-utils and qemu-img. It must reach the share. The VMID is
allocated and freed again; nothing else on the node is touched.
USAGE
}

[[ "${1:-}" == "-h" || "${1:-}" == "--help" ]] && { usage; exit 0; }

require() {
    local name="$1"
    [[ -n "${!name:-}" ]] || { echo "$name is required; see --help" >&2; exit 2; }
}
for v in HYPERV_TARGET_SSH HYPERV_TARGET_STORAGE HYPERV_TARGET_VMID \
         HYPERV_TARGET_SHARE_HOST HYPERV_TARGET_SHARE_NAME HYPERV_TARGET_CREDENTIALS \
         HYPERV_TARGET_FIXTURE HYPERV_TARGET_VIRTUAL_BYTES; do
    require "$v"
done
SUDO="${HYPERV_TARGET_SUDO-sudo}"
# IdentitiesOnly keeps an agent holding many keys from offering all of them and being cut
# off after too many failures. Overridable, because the caller's own ssh config may differ.
read -r -a SSH_OPTS <<<"${HYPERV_TARGET_SSH_OPTS--o IdentitiesOnly=yes}"
SMALL="${HYPERV_TARGET_STORAGE_SMALL:-}"
PORT_OPT=""
[[ -n "${HYPERV_TARGET_SHARE_PORT:-}" ]] && PORT_OPT="port=${HYPERV_TARGET_SHARE_PORT},"

# The commands come from the product. Retyping them here would prove that this script
# works, which is not the question.
MIGRATION_ID="verifytarget"
# shellcheck disable=SC2153  # every HYPERV_TARGET_* name is checked by require() above.
COMMANDS="$("$PYTHON" - <<PY
import sys
sys.path.insert(0, "$REPO")
from pegaprox.core import hyperv_transfer as t

mount_point = t.mount_point_for("$MIGRATION_ID")
credentials = mount_point + '.credentials'
source = t.source_file_path(mount_point, "$HYPERV_TARGET_FIXTURE")

# The runner writes the credentials file and creates the mount root in one command;
# see _open_target_node.
print('mkdir -p ' + t.MOUNT_ROOT + ' && ' + t.credentials_file_command(credentials))
print('---')
print(t.mount_command("$HYPERV_TARGET_SHARE_HOST", "$HYPERV_TARGET_SHARE_NAME",
                      mount_point, credentials).replace('-o ', '-o $PORT_OPT', 1))
print('---')
print(t.probe_command(source))
print('---')
print(source)
print('---')
print(t.unmount_command(mount_point, credentials))
PY
)"

CRED_CMD="$(sed -n '1p' <<<"$COMMANDS")"
MOUNT_CMD="$(sed -n '3p' <<<"$COMMANDS")"
PROBE_CMD="$(sed -n '5p' <<<"$COMMANDS")"
SOURCE_PATH="$(sed -n '7p' <<<"$COMMANDS")"
UNMOUNT_CMD="$(sed -n '9p' <<<"$COMMANDS")"

# pvesm alloc takes kibibytes and rounds down, so the runner rounds up. Same arithmetic.
SIZE_KIB=$(( (HYPERV_TARGET_VIRTUAL_BYTES + 1023) / 1024 ))

echo "Running the product's commands on the node..."
# The here-document is deliberately unquoted: the product's command strings are built above
# and have to be substituted here, on this side. Everything the node evaluates for itself is
# escaped below.
# shellcheck disable=SC2029,SC2087
ssh "${SSH_OPTS[@]}" "$HYPERV_TARGET_SSH" "${SUDO} bash -s" <<REMOTE
set -u
export PATH=\$PATH:/usr/sbin:/sbin
SOURCE='$SOURCE_PATH'
SIZE_KIB=$SIZE_KIB
VIRTUAL=$HYPERV_TARGET_VIRTUAL_BYTES

cat '$HYPERV_TARGET_CREDENTIALS' | sh -c '$CRED_CMD'
sh -c '$MOUNT_CMD'
echo "  source on the share:   \$(sh -c '$PROBE_CMD') bytes"

one_case () {
    storage="\$1"; vmid="\$2"; label="\$3"
    echo "=== \$label ==="
    out="\$(pvesm alloc "\$storage" "\$vmid" '' "\$SIZE_KIB" 2>&1)"
    volume="\$(printf '%s' "\$out" | sed -n "s/.*successfully created '\([^']*\)'.*/\1/p")"
    if [ -z "\$volume" ]; then
        echo "  pvesm alloc refused: \$(printf '%s' "\$out" | tail -2 | tr '\n' ' ')"
        return 1
    fi
    device="\$(pvesm path "\$volume" 2>&1 | tail -1)"
    echo "  volume:                \$volume"
    echo "  what pvesm path gives: \$device (block device: \$([ -b "\$device" ] && echo yes || echo no))"
    [ -f "\$device" ] && echo "  allocated before:      \$(du -k "\$device" | cut -f1) KiB of \$(du -k --apparent-size "\$device" | cut -f1) KiB apparent"

    start=\$(date +%s)
    ( rc=0
      qemu-img convert -p -f vhdx -O raw -t none -T none "\$SOURCE" "\$device" \
          >/tmp/verify-target.out 2>/tmp/verify-target.err || rc=\$?
      echo "\$rc" > /tmp/verify-target.rc ) &
    converter=\$!
    peak=0
    while kill -0 \$converter 2>/dev/null; do
        for pid in \$(pidof qemu-img 2>/dev/null || true); do
            hwm=\$(sed -n "s/^VmHWM:[[:space:]]*\([0-9]*\).*/\1/p" /proc/\$pid/status 2>/dev/null || echo 0)
            [ "\${hwm:-0}" -gt "\$peak" ] && peak=\$hwm
        done
        sleep 0.2
    done
    wait \$converter 2>/dev/null
    finish=\$(date +%s)
    rc="\$(cat /tmp/verify-target.rc 2>/dev/null || echo 'not recorded')"

    echo "  qemu-img exit code:    \$rc"
    [ "\$peak" = "0" ] && echo "  peak resident KiB:     not sampled (shorter than the interval)" \
                       || echo "  peak resident KiB:     \$peak"
    echo "  duration seconds:      \$(( finish - start ))"
    [ -f "\$device" ] && echo "  allocated after:       \$(du -k "\$device" | cut -f1) KiB of \$(du -k --apparent-size "\$device" | cut -f1) KiB apparent"

    if [ "\$rc" = "0" ]; then
        expected="\$(cut -d' ' -f1 "\$(dirname "\$SOURCE")/\$(basename "\$SOURCE" .vhdx).sha256")"
        actual="\$(head -c "\$VIRTUAL" "\$device" | sha256sum | cut -d' ' -f1)"
        if [ "\$expected" = "\$actual" ]; then
            echo "  content:               matches the fixture checksum"
        else
            echo "  content:               MISMATCH (\$actual)"
        fi
    else
        echo "  what it said:          \$(tail -c 200 /tmp/verify-target.err | tr '\n' ' ')"
    fi

    if pvesm free "\$volume" >/dev/null 2>&1; then
        echo "  volume freed"
    else
        echo "  WARNING: the volume was not freed: \$volume"
    fi
}

one_case '$HYPERV_TARGET_STORAGE' '$HYPERV_TARGET_VMID' 'A volume Proxmox allocated' || true
if [ -n '$SMALL' ]; then
    one_case '$SMALL' \$(( $HYPERV_TARGET_VMID + 1 )) 'A target too small to hold it' || true
fi

sh -c '$UNMOUNT_CMD'
echo "  share unmounted, credentials removed"
REMOTE

echo
echo "Measured. Record these in docs/hyperv-transfer.md rather than rounding them up."
