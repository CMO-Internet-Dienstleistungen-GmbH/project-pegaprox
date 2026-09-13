# Migrating a VM from Hyper-V to Proxmox

Scope: how the Hyper-V migration source behaves, what it refuses, and what an
operator has to do around it. Fork issue #15 and its sub-issues are the
requirement; this file is the description. The verification contract and the
test path are in `docs/hyperv-verification.md`; what one transfer costs and what
it does when the target runs out is in `docs/hyperv-transfer.md`; which guest and
hardware combinations are actually proven is in `docs/hyperv-compatibility.md`.

Nothing here names a real host, account, network or customer.

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

## The order of a migration

1. **Prepare the guest.** VirtIO drivers matter most: the imported VM gets a
   VirtIO SCSI controller, and a guest without drivers for it will not find its
   disk. Mount the driver ISO and install them before migrating, or accept the
   warning and choose SATA on the target, which is slower but works.
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
full size. They are recorded, listed per migration, and they block a second
attempt on that VM — starting again would copy the same disks into a second set
of volumes and fill the storage with copies nobody can tell apart afterwards.

Two ways forward, both a person's decision:

- **Remove them.** The wizard offers it per migration, asks a second time, and
  names what it will delete. It verifies that the target VM still carries that
  migration's mark before deleting anything: a VMID is not ownership, and
  between a failed import and a cleanup that number can have been given to an
  unrelated guest.
- **Keep them deliberately**, for example to look at a half-converted disk. The
  VM stays blocked for a new import until they are gone.

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
