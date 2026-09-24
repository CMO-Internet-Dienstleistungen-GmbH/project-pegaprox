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

On a guest with SELinux configured (enforcing or permissive), the conversion then marks the
agent's domain `virt_qemu_ga_t` permissive, as a CIL module `pegaprox_qemu_ga_permissive`
containing `(typepermissive virt_qemu_ga_t)`, installed with `semodule -i`. Confined, the
agent answers `guest-exec` but may not run `ip`, write under `/etc` or read
`/etc/selinux/config` -- the work guest-exec is kept for. Permissive for that one domain
gives it the rights it has on every guest without SELinux; the rest of the guest stays
enforcing, and its denials are still logged. It is the module `semanage permissive -a`
writes, installed through `semodule` because `semanage` is missing on a minimal RHEL 7.
`semodule -r pegaprox_qemu_ga_permissive` takes it out again. A guest with SELinux disabled
or without a configuration is left alone. Both edits run before virt-v2v's own relabel.

The alternative, a module granting the agent only the rights it needs, was not chosen:
that list would have to be written for RHEL 7, 8 and later policy versions and kept in step
with whatever guest-exec is used for.

A failed Linux conversion is **not** moved to SATA: SATA does not help a guest whose
initramfs lacks both, and virt-v2v documents a failed in-place run as leaving the disk "in
an unknown, possibly corrupted state". The VM stays on VirtIO, is not started, and the run
completes with errors that name the cause. A failed SELinux step is one of those failures,
and is named as such, with `semodule`'s own message in the migration log.

## Measured

- CentOS 7.7, 150 GiB, Ceph RBD on PVE 9.2: conversion 191 s; the VM booted to its login
  prompt on virtio-scsi; a second run on the converted disk (with the guest-agent edit)
  took 173 s and changed nothing else; the agent then reported no command disabled.
- On an HDD-backed Ceph pool virt-v2v's unconditional `fstrim` discarded at about 6 MB/s
  and was still running after 26 minutes on the same guest. The volume is therefore mapped
  with `rbd map -o notrim`; measured on a Debian 12 guest, the trim step then takes under a
  second and the conversion exits 0.
- SELinux step, virt-v2v-in-place 2.6.0 on PVE 9.2, CentOS 7.9 (GenericCloud 2009) and
  Rocky Linux 8.10 (GenericCloud), both enforcing: `semodule -i` inside the appliance took
  10 s and 23 s, conversion exit 0 on both. Without the step, `guest-exec` on both guests
  ran as `virt_qemu_ga_t` and was refused `/usr/sbin/ip` (Permission denied), `touch
  /etc/…` and reading `/etc/selinux/config`. With it, both still reported `Enforcing` and
  the agent still ran as `virt_qemu_ga_t`, and all three succeeded. A deliberately failing
  `semodule` made virt-v2v stop with exit 1 and the error line naming the command.
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
- Every Linux guest converted here carries `pegaprox_qemu_ga_permissive` where SELinux is
  configured: its guest agent is not confined by SELinux. Anyone who can run `guest-exec`
  on the Proxmox side can do in the guest what root can. That is the rights the agent has
  on every guest without SELinux, and it is what this estate uses guest-exec for.
- Only the Linux preparation installs it. A Windows guest, and a guest imported with
  `No`, are not touched.
