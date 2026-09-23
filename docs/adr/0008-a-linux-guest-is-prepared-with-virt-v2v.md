# ADR 0008 — A Linux guest is prepared for VirtIO with virt-v2v

**Status:** accepted
**Date:** 2026-09-23
**Scope:** fork issue #15 (Hyper-V migration source)

## Context

The import offered one checkbox: inject the VirtIO drivers and build the VM on VirtIO, or
build it on the compatible controller. The injection is the Windows registry injection
shared with the VMware direction. A Linux guest that was ticked went through it, found no
NTFS partition, and the run moved the VM to SATA.

Measured on a CentOS 7.7 guest (kernel 3.10): after that move the guest no longer found its
boot partition. Its initramfs had been built on Hyper-V and loads `hv_storvsc` and nothing
else. The VirtIO modules are in its kernel tree, and SATA is no better: neither is in the
image the kernel boots from. What such a guest needs is its own initramfs rebuilt with its
own tools — not a driver copied in from outside.

## Options

1. **Rebuild the initramfs ourselves**, with `virt-customize` and a dracut call per
   distribution. Every distribution family has its own tool (dracut, mkinitrd,
   update-initramfs), its own way to name the default kernel, and its own device naming.
   That is several hundred lines of distribution knowledge to write and maintain.
2. **chroot into the guest on the node.** Needs no package, but activates the guest's
   volume groups on the host, where a guest's `centos` or `ubuntu-vg` sits beside the
   node's own and LVM on a PVE node is shared state.
3. **`virt-v2v-in-place` on the copied disk.** libguestfs' own conversion: picks the kernel,
   rebuilds the initramfs with the VirtIO modules for every distribution virt-v2v supports
   (RHEL/CentOS 4–10, Debian, Ubuntu, SLES, …), adjusts the boot loader and device names,
   relabels for SELinux, all inside an appliance so nothing is activated on the node.

## Decision

Option 3. The wizard offers the preparation as a choice — No, Windows, Linux — preselected
from the OS type the disks show and moved along whenever the OS type field changes. Linux
runs `virt-v2v-in-place --block-driver virtio-scsi` on the target volume after the copy and
before anything starts the VM; one disk goes in as `-i disk`, several as `-i libvirtxml`.
The conversion also empties `BLACKLIST_RPC` / `FILTER_RPC_ARGS` in
`/etc/sysconfig/qemu-ga`, because every guest in this estate runs with `guest-exec`
available and RHEL-family packages switch it off.

A failed Linux conversion is **not** moved to SATA: SATA does not help a guest whose
initramfs lacks both, and virt-v2v documents a failed in-place run as leaving the disk "in
an unknown, possibly corrupted state". The VM stays on VirtIO, is not started, and the run
completes with errors that name the cause.

## Measured

- CentOS 7.7, 150 GiB, Ceph RBD on PVE 9.2: conversion 191 s; the VM booted to its login
  prompt on virtio-scsi; a second run on the converted disk (with the guest-agent edit)
  took 173 s and changed nothing else; the agent then reported no command disabled.
- On an HDD-backed Ceph pool virt-v2v's unconditional `fstrim` discarded at about 6 MB/s
  and was still running after 26 minutes on the same guest. The volume is therefore mapped
  with `rbd map -o notrim`; measured on a Debian 12 guest, the trim step then takes under a
  second and the conversion exits 0.
- `apt-get install --no-install-recommends virt-v2v libguestfs-xfs` beside `pve-qemu-kvm`:
  nothing removed or upgraded. `mdadm` comes with it and rebuilds the node's initramfs and
  grub configuration once — see `docs/hyperv-target-node-requirements.md`.

## Consequences

- The node needs virt-v2v for a Linux migration. PegaProx installs it when missing; a fleet
  is better given it through configuration management because of the `mdadm` step.
- Linux is recognised before the copy from partition types (Linux data, LVM, swap, RAID),
  which the disk inspection on the Hyper-V host now reads. Windows is still read from the
  image itself.
- Not covered: network naming. A guest whose `ifcfg` pins `HWADDR`, or whose interface
  name changes from `hv_netvsc` to `virtio_net`, may come up without its network; virt-v2v
  does not change that either.
- `guest-exec` on a RHEL-family guest with SELinux enforcing runs confined to
  `virt_qemu_ga_t`, which cannot read much of the system. Lifting that is left open.
