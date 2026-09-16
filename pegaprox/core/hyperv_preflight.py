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

# What the import builds unless somebody chooses otherwise. Spelled here rather than
# imported from the runner: this module is the one a preflight can be run from on its own,
# and a circular import would be the price of sharing the constant.
DEFAULT_TARGET_CONTROLLER = 'sata'

# Generation to target firmware. Gen 1 boots BIOS, Gen 2 boots UEFI, and getting this wrong
# produces a VM that is imported perfectly and does not boot.
#
# The machine names here are the ones that go on the wire, not the ones the hardware is
# called after: Proxmox refuses 'i440fx' with HTTP 400 and means 'pc' by it. A check that
# announces a machine type the target would reject describes a migration that cannot
# happen.
GENERATION_FIRMWARE = {1: ('seabios', 'pc'), 2: ('ovmf', 'q35')}

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
_ACKNOWLEDGEABLE_CHECKS = frozenset({'vtpm', 'bitlocker', 'virtio_drivers',
                                     'power_state', 'merge_state', 'source_access',
                                     'automatic_start', 'orderly_shutdown',
                                     # A guest that arrives under a new MAC is a new
                                     # machine to every switch, lease and licence that
                                     # knew it by the old one. Nothing downstream reports
                                     # that, so it is confirmed here or not at all.
                                     'mac_addresses'})


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


#: Values of AutomaticStartAction that bring a VM back up on its own.
_RESTARTS_BY_ITSELF = ('Start', 'StartIfRunning')


def check_automatic_start(action: str | None, delay_seconds: int = 0) -> Finding:
    """Whether the source can come back up while its copy exists.

    Measured across a production estate of 159 VMs: 147 are set to StartIfRunning, 12 to
    Nothing, none to Start. StartIfRunning fires when the *host* boots, and only for VMs
    that were running when it went down - so a source that was shut down for the migration
    stays down, and the window is narrow: the host restarting while the source is still
    running. Narrow is not closed, and the failure it ends in is the one this whole patch
    is built to avoid, two copies of one machine running at once.
    """
    if not action:
        return Finding('automatic_start', WARNING,
                       'Whether the source restarts on its own is unknown.',
                       'The host did not report the VM\'s automatic start action. Check it on '
                       'the Hyper-V host before starting the copy, so the original cannot come '
                       'back up beside it.')
    if action not in _RESTARTS_BY_ITSELF:
        return Finding('automatic_start', OK,
                       f'The source will not start on its own (automatic start: {action}).')
    when = 'whenever the host boots' if action == 'Start' else (
        'when the host boots, if it was running at the time')
    delay = f' after {delay_seconds} seconds' if delay_seconds else ''
    return Finding('automatic_start', WARNING,
                   f'The source is set to start {when}{delay}.',
                   'Shut the source down before the migration and it stays down. But if the '
                   'Hyper-V host restarts while the source is still running, it comes back up - '
                   'and if the copy has been started by then, one machine is running twice, '
                   'with both halves writing to their own disks. Set the VM\'s automatic start '
                   'action to Nothing on the Hyper-V host for the duration of the migration.')


def check_orderly_shutdown(can_shut_down: bool | None, state: str | None) -> Finding:
    """Whether this guest can be asked to shut itself down before it is copied.

    It matters before the migration, not during it: a guest that cannot be shut down
    through Hyper-V has to be shut down from inside, and finding that out after somebody
    has scheduled a window is finding it out too late. Forcing it off instead is not on
    offer - that leaves the disks as an unexpected power cut would, which is the one state
    a migration must not copy.
    """
    if state in SAFE_TO_READ_STATES:
        return Finding('orderly_shutdown', OK,
                       'The VM is already off, so nothing has to shut it down.')
    if can_shut_down is None:
        return Finding('orderly_shutdown', WARNING,
                       'Whether this guest can be shut down through Hyper-V is unknown.',
                       'The host reported no integration services for it. Check on the '
                       'Hyper-V host that the shutdown service is enabled, or plan to shut '
                       'the guest down from inside before the migration.')
    if can_shut_down:
        return Finding('orderly_shutdown', OK,
                       'The guest can be asked to shut itself down.')
    return Finding('orderly_shutdown', WARNING,
                   'This guest cannot be asked to shut itself down.',
                   'Its shutdown integration service is missing, disabled, or not '
                   'responding, so Hyper-V cannot deliver the request. Shut the guest down '
                   'from inside, or enable the service on the Hyper-V host. It will not be '
                   'forced off: that would leave its disks in the state an unexpected power '
                   'cut leaves them in, which is exactly what a migration must not copy.')


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

    unmapped = [adapter_label(a, i)
                for i, a in enumerate(adapters)
                if not (mapping or {}).get(adapter_key(a, i))]
    if unmapped:
        return Finding('network_mapping', BLOCKING,
                       f'{len(unmapped)} network adapter(s) have no target network.',
                       'Unmapped: ' + ', '.join(unmapped) + '. Each adapter needs a target '
                       'bridge chosen explicitly; a guessed network can put the VM somewhere '
                       'it should not be reachable from.')
    return Finding('network_mapping', OK,
                   f'All {len(adapters)} network adapter(s) are mapped to a target network.')


def check_mac_addresses(adapters: list[dict]) -> Finding:
    """Will every adapter arrive with the address it has on the source?

    It has to. A MAC is what the rest of the network knows a guest by: DHCP reservations,
    static leases, port security on the switches, licence bindings and firewall rules are
    all written against it. A guest that comes up with a different one is a different
    machine to all of them, and nothing about the migration's own result shows it.

    The import carries the MAC across unchanged — including a dynamic one, which is a real
    address that Hyper-V happened to pick rather than an operator. Only the spelling
    changes, because Proxmox refuses Hyper-V's separator-free form.

    The exception is an adapter that has never had an address at all: Hyper-V assigns a
    dynamic MAC at the VM's first start and reports zeroes until then. There is nothing to
    carry over, so the target assigns one — and that is the case worth stopping an operator
    for, because it is the one where the guest arrives under a new identity.
    """
    if not adapters:
        return Finding('mac_addresses', OK, 'The VM has no network adapters.')

    unassigned = [adapter_label(a, i) for i, a in enumerate(adapters)
                  if is_unset_mac(a.get('mac_address') or '')]
    if not unassigned:
        return Finding('mac_addresses', OK,
                       'Every adapter keeps the MAC address it has on the source.')
    return Finding('mac_addresses', WARNING,
                   f'{len(unassigned)} network adapter(s) have no MAC address yet.',
                   'The source reports all zeroes for: ' + ', '.join(unassigned) + '. '
                   'Hyper-V assigns a dynamic address at a VM\'s first start, so a VM that '
                   'has never run has none to carry over and the target will assign its '
                   'own. Anything that identifies this guest by its MAC — a DHCP '
                   'reservation, a switch port, a licence — will not recognise it. Start '
                   'the VM once on the source if it has to keep a specific address.')


def check_vlan_mapping(adapters: list[dict], vlan_map: dict | None = None) -> Finding:
    """Say which VLAN each adapter will arrive on, and name the ones that get none.

    A VLAN is not something a migration may quietly get wrong: an adapter on the wrong
    one is reachable by the wrong people, and the guest looks healthy either way. So the
    answer is shown before the run rather than discovered after it.

    An adapter whose Hyper-V mode carries more than one id -- `Trunk`, or the private-VLAN
    role `Isolated` -- has no single number to carry over. It arrives untagged and is
    named here, because a trunk port put on one guessed VLAN looks like it worked.
    """
    from pegaprox.core.hyperv_xhm import vlan_for_adapter, VLAN_MODE_ACCESS

    if not adapters:
        return Finding('vlan_mapping', OK, 'The VM has no network adapters.')

    vlan_map = vlan_map or {}
    multi_mode = []
    untagged = []
    tagged = []
    for index, adapter in enumerate(adapters):
        label = adapter_label(adapter, index)
        mode = (adapter.get('vlan_mode') or '').strip()
        key = adapter_key(adapter, index)
        chosen = vlan_map.get(key) if key in vlan_map else vlan_for_adapter(adapter)
        try:
            vlan = int(chosen)
        except (TypeError, ValueError):
            vlan = 0
        if mode and mode not in (VLAN_MODE_ACCESS, 'Untagged'):
            multi_mode.append(f'{label} ({mode})')
        elif vlan:
            tagged.append(f'{label} -> VLAN {vlan}')
        else:
            untagged.append(label)

    if multi_mode:
        return Finding('vlan_mapping', WARNING,
                       f'{len(multi_mode)} adapter(s) carry more than one VLAN on the source.',
                       ', '.join(multi_mode) + '. A trunk or isolated adapter has no single '
                       'VLAN id, so it arrives on the target bridge without a tag. Set the '
                       'target up by hand if the guest needs those VLANs.')
    if untagged:
        return Finding('vlan_mapping', WARNING,
                       f'{len(untagged)} adapter(s) will arrive without a VLAN tag.',
                       'Untagged: ' + ', '.join(untagged) + '. They land on whatever the '
                       "target bridge's native VLAN is, which is not necessarily the "
                       'network the source was on.')
    return Finding('vlan_mapping', OK,
                   f'All {len(adapters)} adapter(s) have a VLAN.', ', '.join(tagged))


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
    """Secure Boot is reproduced on the target, and says how.

    Proxmox ships an OVMF variable store with Microsoft's certificates already enrolled,
    and the import asks for it whenever the source had Secure Boot on
    (`efidisk0=...,pre-enrolled-keys=1`). That is the same set of keys the Hyper-V
    "Microsoft Windows" template holds, so a Windows guest boots with Secure Boot on as it
    did before. What is NOT carried across is a custom or third-party template, because
    nothing here can read which certificates it contained.
    """
    if generation == 1 or secure_boot_enabled is False:
        return Finding('secure_boot', OK, 'Secure Boot is not enabled on this VM.')
    if secure_boot_enabled is None:
        # The host did not report it. Saying "not enabled" would be a guess, and the import
        # does not enrol keys under a guest it cannot read — so a guest that DID need
        # Secure Boot arrives without it and may refuse to boot.
        return Finding('secure_boot', WARNING, 'This host did not report the Secure Boot state.',
                       'The imported VM gets a UEFI variable store with no keys enrolled, '
                       'because enrolling them under a guest whose loader might be unsigned '
                       'would stop it booting at all. If this guest was using Secure Boot, '
                       'enrol the keys on the target afterwards.')
    return Finding('secure_boot', OK, 'Secure Boot is enabled and is reproduced on the target.',
                   "The imported VM gets a UEFI variable store with Microsoft's keys already "
                   'enrolled, which is what the standard Hyper-V template holds. A guest that '
                   'was booting under a custom Secure Boot template will need its own '
                   'certificates enrolled on the target, because they cannot be read from here.')


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


def check_virtio_drivers(driver_state: str | None, controller: str,
                         drivers_injected: bool = False) -> Finding:
    """A Windows guest without VirtIO drivers will not see a VirtIO disk."""
    if controller not in ('virtio', 'scsi'):
        return Finding('virtio_drivers', OK,
                       f'The target uses a {controller} controller, which needs no VirtIO driver.')
    if driver_state == 'present':
        return Finding('virtio_drivers', OK, 'VirtIO drivers are reported as present in the guest.')
    if drivers_injected:
        # Asking the operator to confirm a risk the migration is about to remove reads as a
        # contradiction: the box above this list says the drivers will be written in, and
        # this said the VM would not find its disk. Neither of the two cases the injection
        # cannot cover ends in a VM that fails to start - a driver the loader refuses, or a
        # guest that is not Windows, both leave it on the controller it arrived on.
        return Finding('virtio_drivers', OK,
                       'The migration writes the VirtIO drivers into the disk before the VM '
                       'is started.',
                       'Should this guest turn out to be one the drivers cannot be '
                       'installed into - a Windows version whose driver carries no '
                       'signature the loader accepts, or a guest that is not Windows - the '
                       'VM is built on its compatible controller instead, and the '
                       'migration log says so.')
    #: Reached only when the VM is to be built on a VirtIO controller and nothing is going
    #: to put the drivers there. That is a VM which starts into a disk it cannot address.
    return Finding('virtio_drivers', WARNING,
                   f'VirtIO driver state is {driver_state or "unknown"} and the target uses '
                   f'a {controller} controller, with no drivers being installed.',
                   'Whether the guest already has them cannot be read from outside it, and '
                   'if it does not, the VM will not find its disk and will not boot. Either '
                   'let the migration write the drivers in, or build the VM on the '
                   'compatible hardware every guest already has drivers for and switch to '
                   'VirtIO afterwards.')


def check_source_file_access(reachable_paths: dict, probed: bool = True,
                             host_check: dict | None = None) -> Finding:
    """Every disk file has to be readable before anything else is worth doing.

    `probed` separates "checked and failed" from "not checked yet". A runner never passes
    it, so an empty result on the way to an irreversible copy still blocks. Only a caller
    that knows no transport is configured — the preflight preview, before the file share
    is set up — may set it False, and then the answer is an explicit unknown.

    `host_check` is what a real mount from a target node last found. Whether cifs-utils is
    installed, whether TCP 445 is open and whether the account may read the share is a
    property of the host and the network, not of the VM being looked at — the same answer
    for every guest on it. Once that has been measured, this stops being a question
    somebody confirms per VM and becomes a fact with a timestamp. It never turns a real
    failure into an OK: the per-disk probe below still decides, and a host check that
    FAILED is reported as a blocker rather than as an unknown.
    """
    if not reachable_paths and not probed and host_check:
        when = host_check.get('at_text') or 'earlier'
        if host_check.get('ok'):
            shares = ', '.join(host_check.get('shares') or []) or 'the configured share'
            return Finding('source_access', OK,
                           f'A target node read this host over SMB ({when}).',
                           f'{host_check.get("node") or "The target node"} mounted {shares} '
                           'read-only and listed it, so cifs-utils, the route to TCP 445 and '
                           "the account's read access are all in place. The disks of this "
                           'particular VM are still probed individually before anything is '
                           'copied.')
        return Finding('source_access', BLOCKING,
                       f'A target node could not read this host over SMB ({when}).',
                       (host_check.get('error') or 'The mount failed.') + ' Until this is '
                       'fixed no migration from this host can move a byte, so it is a host '
                       'problem to solve once rather than a risk to accept per VM.')

    if not reachable_paths and not probed:
        # The flag only speaks for an empty result. A caller that handed over findings has
        # probed, whatever it claims, and a real failure must never be downgraded by it.
        return Finding('source_access', WARNING,
                       'Whether PegaProx itself can read the disk files has not been checked.',
                       'The target node mounts the disks over SMB and reads them itself, so three '
                       'things have to be true before a transfer moves a byte: the drive holding '
                       'the VHDX files is shared on the Hyper-V host — either as a dedicated '
                       'read-only share named in this host\'s share map, or as the built-in '
                       'administrative share, which needs the registered account to be a local '
                       'administrator; the registered Hyper-V account may read that share; and '
                       'the target node has cifs-utils installed and can reach the host on TCP '
                       '445. None of that has been tested here, so this says nothing about '
                       'whether a migration would get past its first byte.')
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
    report.add(check_orderly_shutdown(vm.get('can_shut_itself_down'), vm.get('state')))
    report.add(check_automatic_start(vm.get('automatic_start_action'),
                                     vm.get('automatic_start_delay_seconds') or 0))
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
    report.add(check_vlan_mapping(vm.get('network_adapters') or [],
                                  options.get('vlan_map') or {}))
    report.add(check_mac_addresses(vm.get('network_adapters') or []))
    report.add(check_secure_boot(vm.get('secure_boot_enabled'), vm.get('generation')))
    report.add(check_vtpm(vm.get('vtpm_enabled')))
    report.add(check_bitlocker(vm.get('bitlocker_state'), vm.get('vtpm_enabled')))
    # The controller the caller is actually going to build. Defaulting to 'scsi' here made
    # the report warn about drivers for hardware the import would not create: the product's
    # own default is the compatible one, and the plan said 'sata' two lines further down in
    # the same document.
    report.add(check_virtio_drivers(vm.get('virtio_driver_state'),
                                    options.get('controller') or DEFAULT_TARGET_CONTROLLER,
                                    bool(options.get('drivers_injected'))))
    report.add(check_source_file_access(options.get('reachable_paths') or {},
                                       probed=options.get('source_access_probed', True),
                                       host_check=options.get('host_transfer_check')))
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


def adapter_label(adapter: dict, index: int) -> str:
    """What one adapter is called in a message an operator has to act on.

    Always carries its position, because the name and the MAC both stop distinguishing two
    adapters on the same VM — which is the case this whole pair of functions exists for.
    """
    parts = [part for part in (adapter.get('name'), adapter.get('mac_address')) if part]
    return f"#{index + 1}" + (f" ({' / '.join(parts)})" if parts else '')


def is_unset_mac(mac: str) -> bool:
    """Is this the all-zero MAC Hyper-V reports for an adapter that has never been used?

    Hyper-V assigns a dynamic MAC when the VM first starts. Until then the adapter reports
    zeroes, and passing those on is refused by the target rather than ignored.
    """
    return not set((mac or '').replace(':', '').replace('-', '')) - {'0'}


def adapter_key(adapter: dict, index: int) -> str:
    """How one source adapter is addressed in a network map. Unique per adapter.

    The MAC alone is not unique, which is the whole reason this takes an index. Hyper-V
    reports all zeroes until a VM with a dynamic MAC has started once, and every adapter
    on a VM is called "Network Adapter" — or its localised spelling — unless somebody
    renamed it. Two adapters like that produced one key, and the consequences were
    invisible in exactly the wrong way: the wizard's two dropdowns read and wrote the same
    entry, so changing one changed the other, and the run mapped both adapters to whatever
    the surviving value said. A VM on the wrong VLAN is reachable by the wrong people, and
    nothing about the migration's own result shows it.

    The fallback is the adapter's position in the VM's list, which is the order the plan,
    the preflight and the run all enumerate them in.
    """
    mac = (adapter.get('mac_address') or '').strip()
    if mac and not is_unset_mac(mac):
        return mac
    return f'adapter{index + 1}'


def _gib(num_bytes: int) -> str:
    """Bytes as GiB, for a message somebody has to act on."""
    return f'{num_bytes / (1024 ** 3):.1f} GiB'
