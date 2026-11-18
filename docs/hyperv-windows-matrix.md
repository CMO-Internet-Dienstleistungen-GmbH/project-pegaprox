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

### Reproduced by the testbed, 2026-09-15

Every row below was produced by one command against a pristine snapshot, with the
product's own `v2p._inject_virtio_drivers` and the commit under test verified present on
the machine that ran it. Nothing was typed by hand and nothing was a copy of the product.

| Windows version | Build | Injection | Screen after 7 minutes |
|---|---|---|---|
| Server 2016 | 14393 | `INJECTION_OK` | **login screen** (`LogonUI.exe`) |
| Server 2022 | 20348 | `INJECTION_OK` | **login screen** (`LogonUI.exe`) |
| Server 2025 | 26100 | `INJECTION_OK` | **login screen** (`LogonUI.exe`) |
| Server 2012 R2 | 9600 | refused: `BOOT_SIGNATURE_MISSING viostor`, `vioscsi` | left on SATA — but that row was run with the *current* virtio-win, which is the wrong release for this version |

**The 2012 R2 cell above is not this version's result.** It is what the product does when
it is handed a driver release that has no usable 2012 R2 variant: it refuses to register
the driver and leaves the guest on a controller that boots. Given the release this version
needs, the same image reaches VirtIO SCSI like every other row — see the next section.

### Server 2012 R2 with the release it needs, 2026-09-16

| Windows version | Build | Driver ISO | Injection | Screen |
|---|---|---|---|---|
| Server 2012 R2 | 9600 | virtio-win **0.1.189** | `INJECTION_OK`; `viostor`, `vioscsi`, `NetKVM`, `Balloon`, `pvpanic`, `vioserial` and `viorng` copied from `2k12R2/amd64` | **login screen after 1 minute**, still there after 7 |

Same command as every other row, same pristine snapshot, one argument different:

```
run.sh matrix --only 2012r2 --driver-iso <path to virtio-win-0.1.189.iso>
```

### The earlier hand-run rounds

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
| 0.1.189 | chains to `Microsoft Code Verification Root` |
| 0.1.190 | `Symantec Class 3 SHA256 Code Signing CA - G2` |
| 0.1.208 | `Symantec Class 3 SHA256 Code Signing CA - G2` |
| 0.1.221 and newer | `virtio-win / Red Hat Inc.` — self-signed |

0.1.208 is the last release whose 2012 R2 drivers carry a cross-certificate at all. Server
2016 and newer are unaffected; theirs are signed by `Microsoft Windows Third Party
Component CA 2014`.

**Use virtio-win 0.1.189 for Server 2012 R2.** It is the release this version is run with,
and the one the row above was measured against: the 2012 R2 drivers in it are dated
2020-08-10 and their PE certificate table chains to `Microsoft Code Verification Root` —
one of the three signers the injection accepts, read from the file rather than assumed.
Later releases that still carry a cross-certificate are not an improvement on it for this
version, and picking the newest one that happens to pass is how a guest ends up on a driver
nobody has booted.

The product checks the signature before it registers anything, so a guest handed an
unusable release is left on the controller it arrived on — which boots — rather than on one
it cannot start from. ADR 3 has the decision.

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

### Does Windows Server have Fast Startup at all?

Yes, and it is off by default. Read out of a guest with `powercfg /a` on Windows Server
2016 (build 14393), on an untouched installation and then again after enabling
hibernation:

| | `Hibernate` | `Fast Startup` |
|---|---|---|
| untouched | not available — *"Hibernation has not been enabled."* | not available — *"Hibernation is not available."* |
| after `powercfg /hibernate on` | **available** | **available** |

So the mechanism is present on Server; it is simply invisible while hibernation is off,
and the reason `powercfg` gives for Fast Startup being unavailable is the hibernation
setting, not the edition. On Windows 10 and 11 hibernation is on by default, which is why
Fast Startup is the normal state there and the exception here.

**What that means for a migration source.** A Windows Server guest arrives with no
`hiberfil.sys` unless somebody enabled hibernation on it. And a Hyper-V host's own
`AutomaticStopAction: Save` is a different thing entirely: it writes the saved state
*beside* the VHDX, where `remove_hiberfile` neither looks nor needs to.

**A hibernated volume cannot be inspected without changing it.** Every tool that reads one
alters it: a mount clears the logfile even with `-o ro,force`, `ntfsfix -n` reports nothing
about hibernation, and `ntfsls` and `ntfscat` refuse the volume outright with *"Volume is
scheduled for check"*. Only `ntfsfix` without `-n` names the state — *"Windows is
hibernated, refused to mount."* — which is exactly what the import reads it from.

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

### What a hibernated guest does, 2026-09-16

A Windows Server 2016 image was made to hibernate itself and the compatible import path was
run against it. The state is real: `hiberfil.sys` is 4 294 422 528 bytes, begins with
`HIBR`, and a read-write mount answers *"Windows is hibernated, refused to mount."*

What the product said, running `v2p._inject_virtio_drivers(..., clear_hibernation_only=True)`
against that volume:

```
[VirtIO] WIN_PART=/dev/loop1p2
[VirtIO] The guest was hibernated or had shut down with Fast Startup. Its saved session
         has been discarded, so it will boot cold on the target — which is the only way it
         can come up on hardware it was not saved on. Anything that was open in that
         session is gone.
[VirtIO] ✓ Windows volume prepared; no drivers were installed.
RESULT ok=True
```

So the compatible path finds a hibernated volume, says so in the words an operator reads,
and clears it. That is the half of `ADR 0005` that was open about detection and reporting.

**Still not photographed: the boot afterwards.** The screens for both paths are not taken
yet, so "and then it boots" remains the one unproven step — for the driver path as well.
Producing the state at all took three findings that live in the testbed's own notes: Proxmox
starts every guest with S4 disabled, so a guest cannot hibernate until `args` says
otherwise; `powercfg`'s output does not survive a redirect from a service; and any probe of
the volume taken before the import destroys the state the import is supposed to find.

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
