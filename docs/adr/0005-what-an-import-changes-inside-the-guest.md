# ADR 0005 — What an import changes inside the guest, and what it reproduces

**Status:** accepted
**Date:** 2026-09-15
**Scope:** fork issue #15 (Hyper-V migration source)

## Context

Two things a migrated guest carries were being decided by the import rather than by the
source, and both were decided the same way for every guest.

**Secure Boot was switched off for everyone.** `_attach_volumes` created the UEFI variable
store as `efidisk0=<storage>:1,efitype=4m,pre-enrolled-keys=0`, hard-coded. A Generation 2
guest that had been running under Hyper-V's standard "Microsoft Windows" Secure Boot
template arrived with an empty store, which means Secure Boot is off. The preflight
reported that as a risk to accept by name, per VM, forever — a warning about a decision
the product itself had made and could just as well make differently.

Proxmox ships the other store. `pre-enrolled-keys=1` gives OVMF Microsoft's certificates
already enrolled, which is the same set the Hyper-V template holds.

**A hibernated volume was only handled on one of the two import paths.** Windows 8 and
Server 2012 and later can end a shutdown by writing the kernel session to `hiberfil.sys`
rather than ending it (Fast Startup). A guest that starts from such a volume resumes that
session instead of booting, and resuming it against a different chipset, timer and disk
controller is not something Windows supports. The VirtIO path already cleared it, because
`v2p._inject_virtio_drivers` mounts the volume with `ntfsfix` and `-o remove_hiberfile` on
its way in. The compatible path (`hardware != 'virtio'`, fork issue #40) never mounts the
volume at all, so it cleared nothing.

The compatible controller is not an answer to this. It decides whether the loader can
*read* the disk; it says nothing about what a resumed kernel finds attached to it.

## Decision

**Secure Boot follows the source.** `pre-enrolled-keys` is `1` when the source reports
`secure_boot_enabled`, `0` otherwise, and the run logs which it chose. `check_secure_boot`
reports OK rather than a warning, and `secure_boot` leaves `_ACKNOWLEDGEABLE_CHECKS`.

The direction matters both ways. A guest that had Secure Boot **off** must not get keys
enrolled: its bootloader or one of its drivers may be unsigned, and enrolling would stop
it booting at all. That is why this is read from the source rather than offered as a
choice.

What is still not carried across is a **custom** Secure Boot template. Nothing on this
side can read which certificates it held, so such a guest needs its own enrolled on the
target. The finding says so.

**Every import clears a hibernation file.** `_inject_virtio_drivers` grows a
`clear_hibernation_only` mode that runs its first half alone — resolve the disk, find the
Windows volume, `ntfsfix`, mount with `remove_hiberfile` — and stops. The compatible path
calls it. The mode skips the ISO lookup and the hivex packages, because it needs neither,
so a node without `virtio-win.iso` is not a failure for it.

When the volume was hibernated, the run says so in the migration log: the saved session is
discarded, the guest will boot cold, and anything that was open in that session is gone.
Discarding it is right; doing it silently is not.

Failing to prepare the volume never fails the migration. The disks are already copied by
then, and throwing away a finished transfer over a preparation step would discard the
expensive half of the run.

## What was measured

`tests/hyperv_testbed/verify_hibernation_clear.sh` — fifteen assertions against real
`ntfs-3g` in a privileged container, on a volume carrying a `hiberfil.sys` with the
signature ntfs-3g decides from:

- the hibernated state is real: `ntfsfix` answers `Windows is hibernated, refused to
  mount` and exits 1, and a plain `-o rw` mount falls back to read-only
- `hiberfil.sys` is gone afterwards
- every other file is byte-identical by sha256, and the file count is unchanged
- `ntfsfix -n` is clean afterwards and the volume still mounts
- a volume with no `hiberfil.sys` comes out byte-identical, and none is created
- an ext4 volume is never selected and is byte-identical

That run also shows the option is not optional: without it the volume mounts read-only and
the injection's own read-write check fails the run.

**Not measured: that a guest which was hibernated boots after the file is cleared.** That
needs Windows and a screen. The four rows in `hyperv-windows-matrix.md` were booted from
images shut down normally, so no version covers this case yet. It is the same open
question the driver path has carried since it started using the option; this ADR does not
close it, it only stops the compatible path from being worse than the driver path.

## Consequences

- A Generation 2 Windows guest keeps Secure Boot across the migration instead of quietly
  losing it, and the wizard asks for one confirmation fewer per VM.
- A guest that had Secure Boot off is unaffected, by construction.
- Both import paths leave a bootable volume behind rather than one path leaving a resumed
  session pointed at hardware that no longer exists.
- `pegaprox/core/v2p.py` gains a parameter and three small blocks. It is shared with the
  VMware direction, whose behaviour is unchanged: the parameter defaults to False and
  every new block is behind it, except the hibernation log line, which is an observation
  both directions benefit from.
