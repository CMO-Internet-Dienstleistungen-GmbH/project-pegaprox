# The Hyper-V migration compatibility matrix

Scope: every guest and hardware combination the Hyper-V source claims to handle, with the
evidence for it or the blocker that stops it being claimed. Fork issue #37 is the
requirement. `docs/hyperv-migration.md` describes the direction, `docs/hyperv-transfer.md`
what a transfer costs, `docs/hyperv-console.md` and `docs/hyperv-verification.md` what is
still owed a real host.

Nothing here names a real host, account, network or customer.

**A row with no evidence is not supported.** That is the rule this file exists to enforce:
an untested combination is not a combination that probably works, and it is not listed as
one. Where the evidence is a Docker testbed rather than a Hyper-V host, the row says so,
because those prove different things.

## What the evidence can be

| Kind | What it proves | What it does not |
|---|---|---|
| **Unit test** | The product decides the way it says it does | Nothing about the host that answered |
| **Testbed** | The commands work against a real mount, a real converter or a real guacd | Nothing about Windows |
| **Browser** | The page a person actually uses does what it says | Nothing that needs a source host to answer |
| **Real node** | A production-shaped Proxmox answered — its storage, its API, its refusals | Nothing about the Hyper-V side, which is still synthetic |
| **Blocked** | A named thing missing, not a shrug | — |

Measured with the full suite at **1 859 passed**, no failures. The other cross-hypervisor
directions are in that run, so this patch is not carrying an undetected regression into
them.

## Guest operating systems

Every row here needs a guest that boots after the import. The Hyper-V host is a nested one:
a Windows Server with the Hyper-V role, running as a VM on the Proxmox node with
`kvm_intel nested=1` and `cpu: host`. That is a real Hyper-V, reached over the transport
the product uses, and it is where the rows below were measured.

| Guest | State |
|---|---|
| Linux (Ubuntu, Generation 1) | **Measured (real host)** — 10 GiB guest, integration services running, migrated in 44 s, full systemd boot on Proxmox |
| Windows Server 2022 Standard, Core (Generation 1) | **Measured (real host)** — 40 GiB guest with no driver preparation, migrated in 78 s, booted on Proxmox, and switched to VirtIO afterwards; see *A Windows guest, measured end to end* |
| Windows Server 2016 / 2019 / 2025 | **Not measured** — the same product path as 2022, but the guest's own inbox driver set is what decides whether it boots |
| Windows 10 / 11 | **Not measured** |

Both measured rows prove the same thing, which is the point of the direction: a guest that
ran under Hyper-V, was shut down, migrated by the product with **no drivers installed
inside it**, and booted on Proxmox on `seabios` / `pc` / `sata0` / `e1000`. That is exactly
the shape the compatible default exists to produce.

They differ in what comes after. Linux is the easy guest — VirtIO is in its kernel, so the
switch asks nothing of it. Windows is where the driver step matters, and where the measured
answer is longer than "install the drivers"; the section below is that answer.

What *is* settled for every guest is that the product does not guess: the imported VM gets
no `ostype` invented for it (`test_the_guest_operating_system_is_not_guessed`), so a wrong
guess cannot be the reason a guest misbehaves.

## Firmware and generation

| Combination | State | Evidence |
|---|---|---|
| Generation 1 → SeaBIOS on `pc` | **Measured (real node + unit)** | `test_a_generation_one_source_becomes_a_seabios_pc_machine`, `test_generation_1_maps_to_seabios`. Proxmox rejects `i440fx` with HTTP 400 and accepts `pc` for the same machine — a real node is the only place that shows |
| Generation 2 → OVMF on q35 | **Measured (unit)** | `test_a_generation_two_source_becomes_a_uefi_q35_machine`, `test_generation_2_maps_to_ovmf_on_q35` |
| A UEFI guest keeps its variables | **Measured (unit)** | `test_a_uefi_guest_gets_somewhere_to_keep_its_variables` — an EFI disk is allocated |
| An unknown generation | **Measured (unit)** | `test_an_unsupported_generation_blocks`, `test_an_unknown_generation_blocks` — blocked, not defaulted |
| That either firmware actually boots afterwards | **Blocked** | Needs a Hyper-V host and a target to boot on |

## Secure Boot, vTPM and BitLocker

These are the combinations where an import that "worked" can still hand back a guest that
will not start, so each one is a warning an operator has to confirm rather than a silent
translation.

| Combination | State | Evidence |
|---|---|---|
| Secure Boot on → warned, needs confirming | **Measured (unit)** | `test_secure_boot_warns_and_needs_confirming` |
| Secure Boot claimed on a Generation 1 VM | **Measured (unit)** | `test_secure_boot_on_a_generation_1_vm_is_not_a_thing` — not a real state, not reported as one |
| vTPM present → warned, its state does not travel | **Measured (unit)** | `test_a_vtpm_warns_because_its_state_does_not_travel` |
| BitLocker reported on → warned | **Measured (unit)** | `test_bitlocker_reported_on_warns` |
| BitLocker unknown, with a vTPM → warned | **Measured (unit)** | `test_unknown_bitlocker_with_a_vtpm_warns` |
| BitLocker unknown, without a vTPM → noted only | **Measured (unit)** | `test_unknown_bitlocker_without_a_vtpm_is_only_noted` |
| The finding never claims to have looked inside the guest | **Measured (unit)** | `test_the_bitlocker_finding_never_claims_to_have_looked_inside_the_guest` |
| A BitLocker volume actually unlocking after the import | **Blocked** | Needs a Hyper-V host and a real encrypted guest |

## Disks

| Combination | State | Evidence |
|---|---|---|
| Dynamic VHDX converts with content intact | **Measured (testbed)** | `verify_transfer.sh` — real mount, real `qemu-img`, checksum-compared |
| Fixed VHDX converts with content intact | **Measured (testbed)** | `verify_transfer.sh`, same run |
| Two disks, one share, not mixed up | **Measured (testbed + unit)** | `verify_transfer.sh` converts a second disk with different content; `test_a_second_disk_costs_a_second_conversion_and_nothing_else` |
| A second disk costs no second mount | **Measured (unit)** | Same test — the share is mounted per share |
| A sparse 64 GiB disk stays sparse at the target | **Measured, and it depends on the storage** | `docs/hyperv-transfer.md` — a directory storage allocates what the source held; a thick ZFS storage reserves the full size |
| The conversion writes into a volume Proxmox allocated | **Measured (real node)** | Proxmox VE 9.2.11, directory storage and ZFS block storage, content checksum-compared off the allocated volume |
| Running out of room on a block target | **Measured (real node)** | Reports an I/O error rather than "No space left on device"; the partial volume is freed |
| Disk size is allocated rounded up, never down | **Measured (unit)** | `test_the_allocation_is_rounded_up` |
| A disk with no size | **Measured (unit)** | `test_a_disk_with_no_size_is_refused` |
| A pass-through disk | **Measured (unit)** | `test_a_pass_through_disk_blocks` — blocked, because there is no file to copy |
| A VM with no disks | **Measured (unit)** | `test_a_vm_with_no_disks_blocks` |
| A disk several TB in size | **Not measured** | The largest actually converted is 64 GiB virtual; the call count is size-independent (`docs/hyperv-transfer.md`), the conversion itself is not proven at that size |

## Checkpoints and differencing chains

Importing a differencing chain is out of scope for the epic, so the requirement is that
preflight **blocks** rather than that it merges.

| Combination | State | Evidence |
|---|---|---|
| Any checkpoint present | **Measured (unit)** | `test_any_checkpoint_blocks` |
| A differencing disk | **Measured (unit)** | `test_a_differencing_disk_blocks` |
| A disk whose type says otherwise but has a parent | **Measured (unit)** | `test_a_disk_with_a_parent_blocks_even_if_its_type_says_otherwise` |
| An unknown checkpoint count | **Measured (unit)** | `test_an_unknown_checkpoint_count_blocks` — unknown is never fine |
| A merge still running after a checkpoint was deleted | **Measured (unit)** | `test_a_source_still_merging_its_disks_is_refused` |
| An orphaned AVHDX on a real host | **Blocked** | `qemu-img` cannot build a differencing VHDX, so no chain exists here to be orphaned. The decision is taken from the `ParentPath` metadata, which is what the tests drive |

## Storage paths on the source

| Path shape | State | Evidence |
|---|---|---|
| A local drive path (`C:\vm\disk.vhdx`) | **Measured (unit)** | `test_a_drive_path_becomes_its_administrative_share` |
| A drive mapped to a dedicated read-only share | **Measured (unit)** | `test_a_mapped_drive_uses_the_share_the_operator_made` |
| Cluster Shared Volumes (`C:\ClusterStorage\…`) | **Measured (unit), as a path** | It is a local drive path and resolves as one. Whether a CSV serves it over SMB while the cluster owns it is a host property and is **not** claimed |
| A UNC path or a volume-GUID path | **Measured (unit)** | `test_a_path_this_does_not_understand_is_refused_not_guessed` — refused with a sentence, never guessed |
| A path that would escape the mount | **Measured (unit)** | `test_a_relative_path_cannot_escape_the_mount`, `test_the_resolved_path_is_inside_the_mount` |
| A path holding a quote, a space or a newline | **Measured (unit)** | `test_a_path_with_a_space_or_a_quote_reaches_the_shell_as_one_word`, `test_a_path_that_would_be_two_words_cannot_become_two_commands` |
| A file the account cannot read | **Measured (unit)** | `test_one_unreadable_path_blocks_and_names_it` — named, before anything is allocated |

## Networking

| Combination | State | Evidence |
|---|---|---|
| Every adapter mapped | **Measured (unit)** | `test_a_fully_mapped_vm_passes` |
| An unmapped adapter | **Measured (unit)** | `test_an_unmapped_adapter_blocks`, `test_an_unmapped_adapter_stops_the_run_rather_than_guessing` |
| Adapters addressed by MAC, because names repeat | **Measured (unit)** | `test_an_adapter_is_addressed_by_mac_because_names_repeat` |
| The MAC address survives the import | **Measured (unit)** | `test_the_mac_address_comes_across` |
| A VM with no adapters | **Measured (unit)** | `test_a_vm_with_no_adapters_passes` |
| An adapter that has never had a MAC gets one from the target | **Measured (real host + unit)** | Hyper-V assigns a dynamic MAC at first start, so a VM that never ran reports `000000000000`; `test_an_adapter_that_has_never_had_a_mac_gets_one_from_the_target` |
| The MAC is sent in the spelling Proxmox accepts | **Measured (real node + unit)** | The API rejects an unseparated MAC, so no VM with an adapter could be created at all; `test_the_mac_is_sent_in_the_spelling_the_target_accepts` |
| The card model follows the disk controller | **Measured (unit)** | `test_the_default_hardware_needs_no_drivers_the_guest_does_not_have`, `test_choosing_virtio_moves_the_disk_and_the_card_together` — a VirtIO card on a guest without drivers is a VM with no network |
| A guest actually reaching its network afterwards | **Not measured** | The imported Linux guest booted with its card present; nothing has been sent over it |

## Power state and the source's safety

| Combination | State | Evidence |
|---|---|---|
| A VM that is off | **Measured (unit)** | `test_a_vm_that_is_off_passes` |
| A saved VM | **Measured (unit)** | `test_a_saved_vm_warns_rather_than_passing`, `test_migrating_a_saved_vm_has_to_be_confirmed` |
| A VM still starting, stopping or migrating | **Measured (unit)** | `test_anything_still_moving_blocks` |
| The source is never started while its copy runs | **Measured (unit)** | `TestNeitherSideIsStartedWhileTheOtherRuns` — both directions, and an unreadable state blocks rather than allows |
| The source is never removed, never reconfigured | **Measured (unit)** | `test_a_whole_run_reaches_completed_and_leaves_the_source_alone`, `test_the_source_is_never_part_of_a_cleanup`, `TestOptionsThisDirectionRefuses` |

## Cleaning up after a failed import

Every row below was run against Proxmox VE 9.2.11 by
`tests/hyperv_testbed/verify_cleanup.py`, which creates the VM the way the runner creates
it and then asks the product to remove it.

| Combination | State | Evidence |
|---|---|---|
| A VM this migration created is removed | **Measured (real node)** | The API reports it gone afterwards; `test_a_confirmed_cleanup_removes_the_vm_and_frees_the_volume` |
| A VMID that no longer carries the mark is left alone | **Measured (real node)** | The cleanup reports it as kept, and the VM is still there afterwards; `test_a_vmid_that_now_belongs_to_somebody_else_is_left_alone` |
| A cleanup without a confirmation removes nothing | **Measured (real node)** | Same run, against a real recorded migration; `test_without_confirmation_nothing_is_touched` |
| What was removed stops being recorded, what was refused stays | **Measured (real node)** | The leftover list is empty after the first case and unchanged after the second |
| A cleanup while the migration still runs | **Measured (unit)** | `test_a_running_migration_is_not_cleaned_up_under_its_own_worker`, `test_a_second_import_on_the_same_source_blocks_the_cleanup` |
| A target VM that is already gone | **Measured (unit)** | `test_a_target_vm_that_is_already_gone_counts_as_removed` |
| Freeing a volume the VM did not take with it | **Measured (unit); the command on a real node** | `_free_leftover_volumes` is unit-tested, and the `pvesm free` it issues is run on a real node by `verify_target.sh`. What is *not* exercised is the product's own SSH hop to the node: it authenticates with the credentials stored on the cluster, which needs a key file or a password in the cluster config |

## After the import: drivers and the VM standard

What the two post-import actions do, and what they refuse. All of it acts on the VM on
Proxmox; the Hyper-V source is reached for nothing but the permission check.

| Combination | State | Evidence |
|---|---|---|
| The VirtIO ISO is found among the node's other ISOs | **Measured (real node)** | `find_virtio_isos` picked `local:iso/virtio-win.iso` out of 15 ISOs across two storages, matched by name and nothing else |
| An existing database gains the post-import column | **Measured (real node)** | `post_import` was added by `ALTER TABLE` to a database created before the feature; it appears last in `PRAGMA table_info(hyperv_migrations)` |
| The driver state is read off a real VM | **Measured (real node)** | `describe_driver_state` reported the imported VM's controller, card, boot order and empty driver confirmation from the live config |
| The driver ISO is attached as a CD to the imported VM | **Measured (unit)** | `test_the_iso_is_attached_as_a_cdrom_and_the_guest_is_not_touched` — the boot disk and boot order are exactly as the import left them |
| The ISO is found on the node without being named | **Measured (unit)** | `test_the_iso_is_found_without_being_named` |
| A node with no VirtIO ISO says so rather than downloading one | **Measured (unit)** | `test_a_node_without_the_iso_says_so_instead_of_downloading_one` |
| A medium already in the drive is not displaced | **Measured (unit)** | `test_a_medium_that_is_already_in_the_drive_is_not_displaced`, `test_replacing_it_is_possible_when_it_is_said_explicitly` |
| An attached ISO is never taken for an installed driver | **Measured (unit)** | `test_an_attached_iso_is_not_an_installed_driver` — the two facts are separate fields and one never sets the other |
| The confirmation records who made it | **Measured (unit)** | `test_a_confirmation_is_recorded_with_who_made_it`, and it can be taken back |
| The differences are shown before anything changes | **Measured (unit)** | `test_the_differences_are_shown_before_anything_is_changed` — nothing is posted to the target |
| A guest whose drivers nobody confirmed is not switched | **Measured (unit)** | `test_a_guest_whose_drivers_were_never_confirmed_is_not_switched` |
| A running VM is named and never powered off | **Measured (unit)** | `test_a_running_vm_is_never_powered_off_to_make_the_change` |
| The disk moves to VirtIO SCSI in one request | **Measured (real node + unit)** | Proxmox VE 9.2.11 accepts `scsi0=<volume>` together with `delete=sata0` in one config request and leaves no `unused0` behind; `test_the_disk_moves_to_the_virtio_controller_in_one_step` |
| The switched VM boots | **Measured (real node)** | The imported Ubuntu guest booted fully after the switch. The monitor reports `drive-scsi0` on the VirtIO SCSI controller and `virtio-net-pci` carrying the original MAC |
| The card changes model and keeps its address | **Measured (unit)** | `test_the_card_changes_model_and_keeps_its_address` — a changed MAC is a new machine to a DHCP server and to every licence check |
| Everything outside the profile is left alone | **Measured (unit)** | `test_everything_outside_the_profile_is_left_as_it_was` |
| A moved disk never takes a key that is already in use | **Measured (unit)** | `test_a_disk_already_on_the_target_controller_is_not_overwritten`, `test_a_cdrom_on_the_target_controller_holds_its_number_too` |
| The boot order is rewritten, not re-guessed | **Measured (unit)** | `TestTheBootOrder` — a VM with two disks boots from the one it booted from |
| A VM that disappears between preview and change | **Measured (unit)** | `test_a_vm_that_disappears_after_the_preview_reports_why` |
| A Windows guest booting after the switch | **Measured (real host), and it needs one step more** | Windows Server 2022 fails into the recovery environment after the switch even with the drivers installed and confirmed, and boots once `vioscsi` has been bound to a device — see *A Windows guest, measured end to end* |
| The same mechanism switching a VM back | **Measured (real node)** | `apply_profile` with a compatible profile moved the Windows guest from `scsi0` back to `sata0` and from `virtio` back to `e1000`; it booted to the logon screen, which is what separates a controller problem from a data problem |
| The profile's own settings reach a real VM | **Measured (real node)** | Proxmox VE 9.2.11 accepted `cpu=x86-64-v2-AES`, `numa=1`, `balloon=0`, `agent=1` and a disk carrying `cache=writeback,discard=on,ssd=1` in one config request; the Windows guest then booted to the logon screen and answered WinRM |
| A VM on the right controller but not on the profile | **Measured (real node + unit)** | VM 133 had already been switched before these values existed. The preview named five changes and no disk move, the disk kept its key, no `unused0` appeared and the boot order was untouched; `test_a_disk_already_on_the_controller_still_gains_the_options` |
| A CD added to a VM that is already running | **Measured (real node + unit)** | The disc lands in Proxmox's pending config and the guest sees nothing until the next start; the state says `iso_pending` and the message says so. `TestAdrivedAddedToARunningVm` |

**The profile's values come from the manual procedure**, not from taste. The runbook that
this direction automates sets them by hand on every migrated VM, so the button has to
produce the same machine:

| Setting | Value |
|---|---|
| Disk controller | VirtIO SCSI single |
| Disk options | `cache=writeback`, `discard=on`, `ssd=1` |
| Network model | `virtio` |
| CPU type | `x86-64-v2-AES` |
| NUMA | on |
| Ballooning device | off |
| QEMU guest agent | on |

Three settings the runbook also names are deliberately **not** in the profile, because this
button changes a VM that already has an operating system on it: the machine type and BIOS
(they follow the source's generation and are decided at import — an installed guest does
not move from SeaBIOS to OVMF and boot), a TPM (a new state volume that an already-imported
guest gains nothing from), and firewall and HA (cluster policy, not the VM's hardware).

One documented exception to the CPU type: an older Windows Server 2016 or 2019 can
bluescreen with `DXGKRNL_FATAL_ERROR` roughly every eleven minutes on a modern CPU model,
and `kvm64` settles it. A profile is data rather than this module's opinion, so that is a
per-VM override and not a reason to hold every other VM back
(`test_a_profile_can_carry_further_settings_when_they_are_decided`).

An option a disk already carries is never overruled by the profile
(`test_an_option_the_disk_already_carries_wins`), and an agent written in its long form
(`enabled=1,…`, carrying settings somebody chose) is left alone rather than replaced by a
bare `1`.

## A Windows guest, measured end to end

One Windows Server 2022 Standard (Core), Generation 1, installed unattended on the nested
Hyper-V host with **no driver preparation of any kind**. Measured inside the guest before
the migration: no VirtIO or Red Hat device present, and an inbox driver set covering
`e1000` (`nete1g3e.inf`), `e1000e` (`net1ix64.inf`) and `rtl8139` (`netrtl64.inf`) — and no
virtio-net. That is the starting state this direction claims to handle.

| Step | Result |
|---|---|
| Migration of the 40 GiB disk, source untouched | Completed in 78 s |
| First boot on Proxmox, `seabios` / `pc` / `sata0` / `e1000` | Reached SConfig; WinRM answering over the network |
| Driver ISO attached through the product | Visible in the guest as `virtio-win-0.1.285` — after the next start, see the pending-config defect below |
| `virtio-win-gt-x64.msi` run in the guest | Exit code 0, twelve VirtIO drivers in the driver store |
| `vioscsi` registered as a boot-start service | **No.** `Start` absent, because no device matched it at install time |
| The three refusals: unconfirmed, running, both | Each fired; the change went through only after a confirmation and a shutdown |
| Switch to the VM standard | Applied as asked — and the guest booted into the recovery environment |
| The same mechanism, switched back to a compatible profile | Booted to the logon screen: the data was never the problem, the controller was |
| A temporary VirtIO SCSI disk attached once while still on SATA | `vioscsi Start = 0`; "Red Hat VirtIO SCSI pass-through controller" present |
| Temporary disk removed, VM standard applied a second time | Booted to the logon screen on `scsi0`, WinRM answering after 30 s over the VirtIO card |

Two things this settles, and one it does not.

**The central claim holds for Windows.** A Hyper-V guest with nothing installed in it
migrates and boots on Proxmox. The compatible default is what makes that true, and it is
why the preflight no longer asks for drivers before a migration.

**The driver confirmation is necessary and not sufficient.** An operator who installs the
drivers and confirms it honestly has still not made the guest bootable on VirtIO: an
installer cannot bind a boot-critical driver to a device that does not exist yet. The
procedure that does is in `docs/hyperv-migration.md`. PegaProx documents it and does not
perform it — whether it should is a decision, not a defect.

**Generation 2 is measured as far as the import, and no further.** A Generation 2 source
imported onto a real node produces `bios: ovmf`, `machine: q35` and
`efidisk0: …,efitype=4m,pre-enrolled-keys=0` beside the boot disk — the shape a UEFI guest
needs, rather than the firmware shell an OVMF VM without a variable store lands in. The
empty variable store is deliberate: a guest arriving from a host with Secure Boot enabled
boots here because nothing enrols keys that would then refuse its loader.

What has not been measured is a Generation 2 guest with an operating system on it. Whether
such a guest boots after import, and whether the VirtIO switch behaves there as it does on
SeaBIOS, is open.

## Defects a real host found that no double could

Five defects — four of them blocking, one a false report — each invisible to the suite
because a fixture used a shape the real source or target never produces. They are listed because the pattern is the finding: a
test double that answers plausibly is not a test double that answers correctly.

| Defect | What it did | Fixed in |
|---|---|---|
| A mandatory script parameter assigned after the `param()` block instead of bound | Every VM-addressed read and action on the host failed with "missing mandatory parameters" | `9e857e2`, locked by `tests/test_hyperv_script_parameters.py` against real `pwsh` |
| The MAC sent as `00155D000001` | The Proxmox API refused every VM that had an adapter | `ce64f1e` |
| No wait for the asynchronous creation task, and attach failures only logged | A VM with no disks at all, reported as `completed` | `850ee08` |
| `machine='i440fx'` | HTTP 400 on every Generation 1 import | `e933503` |
| A CD attached to a running VM reported as attached | The change sat in Proxmox's pending config; the guest saw no disc while the panel said it was in the drive | `a90038a`, locked by `TestAdrivedAddedToARunningVm` |

## The browser path

| Step | State | Evidence |
|---|---|---|
| Registering a Hyper-V host and seeing every cluster page render | **Measured (browser)** | Against a locally provisioned instance; the crash that made this necessary is fixed and locked by `tests/test_hyperv_api.py` |
| The migration wizard: analyse, review, preflight, target selection | **Measured (browser)** | The start button stays disabled until preflight has been re-asked for the chosen target |
| The migration panel listing what a run left behind | **Measured (browser)** | Including a cleanup that refuses without the typed confirmation |
| The console opening in a browser | **Measured (browser, against guacd)** | `tests/hyperv_testbed/verify_console.sh`; the Windows half is blocked, see `docs/hyperv-console.md` |
| A full import driven from the browser | **Not measured** | The same import has been driven through the product's own runner against a real host and a real node, but not from the page |
| A boot test of the imported guest | **Measured (real node)** | The imported Ubuntu guest booted to a login prompt with `ssh.socket` listening, read off the node's console |
| The post-import panel | **Not measured (browser)** | The two buttons are unit-tested and the page builds; nobody has clicked them |
| A manual rollback after a failed import | **Partly measured** | The cleanup path is proven by `TestCleaningUpWhatAFailedImportLeft` and against a real node, see above; doing it after a *real* failed import is not measured |

## What this matrix does not claim

- **No throughput or duration figures.** See the limits in `docs/hyperv-transfer.md`.
- **No failover or CSV support statement.** The epic excludes it and nothing here tested it.
- **No production migration.** Every measurement above used synthetic local data, on a
  nested Hyper-V host built for this work.
- **Almost nothing inside a guest.** The Windows run above is the one exception: its
  registry, its driver store and its device list were read, to establish what the driver
  step actually does. Everywhere else, that the VirtIO drivers are installed is a recorded
  statement and nothing checks it.
- **No combination is listed as working because a neighbouring one does.** Generation 2
  passing says nothing about Secure Boot; a dynamic VHDX converting says nothing about a
  differencing chain.
