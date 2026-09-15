# Windows guests: what is proven, per version

A migrated Windows guest has to boot on hardware it has never seen. This is the matrix of
what has actually been measured, version by version, so that "it works" is never a claim
about a version nobody tried.

Rows are the Windows versions that occur in the estates this patch was built for, plus
Server 2025 as the version guests will be migrated *to* over the life of this code.

**Read the screen, not the log.** The injection reports success in the failing case too;
that is precisely how its defects survived for as long as they did. Every row below was
filled by reading the guest's screen.

## The driver values, checked statically across every version

Read from the INF files on the virtio-win ISO (DriverVer 07/22/2026,
100.103.104.30200). All thirteen variants the ISO ships — `2k8`, `2k8R2`, `2k12`,
`2k12R2`, `2k16`, `2k19`, `2k22`, `2k25`, `w7`, `w8`, `w8.1`, `w10`, `w11`:

| What | vioscsi | viostor |
|---|---|---|
| `Parameters\BusType` | **`0x0000000A`** in every variant | **`0x00000001`** in every variant |
| `DmaRemappingCompatible` | `0` | `0` |
| `StartType` | `SERVICE_BOOT_START` | `SERVICE_BOOT_START` |
| `LoadOrderGroup` | `SCSI miniport` | `SCSI miniport` |
| Hardware IDs | `PCI\VEN_1AF4&DEV_1004`, `PCI\VEN_1AF4&DEV_1048` | `PCI\VEN_1AF4&DEV_1001`, `PCI\VEN_1AF4&DEV_1042` |

**No version is an exception.** That is what makes the injection's values a per-driver
question rather than a per-Windows-version one.

## What has been booted

Each row: install the version unattended on SATA, snapshot the untouched image, run the
product's own injection against it, switch the disk to `scsihw: virtio-scsi-single`, boot,
read the screen. The snapshot is what makes the before/after columns comparable — for
2016, 2022 and 2025 both rounds start from the same bytes.

The Server 2012 R2 row is the exception: its two cells come from two different images,
because the first one had to be rebuilt (see below). They are still comparable, because
what the first column records for that row is a signature the loader refuses, which is a
property of the driver file and not of the image it is written into.

| Windows version | Build | Before the DriverDatabase fix | After it |
|---|---|---|---|
| Server 2012 R2 | 9600 | boot manager, `viostor.sys`, `0xc0000428` | **login screen after 1 min**, with the driver ISO named below |
| Server 2016 | 14393 | recovery environment | **login screen after 1 min, still there after 7** |
| Server 2022 | 20348 | recovery environment | **login screen after 1 min, still there after 7** |
| Server 2025 | 26100 | recovery environment | **login screen after 1 min, still there after 7** |

### Why the first column looked the way it did

Windows 8 and Server 2012 and everything after them stopped reading the
`CriticalDeviceDatabase`. They bind a boot device through `HKLM\SYSTEM\DriverDatabase`
instead. The injection wrote only the old database, so the loader loaded the driver and
the kernel then could not attach it to the controller it had just booted from. ADR 2 has
the measurement and the decision.

### Server 2012 R2 is a different case

Its driver is refused before any of that matters:

```
File:   \Windows\system32\drivers\viostor.sys
Status: 0xc0000428
Info:   The operating system couldn't be loaded because the digital signature
        of a file couldn't be verified.
```

virtio-win stopped having the drivers for out-of-support Windows versions signed through
Microsoft. Read off the files themselves:

| virtio-win release | signer of `viostor/2k12R2` |
|---|---|
| 0.1.190 | `Symantec Class 3 SHA256 Code Signing CA - G2` |
| 0.1.208 | `Symantec Class 3 SHA256 Code Signing CA - G2` |
| 0.1.221 and newer | `virtio-win / Red Hat Inc.` — self-signed |

**0.1.208 is the last release whose 2012 R2 drivers can be boot drivers.** Server 2016 and
newer are unaffected; theirs are signed by `Microsoft Windows Third Party Component CA
2014`.

The product checks this before it registers anything, so a guest in this situation is left
on the controller it arrived on — which boots — rather than on one it cannot start from.
ADR 3 has the decision. To put such a guest on VirtIO SCSI, point `virtio_iso_path` at
virtio-win 0.1.208 or older.

With virtio-win 0.1.208 the injection runs, the signature error is gone, and the guest
reaches its login screen on VirtIO SCSI a minute after starting — the same result as the
other three.

Getting there took a second image. The first Server 2012 R2 image built for this matrix
did not complete a boot even untouched: sixteen minutes on a spinner from its own
snapshot, with nothing injected into it and its disk on SATA. The cause was the answer
file, not the product — `shutdown /s /t 240 /f` cut the first-logon device installation
short on this version, where 2016, 2022 and 2025 survived it. Rebuilt with
`shutdown /s /t 900` and no `/f`, the image installs, shuts down and boots normally.

## Fast Startup, and what is and is not proven about it

Windows 8 and Server 2012 and everything after them can end a shutdown by writing the
kernel session to `hiberfil.sys` instead of ending it. The volume is then hibernated from
any other system's point of view, and a guest that starts from it resumes that session
rather than booting. Resuming it against a different chipset, timer and controller is not
something Windows supports, so every import clears it: the copied volume is mounted with
`ntfsfix` and `-o remove_hiberfile` before anything else touches it.

Both halves of the import do this — the one that installs VirtIO drivers and, since fork
issue #15, the one that does not. The compatible controller does not make the question go
away: it decides whether the loader can *read* the disk, not what a resumed kernel then
finds attached to it.

**Defaults, not measurements.** Hibernation is off by default on Windows Server (2012
through 2025) and on by default on Windows 10 and 11, and `powercfg /h` can have changed
either. Nothing here has read the setting on the guests in any particular estate.

### What `tests/hyperv_testbed/verify_hibernation_clear.sh` proves

Fifteen assertions against real `ntfs-3g`, on a volume carrying a `hiberfil.sys` with the
signature ntfs-3g decides from:

| Question | Answer |
|---|---|
| Is the hibernated state real? | Yes — `ntfsfix` says `Windows is hibernated, refused to mount` and exits 1; a plain `-o rw` mount silently falls back to read-only |
| Is `hiberfil.sys` gone afterwards? | Yes |
| Is anything else changed? | No — sha256 over every remaining file is identical, and the file count is unchanged |
| Is the filesystem sound afterwards? | Yes — `ntfsfix -n` clean, still mountable |
| A guest that was not hibernated? | Byte-identical; no `hiberfil.sys` is created |
| A Linux guest's ext4? | Never selected, byte-identical |

The run also shows why the option is not optional: without it the volume mounts read-only,
and the injection's own read-write check fails the run.

### What it does not prove

**That a guest which was hibernated boots after the file is cleared.** That needs Windows
and a screen. The four rows above were booted from images that had been shut down
normally, so the matrix does not cover this case for any version. It is the one open
question on this behaviour, and it is the same question the driver path has carried since
it started using the option.

When a guest is found in that state the import now says so in the migration log, naming
that the saved session was discarded — so a boot that then looks different has something
to be read against.

## Version-specific points that still need a boot to settle

**Server 2016 and 2019 with an old patch level.** A guest that has not been updated in a
long time is reported to bluescreen with `DXGKRNL_FATAL_ERROR` on a roughly eleven-minute
cycle, resolved by setting the CPU type to `kvm64`. This is a guest-side interaction with
the CPU model, not with the storage driver, so nothing the injection does would catch it —
but it decides whether a migrated 2016 guest is usable.

**Generation 1 versus Generation 2.** A Generation 1 source produces a seabios/`pc` target
and a Generation 2 source an OVMF/`q35` one. The rows above are seabios/`pc`. The firmware
path is independent of the driver values but has its own failure modes.

## How to fill a row

1. Install the version unattended, let the guest power itself off, snapshot it.
2. Run the injection against the untouched image.
3. Turn the recovery environment off in the guest's BCD first. Without that a failed boot
   opens "Choose your keyboard layout", which says a boot failed and nothing about why;
   with it, the boot manager prints the file and the status code. That single step is what
   turned this matrix from guesswork into measurement.
4. Switch the disk to `scsi0` with `scsihw: virtio-scsi-single` and boot.
5. Record what the screen says.
