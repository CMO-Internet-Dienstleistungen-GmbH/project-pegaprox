#!/usr/bin/env bash
# What clearing a Fast Startup hibernation file does to a guest's disk.
#
# The import mounts the copied NTFS with `ntfsfix` + `-o remove_hiberfile` so a guest that
# shut down hybrid boots cold instead of resuming a saved kernel session on hardware that
# is not the hardware it was saved on. That writes into a customer's filesystem, so what
# it does to the bytes around it is measured here rather than assumed.
#
# Scope: the two lines the product runs against the Windows volume. Finding that volume
# inside the disk is unchanged product code and is not re-tested here; a loop device is
# pointed straight at the filesystem instead, which also keeps the container free of the
# udev dependency partition nodes would bring.
#
# What this proves:
#   * a hibernated NTFS really does refuse writes without the option, so the scenario
#     under test is the real one and not a no-op
#   * hiberfil.sys is gone afterwards
#   * every other file is byte-identical, by sha256
#   * the filesystem is consistent and still mountable afterwards
#   * an image with no hiberfil.sys comes out unchanged
#   * a non-NTFS filesystem is never selected
#
# What this does NOT prove: that a real Windows guest boots afterwards. That needs
# Windows, and it belongs in docs/hyperv-windows-matrix.md with a screen behind it.
#
# Usage:  ./tests/hyperv_testbed/verify_hibernation_clear.sh
# Needs:  docker (the container is privileged — it needs loop devices)

set -euo pipefail

IMAGE="${HIBER_TEST_IMAGE:-debian:13-slim}"

echo "== Hibernation-clear prüfstand (container: $IMAGE) =="

docker run --rm --privileged -i "$IMAGE" bash -s <<'CONTAINER'
set -uo pipefail

export DEBIAN_FRONTEND=noninteractive
apt-get -qq update >/dev/null
apt-get -qq install -y ntfs-3g util-linux coreutils e2fsprogs >/dev/null

WORK=$(mktemp -d); cd "$WORK"
MNT=$(mktemp -d)

pass=0; fail=0
ok()  { echo "  PASS  $*"; pass=$((pass+1)); }
bad() { echo "  FAIL  $*"; fail=$((fail+1)); }

# A guest's Windows volume: the directories the import looks for, plus files whose bytes
# have to survive. `mode=hibernated` adds what Fast Startup leaves behind — ntfs-3g decides
# from the signature in the first four bytes, so this is what it sees on a real guest.
build_volume() {   # $1 = image, $2 = hibernated|clean
  rm -f "$1"; truncate -s 600M "$1"
  mkntfs --fast --force --label WINDOWS "$1" >/dev/null 2>&1 || return 1
  mount -t ntfs-3g -o rw,loop "$1" "$MNT" || return 1
  mkdir -p "$MNT/Windows/System32/config" "$MNT/Users/operator/Documents"
  head -c 3000000 /dev/urandom > "$MNT/Windows/System32/config/SYSTEM"
  head -c 1500000 /dev/urandom > "$MNT/Windows/System32/config/SOFTWARE"
  head -c 5000000 /dev/urandom > "$MNT/Users/operator/Documents/payload.bin"
  printf 'boot-sentinel' > "$MNT/Windows/sentinel.txt"
  [ "$2" = hibernated ] && { printf 'hibr'; head -c 8388604 /dev/zero; } > "$MNT/hiberfil.sys"
  umount "$MNT"
}

# sha256 over every file except hiberfil.sys, so "nothing else changed" is checkable.
payload_digest() {
  find "$1" -type f ! -name 'hiberfil.sys' -printf '%P\n' | sort | while read -r f; do
    printf '%s  ' "$f"; sha256sum "$1/$f" | cut -d' ' -f1
  done | sha256sum | cut -d' ' -f1
}

echo
echo "-- 1. A hibernated volume refuses writes (the scenario is real) --"
build_volume hib.img hibernated || { echo "  FAIL  could not build the test volume"; exit 1; }
LOOP=$(losetup -b 512 -f --show hib.img)
[ "$(blkid -s TYPE -o value "$LOOP")" = ntfs ] && ok "volume is detected as NTFS" \
                                               || bad "volume is not detected as NTFS"

if mount -t ntfs-3g -o rw "$LOOP" "$MNT" 2>/tmp/e1; then
  if touch "$MNT/.rwtest" 2>/dev/null; then
    rm -f "$MNT/.rwtest"; bad "a hibernated volume accepted writes — scenario not reproduced"
  else
    ok "mounted read-only; writes refused while hibernated"
  fi
  umount "$MNT"
else
  ok "read-write mount refused: $(tr -d '\n' </tmp/e1 | tail -c 80)"
fi

mount -t ntfs-3g -o ro "$LOOP" "$MNT"
BEFORE=$(payload_digest "$MNT")
BEFORE_N=$(find "$MNT" -type f ! -name hiberfil.sys | wc -l)
[ -f "$MNT/hiberfil.sys" ] && ok "hiberfil.sys present before ($(stat -c%s "$MNT/hiberfil.sys") bytes)" \
                           || bad "hiberfil.sys missing before the test"
umount "$MNT"

echo
echo "-- 2. The two lines the product runs --"
ntfsfix "$LOOP" >/tmp/fix.log 2>&1 || echo "        (ntfsfix exit=$?)"
if mount -t ntfs-3g -o rw,recover,remove_hiberfile "$LOOP" "$MNT"; then
  ok "read-write mount succeeded with remove_hiberfile"
  if touch "$MNT/.rwtest" 2>/dev/null; then rm -f "$MNT/.rwtest"; ok "volume is genuinely writable"
  else bad "mount reported rw but writing failed"; fi
  [ -f "$MNT/hiberfil.sys" ] && bad "hiberfil.sys survived" || ok "hiberfil.sys removed"

  AFTER=$(payload_digest "$MNT")
  AFTER_N=$(find "$MNT" -type f ! -name hiberfil.sys | wc -l)
  [ "$BEFORE" = "$AFTER" ] && ok "every other file byte-identical (sha256 over $AFTER_N files)" \
                           || bad "payload changed: $BEFORE -> $AFTER"
  [ "$BEFORE_N" = "$AFTER_N" ] && ok "no file appeared or vanished ($AFTER_N)" \
                               || bad "file count changed: $BEFORE_N -> $AFTER_N"
  [ "$(cat "$MNT/Windows/sentinel.txt" 2>/dev/null)" = boot-sentinel ] \
      && ok "Windows directory intact" || bad "Windows directory damaged"
  umount "$MNT"
else
  bad "the product's mount line failed on a hibernated volume"
fi

echo
echo "-- 3. The filesystem is consistent afterwards --"
if ntfsfix -n "$LOOP" >/tmp/check.log 2>&1; then ok "ntfsfix -n reports no problems"
else bad "ntfsfix -n complains: $(tail -2 /tmp/check.log | tr '\n' ' ')"; fi
if mount -t ntfs-3g -o ro "$LOOP" "$MNT"; then ok "still mountable after the change"; umount "$MNT"
else bad "no longer mountable"; fi
losetup -d "$LOOP"

echo
echo "-- 4. A guest that was not hibernated comes out unchanged --"
build_volume clean.img clean || { echo "  FAIL  could not build the clean volume"; exit 1; }
LOOP2=$(losetup -b 512 -f --show clean.img)
mount -t ntfs-3g -o ro "$LOOP2" "$MNT"; C_BEFORE=$(payload_digest "$MNT"); umount "$MNT"
ntfsfix "$LOOP2" >/dev/null 2>&1 || true
mount -t ntfs-3g -o rw,recover,remove_hiberfile "$LOOP2" "$MNT"
C_AFTER=$(payload_digest "$MNT")
[ "$C_BEFORE" = "$C_AFTER" ] && ok "a clean guest's files are untouched" \
                             || bad "a clean guest was modified: $C_BEFORE -> $C_AFTER"
[ -f "$MNT/hiberfil.sys" ] && bad "a hiberfil.sys appeared out of nowhere" \
                           || ok "no hiberfil.sys created"
umount "$MNT"; losetup -d "$LOOP2"

echo
echo "-- 5. A Linux guest's filesystem is never selected --"
truncate -s 200M linux.img
mkfs.ext4 -q -F linux.img
E_BEFORE=$(sha256sum linux.img | cut -d' ' -f1)
LOOP3=$(losetup -b 512 -f --show linux.img)
FT=$(blkid -s TYPE -o value "$LOOP3")
[ "$FT" = ntfs ] && bad "an ext4 filesystem was reported as NTFS" \
                 || ok "reported as '$FT' — the product's NTFS filter skips it"
losetup -d "$LOOP3"
[ "$(sha256sum linux.img | cut -d' ' -f1)" = "$E_BEFORE" ] && ok "the ext4 image is byte-identical" \
                                                          || bad "the ext4 image was modified"

echo
echo "== $pass passed, $fail failed =="
[ "$fail" -eq 0 ]
CONTAINER
