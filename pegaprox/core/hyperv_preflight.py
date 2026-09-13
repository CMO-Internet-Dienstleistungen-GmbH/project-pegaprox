"""What must be true before a Hyper-V VM may be imported, and what merely deserves a warning.

Preflight is the last point at which a migration can be stopped cheaply. After it, disks
are being read and a target VM exists, so a condition that should have blocked the run
turns into a half-built guest somebody has to clean up by hand. The rules therefore live
here as pure functions over already-normalised data: no host, no network, no I/O, which is
what lets every one of them be tested for real rather than reasoned about.

Three severities, and the difference between them is a decision, not a feeling:

`BLOCKING` means proceeding can lose or corrupt data, or cannot work at all. It cannot be
clicked away.

`WARNING` means the migration will run and the result may need work — a driver to install,
a risk to accept. It requires acknowledgement where the epic says a risk must be confirmed.

`OK` is a check that ran and found nothing, reported so the review screen shows what was
examined rather than only what went wrong. A silent pass looks identical to a check that
never ran.

An unknown state is never `OK`. Hyper-V can answer "I don't know" about a disk, and a
migration that treats that as fine is exactly the one that eats a differencing chain.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# Severities, ordered from harmless to fatal so a run's worst finding is max().
OK = 'ok'
WARNING = 'warning'
BLOCKING = 'blocking'
_SEVERITY_ORDER = {OK: 0, WARNING: 1, BLOCKING: 2}

# Hyper-V VM states in which the disks are not being written to.
#
# 'Off' is the only one that is simply fine. A saved VM is not writing either, but its
# memory lives in a separate saved-state file that the disks do not contain: copying the
# disks alone silently discards it and delivers the guest a crash-consistent image of a
# machine that believed it was suspended. That is a decision for a person, so it warns
# rather than passing or blocking.
SAFE_TO_READ_STATES = frozenset({'Off'})
SAVED_STATES = frozenset({'Saved', 'FastSaved'})

# Disk types that can be imported. A differencing disk is the head of a chain whose parents
# must be merged first; the epic explicitly does not import remaining chains.
IMPORTABLE_VHD_TYPES = frozenset({'Fixed', 'Dynamic'})

# Generation to target firmware. Gen 1 boots BIOS, Gen 2 boots UEFI, and getting this wrong
# produces a VM that is imported perfectly and does not boot.
GENERATION_FIRMWARE = {1: ('seabios', 'i440fx'), 2: ('ovmf', 'q35')}

# Headroom on the target beyond the bytes actually transferred, so a storage that reports
# just enough space does not fill up during the copy.
_TARGET_HEADROOM_FRACTION = 0.10


@dataclass(frozen=True)
class Finding:
    """One preflight check and what it concluded.

    `check` is a stable identifier so the UI and tests can refer to a finding without
    matching on prose. `detail` is what a person needs to act, and carries measurements
    rather than adjectives: "needs 42 GB, 12 GB free" tells somebody what to do,
    "insufficient space" does not.
    """

    check: str
    severity: str
    summary: str
    detail: str = ''

    @property
    def blocks(self) -> bool:
        return self.severity == BLOCKING

    def to_dict(self) -> dict:
        return {'check': self.check, 'severity': self.severity,
                'summary': self.summary, 'detail': self.detail}


@dataclass
class PreflightReport:
    """Every finding from one preflight run, and what they add up to."""

    findings: list[Finding] = field(default_factory=list)

    def add(self, finding: Finding) -> None:
        self.findings.append(finding)

    @property
    def severity(self) -> str:
        """The worst thing found. OK when nothing ran, which callers must not confuse
        with a passed check — `findings` being empty is the signal for that."""
        if not self.findings:
            return OK
        return max((f.severity for f in self.findings), key=lambda s: _SEVERITY_ORDER[s])

    @property
    def blocked(self) -> bool:
        return any(f.blocks for f in self.findings)

    @property
    def blocking_findings(self) -> list[Finding]:
        return [f for f in self.findings if f.blocks]

    @property
    def warnings(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == WARNING]

    def requires_acknowledgement(self) -> list[str]:
        """Checks whose warnings a person has to confirm before the run may start."""
        return [f.check for f in self.warnings if f.check in _ACKNOWLEDGEABLE_CHECKS]

    def to_dict(self) -> dict:
        return {
            'severity': self.severity,
            'blocked': self.blocked,
            'findings': [f.to_dict() for f in self.findings],
            'requires_acknowledgement': self.requires_acknowledgement(),
        }


# Warnings that are not merely informational: the epic requires an explicit confirmation
# before a migration carrying one of these may start.
_ACKNOWLEDGEABLE_CHECKS = frozenset({'secure_boot', 'vtpm', 'bitlocker', 'virtio_drivers',
                                     'power_state', 'merge_state', 'source_access'})


# ---------------------------------------------------------------------------
# Individual checks. Each takes normalised data and returns one Finding.
# ---------------------------------------------------------------------------

def check_power_state(state: str | None) -> Finding:
    """The VM must not be writing to its disks while they are read."""
    if not state:
        return Finding('power_state', BLOCKING, 'The VM power state is unknown.',
                       'Without a known state there is no way to tell whether the disks are '
                       'being written to. Read the VM again before migrating.')
    if state in SAFE_TO_READ_STATES:
        return Finding('power_state', OK, 'The VM is off and its disks are at rest.')
    if state in SAVED_STATES:
        return Finding('power_state', WARNING, f'The VM is in {state} state, not shut down.',
                       'Its disks are not being written to, but its memory is in a separate '
                       'saved-state file that the disks do not contain. Migrating the disks '
                       'alone discards that memory, and the guest boots as though it had lost '
                       'power. Shut the VM down properly for a clean migration.')
    return Finding('power_state', BLOCKING, f'The VM is {state.lower()}.',
                   'Copying the disks of a running VM produces an image that is crash-consistent '
                   'at best. Shut the VM down first.')


def check_checkpoints(checkpoint_count: int | None) -> Finding:
    """Checkpoints mean the live data is spread across a chain of files."""
    if checkpoint_count is None:
        return Finding('checkpoints', BLOCKING, 'The number of checkpoints is unknown.',
                       'A VM with checkpoints keeps its current data in differencing files. '
                       'Not knowing whether there are any means not knowing what would be copied.')
    if checkpoint_count == 0:
        return Finding('checkpoints', OK, 'The VM has no checkpoints.')
    return Finding('checkpoints', BLOCKING,
                   f'The VM has {checkpoint_count} checkpoint(s).',
                   'The current disk contents live in differencing files that must be merged '
                   'into the parent disks first. Delete the checkpoints and wait for the merge '
                   'to finish before migrating.')


def check_disk(disk: dict) -> Finding:
    """One virtual disk: is it a shape that can be imported at all?"""
    path = disk.get('path') or '<unnamed disk>'
    vhd_type = disk.get('vhd_type')

    if not vhd_type:
        return Finding('disk_type', BLOCKING, f'The type of {path} is unknown.',
                       'An unknown disk type is not treated as importable: it could be a '
                       'differencing disk or a pass-through, and copying either would lose data.')
    if vhd_type == 'Differencing' or disk.get('parent_path'):
        return Finding('disk_type', BLOCKING, f'{path} is a differencing disk.',
                       f'It depends on a parent ({disk.get("parent_path") or "unknown"}). '
                       'The chain has to be merged before the disk can be imported on its own.')
    if vhd_type not in IMPORTABLE_VHD_TYPES:
        return Finding('disk_type', BLOCKING, f'{path} is of type {vhd_type}.',
                       f'Only {" and ".join(sorted(IMPORTABLE_VHD_TYPES))} disks are imported. '
                       'A pass-through or physical disk has no file to copy.')
    return Finding('disk_type', OK, f'{path} is a {vhd_type.lower()} disk.')


def check_target_capacity(required_bytes: int, available_bytes: int | None) -> Finding:
    """Is there room on the target, with headroom for the copy itself?"""
    if available_bytes is None:
        return Finding('target_capacity', BLOCKING, 'Free space on the target is unknown.',
                       'Starting a transfer without knowing whether it fits risks filling the '
                       'target storage and affecting guests that already run on it.')

    needed = int(required_bytes * (1 + _TARGET_HEADROOM_FRACTION))
    if available_bytes < required_bytes:
        return Finding('target_capacity', BLOCKING, 'The target does not have room for the disks.',
                       f'{_gib(required_bytes)} needed, {_gib(available_bytes)} free.')
    if available_bytes < needed:
        return Finding('target_capacity', WARNING, 'The target has little room to spare.',
                       f'{_gib(required_bytes)} needed, {_gib(available_bytes)} free. '
                       f'That is under the {int(_TARGET_HEADROOM_FRACTION * 100)}% headroom this '
                       'check expects.')
    return Finding('target_capacity', OK, 'The target has room for the disks.',
                   f'{_gib(required_bytes)} needed, {_gib(available_bytes)} free.')


def check_network_mapping(adapters: list[dict], mapping: dict) -> Finding:
    """Every source NIC needs a target network chosen by a person.

    Guessing costs more than asking: a VM that arrives on the wrong VLAN is reachable by
    the wrong people, and that is not visible from the migration's own result.
    """
    if not adapters:
        return Finding('network_mapping', OK, 'The VM has no network adapters to map.')

    unmapped = [a.get('name') or a.get('mac_address') or '<unnamed adapter>'
                for a in adapters if not (mapping or {}).get(_adapter_key(a))]
    if unmapped:
        return Finding('network_mapping', BLOCKING,
                       f'{len(unmapped)} network adapter(s) have no target network.',
                       'Unmapped: ' + ', '.join(unmapped) + '. Each adapter needs a target '
                       'bridge chosen explicitly; a guessed network can put the VM somewhere '
                       'it should not be reachable from.')
    return Finding('network_mapping', OK,
                   f'All {len(adapters)} network adapter(s) are mapped to a target network.')


def check_firmware(generation: int | None) -> Finding:
    """Generation decides the target machine type and firmware."""
    if generation not in GENERATION_FIRMWARE:
        return Finding('firmware', BLOCKING,
                       f'Unsupported or unknown VM generation: {generation!r}.',
                       'Only generation 1 and 2 VMs are migrated, because the firmware and '
                       'machine type on the target are derived from it.')
    firmware, machine = GENERATION_FIRMWARE[generation]
    return Finding('firmware', OK,
                   f'Generation {generation} maps to {firmware.upper()} on {machine}.')


def check_secure_boot(secure_boot_enabled: bool | None, generation: int | None) -> Finding:
    """Secure Boot does not carry across as-is and may stop the guest booting."""
    if generation == 1 or not secure_boot_enabled:
        return Finding('secure_boot', OK, 'Secure Boot is not enabled on this VM.')
    return Finding('secure_boot', WARNING, 'Secure Boot is enabled on the source VM.',
                   "The Hyper-V Secure Boot template does not transfer to Proxmox. The imported "
                   'VM gets UEFI without a pre-enrolled template, and a guest that requires '
                   'Secure Boot may refuse to boot until it is configured on the target.')


def check_vtpm(vtpm_enabled: bool | None) -> Finding:
    """A virtual TPM is not moved, and whatever it protects will notice."""
    if not vtpm_enabled:
        return Finding('vtpm', OK, 'The VM has no virtual TPM.')
    return Finding('vtpm', WARNING, 'The VM has a virtual TPM.',
                   'The Hyper-V vTPM state is not transferred. A new, empty TPM on the target is '
                   'not the same device, so anything sealed against the original — BitLocker '
                   'above all — will not unseal. Suspend or decrypt before migrating.')


def check_bitlocker(bitlocker_state: str | None, vtpm_enabled: bool | None) -> Finding:
    """BitLocker status cannot be read from outside the guest, and says so."""
    if bitlocker_state == 'off':
        return Finding('bitlocker', OK, 'BitLocker is reported as off in the guest.')
    if bitlocker_state == 'on':
        return Finding('bitlocker', WARNING, 'BitLocker is enabled in the guest.',
                       'A volume sealed to the source vTPM will ask for a recovery key after the '
                       'migration. Suspend BitLocker before migrating, or have the key to hand.')
    # The honest answer, and the common one: nothing outside the guest can see this.
    severity = WARNING if vtpm_enabled else OK
    return Finding('bitlocker', severity, 'BitLocker status is unknown.',
                   'It can only be read from inside the guest, which this migration does not '
                   'enter. ' + ('The VM has a vTPM, so an encrypted volume is plausible and '
                                'would need its recovery key after the move.' if vtpm_enabled
                                else 'The VM has no vTPM, so a TPM-sealed volume is unlikely.'))


def check_virtio_drivers(driver_state: str | None, controller: str) -> Finding:
    """A Windows guest without VirtIO drivers will not see a VirtIO disk."""
    if controller not in ('virtio', 'scsi'):
        return Finding('virtio_drivers', OK,
                       f'The target uses a {controller} controller, which needs no VirtIO driver.')
    if driver_state == 'present':
        return Finding('virtio_drivers', OK, 'VirtIO drivers are reported as present in the guest.')
    return Finding('virtio_drivers', WARNING,
                   f'VirtIO driver state is {driver_state or "unknown"} and the target uses '
                   f'a {controller} controller.',
                   'This cannot be read from outside the guest. If the drivers are absent the VM '
                   'will not find its disk and will not boot. Either install them before '
                   'migrating or choose SATA or IDE on the target, which is slower but works '
                   'without a driver.')


def check_source_file_access(reachable_paths: dict, probed: bool = True) -> Finding:
    """Every disk file has to be readable before anything else is worth doing.

    `probed` separates "checked and failed" from "not checked yet". A runner never passes
    it, so an empty result on the way to an irreversible copy still blocks. Only a caller
    that knows no transport is configured — the preflight preview, before the file share
    is set up — may set it False, and then the answer is an explicit unknown somebody has
    to confirm rather than a silent OK.
    """
    if not reachable_paths and not probed:
        # The flag only speaks for an empty result. A caller that handed over findings has
        # probed, whatever it claims, and a real failure must never be downgraded by it.
        return Finding('source_access', WARNING,
                       'Whether PegaProx itself can read the disk files has not been checked.',
                       'The transport reads the disk files over a file share, and that share is '
                       'not configured here yet. Until it is, this says nothing about whether a '
                       'migration would get past its first byte.')
    if not reachable_paths:
        return Finding('source_access', BLOCKING, 'No disk files were checked for readability.',
                       'The transport has to prove it can read each disk before a migration '
                       'starts, or the run fails after a target VM already exists.')
    unreachable = [path for path, ok in reachable_paths.items() if not ok]
    if unreachable:
        return Finding('source_access', BLOCKING,
                       f'{len(unreachable)} disk file(s) cannot be read.',
                       'Unreachable: ' + ', '.join(unreachable) + '. Check the file share, its '
                       'credentials and the permissions on the path.')
    return Finding('source_access', OK,
                   f'All {len(reachable_paths)} disk file(s) are readable over the transport.')


# ---------------------------------------------------------------------------
# The whole run
# ---------------------------------------------------------------------------

def run_preflight(vm: dict, target: dict, options: dict | None = None) -> PreflightReport:
    """Every check, against one VM and one chosen target.

    Runs all of them rather than stopping at the first blocker: somebody fixing a
    migration wants the whole list, not one item at a time across four attempts.
    """
    options = options or {}
    report = PreflightReport()

    report.add(check_power_state(vm.get('state')))
    report.add(check_checkpoints(vm.get('checkpoint_count')))
    report.add(check_firmware(vm.get('generation')))

    disks = vm.get('disks') or []
    if not disks:
        report.add(Finding('disk_type', BLOCKING, 'The VM has no virtual disks to migrate.',
                           'A VM with no disk file has nothing to import.'))
    for disk in disks:
        report.add(check_disk(disk))

    required = sum(int(d.get('size') or 0) for d in disks)
    report.add(check_target_capacity(required, target.get('available_bytes')))

    report.add(check_network_mapping(vm.get('network_adapters') or [],
                                     options.get('network_map') or {}))
    report.add(check_secure_boot(vm.get('secure_boot_enabled'), vm.get('generation')))
    report.add(check_vtpm(vm.get('vtpm_enabled')))
    report.add(check_bitlocker(vm.get('bitlocker_state'), vm.get('vtpm_enabled')))
    report.add(check_virtio_drivers(vm.get('virtio_driver_state'),
                                    options.get('controller', 'scsi')))
    report.add(check_source_file_access(options.get('reachable_paths') or {},
                                       probed=options.get('source_access_probed', True)))
    return report


def unacknowledged(report: PreflightReport, acknowledged: list | None) -> list[str]:
    """Which confirmations the caller still owes before the migration may start."""
    confirmed = set(acknowledged or [])
    return [check for check in report.requires_acknowledgement() if check not in confirmed]


def may_start(report: PreflightReport, acknowledged: list | None = None) -> tuple[bool, str]:
    """The single gate a runner asks before doing anything irreversible."""
    if report.blocked:
        blockers = '; '.join(f.summary for f in report.blocking_findings)
        return False, f'Preflight blocked the migration: {blockers}'
    missing = unacknowledged(report, acknowledged)
    if missing:
        return False, ('These risks have to be confirmed before the migration can start: '
                       + ', '.join(missing))
    return True, ''


def _adapter_key(adapter: dict) -> str:
    """How one source adapter is addressed in a network map.

    The MAC is preferred because it is unique and stable; the name is a fallback for an
    adapter whose MAC is still dynamic and therefore not yet assigned.
    """
    return adapter.get('mac_address') or adapter.get('name') or ''


def _gib(num_bytes: int) -> str:
    """Bytes as GiB, for a message somebody has to act on."""
    return f'{num_bytes / (1024 ** 3):.1f} GiB'
