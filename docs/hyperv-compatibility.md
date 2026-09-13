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
| **Blocked** | A named thing missing, not a shrug | — |

Measured with the full suite at **1 777 passed**, two failures that predate this work and
are environmental (`test_ssl_bootstrap.py::test_missing_pair_is_generated`,
`test_ticket_713_vnc_relay_lock.py::test_vnc_poll_recv_send_are_mutually_exclusive`). The
other cross-hypervisor directions are in that run, so this patch is not carrying an
undetected regression into them.

## Guest operating systems

Every row here needs a guest that boots after the import, and a boot needs a Hyper-V host
to migrate from. None has been released for this work.

| Guest | State |
|---|---|
| Windows Server 2016 / 2019 / 2022 / 2025 | **Blocked** — no Hyper-V test system |
| Windows 10 / 11 | **Blocked** — no Hyper-V test system |
| Linux guests | **Blocked** — no Hyper-V test system |

What *is* settled is that the product does not guess: the imported VM gets no `ostype`
invented for it (`test_the_guest_operating_system_is_not_guessed`), so a wrong guess cannot
be the reason a guest misbehaves.

**The decision this matrix forces:** until a host exists, the operating-system rows stay
empty and the migration is documented as unproven for every guest. Shipping them as
supported on the strength of the disk conversion would be claiming the one thing none of
this measured.

## Firmware and generation

| Combination | State | Evidence |
|---|---|---|
| Generation 1 → SeaBIOS on i440fx | **Measured (unit)** | `test_a_generation_one_source_becomes_a_seabios_i440fx_machine`, `test_generation_1_maps_to_seabios` |
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
| A sparse 64 GiB disk stays sparse at the target | **Measured (testbed)** | `docs/hyperv-transfer.md` — 256 MiB of content allocates 256 MiB |
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
| A guest actually reaching its network afterwards | **Blocked** | Needs a Hyper-V host and a booted guest |

## Power state and the source's safety

| Combination | State | Evidence |
|---|---|---|
| A VM that is off | **Measured (unit)** | `test_a_vm_that_is_off_passes` |
| A saved VM | **Measured (unit)** | `test_a_saved_vm_warns_rather_than_passing`, `test_migrating_a_saved_vm_has_to_be_confirmed` |
| A VM still starting, stopping or migrating | **Measured (unit)** | `test_anything_still_moving_blocks` |
| The source is never started while its copy runs | **Measured (unit)** | `TestNeitherSideIsStartedWhileTheOtherRuns` — both directions, and an unreadable state blocks rather than allows |
| The source is never removed, never reconfigured | **Measured (unit)** | `test_a_whole_run_reaches_completed_and_leaves_the_source_alone`, `test_the_source_is_never_part_of_a_cleanup`, `TestOptionsThisDirectionRefuses` |

## The browser path

| Step | State | Evidence |
|---|---|---|
| Registering a Hyper-V host and seeing every cluster page render | **Measured (browser)** | Against a locally provisioned instance; the crash that made this necessary is fixed and locked by `tests/test_hyperv_api.py` |
| The migration wizard: analyse, review, preflight, target selection | **Measured (browser)** | The start button stays disabled until preflight has been re-asked for the chosen target |
| The migration panel listing what a run left behind | **Measured (browser)** | Including a cleanup that refuses without the typed confirmation |
| The console opening in a browser | **Measured (browser, against guacd)** | `tests/hyperv_testbed/verify_console.sh`; the Windows half is blocked, see `docs/hyperv-console.md` |
| A full import driven from the browser | **Blocked** | Needs a Hyper-V host to import from |
| A boot test of the imported guest | **Blocked** | Needs the import above |
| A manual rollback after a failed import | **Partly measured (unit)** | The cleanup path is proven by `TestCleaningUpWhatAFailedImportLeft`; doing it against a real failed import is blocked with the row above |

## What this matrix does not claim

- **No throughput or duration figures.** See the limits in `docs/hyperv-transfer.md`.
- **No failover or CSV support statement.** The epic excludes it and nothing here tested it.
- **No production migration.** Every measurement above used synthetic local data.
- **No combination is listed as working because a neighbouring one does.** Generation 2
  passing says nothing about Secure Boot; a dynamic VHDX converting says nothing about a
  differencing chain.
