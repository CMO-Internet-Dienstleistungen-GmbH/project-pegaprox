# ADR 0010 — The adapters keep their names, pinned to their MACs

**Status:** accepted
**Date:** 2026-10-10
**Scope:** fork issue #15 (Hyper-V migration source)

## Context

On Hyper-V a Linux guest's adapters are netvsc devices, which the kernel names `eth0`,
`eth1`, … The guest's configuration names them so — netplan, ifupdown, ifcfg,
NetworkManager, networkd, wicked — and so may firewall rules and services. After the
import the same adapter, with the same MAC (the migration carries the MACs over), is a
VirtIO PCI device, and udev gives it a predictable name: `ens18`. Nothing applies the
configuration written for `eth0` to it. The guest boots without network.

The guest agent (ADR 0009) makes it reachable, so the addresses could be set afterwards
through `guest-exec`. But the addresses are already in the guest: the requirement is that
they apply unchanged, to the adapter with the same MAC, without anyone typing them in
again.

## Options

1. **Rewrite the configuration** to the new names, in every format it may be in. Each
   format has its own syntax and its own places (netplan YAML with `match`/`set-name`,
   NetworkManager keyfiles and ifcfg, ifupdown stanzas, wicked, networkd), and names in
   firewall rules and service files stay wrong.
2. **Turn predictable names off** (`net.ifnames=0` on the kernel command line). The kernel
   then hands out `eth<n>` in probe order, which is not tied to the MAC: with more than
   one adapter the order can differ from Hyper-V's, and every boot loader and every
   family writes the command line differently.
3. **Pin the old name to the MAC** with a systemd `.link` file, which udev applies on
   every distribution this migration supports, and leave the configuration as it is.

## Decision

Option 3, as the last `--run-command` of the conversion, with the VM's MACs as Proxmox
holds them after the import:

1. **Which name belongs to which MAC** is taken only from what the guest records: ifcfg
   `HWADDR`, netplan `match: macaddress`, NetworkManager `mac-address`, networkd
   `MACAddress=`, `70-persistent-net.rules`. With a single adapter, the one name the
   configuration uses is enough. Anything else — two adapters and no record, two names for
   one adapter, one name for two — is reported and left alone. Nothing is guessed: a wrong
   pin puts one network's addresses on another network's adapter.
2. A name in the kernel's own eth namespace is pinned only where the kernel would hand it
   out to the same adapter anyway (`eth<n>` for the n-th adapter). Renaming one adapter
   onto a name another one still holds fails half-way, and each adapter ends up with
   neither name.
3. The `.link` file matches the MAC **and** the drivers of the NIC models a migration
   creates (`virtio_net`, `e1000`), so a VLAN or bridge carrying the same MAC is not
   renamed. On RHEL 7, whose initscripts rename by ifcfg `HWADDR`, that is added where it
   is missing.
4. On Debian and Ubuntu the initramfs names the adapter before the root file system is
   mounted, so it is rebuilt (`update-initramfs -u -k all`) and carries the file.
5. **cloud-init's network rendering is switched off** where it is on
   (`/etc/cloud/cloud.cfg.d/99-pegaprox-network.cfg`). Its datasource changed with the
   hypervisor, so it treats the first boot as a new instance and writes its fallback
   configuration — DHCP on the first adapter — over the guest's own.

The step always exits 0 and reports one line per adapter, `PEGAPROX_NET <mac> pinned
<name> (<why>)` or `PEGAPROX_NET <mac> unassigned: <why>`. The log shows every line. An
adapter left unassigned, or a missing report, ends the migration *completed with
errors*; the VM is still started. A guest whose configuration names no adapter at all
(DHCP on whatever comes up, as NetworkManager does by default) is reported but is not an
error: there is nothing to carry over.

## Consequences

- The guest's configuration is not touched. Its addresses, routes and DNS apply to the
  adapter with the same MAC, on the first boot.
- A guest that names its adapters in no file this step reads, and has more than one, is
  left unpinned and the migration reports it; the names are then set on the console or
  through the agent.
- Changing a VM's NIC model to one whose driver is not in the `.link` file after the
  migration loses the pin: the adapter comes up under its predictable name again.
- cloud-init stops rendering the network in that guest. Its other modules keep running.

## Measured

virt-v2v-in-place 2.6.0 and libguestfs 1.54.1 on PVE 9.2. Each guest is the distributor's
cloud image, configured beforehand as a Hyper-V guest is (static address on `eth0` in its
own network stack, predictable names not disabled), imported with a fixed MAC onto an
isolated bridge, converted by the script `conversion_script()` builds, booted, asked
through the agent which name, MAC, address and default route each adapter has, rebooted
and asked again.

| Guest | Stack | Pinned | First boot | After reboot |
|---|---|---|---|---|
| Ubuntu 18.04 | netplan | `eth0` (only name) | `eth0`, address, route | same |
| Ubuntu 20.04 | netplan | `eth0` (only name) | `eth0`, address, route | same |
| Ubuntu 22.04 | netplan | `eth0` (only name) | `eth0`, address, route | same |
| Ubuntu 24.04 | netplan | `eth0` (only name) | `eth0`, address, route | same |
| Debian 10 | ifupdown | `eth0` (only name) | `eth0`, address, route | same |
| Debian 11 | ifupdown | `eth0` (only name) | `eth0`, address, route | same |
| Debian 12 | netplan | `eth0` (only name) | `eth0`, address, route | same |
| CentOS 7.9 | ifcfg | `eth0` (only name), `HWADDR` added | `eth0`, address, route¹ | same |
| CentOS 8.4 | ifcfg | `eth0` (only name) | relabel reboot (see below) | `eth0`, address, route |
| CentOS Stream 8 | ifcfg | `eth0` (only name) | relabel reboot | `eth0`, address, route |
| Rocky Linux 8.10² | ifcfg | `eth0` (only name) | relabel reboot, then `eth0`, address, route | same |
| Rocky Linux 9.8 | NetworkManager keyfile | `eth0` (only name) | `eth0`, address, route | same |
| AlmaLinux 9.8 | NetworkManager keyfile | `eth0` (only name) | `eth0`, address, route | same |
| openSUSE Leap 15.6 | wicked | `eth0` (only name) | `eth0`, address, route | same |
| Rocky Linux 8.10², two adapters | ifcfg with `HWADDR` | `eth0` and `eth1` (recorded MACs) | both addresses, route on `eth0` | same |
| Ubuntu 20.04, without the step | netplan | — | `ens18`, no address | same |

¹ Measured with the first version of the `.link` file (`Type=ether`); with the driver
match the first boot of that run hit a start timeout of the test node, the reboot
showed `eth0` with address and route.
² The Rocky Linux 8 image kept `net.ifnames=0` in its boot entries despite the
preparation, so the kernel named the adapters `eth0`/`eth1` itself and the pin did not
have to rename. Renaming on EL 8 is shown by CentOS 8.4 and Stream 8, where the adapter
was renamed to `eth0` (`name_assign_type` 4).

- The first version of the `.link` file matched `Type=ether`. On Ubuntu 18.04 (systemd 237)
  and Debian 10 (systemd 241) the adapter stayed `ens18`. On Ubuntu 18.04 `udevadm
  test-builtin net_setup_link` parsed the file and applied `99-default.link`: systemd
  before 246 compares `Type=` with the device's `DEVTYPE`, which Ethernet leaves unset. Matching the
  driver instead, the file applied on every guest above. Both releases' initramfs hooks
  copy `/etc/systemd/network/*.link`; the hook was not the cause.
- Where Ubuntu 22.04's `update-initramfs` failed in the appliance, the adapter was renamed
  once the root file system was up, and the configuration applied all the same.
- On the RHEL family virt-v2v touches `/.autorelabel`, so the first boot relabels the file
  system and reboots by itself within about a minute. An agent that answered right after
  the start can be gone for that minute, and a probe right after the reboot can come
  before NetworkManager has applied the addresses (seen once on Rocky Linux 8.10, the
  addresses were there 30 s later). It is virt-v2v's behaviour, not this step's, and it is
  why whatever talks to a freshly imported guest has to retry (ADR 0009).
- cloud-init's network rendering was switched off on every cloud image; the guest kept
  its static configuration instead of a DHCP fallback on the first boot.
