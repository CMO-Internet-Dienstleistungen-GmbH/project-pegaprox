# 2. A boot driver is registered in the DriverDatabase, not only the CriticalDeviceDatabase

Date: 2026-09-15

## Status

Accepted.

## Context

A Hyper-V guest arrives on Proxmox with its disk on hardware it has never seen. For it to
boot on VirtIO SCSI without anyone touching it, the storage driver has to be in place
before the first start: the file copied in, the service registered as boot-start, and the
controller bound to that service.

The injection did the first two and, for the third, wrote
`HKLM\SYSTEM\<ControlSet>\Control\CriticalDeviceDatabase`. That is the mechanism described
in every guide on the subject, and it is the one the VMware direction of this product has
used for as long as it has existed.

It does not work on any Windows version a customer is likely to be migrating.

Measured on freshly installed guests, each injected by the product itself and then started
with its system disk on `virtio-scsi-single`:

| Guest | Result |
|---|---|
| Windows Server 2012 R2 | boot manager: `viostor.sys`, `0xc0000428` |
| Windows Server 2016 | Windows logo, then the recovery environment |
| Windows Server 2022 | Windows logo, then the recovery environment |
| Windows Server 2025 | Windows logo, then the recovery environment |

The service entry, the driver file, its catalogue and the CriticalDeviceDatabase entries
were verified present and correct in the hive of each image before the boot. On an image
where Windows had been allowed to install the driver itself — by booting it from SATA with
a second disk on a VirtIO SCSI controller — the same guest then booted from VirtIO SCSI.
Removing the `Enum` and `Class` entries that installation had created did **not** stop it
booting, so those were not what made the difference either.

The explanation is in libguestfs, which is the implementation this behaviour was read off:

> Windows >= 8 doesn't use the CriticalDeviceDatabase. Instead one must add keys into the
> DriverDatabase.

Windows 8 and Server 2012 replaced the CriticalDeviceDatabase with
`HKLM\SYSTEM\DriverDatabase`. The loader still reads the service entry and loads the
driver; what no longer happens is the binding of the controller to that driver, so the
kernel starts and then cannot reach the disk it started from.

## Decision

The injection registers a boot-critical storage driver in **both** databases:

- `HKLM\SYSTEM\DriverDatabase\DriverInfFiles\pegaprox_<driver>.inf`, `\DriverPackages\…`,
  `\DeviceIds\PCI\…`, in the shape libguestfs writes them, whenever the branch exists.
- `Control\CriticalDeviceDatabase` as before, in every control set.

Both, rather than one or the other, because the old entries are what a Windows 7 or
Server 2008 R2 guest still needs and a newer guest simply does not read them.

The package is named `pegaprox_<driver>.inf` rather than after the driver, so it cannot
collide with one the guest already has. A Server 2025 image was found carrying a virtio
catalogue of its own; an entry under the driver's real INF name would replace whatever that
belongs to with something assembled here. libguestfs names its package the same way, for
the same reason.

The device IDs follow what the driver's own INF declares, both the transitional device
(`REV_00`) and the modern one (`REV_01`), because which of the two a guest is given depends
on the machine type the target VM was built with.

## Consequences

- The offline injection can produce a guest that boots on VirtIO SCSI with no manual step.
  Before this it could not, on any version in the matrix.
- The change is in `v2p.py` and therefore applies to the VMware direction too, which had
  the same defect. Upstream had switched its automatic controller change off because of
  it, describing the symptom and not finding the cause.
- Windows Server 2012 R2 is still not solved by this alone; its driver carries a signature
  the loader refuses. That is a separate decision, recorded in ADR 3.
