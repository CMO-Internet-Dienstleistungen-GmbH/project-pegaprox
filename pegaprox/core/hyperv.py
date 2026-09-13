"""The Hyper-V host as PegaProx sees it.

Sits between the transport, which knows PowerShell, and everything above, which must not.
Its whole job is to turn what a Hyper-V host says into the vocabulary the rest of the
product already speaks, and to refuse to invent anything on the way.

Two rules run through every function here.

**An unknown value stays unknown.** Hyper-V can decline to answer — a disk it cannot read,
a security setting on a VM version that has none. The temptation is to substitute a
plausible default, and the cost of doing so is that preflight, which is built to block on
uncertainty, sees a confident answer instead. So a missing value is `None` and travels as
`None`.

**Nothing here decides anything.** Whether a VM may migrate is preflight's judgement, made
over this data; whether to act on a host is the facade's. This module reads, translates,
and says what it saw.
"""

from __future__ import annotations

import logging

from pegaprox.core import hyperv_scripts as scripts
from pegaprox.core.hyperv_client import HyperVPowerShellClient
from pegaprox.core.hyperv_errors import HyperVError

logger = logging.getLogger(__name__)

# Msvm_ComputerSystem.OperationalStatus[1], the documented place a background operation
# shows up. Numbers rather than the status descriptions, which are not enumerated anywhere
# and would therefore be matching on text nobody promised to keep.
WMI_STATUS_CREATING_SNAPSHOT = 32768
WMI_STATUS_APPLYING_SNAPSHOT = 32769
WMI_STATUS_DELETING_SNAPSHOT = 32770
WMI_STATUS_MERGING_DISKS = 32772
WMI_STATUS_EXPORTING = 32773
WMI_STATUS_MIGRATING = 32774

#: Background operations during which the disks must not be read. A merge above all: the
#: differencing file is being written into its parent, and a copy taken now catches the
#: file mid-consolidation.
WMI_STATUSES_BLOCKING_A_READ = frozenset({
    WMI_STATUS_CREATING_SNAPSHOT, WMI_STATUS_APPLYING_SNAPSHOT, WMI_STATUS_DELETING_SNAPSHOT,
    WMI_STATUS_MERGING_DISKS, WMI_STATUS_EXPORTING, WMI_STATUS_MIGRATING,
})

# Msvm_ComputerSystem.OperationalStatus[0]: 2 is OK, 3 is Degraded, which the documentation
# describes as a VM that "can only be turned off or deleted".
WMI_PRIMARY_OK = 2
WMI_PRIMARY_DEGRADED = 3

# Hyper-V reports memory in bytes; the rest of PegaProx talks megabytes for VM memory.
_BYTES_PER_MB = 1024 * 1024

# How Hyper-V's controller types map onto what a Proxmox target can offer. IDE exists on
# generation 1 only, and its natural target is SATA rather than VirtIO, because a guest
# that booted from IDE has no reason to carry VirtIO drivers.
CONTROLLER_TARGET_HINT = {'IDE': 'sata', 'SCSI': 'scsi'}

# Hyper-V writes MAC addresses without separators; Proxmox wants colons. Keeping the source
# form as well means the value a person sees in Hyper-V Manager is the value they can find
# in the migration screen.
_MAC_GROUPS = 6


def normalise_vm_summary(raw: dict) -> dict:
    """One VM from the inventory listing, in PegaProx's vocabulary."""
    return {
        'guid': raw.get('Id'),
        'name': raw.get('Name'),
        'state': raw.get('State'),
        'generation': raw.get('Generation'),
        'cpu_count': raw.get('ProcessorCount'),
        'memory_startup_bytes': raw.get('MemoryStartup'),
        'memory_mb': _bytes_to_mb(raw.get('MemoryStartup')),
        'dynamic_memory_enabled': raw.get('DynamicMemoryEnabled'),
        'configuration_version': raw.get('Version'),
        'uptime_seconds': raw.get('Uptime'),
    }


def normalise_disk(raw: dict) -> dict:
    """One virtual disk, including whether the host could read it at all.

    `read_error` is the load-bearing field. A disk Get-VHD could not open has no type and
    no size, and preflight must see that as unknown rather than as a disk with no
    properties worth worrying about.
    """
    return {
        'path': raw.get('Path'),
        'controller_type': raw.get('ControllerType'),
        'controller_number': raw.get('ControllerNumber'),
        'controller_location': raw.get('ControllerLocation'),
        'vhd_format': raw.get('VhdFormat'),
        'vhd_type': raw.get('VhdType'),
        'parent_path': raw.get('ParentPath'),
        'file_size': raw.get('FileSize'),
        'size': raw.get('Size'),
        'attached': raw.get('Attached'),
        'disk_identifier': raw.get('DiskIdentifier'),
        'read_error': raw.get('VhdReadError'),
        'target_controller_hint': CONTROLLER_TARGET_HINT.get(raw.get('ControllerType'), 'scsi'),
    }


def normalise_nic(raw: dict) -> dict:
    """One network adapter, with its MAC in both the source and the target spelling."""
    raw_mac = raw.get('MacAddress')
    return {
        'name': raw.get('Name'),
        'mac_address': raw_mac,
        'mac_address_colons': format_mac(raw_mac),
        'dynamic_mac': raw.get('DynamicMacAddressEnabled'),
        'switch_name': raw.get('SwitchName'),
        'connected': raw.get('Connected'),
        'vlan_mode': raw.get('VlanMode'),
        'vlan_id': raw.get('VlanId'),
    }


def normalise_checkpoint(raw: dict) -> dict:
    return {
        'name': raw.get('Name'),
        'type': raw.get('SnapshotType'),
        'created_at': raw.get('CreationTime'),
        'parent': raw.get('ParentSnapshotName'),
    }


def normalise_vm_detail(raw: dict) -> dict:
    """A whole VM, in the shape preflight and the migration planner consume.

    The derived fields — `secure_boot_enabled`, `vtpm_enabled`, `checkpoint_count` — exist
    because every caller would otherwise re-derive them from the raw structure, and two
    callers deriving `secure_boot_enabled` differently is a bug nobody would look for.
    """
    firmware = raw.get('Firmware') or {}
    disks = [normalise_disk(d) for d in raw.get('Disks') or []]
    nics = [normalise_nic(n) for n in raw.get('NetworkAdapters') or []]
    checkpoints = [normalise_checkpoint(c) for c in raw.get('Checkpoints') or []]

    detail = normalise_vm_summary(raw)
    detail.update({
        'disks': disks,
        'network_adapters': nics,
        'dvd_drives': [{'path': d.get('Path'),
                        'controller_number': d.get('ControllerNumber'),
                        'controller_location': d.get('ControllerLocation')}
                       for d in raw.get('DvdDrives') or []],
        'checkpoints': checkpoints,
        'checkpoint_count': len(checkpoints),
        'secure_boot_enabled': _secure_boot_enabled(firmware.get('SecureBoot')),
        'secure_boot_template': firmware.get('SecureBootTemplate'),
        'boot_order': firmware.get('BootOrder') or [],
        'vtpm_enabled': raw.get('TpmEnabled'),
        'memory_minimum_bytes': raw.get('MemoryMinimum'),
        'memory_maximum_bytes': raw.get('MemoryMaximum'),
        # Neither can be seen from outside the guest. Saying so is the honest answer and the
        # one preflight is built to handle; a default of "fine" would silence a real risk.
        'bitlocker_state': None,
        'virtio_driver_state': None,
        'total_disk_bytes': sum(d['size'] for d in disks if d.get('size')),
    })
    return detail


def format_mac(raw_mac: str | None) -> str | None:
    """Hyper-V's separator-free MAC as the colon-separated form a target expects."""
    if not raw_mac:
        return None
    cleaned = raw_mac.replace('-', '').replace(':', '').strip()
    if len(cleaned) != _MAC_GROUPS * 2:
        # An unexpected length is passed through rather than reshaped into something that
        # looks valid and is not.
        return raw_mac
    return ':'.join(cleaned[i:i + 2] for i in range(0, len(cleaned), 2)).lower()


def _secure_boot_enabled(value) -> bool | None:
    """Hyper-V reports Secure Boot as the strings 'On'/'Off'. Anything else is unknown."""
    if value is None:
        return None
    text = str(value).strip().lower()
    if text in ('on', 'true'):
        return True
    if text in ('off', 'false'):
        return False
    return None


def _bytes_to_mb(value) -> int | None:
    if value is None:
        return None
    try:
        return int(value) // _BYTES_PER_MB
    except (TypeError, ValueError):
        return None


class HyperVManager:
    """Reads one Hyper-V host and prepares its VMs for migration.

    Holds no state beyond the client and the host's identity: every answer is read when it
    is asked for. A cached inventory would be wrong precisely when it matters, which is in
    the minutes between a person choosing a VM and the migration reading its disks.
    """

    def __init__(self, cluster_id: str, client: HyperVPowerShellClient, host: str = ''):
        self.cluster_id = cluster_id
        self.host = host
        self._client = client

    # -- reading ---------------------------------------------------------------------

    def host_facts(self) -> dict:
        """Versions of the host, its PowerShell and its Hyper-V module."""
        raw = self._client.run_json(scripts.HOST_FACTS) or {}
        return {
            'os_caption': raw.get('OSCaption'),
            'os_version': raw.get('OSVersion'),
            'powershell_version': raw.get('PSVersion'),
            'hyperv_module_version': raw.get('HyperVModule'),
        }

    def verify_properties(self) -> dict:
        """Check that this host's objects carry the members this product reads.

        Microsoft documents the Hyper-V cmdlets' parameters and almost none of their
        returned objects' properties, so most of the names used here are convention rather
        than contract. A renamed or absent property would not raise: it would read as null,
        and a VM would quietly report an unknown generation or no checkpoints.

        Run once when a host is connected. Missing members are reported rather than raised,
        because a documentation gap should show up as a clear warning about this host, not
        as a product that refuses to start.
        """
        raw = self._client.run_json(scripts.VERIFY_PROPERTIES) or {}
        missing = raw.get('Missing') or {}
        # PowerShell serialises an empty array as null, so "nothing missing" arrives as
        # None and has to be read as the empty list it means.
        normalised = {group: list(names) if isinstance(names, list) else
                      ([] if names is None else [str(names)])
                      for group, names in missing.items()}
        problems = {group: names for group, names in normalised.items() if names}
        return {
            'inspected_vm': raw.get('InspectedVM'),
            'missing': problems,
            'complete': not problems,
        }

    def list_vms(self) -> list[dict]:
        """Every VM on the host, without the per-VM detail that costs a call each."""
        raw = self._client.run_json(scripts.VM_INVENTORY) or []
        return [normalise_vm_summary(vm) for vm in raw]

    def get_vm(self, vm_guid: str) -> dict:
        """Everything about one VM that a migration has to reproduce or refuse."""
        raw = self._client.run_json(_with_parameters(scripts.VM_DETAIL, VmId=vm_guid))
        if not raw:
            raise HyperVError(f'The host returned nothing for VM {vm_guid}.', kind='unknown')
        return normalise_vm_detail(raw)

    def get_vm_state(self, vm_guid: str) -> dict:
        """The cheapest question there is, asked again right before disks are read.

        Between preflight and the transfer somebody can start the VM or take a checkpoint.
        Re-reading costs one round trip and is the only thing standing between that and a
        copy taken from underneath a running guest.
        """
        raw = self._client.run_json(_with_parameters(scripts.VM_STATE, VmId=vm_guid)) or {}
        return {'guid': raw.get('Id'), 'state': raw.get('State'),
                'checkpoint_count': raw.get('CheckpointCount')}

    def get_merge_state(self, vm_guid: str) -> dict:
        """Is a background operation still touching this VM's disks?

        The one question a checkpoint deletion cannot answer about itself. Remove-VMSnapshot
        returns when the checkpoint entry is gone and the merge of its differencing file into
        the parent carries on afterwards, invisible to the checkpoint list. Reading the disks
        in that window copies a file Hyper-V is still writing into.
        """
        raw = self._client.run_json(_with_parameters(scripts.VM_MERGE_STATE, VmId=vm_guid)) or {}
        secondary = raw.get('SecondaryStatus')
        return {
            'primary_status': raw.get('PrimaryStatus'),
            'secondary_status': secondary,
            'enabled_state': raw.get('EnabledState'),
            'health_state': raw.get('HealthState'),
            'merging': secondary == WMI_STATUS_MERGING_DISKS,
            'busy': secondary in WMI_STATUSES_BLOCKING_A_READ,
            'degraded': raw.get('PrimaryStatus') == WMI_PRIMARY_DEGRADED,
        }

    def disks_are_safe_to_read(self, vm_guid: str) -> tuple[bool, str]:
        """The last gate before a transfer opens a disk file.

        Asked immediately before reading, not at preflight time: between somebody approving
        a migration and the copy starting, a checkpoint can be taken, a merge can begin, or
        the VM can be started by another person entirely.
        """
        state = self.get_vm_state(vm_guid)
        if state.get('state') != 'Off':
            return False, f"The VM is {state.get('state')}, not off."
        if state.get('checkpoint_count'):
            return False, f"The VM has {state['checkpoint_count']} checkpoint(s) again."

        merge = self.get_merge_state(vm_guid)
        if merge.get('degraded'):
            return False, 'The host reports the VM as degraded; its configuration storage may be unreachable.'
        if merge.get('merging'):
            return False, 'Hyper-V is still merging a deleted checkpoint into its parent disk.'
        if merge.get('busy'):
            return False, (f"A background operation (status {merge.get('secondary_status')}) "
                           'is still running on this VM.')
        return True, ''

    def get_disk_chains(self, vm_guid: str) -> list[dict]:
        """Each disk with its parent chain walked to the root.

        A chain longer than one link means checkpoint data that has not been merged into
        the parent. The chain is read rather than the checkpoint list, because the list
        empties the moment a checkpoint is deleted while the merge is still running.
        """
        raw = self._client.run_json(_with_parameters(scripts.VM_DISK_CHAIN, VmId=vm_guid)) or []
        return [{'path': entry.get('Path'),
                 'chain_length': entry.get('ChainLength'),
                 'chain': [{'path': link.get('Path'), 'vhd_type': link.get('VhdType'),
                            'size': link.get('Size'), 'file_size': link.get('FileSize')}
                           for link in entry.get('Chain') or []]}
                for entry in raw]

    def list_isos(self, library_paths: list[str]) -> list[dict]:
        """ISO files under the paths an operator configured.

        The paths are given, never discovered: walking a customer's filesystem to find ISOs
        is not something this product should do uninvited.
        """
        if not library_paths:
            return []
        raw = self._client.run_json(
            _with_parameters(scripts.ISO_LIBRARY, Paths=library_paths)) or []
        return [{'path': iso.get('Path'), 'name': iso.get('Name'),
                 'size': iso.get('Length'), 'modified_at': iso.get('LastModified')}
                for iso in raw]

    # -- acting ----------------------------------------------------------------------

    def start_vm(self, vm_guid: str) -> dict:
        """Start a VM, for preparation only. A migration never starts a source VM."""
        raw = self._client.run_action(
            _with_parameters(scripts.START_VM, VmId=vm_guid),
            f'start Hyper-V VM {vm_guid}') or {}
        return {'guid': raw.get('Id'), 'state': raw.get('State')}

    def shutdown_vm(self, vm_guid: str, timeout_seconds: int = 300) -> dict:
        """Ask the guest to shut down, and report honestly whether it did.

        Never forces. A guest that ignores the request is a fact somebody has to see;
        pulling its power to get on with the migration is how a copy ends up
        crash-consistent while the log says the shutdown succeeded.
        """
        raw = self._client.run_action(
            _with_parameters(scripts.SHUTDOWN_VM, VmId=vm_guid, TimeoutSeconds=timeout_seconds),
            f'request orderly shutdown of Hyper-V VM {vm_guid}') or {}
        return {
            'guid': raw.get('Id'),
            'state': raw.get('State'),
            'shutdown_requested': raw.get('ShutdownRequested'),
            'succeeded': raw.get('State') == 'Off',
            'reason': raw.get('Reason'),
        }

    def remove_checkpoints(self, vm_guid: str, checkpoint_name: str | None = None,
                           remove_all: bool = False) -> dict:
        """Delete a checkpoint, and report what the disks look like afterwards.

        The returned `merge_complete` is the answer to the question that matters. Hyper-V
        removes the checkpoint entry immediately and merges its differencing file into the
        parent in the background, so the entry disappearing is not the merge finishing. A
        migration that reads the disk in between is reading a file still being written.
        """
        if not remove_all and not checkpoint_name:
            raise ValueError('Deleting a checkpoint needs either a name or remove_all=True.')

        raw = self._client.run_action(
            _with_parameters(scripts.REMOVE_CHECKPOINTS, VmId=vm_guid,
                             CheckpointName=checkpoint_name or '', All=remove_all),
            f'remove checkpoint(s) from Hyper-V VM {vm_guid}') or {}

        disks = [{'path': d.get('Path'), 'vhd_type': d.get('VhdType'),
                  'parent_path': d.get('ParentPath')} for d in raw.get('Disks') or []]
        return {
            'removed_count': raw.get('RemovedCount'),
            'remaining_checkpoints': raw.get('RemainingCheckpoints'),
            'disks': disks,
            'merge_complete': merge_is_complete(disks),
        }

    def mount_iso(self, vm_guid: str, iso_path: str) -> dict:
        raw = self._client.run_action(
            _with_parameters(scripts.MOUNT_ISO, VmId=vm_guid, IsoPath=iso_path),
            f'mount an ISO on Hyper-V VM {vm_guid}') or {}
        return {'path': raw.get('Path')}

    def eject_iso(self, vm_guid: str) -> dict:
        raw = self._client.run_action(
            _with_parameters(scripts.EJECT_ISO, VmId=vm_guid),
            f'eject the ISO from Hyper-V VM {vm_guid}') or {}
        return {'path': raw.get('Path')}

    def close(self) -> None:
        self._client.close()


def merge_is_complete(disks: list[dict]) -> bool:
    """Has the checkpoint merge actually finished, as opposed to started?

    True only when no attached disk is still a differencing file and none has a parent.
    Deliberately conservative: a disk whose type could not be read counts as not merged,
    because the alternative is reading a file that Hyper-V is still writing into.
    """
    if not disks:
        return False
    for disk in disks:
        if disk.get('parent_path'):
            return False
        if disk.get('vhd_type') != 'Fixed' and disk.get('vhd_type') != 'Dynamic':
            return False
    return True


def _with_parameters(script: str, **parameters) -> str:
    """Bind values to a script's param() block without putting them in the script text.

    Caller data never becomes PowerShell source. Values are emitted as literals through
    `_as_powershell_literal`, which escapes them, and a VM named `x'; Remove-VM -Name *`
    therefore stays a string.
    """
    if not parameters:
        return script
    assignments = '\n'.join(
        f'${name} = {_as_powershell_literal(value)}' for name, value in parameters.items())
    # The param() block has to stay first in the script, so the bindings follow it and the
    # assignments simply overwrite the (unsupplied) parameters.
    lines = script.strip().splitlines()
    param_end = _end_of_param_block(lines)
    return '\n'.join(lines[:param_end] + [assignments] + lines[param_end:])


def _end_of_param_block(lines: list[str]) -> int:
    """Index just past the closing line of a leading param(...) block, or 0 if there is none."""
    depth = 0
    started = False
    for index, line in enumerate(lines):
        stripped = line.strip()
        if not started and not stripped.startswith('param('):
            if stripped and not stripped.startswith('#'):
                return 0
            continue
        started = True
        depth += line.count('(') - line.count(')')
        if depth <= 0:
            return index + 1
    return 0


def _as_powershell_literal(value) -> str:
    """One Python value as a PowerShell literal that cannot become code."""
    if value is None:
        return '$null'
    if isinstance(value, bool):
        return '$true' if value else '$false'
    if isinstance(value, int):
        return str(value)
    if isinstance(value, (list, tuple)):
        return '@(' + ','.join(_as_powershell_literal(item) for item in value) + ')'
    # Single quotes are PowerShell's literal string: nothing inside is expanded, and the
    # only character needing an escape is the quote itself, doubled.
    return "'" + str(value).replace("'", "''") + "'"
