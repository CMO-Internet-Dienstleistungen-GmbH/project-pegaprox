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
import threading

from pegaprox.core import hyperv_scripts as scripts
from pegaprox.core import hyperv_tasks
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
        'memory_assigned_bytes': raw.get('MemoryAssigned'),
        'cpu_usage_percent': raw.get('CPUUsage'),
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


#: `Get-WindowsImage` answers with 9 for x64 and 0 for x86 — the PROCESSOR_ARCHITECTURE
#: values, not a string. Spelled out here so nothing downstream compares magic numbers.
_ARCHITECTURES = {0: 'x86', 5: 'arm', 9: 'x64', 12: 'arm64'}


def normalise_disk_inspection(raw: dict) -> dict:
    """What the inspection found, in this product's vocabulary.

    `dirty` is derived from the exit code rather than the sentence: `fsutil` answers in
    the host's language, and matching English text reads every German host as clean.
    """
    disks = []
    for entry in (raw.get('Disks') or []):
        volumes = []
        for vol in (entry.get('Volumes') or []):
            exit_code = vol.get('DirtyExit')
            volumes.append({
                'file_system': vol.get('FileSystem') or '',
                'label': vol.get('Label') or '',
                'size': int(vol.get('Size') or 0),
                'free': int(vol.get('Free') or 0),
                'windows': bool(vol.get('Windows')),
                'hibernated': bool(vol.get('Hiberfil')),
                'hiberfil_size': int(vol.get('HiberfilSize') or 0),
                'page_file': bool(vol.get('PageFile')),
                # None where it could not be asked, which is not the same as clean.
                'dirty': None if exit_code is None else bool(exit_code),
                # The guest's registry, opened rather than inferred. None means the hive
                # was not opened at all — on a volume with no Windows that is expected.
                'hive_readable': (None if vol.get('HiveLoadExit') is None
                                  else vol.get('HiveLoadExit') == 0),
                'product_name': vol.get('ProductName') or '',
                'edition_id': vol.get('EditionID') or '',
                'installation_type': vol.get('InstallationType') or '',
                'build': vol.get('CurrentBuildNumber') or '',
                'revision': vol.get('UBR') or '',
                'display_version': vol.get('DisplayVersion') or '',
            })
        partitions = [{
            'gpt_type': str(part.get('GptType') or '').strip('{}').lower(),
            'mbr_type': int(part.get('MbrType') or 0),
            'size': int(part.get('Size') or 0),
        } for part in (entry.get('Partitions') or [])]
        disks.append({
            'path': entry.get('Path') or '',
            'mounted': bool(entry.get('Mounted')),
            'partitions': partitions,
            'error': entry.get('Error') or '',
            'attached_after': entry.get('AttachedAfter'),
            'volumes': volumes,
        })
    return {
        'state': raw.get('State') or '',
        'inspected': bool(raw.get('Inspected')),
        'error': raw.get('Error') or '',
        'disks': disks,
    }


def normalise_image_facts(raw: dict) -> dict:
    """One disk's image facts, in this product's vocabulary.

    `edition_id` and `installation_type` come out of the guest's SOFTWARE hive and are
    sometimes empty while the version fields are filled. That is not evidence that the
    hive is unreadable — measured on such a disk, Windows' own offline loader opened it
    and every value was there. Whether the hive opens is answered by the disk inspection,
    which opens it.
    """
    windows = bool(raw.get('Windows'))
    build = int(raw.get('Build') or 0)
    edition = (raw.get('EditionId') or '').strip()
    install_type = (raw.get('InstallationType') or '').strip()
    return {
        'path': raw.get('Path') or '',
        'controller_type': raw.get('ControllerType') or '',
        'controller_number': raw.get('ControllerNumber'),
        'controller_location': raw.get('ControllerLocation'),
        'exists': bool(raw.get('Exists')),
        'attached': raw.get('Attached'),
        'vhd_type': raw.get('VhdType') or '',
        'parent_path': raw.get('ParentPath') or '',
        'size': int(raw.get('Size') or 0),
        'file_size': int(raw.get('FileSize') or 0),
        'windows': windows,
        'windows_error': raw.get('WindowsError') or '',
        'build': build,
        'version': raw.get('Version') or '',
        'revision': raw.get('SPBuild') or '',
        'architecture': _ARCHITECTURES.get(raw.get('Architecture'), raw.get('Architecture')),
        'edition_id': edition,
        'installation_type': install_type,
        'system_root': raw.get('SystemRoot') or '',
        # Deliberately not a 'registry_readable' flag any more. Concluding from an empty
        # EditionId that the hive cannot be opened was measured and found wrong: on such a
        # disk `reg load` succeeded and every value was there. Whether the hive opens is
        # now answered by opening it, in the disk inspection.

        'seconds': raw.get('Seconds'),
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
    # Kept as the hypervisor spells them. These are enum names, not display strings, so
    # they read the same on an English and a German host.
    # Keyed by the .NET type, which is the same on every host language.
    services = {}
    for entry in raw.get('IntegrationServices') or []:
        kind = entry.get('Kind')
        if kind:
            services[kind] = {
                'enabled': bool(entry.get('Enabled')),
                # The number decides, the text is for the operator: the description is
                # localised by the host ('Kein Kontakt' on a German one), so comparing it
                # works on some hosts and silently fails on others. CIM 2 is OK.
                'ok': int(entry.get('OperationalStatus') or 0) == 2,
                'status': entry.get('Status') or '',
            }
    detail['integration_services'] = services
    shutdown = services.get('ShutdownComponent')
    # Three states, not two: available, unavailable, and not known. A guest whose host did
    # not report its services must not read the same as one that reported them missing.
    detail['can_shut_itself_down'] = (
        None if not services
        else bool(shutdown and shutdown['enabled'] and shutdown['ok']))
    detail['automatic_start_action'] = raw.get('AutomaticStartAction') or ''
    detail['automatic_start_delay_seconds'] = int(raw.get('AutomaticStartDelay') or 0)
    detail['automatic_stop_action'] = raw.get('AutomaticStopAction') or ''
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
        # One lock per VM for everything that changes a VM or attaches its disks. With
        # several sessions to a host, two inspections of the same VM would otherwise run at
        # once, and two Mount-VHD on one file lock each other out. Reads take no lock: they
        # can run side by side on the same VM, which is the point of having the sessions.
        self._vm_locks: dict[str, threading.Lock] = {}
        self._vm_locks_guard = threading.Lock()

    def _vm_lock(self, vm_guid: str) -> threading.Lock:
        with self._vm_locks_guard:
            return self._vm_locks.setdefault(vm_guid.lower(), threading.Lock())

    # -- the two ways a script reaches the host -----------------------------------------
    #
    # Every call goes through one of these, so every call shows up in the task bar under a
    # name that says what it is for. The task type is the only thing a caller adds; the
    # read-only guard and the audit line stay the transport's.

    def _read(self, task_type: str, vm_guid: str, script: str, **parameters):
        with hyperv_tasks.track(self.cluster_id, task_type, vm_guid):
            return self._client.run_json(script, **parameters)

    def _act(self, task_type: str, vm_guid: str, script: str, description: str, **parameters):
        # The VM lock is taken inside the task, so waiting for it shows as waiting.
        with hyperv_tasks.track(self.cluster_id, task_type, vm_guid), self._vm_lock(vm_guid):
            return self._client.run_action(script, description, **parameters)

    # -- reading ---------------------------------------------------------------------

    def host_facts(self) -> dict:
        """Versions of the host, its PowerShell and its Hyper-V module."""
        raw = self._read('hv_host_facts', '', scripts.HOST_FACTS) or {}
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
        raw = self._read('hv_verify', '', scripts.VERIFY_PROPERTIES) or {}
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
        raw = self._read('hv_inventory', '', scripts.VM_INVENTORY) or []
        return [normalise_vm_summary(vm) for vm in raw]

    def get_vm(self, vm_guid: str) -> dict:
        """Everything about one VM that a migration has to reproduce or refuse."""
        raw = self._read('hv_detail', vm_guid, scripts.VM_DETAIL, VmId=vm_guid)
        if not raw:
            raise HyperVError(f'The host returned nothing for VM {vm_guid}.', kind='unknown')
        return normalise_vm_detail(raw)

    def get_guest_image_facts(self, vm_guid: str) -> list[dict]:
        """What each of this VM's disks says about the guest, without starting it.

        The only way to know a stopped guest's Windows version. The integration services
        report it over KVP and those items exist only while the VM runs — measured on a
        Server 2022 host: complete while running, empty three seconds after the guest
        finished shutting down. A migration requires a stopped VM, so the host has
        forgotten by the time the wizard asks. `Get-WindowsImage` reads it off the disk
        instead, in place, without mounting or attaching anything.

        Costs two to three seconds per disk. A disk with no Windows on it comes back as
        `windows: False` with the reason, which is an answer and not an error — and it
        says that about *that disk*, never about the guest.
        """
        raw = self._read('hv_image_facts', vm_guid, scripts.VM_IMAGE_FACTS,
                         VmId=vm_guid) or []
        return [normalise_image_facts(row) for row in raw]

    def inspect_disks(self, vm_guid: str) -> dict:
        """Look inside this VM's disks for what makes a migration fail late.

        Mounts each disk read-only on the host, reads what cannot be seen from outside —
        a hibernation file, an unclean file system, whether a Windows is there at all —
        and releases it again. Only for a stopped VM: a running one is already writing to
        those disks.

        Six seconds for a 100 GiB disk with three volumes. `attached_after` is reported
        per disk, because the one way this can hurt is by not letting go.
        """
        # Not `run_json`: its read-only guard refuses `Mount-VHD` by name, so the inspection
        # never ran and every check built on it reported "not inspected" as a pass. It is
        # a read-only mount and changes no data, but it does attach the disk to the host
        # for a few seconds, which is exactly what the audited action path is for.
        raw = self._act('hv_inspect', vm_guid, scripts.VM_DISK_INSPECTION,
                        'inspecting the disks of a stopped VM read-only',
                        VmId=vm_guid) or {}
        return normalise_disk_inspection(raw)

    def get_vm_state(self, vm_guid: str) -> dict:
        """The cheapest question there is, asked again right before disks are read.

        Between preflight and the transfer somebody can start the VM or take a checkpoint.
        Re-reading costs one round trip and is the only thing standing between that and a
        copy taken from underneath a running guest.
        """
        raw = self._read('hv_state', vm_guid, scripts.VM_STATE, VmId=vm_guid) or {}
        return {'guid': raw.get('Id'), 'state': raw.get('State'),
                'checkpoint_count': raw.get('CheckpointCount')}

    def get_merge_state(self, vm_guid: str) -> dict:
        """Is a background operation still touching this VM's disks?

        The one question a checkpoint deletion cannot answer about itself. Remove-VMSnapshot
        returns when the checkpoint entry is gone and the merge of its differencing file into
        the parent carries on afterwards, invisible to the checkpoint list. Reading the disks
        in that window copies a file Hyper-V is still writing into.
        """
        raw = self._read('hv_merge_state', vm_guid, scripts.VM_MERGE_STATE,
                         VmId=vm_guid) or {}
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
        # One task for the whole gate, not one per question: it is a single check to the
        # person watching, and two rows would read as two things the runner asked for.
        with hyperv_tasks.track(self.cluster_id, 'hv_safety_check', vm_guid):
            return self._disks_are_safe_to_read(vm_guid)

    def _disks_are_safe_to_read(self, vm_guid: str) -> tuple[bool, str]:
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
        raw = self._read('hv_disk_chain', vm_guid, scripts.VM_DISK_CHAIN,
                         VmId=vm_guid) or []
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
        raw = self._read('hv_iso_list', '', scripts.ISO_LIBRARY, Paths=library_paths) or []
        return [{'path': iso.get('Path'), 'name': iso.get('Name'),
                 'size': iso.get('Length'), 'modified_at': iso.get('LastModified')}
                for iso in raw]

    # -- acting ----------------------------------------------------------------------

    def start_vm(self, vm_guid: str) -> dict:
        """Start a VM, for preparation only. A migration never starts a source VM."""
        raw = self._act('hv_start', vm_guid,
                        scripts.START_VM, f'start Hyper-V VM {vm_guid}', VmId=vm_guid) or {}
        return {'guid': raw.get('Id'), 'state': raw.get('State')}

    def shutdown_vm(self, vm_guid: str, timeout_seconds: int = 300) -> dict:
        """Ask the guest to shut down, and report honestly whether it did.

        Never forces. A guest that ignores the request is a fact somebody has to see;
        pulling its power to get on with the migration is how a copy ends up
        crash-consistent while the log says the shutdown succeeded.
        """
        raw = self._act(
            'hv_shutdown', vm_guid, scripts.SHUTDOWN_VM,
            f'request orderly shutdown of Hyper-V VM {vm_guid}',
            VmId=vm_guid, TimeoutSeconds=timeout_seconds) or {}
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

        raw = self._act(
            'hv_checkpoint_remove', vm_guid, scripts.REMOVE_CHECKPOINTS, f'remove checkpoint(s) from Hyper-V VM {vm_guid}',
            VmId=vm_guid, CheckpointName=checkpoint_name or '', All=remove_all) or {}

        disks = [{'path': d.get('Path'), 'vhd_type': d.get('VhdType'),
                  'parent_path': d.get('ParentPath')} for d in raw.get('Disks') or []]
        return {
            'removed_count': raw.get('RemovedCount'),
            'remaining_checkpoints': raw.get('RemainingCheckpoints'),
            'disks': disks,
            'merge_complete': merge_is_complete(disks),
        }

    def mount_iso(self, vm_guid: str, iso_path: str) -> dict:
        raw = self._act(
            'hv_iso_mount', vm_guid, scripts.MOUNT_ISO, f'mount an ISO on Hyper-V VM {vm_guid}',
            VmId=vm_guid, IsoPath=iso_path) or {}
        return {'path': raw.get('Path')}

    def eject_iso(self, vm_guid: str) -> dict:
        raw = self._act(
            'hv_iso_eject', vm_guid, scripts.EJECT_ISO, f'eject the ISO from Hyper-V VM {vm_guid}',
            VmId=vm_guid) or {}
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
