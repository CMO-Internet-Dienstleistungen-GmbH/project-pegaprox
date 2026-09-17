# Migrating a VM from Hyper-V to Proxmox

Scope: how the Hyper-V migration source behaves, what it refuses, and what an
operator has to do around it. Fork issue #15 and its sub-issues are the
requirement; this file is the description. The verification contract and the
test path are in `docs/hyperv-verification.md`; what one transfer costs and what
it does when the target runs out is in `docs/hyperv-transfer.md`; which guest and
hardware combinations are actually proven is in `docs/hyperv-compatibility.md`.

Nothing here names a real host, account, network or customer.

## What survives, and where it is

A migration's record lives in `config/pegaprox.db`, table `hyperv_migrations` — one row
per migration, kept after the run ends. The list in the interface is built from a dict in
the process and is emptied by a restart; the table is what the list falls back to, and
what the refusal to start the same VM twice reads.

| Column | |
|---|---|
| `migration_id`, `source_cluster`, `source_vm_guid`, `source_vm_name` | which VM, from where |
| `target_cluster`, `target_node`, `target_storage`, `target_vmid` | where it went |
| `status`, `phase`, `progress`, `error` | how it ended, and why not |
| `log_lines` | the run's own log, JSON, last 500 lines |
| `created_resources` | what it left on the target, JSON — the reason a record outlives its run |
| `disk_progress`, `post_import` | JSON |
| `started_at`, `updated_at`, `completed_at` | Unix time |

The phase timeline is **not** kept: it lived in the process. For a record read back after
a restart, the status is the verdict.

Taking the data out, without PegaProx:

```bash
sqlite3 -header -csv config/pegaprox.db \
  "SELECT * FROM hyperv_migrations ORDER BY started_at" > migrations.csv
```

Removing it again: an entry can be dismissed in the interface once nothing of it is left
on the target, and `DELETE FROM hyperv_migrations WHERE completed_at < …` does it on the
database. The refusal reads the same table, so gone is gone.

## What this direction is

A Hyper-V host is registered like any other cluster and appears in the sidebar,
but it is a **restricted source**: PegaProx reads it, prepares a VM on it, and
copies that VM to Proxmox. It never creates, deletes or reconfigures anything
there. The whole allowed vocabulary on the source is:

- start a VM, so a guest can be prepared,
- ask a guest to shut itself down, and wait for it,
- remove checkpoints, so each disk becomes a single readable file,
- mount and eject an ISO from the host's configured library.

The data never passes through PegaProx. The Proxmox target node mounts the
source's file share read-only and runs `qemu-img convert` itself, so the
management server is irrelevant to the transfer's speed and to its failure
modes.

What the target node has to have for any of this to work -- the one package
nobody installs for you, the tools PVE already brings, and the one package that
must never be installed on a node -- is in `hyperv-target-node-requirements.md`.

How PegaProx reaches the host -- HTTP or HTTPS, which authentication provider,
whether the payload over HTTP is sealed -- is configured per host and follows
what the host already offers. The product sets no minimum: a source is never
reconfigured to suit a migration tool, and an estate that runs the default HTTP
listener is registered as it is. The trade-offs of each choice are stated in
`hyperv-verification.md`, not enforced.

## What the host view shows, and how old it is

Reading a host takes tens of seconds: one WinRM call for the inventory and one
for the host facts, against a hypervisor with its own load. So the host view
never waits for that. It shows the inventory PegaProx last read, with the time
it was read on the line above the table, and the read itself runs in the
background. When it finishes, the list is replaced without anyone doing
anything -- the server pushes a notification over the same SSE channel the rest
of the UI already uses, and the view fetches the new list from the server's
cache.

Which means three things on screen, and each says which it is:

- *"Reading the host…"* -- nothing is known about this host yet. The table is
  empty because nothing has been read, not because the host has no VMs.
- *"As of 14:32"* -- this is what the host said at that time.
- *"As of 14:32 · reading the host again"* -- that, and a read is in flight.

A read starts when the view is opened and what is stored is older than five
minutes, when a VM is started or shut down through PegaProx, and whenever the
refresh button is pressed. Nothing else asks a customer's hypervisor: opening
the view repeatedly, re-rendering, and every generic PegaProx page that
enumerates VMs are served from the cache. A host nobody has opened is never
contacted at all.

If the host stops answering, the rows stay and the banner turns amber with the
reason and its remedy. That is deliberate: which VMs are on a host is still
worth knowing while the host is briefly unreachable, and an empty table would
throw away the only record that it ever had them.

**What is never cached is anything a migration acts on.** The VM detail, the
disk chains, the power state and the "are these disks safe to read" check are
read from the host each time they are asked for, and the preflight re-runs
against the host immediately before a transfer starts. The cache is for
choosing a VM out of a list; it is not evidence about a disk. See
`adr/0004-the-hyper-v-inventory-is-served-from-a-cache.md`.

## The order of a migration

1. **Prepare the guest — but not with drivers.** Nothing has to be installed
   inside the guest before a migration. The import writes the VirtIO drivers
   into the copied disk itself, before anything starts the VM, so a Windows
   guest comes up on VirtIO SCSI without having been touched on the Hyper-V
   side. Clearing that box in the wizard puts the VM on hardware every guest
   already has drivers for instead, and the switch to VirtIO is then a step of
   its own afterwards (see *After the import*). Preparing still means the steps
   below: shut it down, and remove its checkpoints.
2. **Shut the VM down.** This is an offline migration. A running VM's disks are
   being written to while they are read, and the copy would be a crash image of
   an unknown moment.
3. **Remove checkpoints.** A VM with checkpoints has a differencing chain rather
   than one file per disk. Deleting a checkpoint returns immediately and the
   merge runs on afterwards, so PegaProx waits for the merge to actually finish
   before it reads anything.
4. **Analyse.** The wizard shows what the source is and what the preflight found.
   Blockers cannot be clicked past; warnings must each be confirmed by name.
5. **Choose the target.** Node, storage, and a target network for every adapter.
   None of these is guessed: an unmapped adapter blocks the migration, because a
   VM that arrives on the wrong VLAN is reachable by the wrong people and that
   is not visible in the result.
6. **Start it.** The copy is created, the disks are converted one at a time, and
   the VM is assembled on the target.
7. **Boot it.** Check the copy on its console, with the original still intact
   behind you. If the drivers were left out — or if this guest's Windows version
   has none the loader would accept, in which case the log says so and the VM is
   built on its compatible controller — the switch to VirtIO is the step that
   follows, on the Proxmox side.

## What the preflight refuses, and why

| Check | Refuses when | Because |
|---|---|---|
| `power_state` | the VM is running, or a merge is still going | the copy would be a crash image |
| `checkpoints` | a checkpoint chain is still present | a differencing disk is not one file |
| `disk_type` | a disk is a differencing or shared disk | it does not describe its own contents |
| `firmware` | the generation is unknown | generation decides BIOS and machine type; guessing boots a UEFI guest into a firmware shell |
| `target_capacity` | the target has less room than the disks need | a transfer that fills a storage affects guests already on it |
| `network_mapping` | an adapter has no target network | see above |
| `source_access` | the file share has not been proven readable | a transfer that cannot read its source fails at its first byte |

Warnings are different: `secure_boot`, `virtio_drivers`, `bitlocker` and
`source_access` can each be accepted by a person who has read them. The API
refuses to start a migration whose warnings have not been confirmed, so the
checkboxes in the wizard are not decoration.

## After the copy exists

The migration is not over when the copy exists. Both machines now exist, they
carry the same hostname and the same MAC address, and **only one of them may
run**.

PegaProx enforces that from both sides. Starting the imported VM is refused
while the Hyper-V original is running; starting the original is refused while
the copy is running. A state that cannot be read refuses too — "I could not
reach the target" is not "the target is off", and acting on the difference is
exactly how both end up running.

The imported VM is never started automatically, and the option to do so cannot
be switched on, not even by a hand-written request.

## After the import: drivers, then the VM standard

This section is for an import that landed on a SATA disk and an emulated Intel
card, which happens in two cases: the wizard's driver box was cleared, or this
guest's Windows version has no VirtIO driver the loader would accept as a boot
driver and the import therefore built it on hardware it can start from. Both are
visible in the migration's log.

A VM whose disk is on a controller it cannot address does not boot far enough to
install anything, which is why the drivers have to be in place before the first
start rather than after it. When the box is ticked the import does that itself,
offline, on the copied disk; measured on Windows Server 2012 R2, 2016, 2022 and
2025, each reaching its login screen on `virtio-scsi-single` about a minute after
starting. The steps below are the same work done by hand.

Everything that follows happens on the target, in the *Hyper-V imports on
record* panel, and none of it reaches the Hyper-V host.

### 1. Mount the VirtIO driver ISO

Attaches the driver ISO to the imported VM as a CD. It installs nothing, needs
no network in the guest and no guest agent, and it does not replace a medium
that is already in the drive unless that is said explicitly.

The ISO is not shipped with PegaProx and is not downloaded by it. Upload
`virtio-win-*.iso` to an ISO storage on the node the way any other ISO gets
there; PegaProx finds it by name.

### 2. Say the drivers are installed

Boot the VM, install the drivers from the CD, shut it down again, and record
that it happened. Use `virtio-win-guest-tools.exe` from the disc rather than the
bare driver package: it installs the QEMU guest agent as well, which the VM
standard switches on.

This step exists because **an attached ISO is not an installed driver**, and
nothing outside a guest can tell the difference: there is no agent, no network
into the guest, and its disk is not inspected. So the confirmation is a
statement, stored with the name of whoever made it and the time — never inferred
from the ISO being in the drive, never from a successful boot, never from
elapsed time. Each of those guesses is wrong for some guest, in the direction
that leaves it unbootable.

#### Windows: the installer alone does not make the disk bootable

Measured on Windows Server 2022: `virtio-win-gt-x64.msi` finishes with exit code
0, every VirtIO driver lands in the driver store — and the guest still boots into
the recovery environment after the switch.

The reason is how Windows loads a boot-critical driver. `vioscsi` becomes a
boot-start service only when Windows sees a device it matches. While the system
disk is on SATA no such device exists, so the installer stages the driver
without registering it, and at the next start the boot loader has nothing to
address the disk with. The network card has no such problem: `netkvm` is loaded
after the switch like any ordinary driver.

Let the guest see the controller once, while it can still boot:

1. With the system disk still on SATA, attach a small second disk on the VirtIO
   SCSI controller (`scsi1`, `scsihw: virtio-scsi-single`).
2. Start the VM. Windows finds the controller, matches `vioscsi` and registers
   it as boot-start; *Device Manager* then shows "Red Hat VirtIO SCSI
   pass-through controller".
3. Shut down, remove the temporary disk, and only then switch to the VM
   standard.

What has to be true is readable in the guest, and it is the device, not the
service:

```powershell
Get-PnpDevice -Class SCSIAdapter
```

"Red Hat VirtIO SCSI pass-through controller" with status `OK` is the answer that
carries. The service's own `Start` value looks like the same check and is not:
measured on a guest whose `Services\vioscsi` already read `Start = 0` and which
still stopped with `INACCESSIBLE_BOOT_DEVICE` after the switch, because nothing
had ever bound that driver to a device. Nor is it a signing problem — the same
guest was started once with `nointegritychecks` and `testsigning` on and stopped
at the same place.

That stop code is usually not on screen. After two failed starts the boot loader
opens the recovery environment instead of trying again, so what the console shows
is "choose your keyboard layout" — the same picture a broken boot record gives.
`bcdedit /set {current} recoveryenabled No` brings the stop code back.

PegaProx does not do this for you. The confirmation in this step is a statement
about the guest, and on Windows this is part of what has to be true before it can
honestly be made.

### 3. Switch to the VM standard

Moves the disk to VirtIO SCSI and the card to `virtio`, in one request, so the
volume is never briefly detached, and sets the rest of the standard:

| Setting | Value |
|---|---|
| Disk controller | VirtIO SCSI single |
| Disk options | `cache=writeback`, `discard=on`, `ssd=1` |
| Network model | `virtio` |
| CPU type | `x86-64-v2-AES` |
| NUMA | on |
| Ballooning device | off |
| QEMU guest agent | on |

Machine type, BIOS and TPM are **not** part of it: they follow the source VM's
generation and are decided at import, and an installed guest does not move from
SeaBIOS to OVMF and still boot. Firewall and HA are cluster policy, not the VM's
hardware.

An older Windows Server 2016 or 2019 can bluescreen with `DXGKRNL_FATAL_ERROR`
about every eleven minutes on a modern CPU model. `cpu: kvm64` settles that one,
per VM.

- The differences are shown before anything happens, and the change needs an
  explicit confirmation naming the migration.
- The VM must be powered off, and PegaProx never powers one off to do this.
- A guest whose drivers nobody confirmed is refused. An operator who knows
  better — a guest that already had the drivers before it was imported — can
  override that, deliberately and per VM.
- Everything outside the profile is left alone: the name, the memory, the
  firmware, the network assignment, the MAC address, and anything set by hand.
  Only how the disks and the card are attached changes.
- **On Windows, do the step above first.** A guest whose `vioscsi` has never
  been bound to a device boots into the recovery environment after the switch,
  with the drivers installed and the confirmation recorded. Reverting is the
  same button with the compatible profile, and costs one more restart.

The whole step is optional. A VM left on SATA and `e1000` runs.

### Rolling back

There is no rollback button, and that is deliberate: the rollback is the
original, and the only thing an automated version would add is a chance to do it
to the wrong machine.

1. Stop the copy on the Proxmox node.
2. Start the original again on the Hyper-V host.
3. Decide later what happens to the copy. It costs storage until it is removed.

Two things that catch people out:

- **Anything the copy wrote since the import stays on the copy.** Going back
  means going back to the state the original was read in. Nothing merges the
  difference, and PegaProx does not offer to.
- **Preparation is not undone by going back.** A checkpoint that was deleted to
  prepare the migration is deleted on the original too, and its history is gone
  for good — Hyper-V offers no undo for it. A mounted ISO stays mounted. Those
  changes were confirmed separately, before the migration, and the rollback does
  not reach them.

The source VM itself is never deleted by PegaProx, on any path, including a
successful migration. That is what keeps step 2 possible at all.

## When an import fails

A failed import leaves what it had already created: a VM shell, and volumes at
full size. They are recorded and listed per migration. They do not block a new
attempt on that VM: a new import allocates its own volumes next to them, and
what happens to the old ones is a person's decision:

- **Remove them.** The wizard offers it per migration, asks a second time, and
  names what it will delete. It verifies that the target VM still carries that
  migration's mark before deleting anything: a VMID is not ownership, and
  between a failed import and a cleanup that number can have been given to an
  unrelated guest.
- **Keep them**, for example to look at a half-converted disk. They cost
  storage until they are removed.

Cleanup never touches the Hyper-V source.

## What survives a restart

The migration record is in the database, not in the server's memory. A process
that stops mid-transfer leaves its migration marked as interrupted, with the
list of what it had already created on the target — which is the moment that
list matters most. The in-app migration list above it is the live one and is
empty after a restart; the record below it is not.

A source VM can be claimed by one migration at a time, and the claim is a
primary key rather than a check, so two starts in the same second cannot both
pass it. A claim is live only while its migration is, so a process that dies
mid-transfer cannot leave a VM permanently unmigratable.

## What this does not do

- No re-migration back to Hyper-V, and no data sync in either direction.
- No control over starts made outside PegaProx. Somebody with a Hyper-V console
  can start the original whatever this product thinks.
- No automatic cleanup, ever. Everything destructive on the target is a
  confirmed, named action.
- No source deletion.
- Nothing inside a guest is read or verified, on either side. That the VirtIO
  drivers are installed is somebody's recorded statement, not a measurement --
  see *After the import*.
