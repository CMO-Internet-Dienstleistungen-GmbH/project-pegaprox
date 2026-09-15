"""The Hyper-V to Proxmox migration: what it will do, and doing it.

This lives beside the existing cross-hypervisor engine rather than inside it. The engine's
file is upstream code that every release rebuild replays this patch onto, and a direction
added in the middle of it would conflict on every one of those. Here it conflicts on
nothing, and the engine is reached through two names it already exports.

The run differs from the other directions in one way that shapes everything else: the data
never passes through PegaProx. The target node mounts the source's file share read-only and
converts the VHDX itself. That makes the management server irrelevant to the transfer's
throughput and to its failure modes, and it means the credentials for that share have to
reach the node without ever appearing in a command line.

What it refuses to do is as deliberate as what it does. It never powers a source VM off; it
asks preflight and stops if the answer is no. It never takes a checkpoint of the source. It
never deletes the source, even on success — the operator decides that afterwards, when they
have seen the migrated VM boot.
"""

from __future__ import annotations

import contextlib
import logging
import re
import shlex
import time

from pegaprox.core import hyperv_db, hyperv_preflight, hyperv_transfer
from pegaprox.core.hyperv import format_mac
from pegaprox.core.hyperv_errors import HyperVError
from pegaprox.core.hyperv_transfer import TransferError
from pegaprox.globals import cluster_managers

logger = logging.getLogger(__name__)

DIRECTION = 'hyperv_to_pve'

# How a Hyper-V generation lands on Proxmox. Generation is a property of the VM, never
# something the disk reveals, which is why it is read from the inventory and carried here
# rather than guessed from the image.
# 'pc' is the i440fx machine, and it is the spelling Proxmox accepts: 'i440fx' is refused
# outright ("machine.type: value does not match the regex pattern"), which failed every
# Generation 1 import — the older estates that most need migrating.
GENERATION_MACHINE = {1: 'pc', 2: 'q35'}
# The fallback for a generation nothing recognised. Preflight blocks that case, so this is
# reached only by a caller that skipped it -- and it still must be a machine the target
# accepts rather than the name the hardware is known by.
DEFAULT_MACHINE = 'pc'
GENERATION_BIOS = {1: 'seabios', 2: 'ovmf'}

# Where a converted disk is attached. Hyper-V's IDE controller becomes SATA because a
# Generation 1 guest boots from IDE and its installed drivers expect something like it;
# its SCSI controller becomes VirtIO SCSI, which is what a prepared guest has drivers for.
CONTROLLER_FOR_HINT = {'sata': 'sata', 'scsi': 'scsi'}

# What the target hardware is, as one decision rather than two that can disagree. It used
# to be read from the operator's choice when preflight asked about drivers, and from the
# source's controller hint when the disk was attached — so choosing SATA silenced the
# VirtIO warning and then attached the disk to VirtIO SCSI anyway.
#
# 'compatible' is the default because an imported guest has whatever drivers it had on
# Hyper-V, which for Windows does not include VirtIO. SATA and e1000 are emulations every
# supported guest already has a driver for, so the VM boots without being prepared first.
# 'virtio' is for a guest that was prepared, and for the profile switch that happens after
# the drivers are installed on the Proxmox side.
COMPATIBLE_CONTROLLER = 'sata'
COMPATIBLE_NIC_MODEL = 'e1000'
VIRTIO_CONTROLLER = 'scsi'
VIRTIO_NIC_MODEL = 'virtio'
DEFAULT_CONTROLLER = COMPATIBLE_CONTROLLER
DEFAULT_HARDWARE = 'compatible'


def target_hardware(config) -> dict:
    """The disk controller and NIC model this migration will actually create.

    One function so the preflight warning and the created hardware cannot disagree: both
    ask this. `hardware` is what the wizard offers; `controller` is still read for a caller
    that names the controller directly.
    """
    choice = (config or {}).get('hardware') or DEFAULT_HARDWARE
    if choice not in ('compatible', 'virtio'):
        choice = DEFAULT_HARDWARE
    named = (config or {}).get('controller')
    if named in ('sata', 'scsi'):
        # An explicitly named controller decides, and the NIC follows it rather than
        # staying on VirtIO while the disk is on SATA.
        choice = 'virtio' if named == VIRTIO_CONTROLLER else 'compatible'
    if choice == 'virtio':
        return {'hardware': 'virtio', 'controller': VIRTIO_CONTROLLER,
                'nic_model': VIRTIO_NIC_MODEL}
    return {'hardware': 'compatible', 'controller': COMPATIBLE_CONTROLLER,
            'nic_model': COMPATIBLE_NIC_MODEL}

# Proxmox refuses more than this many of either, and a source with more needs a decision
# rather than a silently truncated VM.
MAX_NETWORK_ADAPTERS = 8

#: The VLAN an imported adapter lands on when the source names none. Hyper-V leaves an
#: adapter untagged far more often than the network it sits on is actually untagged: the
#: tagging is done by the physical switch port, which the guest cannot see. Importing such
#: an adapter with no tag puts it on the target bridge's native VLAN, which is a different
#: network. The value is an operator setting; this is only the fallback when none is saved.
DEFAULT_IMPORT_VLAN = 1006

#: Hyper-V VLAN modes. Only `Access` carries a single id that a Proxmox `tag=` can express.
#: `Trunk` passes several, `Isolated` is a private-VLAN role; neither has one number, so
#: neither is guessed at -- the preflight says so and the adapter arrives untagged.
VLAN_MODE_ACCESS = 'Access'
_SINGLE_VLAN_MODES = (VLAN_MODE_ACCESS, 'Untagged', '')


def default_import_vlan() -> int | None:
    """The configured fallback VLAN, or None when the operator turned it off.

    Read per call rather than cached: a migration is rare and an operator who changes this
    expects the next run to use it, not the next restart.
    """
    try:
        from pegaprox.api.helpers import load_server_settings
        raw = (load_server_settings() or {}).get('hyperv_default_vlan', DEFAULT_IMPORT_VLAN)
    except Exception:                                        # noqa: BLE001
        raw = DEFAULT_IMPORT_VLAN
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_IMPORT_VLAN
    # 0 is how the setting says "leave imports untagged"; VLAN ids stop at 4094.
    return value if 1 <= value <= 4094 else None


def vlan_for_adapter(nic: dict) -> int | None:
    """The VLAN id an adapter should arrive on, or None to leave it untagged.

    The source wins when it names one. A mode that carries more than one id names none,
    so it falls through to untagged rather than to the default -- putting a trunk port on
    a single guessed VLAN is worse than leaving it off, because it looks like it worked.
    """
    mode = (nic.get('vlan_mode') or '').strip()
    if mode and mode not in _SINGLE_VLAN_MODES:
        return None
    raw = nic.get('vlan_id')
    try:
        source_vlan = int(raw)
    except (TypeError, ValueError):
        source_vlan = 0
    if 1 <= source_vlan <= 4094:
        return source_vlan
    return default_import_vlan()

# The transfer is one qemu-img run per disk with no resume, so the retry budget is small:
# a second attempt covers a dropped SSH connection, a third is already evidence that
# something is wrong rather than unlucky.
TRANSFER_ATTEMPTS = 3
_RETRY_BACKOFF_SECONDS = 5

# pvesm alloc wants kibibytes and rounds down, so a disk whose size is not a whole number
# of kibibytes would get a volume one write short of what the conversion needs.
_BYTES_PER_KIB = 1024

_ALLOC_VOLUME = re.compile(r"successfully created '([^']+)'")

_SSH_COMMAND_TIMEOUT = 60

# Creating a VM is quick; anything longer than this means the node is not answering.
_CREATE_TASK_TIMEOUT = 120
_CONVERT_TIMEOUT = 24 * 3600


# ===========================================================================
# Planning
# ===========================================================================

def plan_hyperv_to_pve(source_cluster_id, source_vmid, target_cluster_id) -> dict:
    """What this migration would create, and what stands in its way.

    Returns the same shape the other planners return, plus the preflight report. The report
    is the point: the other directions answer "here is what we found", and this one also
    answers "here is why you cannot start yet", because a Hyper-V source has states — an
    open checkpoint, a differencing chain, a guest that is merely saved rather than off —
    that produce a corrupt copy instead of a failure.
    """
    source = cluster_managers.get(source_cluster_id)
    if not source or getattr(source, 'cluster_type', '') != 'hyperv':
        return {'error': 'Source Hyper-V host not found'}

    target = cluster_managers.get(target_cluster_id)
    if not target or getattr(target, 'cluster_type', 'proxmox') != 'proxmox':
        return {'error': 'Target must be a Proxmox cluster'}

    if not source.is_connected:
        return {'error': 'Source Hyper-V host not connected'}
    if not target.is_connected:
        return {'error': 'Target Proxmox cluster not connected'}

    try:
        export_info = source.get_vm_disks_for_export(source_vmid)
    except HyperVError as exc:
        return {'error': exc.message, 'kind': exc.kind, 'remedy': exc.remedy}
    if 'error' in export_info:
        return export_info
    data = export_info.get('data', {})

    try:
        detail = source.vm_detail(source_vmid)
    except HyperVError as exc:
        return {'error': exc.message, 'kind': exc.kind, 'remedy': exc.remedy}
    if 'error' in detail:
        return detail

    generation = data.get('generation')
    disks = data.get('disks') or []
    total_bytes = sum(int(disk.get('capacity_bytes') or 0) for disk in disks)

    report = hyperv_preflight.run_preflight(
        detail,
        # Capacity is unknown until a storage is chosen, and the check says so. The plan is
        # what the wizard renders before that choice exists.
        {'available_bytes': None},
        # The controller the plan below announces, so the driver warning and the disk row
        # describe the same import. They disagreed before: the plan said 'sata' and the
        # warning asked for drivers the SATA path does not need.
        # The plan is rendered before the operator has ticked anything, so it describes
        # the default: the compatible controller, and therefore no injection.
        {'network_map': {}, 'source_access_probed': False,
         'controller': DEFAULT_CONTROLLER,
         'drivers_injected': DEFAULT_HARDWARE == 'virtio'})

    from pegaprox.core.xhm import _get_pve_targets

    return {
        'source': {
            'name': data.get('name', ''),
            'vcpus': data.get('cpu_count') or 1,
            'memory_mb': data.get('memory_mb') or 1024,
            'disks': [{
                'key': disk.get('key', ''),
                'label': disk.get('label', ''),
                'path': disk.get('path', ''),
                'size': disk.get('capacity_bytes') or 0,
                'size_gb': disk.get('capacity_gb') or 0,
                'thin': disk.get('thin', False),
                'vhd_type': disk.get('vhd_type'),
                'target_controller': CONTROLLER_FOR_HINT.get(
                    disk.get('target_controller_hint'), DEFAULT_CONTROLLER),
            } for disk in disks],
            # `network` and `bridge` are what the shared migration wizard reads: it keys
            # its mapping by `network` and labels the row with `bridge`. A Hyper-V adapter
            # is addressed by its MAC — the same key the preflight and the runner use — and
            # the row reads better with the switch it hangs on than with a MAC. Getting
            # this pair wrong is invisible in the UI and fatal: the wizard writes the map
            # under one key and the preflight looks it up under another, so every adapter
            # stays unmapped and the migration can never be started.
            'networks': [{
                'name': nic.get('name'),
                'mac_address': nic.get('mac_address'),
                'switch_name': nic.get('switch_name'),
                'network': nic.get('mac_address') or nic.get('name') or '',
                'bridge': nic.get('switch_name') or nic.get('name') or '',
                # What the source says, and what this adapter would arrive on if nobody
                # touches the field. The wizard shows the mode so an operator can see why
                # a trunk adapter's VLAN box is empty rather than wondering.
                'vlan_id': nic.get('vlan_id'),
                'vlan_mode': nic.get('vlan_mode'),
                'vlan_suggested': vlan_for_adapter(nic),
            } for nic in (data.get('network_adapters') or [])],
            'generation': generation,
            'bios': GENERATION_BIOS.get(generation or 0, 'seabios'),
            'machine': GENERATION_MACHINE.get(generation or 0, DEFAULT_MACHINE),
            'power_state': data.get('power_state'),
            'checkpoint_count': data.get('checkpoint_count'),
            'secure_boot_enabled': data.get('secure_boot_enabled'),
            'vtpm_enabled': data.get('vtpm_enabled'),
            'hyperv_guid': data.get('hyperv_guid'),
        },
        'targets': _get_pve_targets(target),
        'preflight': report.to_dict(),
        # Deliberately not an estimate in seconds. The other directions derive one from a
        # fixed bytes-per-second figure, which is a guess presented as a number; this
        # transfer's speed depends on a file share nobody here has measured.
        'total_bytes': total_bytes,
        'direction': DIRECTION,
    }


# ===========================================================================
# Running
# ===========================================================================

class _Node:
    """One SSH connection to the target node, with the few shapes of call this run makes.

    Wrapped rather than used directly so the run reads as steps instead of as channel
    bookkeeping, and so a test can drive the whole runner without a Proxmox node.
    """

    def __init__(self, ssh, user='root'):
        self._ssh = ssh
        self._user = user or 'root'

    def _as_root(self, command):
        """Every command here needs root, and the configured account need not be root.

        PegaProx supports a non-root node account (`pegaprox@pam` and friends) and wraps
        its own node commands the same way. This transfer mounts a share, allocates a
        volume and runs qemu-img — none of which a plain account may do — so without the
        wrapper a correctly configured cluster failed with a bare `Permission denied` from
        mount, several steps after the VM had already been created.

        The command is passed as one argument rather than piped in, because two of the
        calls here feed the remote command on stdin — the share credentials are written
        with `cat`, and a password may not travel in a command line. PegaProx' own
        `_wrap_with_sudo` pipes a base64 script into `sudo bash`, which takes that stdin
        for the script itself and would leave the credentials file empty.
        """
        if self._user == 'root':
            return command
        # -n rather than a prompt: an account without NOPASSWD has to fail immediately
        # instead of hanging on a password nobody is there to type.
        return 'sudo -n bash -c ' + shlex.quote(command)

    def run(self, command, stdin_data=None, timeout=_SSH_COMMAND_TIMEOUT):
        stdin, stdout, stderr = self._ssh.exec_command(self._as_root(command), timeout=timeout)
        if stdin_data is not None:
            stdin.write(stdin_data)
            stdin.channel.shutdown_write()
        err = stderr.read().decode('utf-8', errors='replace')
        out = stdout.read().decode('utf-8', errors='replace')
        return stdout.channel.recv_exit_status(), out, err

    def run_with_progress(self, command, on_progress, cancelled, timeout=_CONVERT_TIMEOUT):
        """Run a long command, reporting progress as it arrives.

        qemu-img writes its percentage to stdout with carriage returns rather than
        newlines, so this reads bytes rather than lines. A line-oriented read would return
        nothing at all until the conversion finished, which is exactly when a progress bar
        stops being useful.
        """
        _, stdout, stderr = self._ssh.exec_command(self._as_root(command), timeout=timeout)
        channel = stdout.channel
        buffered = ''
        while not channel.exit_status_ready() or channel.recv_ready():
            if cancelled():
                channel.close()
                return -1, '', 'cancelled'
            if channel.recv_ready():
                buffered = channel.recv(4096).decode('utf-8', errors='replace')
                percent = hyperv_transfer.parse_progress(buffered)
                if percent is not None:
                    on_progress(percent)
            else:
                time.sleep(0.5)
        return (channel.recv_exit_status(),
                buffered,
                stderr.read().decode('utf-8', errors='replace'))

    def close(self):
        try:
            self._ssh.close()
        except Exception:
            logger.debug('Closing the target node connection failed', exc_info=True)


def _run_hyperv_to_pve(task):
    """Move one VM from a Hyper-V host to Proxmox.

    The order is chosen so that the expensive, irreversible part happens last and only
    after everything cheap has agreed it should. Preflight runs again here even though the
    wizard already ran it: the answer can have changed between the operator reading it and
    pressing the button, and an open checkpoint that appeared in between produces a disk
    that mounts and is quietly wrong.
    """
    migration_id = None
    node = None
    credentials_path = None
    claimed = None
    # One mount per distinct share: a VM's disks can sit on different drives of the same
    # host, and each drive is a share of its own.
    mounts = {}
    allocated = []

    try:
        source = cluster_managers.get(task.source_cluster)
        target = cluster_managers.get(task.target_cluster)
        if not source or getattr(source, 'cluster_type', '') != 'hyperv':
            task.set_phase('failed', 'Source Hyper-V host not found')
            return
        if not target or not target.is_connected:
            task.set_phase('failed', 'Target Proxmox cluster not connected')
            return

        # === PLANNING ===
        task.set_phase('planning')
        task.progress = 2

        guid = source.guid_for(task.source_vmid)
        if not guid:
            task.set_phase('failed', f'No Hyper-V VM is known here as {task.source_vmid}')
            return

        detail = source.vm_detail(task.source_vmid)
        if 'error' in detail:
            task.set_phase('failed', detail['error'])
            return
        task.vm_name = task.vm_name or detail.get('name') or ''
        task.log(f"Source VM: {task.vm_name}")

        migration_id = _record_start(task, guid)

        # Taken before anything is created, and after the row exists: the claim's liveness
        # is read off that row, so claiming first would leave a window in which this claim
        # looks abandoned. Two operators pressing start on the same VM at the same moment
        # both reach here; only one of them gets the claim, and the other stops having
        # created nothing on either side.
        holder = hyperv_db.claim_source(_conn(), task.source_cluster, guid, task.id)
        if holder:
            _fail(task, migration_id,
                  f'Migration {holder} is already moving this VM. Two imports of one '
                  f'source would copy the same disks into two targets.')
            return
        claimed = guid

        blocked = _preflight_gate(task, source, target, detail, guid)
        if blocked:
            _fail(task, migration_id, blocked)
            return

        new_vmid = _next_target_vmid(target)
        task.target_vmid = new_vmid
        _update_migration_row(migration_id, target_vmid=new_vmid)
        task.log(f"Target VMID: {new_vmid}")
        task.progress = 8

        if task.cancel_event.is_set():
            _fail(task, migration_id, 'Cancelled before anything was created')
            return

        # === TRANSFER ===
        task.set_phase('transfer')
        _update_migration_row(migration_id, phase='transfer')

        disks = detail.get('disks') or []
        if not disks:
            _fail(task, migration_id, 'The VM has no virtual disks to migrate')
            return

        node, credentials_path = _open_target_node(task, source, target)
        share_map = getattr(source.config, 'smb_share_map', {}) or {}

        for index, disk in enumerate(disks):
            if task.cancel_event.is_set():
                _fail(task, migration_id, 'Cancelled during the transfer')
                return
            source_file = _mounted_path_for(task, node, source, credentials_path,
                                            mounts, share_map, disk.get('path') or '')
            volume = _transfer_one_disk(task, node, migration_id, source_file,
                                        new_vmid, index, disk)
            if volume is None:
                return
            allocated.append(volume)

        task.progress = 78

        # === CREATING ===
        task.set_phase('creating')
        _update_migration_row(migration_id, phase='creating')

        created = _create_target_vm(task, target, new_vmid, detail)
        if created is not True:
            _fail(task, migration_id, created)
            return
        hyperv_db.record_created_resource(_conn(), migration_id, 'vm', str(new_vmid),
                                          f'on node {task.target_node}')
        task.progress = 88

        # === ATTACHING ===
        task.set_phase('attaching')
        _update_migration_row(migration_id, phase='attaching')
        not_attached = _attach_disks(task, target, new_vmid, allocated, detail)
        if not_attached:
            # Everything created stays recorded and nothing is deleted: the volumes hold
            # the converted data and an operator can attach them by hand. What does not
            # happen is calling this a completed migration. A VM whose disks are missing
            # boots to a network prompt, and reporting that as done sends somebody to a
            # machine they believe is migrated.
            _fail(task, migration_id,
                  f'The disks were converted but {", ".join(not_attached)} could not be '
                  f'attached to VM {new_vmid}. The data is on the target and is recorded '
                  f'as this migration\'s; attach it or clean the migration up.')
            return

        # The drivers go in after the disks are attached and before anybody starts the VM:
        # the injection writes into the guest's filesystem, which is only safe while it is
        # not running.
        injection_note = _inject_drivers_if_asked(task, target, new_vmid, allocated,
                                                 detail)
        if injection_note:
            task.log(injection_note)

        task.progress = 96
        _finish(task, migration_id, new_vmid)

    except TransferError as exc:
        logger.warning('[XHM:%s] transfer refused: %s', task.id, exc)
        _fail(task, migration_id, str(exc))
    except HyperVError as exc:
        logger.warning('[XHM:%s] source failure: %s', task.id, exc.kind)
        _fail(task, migration_id, f'{exc.message} {exc.remedy}'.strip())
    except Exception as exc:  # noqa: BLE001 - a runner thread must never die silently
        logger.exception('[XHM:%s] unhandled error', task.id)
        _fail(task, migration_id, str(exc))
    finally:
        if claimed:
            # On every path, including the unhandled one. A claim left behind would make
            # the VM permanently unmigratable until a restart swept it.
            try:
                hyperv_db.release_source(_conn(), task.id)
            except Exception:
                logger.warning('[XHM:%s] could not release the source claim', task.id,
                               exc_info=True)
        if node is not None:
            # Always, on every path. A mount left behind holds a connection open to a
            # customer's hypervisor, and the credentials file is a password on disk.
            for point in mounts.values():
                try:
                    node.run(hyperv_transfer.unmount_command(point, credentials_path or ''))
                except Exception:
                    logger.warning('[XHM:%s] could not unmount %s', task.id, point,
                                   exc_info=True)
            if credentials_path:
                try:
                    node.run(f'rm -f {shlex.quote(credentials_path)}')
                except Exception:
                    logger.warning('[XHM:%s] could not remove the credentials file', task.id,
                                   exc_info=True)
            node.close()


# ---------------------------------------------------------------------------
# Starting again after a failure
# ---------------------------------------------------------------------------

# What a target VM's description says about where it came from. Read back before a
# cleanup deletes anything, so a recycled VMID cannot cost somebody else their guest.
_DESCRIPTION_PREFIX = 'Imported from Hyper-V by PegaProx migration'


def target_vm_description(migration_id, vm_name=''):
    """The ownership mark written onto every VM this patch creates."""
    source = f' (source: {vm_name})' if vm_name else ''
    return f'{_DESCRIPTION_PREFIX} {migration_id}{source}'


def _describes_migration(description, migration_id):
    return f'{_DESCRIPTION_PREFIX} {migration_id}' in (description or '')


def _forget_volume(migration_id, volume):
    """A volume that was freed again is no longer a leftover, and must stop being listed."""
    if not migration_id:
        return
    try:
        hyperv_db.forget_created_resource(_conn(), migration_id, 'volume', volume)
    except Exception:
        logger.warning('Could not forget the freed volume %s', volume, exc_info=True)


# Options the other directions accept and this one does not. Neither is offered in the
# wizard, so a request carrying them was written by hand — and both are ways of losing the
# thing that makes this direction recoverable.
_REFUSED_OPTIONS = {
    'remove_source': 'This direction never deletes the Hyper-V source. The surviving '
                     'original is the entire rollback: without it a failed import has '
                     'nothing to go back to.',
    'start_after': 'An imported VM is not started automatically. It carries the original\'s '
                   'hostname and MAC, so starting it before somebody has stopped the '
                   'original puts the same machine on the network twice.',
}


def refuse_hyperv_start(source_cluster_id, source_vmid, options=None):
    """Why this VM may not be started now, or None.

    Two reasons, and they are different failures with the same shape. A live claim means
    somebody else is already moving this VM. Leftovers mean an earlier attempt created
    things on the target that nobody has looked at: starting again would allocate a second
    set of volumes for the same disks, and the storage would fill with copies whose origin
    nobody can reconstruct.

    Neither is resolved by trying harder. The first ends by itself, the second needs a
    person to decide what happens to what the last attempt left behind.
    """
    for name, reason in _REFUSED_OPTIONS.items():
        if (options or {}).get(name):
            return reason

    source = cluster_managers.get(source_cluster_id)
    if not source or getattr(source, 'cluster_type', '') != 'hyperv':
        return None

    guid = source.guid_for(source_vmid)
    if not guid:
        return None

    try:
        conn = _conn()
        holder = hyperv_db.active_claim(conn, source_cluster_id, guid)
        if holder:
            return (f'Migration {holder["migration_id"]} is already moving this VM. '
                    f'Wait for it to finish, or cancel it.')

        for migration in hyperv_db.migrations_for_cluster(conn, source_cluster_id):
            if migration['source_vm_guid'] != guid:
                continue
            if migration['status'] not in (hyperv_db.STATUS_FAILED,
                                           hyperv_db.STATUS_INTERRUPTED):
                continue
            leftovers = migration.get('created_resources') or []
            if leftovers:
                what = ', '.join(f'{r.get("kind")} {r.get("id")}' for r in leftovers[:4])
                return (f'Migration {migration["migration_id"]} failed and left '
                        f'{len(leftovers)} resource(s) on the target ({what}). Remove them '
                        f'or keep them deliberately before starting again, so the same '
                        f'disks are not copied twice.')
    except Exception:
        # A bookkeeping failure must not become a migration nobody can start. It is logged
        # and the start proceeds, which is the same position the product was in before.
        logger.warning('Could not check whether a Hyper-V migration may start',
                       exc_info=True)
    return None


# What the two sides call a machine that is running. Hyper-V says 'Running', Proxmox says
# 'running', and anything else — 'Off', 'stopped', 'paused', 'Saved' — is not running.
_RUNNING = 'running'


def refuse_source_start(source_cluster_id, source_vmid):
    """Why starting the Hyper-V original now would run two copies of one machine, or None.

    After a migration the original and the copy are the same machine with the same disks,
    the same hostname and — deliberately — the same MAC. Both running at once is an address
    conflict at best and two divergent sets of data at worst, and nothing afterwards merges
    them. So PegaProx does not start one while it can see the other running.

    An unknown counter-state refuses too. "I could not read the target" is not "the target
    is off", and the difference is the whole point of the check.
    """
    source = cluster_managers.get(source_cluster_id)
    if not source or getattr(source, 'cluster_type', '') != 'hyperv':
        return None
    guid = source.guid_for(source_vmid)
    if not guid:
        return None

    for migration in _migrations_of(source_cluster_id, guid):
        target_vmid = migration.get('target_vmid')
        if not target_vmid:
            continue
        target = cluster_managers.get(migration.get('target_cluster') or '')
        if target is None:
            continue
        state, known = _target_state(target, migration, target_vmid)
        if state is None and known:
            continue  # the VM this migration created is gone; nothing to collide with
        if not known:
            return (f'Whether the imported VM {target_vmid} on '
                    f'{migration.get("target_cluster")}/{migration.get("target_node")} is '
                    f'running cannot be read right now. Starting this VM while the copy '
                    f'may also be running would put two machines with the same identity on '
                    f'the network.')
        if state == _RUNNING:
            return (f'The imported copy of this VM is running as {target_vmid} on '
                    f'{migration.get("target_cluster")}/{migration.get("target_node")}. '
                    f'Stop it first: the two share a hostname and a MAC address, and '
                    f'nothing merges what they write while both are up.')
    return None


def refuse_target_start(target_cluster_id, target_vmid):
    """The same question asked from the Proxmox side, or None.

    Only a VM this patch imported is ever affected, and only while its Hyper-V original is
    still there and running. A VMID that has since been given to an unrelated guest is not
    blocked: the mark on the VM, not the number, decides whether this is that copy.
    """
    target = cluster_managers.get(target_cluster_id)
    if target is None:
        return None

    try:
        conn = _conn()
        migrations = [m for m in hyperv_db.list_migrations(conn, limit=500)
                      if m.get('target_cluster') == target_cluster_id
                      and str(m.get('target_vmid') or '') == str(target_vmid)]
    except Exception:
        logger.warning('Could not read the Hyper-V migration record', exc_info=True)
        return None

    for migration in migrations:
        if not _is_our_target_vm(target, migration, target_vmid):
            continue
        source = cluster_managers.get(migration.get('source_cluster') or '')
        if source is None or getattr(source, 'cluster_type', '') != 'hyperv':
            continue
        state, known = _source_state(source, migration)
        if state is None and known:
            continue  # the original is gone from the host's inventory
        name = migration.get('source_vm_name') or migration.get('source_vm_guid')
        if not known:
            return (f'Whether the Hyper-V original of this VM ({name}) is running cannot '
                    f'be read right now. Starting the copy while the original may also be '
                    f'running would put two machines with the same identity on the network.')
        if state == _RUNNING:
            return (f'The Hyper-V original of this VM ({name}) is running on '
                    f'{migration.get("source_cluster")}. Shut it down first: this copy '
                    f'carries the same hostname and MAC address, and nothing merges what '
                    f'the two write while both are up.')
    return None


def _migrations_of(source_cluster_id, guid):
    """Every recorded migration of one source VM, newest first."""
    try:
        return [m for m in hyperv_db.migrations_for_cluster(_conn(), source_cluster_id)
                if m.get('source_vm_guid') == guid]
    except Exception:
        logger.warning('Could not read the Hyper-V migration record', exc_info=True)
        return []


def _is_our_target_vm(target, migration, target_vmid):
    """Whether that VMID still carries this migration's mark."""
    node = migration.get('target_node') or ''
    try:
        response = target._api_get(
            f'https://{target.host}:{target.api_port}'
            f'/api2/json/nodes/{node}/qemu/{target_vmid}/config')
    except Exception:
        return False
    if response.status_code != 200:
        return False
    description = (response.json().get('data') or {}).get('description', '')
    return _describes_migration(description, migration['migration_id'])


def _target_state(target, migration, target_vmid):
    """(state, known). `known` is False when the answer could not be read at all."""
    node = migration.get('target_node') or ''
    try:
        response = target._api_get(
            f'https://{target.host}:{target.api_port}'
            f'/api2/json/nodes/{node}/qemu/{target_vmid}/status/current')
    except Exception:
        logger.debug('Could not read the state of target VM %s', target_vmid, exc_info=True)
        return None, False
    if response.status_code == 404:
        return None, True
    if response.status_code != 200:
        return None, False
    if not _is_our_target_vm(target, migration, target_vmid):
        return None, True
    return str((response.json().get('data') or {}).get('status', '')).lower(), True


def _source_state(source, migration):
    """(state, known) for the Hyper-V original, read through the durable identity map."""
    guid = migration.get('source_vm_guid')
    try:
        detail = source.manager.get_vm(guid)
    except HyperVError:
        logger.debug('Could not read the state of Hyper-V VM %s', guid, exc_info=True)
        return None, False
    except Exception:
        logger.warning('Unexpected failure reading Hyper-V VM %s', guid, exc_info=True)
        return None, False
    if not detail:
        return None, True
    return str(detail.get('state') or detail.get('power_state') or '').lower(), True


def cleanup_migration(migration_id, *, confirmed=False):
    """Remove what one failed migration created on the target. Nothing else, ever.

    Everything about this is deliberately narrow. It acts only on resources this migration
    recorded as its own; it verifies the target VM still carries this migration's mark
    before deleting it, because a VMID can have been handed to somebody else's guest since;
    it refuses while any worker could still be writing; and it never touches the Hyper-V
    source, whose survival is the whole rollback story of this direction.

    Returns a result dict. A refusal is a result, not an exception: every one of them is
    something an operator has to read.
    """
    conn = _conn()
    migration = hyperv_db.get_migration(conn, migration_id)
    if migration is None:
        return {'success': False, 'error': f'No such migration: {migration_id}'}

    if not confirmed:
        return {'success': False,
                'error': 'Cleanup removes a VM and its disks and cannot be undone. '
                         'It runs only with an explicit confirmation.'}

    if migration['status'] == hyperv_db.STATUS_RUNNING:
        return {'success': False,
                'error': 'This migration is still running. Cancel it and let it stop '
                         'before removing what it created.'}

    claim = hyperv_db.active_claim(conn, migration['source_cluster'],
                                   migration['source_vm_guid'])
    if claim:
        return {'success': False,
                'error': f'Migration {claim["migration_id"]} is working on the same source '
                         f'VM. Removing target resources under a running import is how two '
                         f'runs corrupt each other.'}

    resources = migration.get('created_resources') or []
    if not resources:
        return {'success': True, 'removed': [], 'kept': [],
                'message': 'This migration left nothing on the target.'}

    target = cluster_managers.get(migration['target_cluster'])
    if not target or not getattr(target, 'is_connected', False):
        return {'success': False,
                'error': f'Target cluster {migration["target_cluster"]} is not connected, '
                         f'so nothing can be verified before it is deleted.'}

    return _remove_resources(migration, target, resources)


def _remove_resources(migration, target, resources):
    """Delete the VM, then free whatever volumes outlived it."""
    migration_id = migration['migration_id']
    node = migration.get('target_node') or ''
    removed, kept = [], []

    vms = [r for r in resources if r.get('kind') == 'vm']
    volumes = [r for r in resources if r.get('kind') == 'volume']

    for entry in vms:
        outcome = _remove_target_vm(target, node, entry.get('id'), migration_id)
        (removed if outcome['removed'] else kept).append(
            {'kind': 'vm', 'id': entry.get('id'), 'note': outcome['note']})
        if outcome.get('foreign'):
            # The number belongs to somebody else now. Its disks do too, so the volume
            # list is not touched either.
            return {'success': False, 'removed': removed, 'kept': kept,
                    'error': outcome['note']}

    if volumes:
        freed, left = _free_leftover_volumes(migration, target, volumes)
        removed.extend(freed)
        kept.extend(left)

    if not kept:
        hyperv_db.clear_created_resources(_conn(), migration_id)

    return {'success': not kept, 'removed': removed, 'kept': kept,
            'message': f'Removed {len(removed)} resource(s); {len(kept)} could not be '
                       f'removed and are still recorded.' if kept
                       else f'Removed {len(removed)} resource(s). The Hyper-V source was '
                            f'not touched.'}


def _remove_target_vm(target, node, vmid, migration_id):
    """Delete one VM, but only after it says it is this migration's."""
    base = f'https://{target.host}:{target.api_port}/api2/json/nodes/{node}/qemu/{vmid}'
    try:
        response = target._api_get(f'{base}/config')
    except Exception as exc:
        return {'removed': False, 'note': f'Could not read VM {vmid}: {exc}'}

    if response.status_code == 404:
        return {'removed': True, 'note': f'VM {vmid} no longer exists.'}
    if response.status_code != 200:
        return {'removed': False,
                'note': f'Could not read VM {vmid}: {response.text[:160]}'}

    description = (response.json().get('data') or {}).get('description', '')
    if not _describes_migration(description, migration_id):
        return {'removed': False, 'foreign': True,
                'note': f'VM {vmid} on {node} does not carry this migration\'s mark. The '
                        f'VMID now belongs to something else, and nothing about it will be '
                        f'deleted here.'}

    try:
        response = target._api_delete(base)
    except Exception as exc:
        return {'removed': False, 'note': f'Could not delete VM {vmid}: {exc}'}
    if response.status_code not in (200, 201):
        return {'removed': False,
                'note': f'Could not delete VM {vmid}: {response.text[:160]}'}
    return {'removed': True, 'note': f'Deleted VM {vmid} and the disks attached to it.'}


def _free_leftover_volumes(migration, target, volumes):
    """Free volumes the VM deletion did not take with it.

    A volume that was allocated and never attached survives its VM, and it is the most
    expensive kind of leftover: full size, no name in the UI, and nothing pointing at it.
    """
    from pegaprox.core.xhm import _connect_ssh, _resolve_pve_node_ip

    node_name = migration.get('target_node') or ''
    freed, kept = [], []
    node_ip = _resolve_pve_node_ip(target, node_name)
    if not node_ip:
        return [], [{'kind': 'volume', 'id': v.get('id'),
                     'note': f'No address for node {node_name}; volume left in place.'}
                    for v in volumes]

    ssh = None
    try:
        ssh = _connect_ssh(node_ip,
                           getattr(target.config, 'ssh_user', '') or 'root',
                           getattr(target.config, 'pass_', ''),
                           key_path=getattr(target.config, 'ssh_key', ''),
                           port=int(getattr(target.config, 'ssh_port', 22) or 22))
        node = _Node(ssh, getattr(target.config, 'ssh_user', '') or 'root')
        for entry in volumes:
            volume = entry.get('id') or ''
            exit_code, _, err = node.run(f'pvesm free {shlex.quote(volume)}')
            if exit_code == 0 or 'does not exist' in err.lower():
                freed.append({'kind': 'volume', 'id': volume, 'note': 'Freed.'})
            else:
                kept.append({'kind': 'volume', 'id': volume,
                             'note': err.strip()[:160] or f'pvesm free exited {exit_code}'})
    except Exception as exc:  # noqa: BLE001 — reported, never raised at the caller
        logger.warning('Could not free leftover volumes of %s',
                       migration.get('migration_id'), exc_info=True)
        already = {f['id'] for f in freed}
        kept.extend({'kind': 'volume', 'id': v.get('id'), 'note': str(exc)[:160]}
                    for v in volumes if v.get('id') not in already)
    finally:
        if ssh is not None:
            try:
                ssh.close()
            except Exception:
                pass

    for entry in freed:
        hyperv_db.forget_created_resource(_conn(), migration['migration_id'],
                                          'volume', entry['id'])
    return freed, kept


# ---------------------------------------------------------------------------
# The steps
# ---------------------------------------------------------------------------

def _conn():
    from pegaprox.core.db import get_db
    return get_db().conn


def _update_migration_row(migration_id, **fields):
    """Write live state to the durable row, without letting a bookkeeping failure win.

    The migration itself is what matters; a database hiccup during it must not abort a
    transfer that is otherwise going fine. It is logged, because a silent one would make
    the recovery record quietly incomplete.
    """
    if not migration_id:
        return
    try:
        hyperv_db.update_migration(_conn(), migration_id, **fields)
    except Exception:
        logger.warning('Could not update Hyper-V migration %s', migration_id, exc_info=True)


def _record_start(task, guid):
    try:
        return hyperv_db.create_migration(
            _conn(), source_cluster=task.source_cluster, source_vm_guid=guid,
            source_vm_name=task.vm_name, target_cluster=task.target_cluster,
            target_node=task.target_node, target_storage=task.target_storage,
            migration_id=task.id)
    except Exception:
        logger.warning('Could not record the start of Hyper-V migration %s', task.id,
                       exc_info=True)
        return None


def _preflight_gate(task, source, target, detail, guid):
    """Re-run every check at the moment of starting. Returns a reason, or None to proceed.

    Two things are asked that the wizard could not ask: whether the host is still finished
    merging the disks it was told to merge, and whether the target storage still has room.
    Both can change between planning and starting, and both produce silent corruption or a
    filled-up storage rather than an error if they are not asked again.
    """
    safe, reason = source.manager.disks_are_safe_to_read(guid)
    if not safe:
        return f'The source VM is not in a state its disks can be read from: {reason}'

    available = _target_free_bytes(target, task.target_node, task.target_storage)
    report = hyperv_preflight.run_preflight(
        detail,
        {'available_bytes': available},
        {'network_map': task.network_map or {},
         'controller': target_hardware(task.config)['controller'],
         'drivers_injected': target_hardware(task.config)['hardware'] == 'virtio',
         # The share is mounted and each file probed further down, before anything is
         # allocated. Claiming it was probed here would be a claim about a mount that does
         # not exist yet.
         'source_access_probed': False})

    allowed, why = hyperv_preflight.may_start(report, task.config.get('acknowledged') or [])
    if not allowed:
        return why

    task.log(f'Preflight passed with {len(report.warnings)} warning(s)')
    return None


def _target_free_bytes(target, node, storage):
    try:
        response = target._api_get(
            f'https://{target.host}:{target.api_port}'
            f'/api2/json/nodes/{node}/storage/{storage}/status')
        if response.status_code != 200:
            return None
        return response.json().get('data', {}).get('avail')
    except Exception:
        logger.debug('Could not read free space on the target storage', exc_info=True)
        return None


def _next_target_vmid(target):
    try:
        response = target._api_get(
            f'https://{target.host}:{target.api_port}/api2/json/cluster/nextid')
        return int(response.json().get('data'))
    except Exception:
        logger.warning('Could not ask Proxmox for the next free VMID', exc_info=True)
        raise TransferError('Proxmox did not hand out a VMID for the new VM.')


def _open_target_node(task, source, target):
    """Connect to the target node and put the share credentials on it, readable by nobody.

    The account is the one configured for the Hyper-V source. Reusing it rather than
    storing a second password means there is exactly one credential per host, and it stays
    in the encrypted cluster row rather than being copied into a table of this patch's own.
    """
    from pegaprox.core.xhm import _connect_ssh, _resolve_pve_node_ip

    node_ip = _resolve_pve_node_ip(target, task.target_node)
    if not node_ip:
        raise TransferError(f'Cannot resolve an address for Proxmox node {task.target_node}.')

    try:
        ssh = _connect_ssh(node_ip,
                           getattr(target.config, 'ssh_user', '') or 'root',
                           getattr(target.config, 'pass_', ''),
                           key_path=getattr(target.config, 'ssh_key', ''),
                           port=int(getattr(target.config, 'ssh_port', 22) or 22))
    except Exception as exc:
        # paramiko's own wording — "Bad authentication type; allowed types: ['publickey']"
        # — names the protocol and not the thing to do about it. This transfer runs on the
        # node, so an operator reading a failed migration needs to know that the node login
        # is what failed and where it is configured.
        raise TransferError(
            f'Cannot log in to Proxmox node {task.target_node} as '
            f'{getattr(target.config, "ssh_user", "") or "root"}: {exc}. The transfer runs '
            f'on the node itself — it mounts the Hyper-V share there and converts the disk '
            f'with qemu-img — so the cluster needs working node credentials. Add an SSH key '
            f'for this cluster, or allow password logins for that account.') from exc
    node = _Node(ssh, getattr(target.config, 'ssh_user', '') or 'root')

    credentials_path = hyperv_transfer.mount_point_for(task.id) + '.credentials'
    user, domain = hyperv_transfer.split_account(source.config.user)
    domain = domain or getattr(source.config, 'smb_domain', '') or ''
    content = hyperv_transfer.credentials_file_content(user, source.config.pass_, domain)

    exit_code, _, err = node.run(
        f'mkdir -p {shlex.quote(hyperv_transfer.MOUNT_ROOT)} && '
        + hyperv_transfer.credentials_file_command(credentials_path),
        stdin_data=content)
    if exit_code != 0:
        node.close()
        raise TransferError(
            f'Could not prepare the share credentials on the target node: {err.strip()[:200]}')
    return node, credentials_path


def _mounted_path_for(task, node, source, credentials_path, mounts, share_map, disk_path):
    """The disk file as a path on the target node, mounting its share if that is new.

    A VM's disks can live on different drives of the same host, and each drive is its own
    share. Mounting per share rather than once per migration is what keeps a second disk on
    another drive from silently resolving to nothing.
    """
    share, relative = hyperv_transfer.share_for(disk_path, share_map)
    point = mounts.get(share)
    if point is None:
        point = hyperv_transfer.mount_point_for(f'{task.id}-{_share_slug(share)}')
        exit_code, _, err = node.run(
            hyperv_transfer.mount_command(source.config.host, share, point, credentials_path))
        if exit_code != 0:
            raise TransferError(
                f'Could not mount //{source.config.host}/{share} on {task.target_node}: '
                f'{err.strip()[:200]}. The node needs cifs-utils, network access to the '
                f'Hyper-V host, and an account that may read that share.')
        mounts[share] = point
        task.log(f'Mounted {share} read-only on {task.target_node}')
    return hyperv_transfer.source_file_path(point, relative)


def _share_slug(share):
    """A share name as something that can be a directory. `C$` would be a shell variable."""
    return re.sub(r'[^A-Za-z0-9]', '-', share).strip('-') or 'share'


def _transfer_one_disk(task, node, migration_id, source_file, new_vmid, index, disk):
    """Copy and convert one disk. Returns the volume, or None after failing the task."""
    disk_key = f'disk-{index}'
    path = disk.get('path') or ''
    size = int(disk.get('size') or 0)
    if size <= 0:
        _fail(task, migration_id, f'Disk {index} reports no size; refusing to allocate for it')
        return None

    exit_code, out, _ = node.run(hyperv_transfer.probe_command(source_file))
    if exit_code != 0:
        _fail(task, migration_id,
              f'The target node cannot read {path} on the mounted share. The inventory and '
              f'the share disagree about what is there.')
        return None
    task.log(f'Disk {index}: {path} ({size / (1024 ** 3):.1f} GiB), '
             f'{out.strip()} bytes on the share')

    last_error = 'unknown'
    for attempt in range(1, TRANSFER_ATTEMPTS + 1):
        volume = _allocate_volume(task, node, new_vmid, size)
        if volume is None:
            _fail(task, migration_id, 'Proxmox did not allocate a volume for the disk')
            return None
        hyperv_db.record_created_resource(_conn(), migration_id, 'volume', volume,
                                          f'for {disk_key}')

        device = _volume_device_path(node, volume)
        if not device:
            _free_volume(node, volume)
            _forget_volume(migration_id, volume)
            _fail(task, migration_id, f'Proxmox could not name a device for {volume}')
            return None

        def report(percent, key=disk_key, total=size):
            task.update_progress(key, int(total * percent / 100), total)
            try:
                hyperv_db.set_disk_progress(_conn(), migration_id, key,
                                            int(total * percent / 100), total)
            except Exception:
                logger.debug('Could not record disk progress', exc_info=True)

        exit_code, _, err = node.run_with_progress(
            hyperv_transfer.convert_command(source_file, device),
            report, task.cancel_event.is_set)

        if exit_code == 0:
            task.log(f'Disk {index} converted into {volume}')
            # The operator's choice, not the source's hint: the hint says what the disk
            # hung on in Hyper-V, which says nothing about what the guest has drivers for
            # on the other side.
            return {'volume': volume, 'index': index,
                    'controller': target_hardware(task.config)['controller']}

        # A partial conversion is not a partial disk anybody can use. qemu-img writes the
        # target in whatever order the source's block table dictates, so the volume goes
        # and the attempt starts over on a fresh one.
        _free_volume(node, volume)
        _forget_volume(migration_id, volume)
        last_error = err.strip()[:300] or f'qemu-img exited {exit_code}'
        if task.cancel_event.is_set():
            _fail(task, migration_id, 'Cancelled during the conversion')
            return None
        task.log(f'Disk {index} attempt {attempt} failed: {last_error}')
        if attempt < TRANSFER_ATTEMPTS:
            time.sleep(_RETRY_BACKOFF_SECONDS * attempt)

    _fail(task, migration_id,
          f'Disk {index} could not be converted after {TRANSFER_ATTEMPTS} attempts: {last_error}')
    return None


def _allocate_volume(task, node, new_vmid, size_bytes):
    """Ask Proxmox for a volume big enough for the converted disk.

    Rounded up: `pvesm alloc` takes kibibytes and a disk whose size is not a whole number
    of them would get a volume one write short of what the conversion needs, and fail at
    the very end of a long copy.
    """
    size_kib = (size_bytes + _BYTES_PER_KIB - 1) // _BYTES_PER_KIB
    exit_code, out, err = node.run(
        f'pvesm alloc {shlex.quote(task.target_storage)} {int(new_vmid)} \'\' {size_kib}')
    if exit_code != 0:
        logger.warning('[XHM:%s] pvesm alloc failed: %s', task.id, err.strip()[:200])
        return None

    match = _ALLOC_VOLUME.search(out)
    if match:
        return match.group(1)
    # LVM prints warnings before the volume line, so the last non-warning line is the
    # answer when the quoted form is absent.
    for line in reversed(out.splitlines()):
        line = line.strip()
        if line and not line.startswith('WARNING'):
            return line
    return None


def _volume_device_path(node, volume):
    exit_code, out, _ = node.run(f'pvesm path {shlex.quote(volume)}')
    return out.strip() if exit_code == 0 else ''


def _free_volume(node, volume):
    try:
        node.run(f'pvesm free {shlex.quote(volume)}')
    except Exception:
        logger.warning('Could not free the partial volume %s', volume, exc_info=True)


def _is_unset_mac(mac: str) -> bool:
    """Is this the all-zero MAC Hyper-V reports for an adapter that has never been used?

    Hyper-V assigns a dynamic MAC when the VM first starts. Until then the adapter reports
    zeroes, and passing those on is refused by the target rather than ignored.
    """
    return not set(mac.replace(':', '').replace('-', '')) - {'0'}


def _inject_drivers_if_asked(task, target, new_vmid, volumes, detail):
    """Run the product's own offline driver injection on the freshly imported disk.

    The same function the VMware direction uses (`v2p._inject_virtio_drivers`), rather than
    a second implementation: the defects found in it - the apt conflict, the missing hivex
    shell, the catalogue location, the BusType value, the INF spelling - were each found
    once and have to stay fixed for both directions.

    It reads a handful of attributes off the task it is given. The Hyper-V task carries
    different names for two of them, so it is handed a small stand-in rather than being
    taught to look elsewhere; that keeps the shared function unaware of which direction
    called it.

    Returns None when there is nothing to report, or a line for the log.
    """
    if target_hardware(task.config)['hardware'] != 'virtio':
        return None

    from pegaprox.core import v2p

    class _InjectionView:
        """What v2p's injection reads, filled from a Hyper-V migration."""
        install_virtio_drivers = True

        def __init__(self, inner, vmid):
            self.proxmox_vmid = vmid
            self.target_node = inner.target_node
            self.target_storage = inner.target_storage
            self.config = inner.config or {}
            self.virtio_iso_path = (inner.config or {}).get('virtio_iso_path', '') or ''
            #: Why the injection did not happen, when it did not. Read off the log lines
            #: rather than a return value: the injection is shared with the VMware
            #: direction and returns a plain bool, and widening that would change a
            #: function four other call sites depend on. The markers are written by the
            #: same repository and are pinned by tests.
            self.refused_for_signature = False
            #: There is no Windows on this disk. A Linux guest carries VirtIO in its
            #: kernel and wants the VirtIO hardware it was given, so this is the one
            #: failure that must not send the VM back to the compatible controller.
            self.guest_is_not_windows = False
            self._inner = inner

        def log(self, message):
            text = str(message)
            if 'BOOT_SIGNATURE_MISSING vioscsi' in text:
                self.refused_for_signature = True
            if 'NO_WINDOWS_DIR' in text:
                self.guest_is_not_windows = True
            self._inner.log(text)

    view = _InjectionView(task, new_vmid)
    try:
        with _node_session(task, target) as run_on_node:
            ok = v2p._inject_virtio_drivers(target, view, node_exec=run_on_node)
    except Exception as exc:                                   # noqa: BLE001
        # The migration itself succeeded; the drivers are a preparation for the switch that
        # follows it. Failing the whole run here would throw away a completed copy.
        return f'VirtIO driver injection failed: {exc}'
    if ok:
        return None
    if view.guest_is_not_windows:
        # Nothing was injected because there was nothing to inject into. A Linux guest has
        # VirtIO in its kernel, so the hardware it was given is the hardware it wants.
        return ('No Windows installation was found on the disk, so no drivers were '
                'injected. The VM keeps the VirtIO hardware it was created with, which is '
                'what a Linux guest wants.')

    # The VM was built on VirtIO because that is what was asked for, and the drivers that
    # would let it start from VirtIO are not in it. Whatever the reason, the machine as
    # configured does not boot. Moving it back to the compatible controller is the
    # difference between a machine that starts and one that does not, and nothing has
    # started it yet, so it can still be moved.
    reason = ('This guest\'s Windows version has no VirtIO driver that its loader would '
              'accept as a boot driver'
              if view.refused_for_signature else
              'The VirtIO drivers could not be injected')
    moved = _move_to_compatible_hardware(task, target, new_vmid, volumes, detail)
    if moved:
        return (f'{reason}, so the VM was built on its compatible controller instead and '
                f'boots as it is. See the log above for the reason, and switch it to '
                f'VirtIO once the drivers are in.')
    return (f'{reason}, and the VM could not be moved back to the compatible controller. '
            f'It will not start as configured - change the disk controller to SATA before '
            f'starting it.')



@contextlib.contextmanager
def _node_session(task, target):
    """A way for the injection to reach the node that works for this cluster.

    The shared `_pve_node_exec` logs in as `root` with the cluster's password. A cluster
    registered the other way PegaProx supports - an API token plus an SSH key for a
    non-root account - has neither, so every command it sends comes back with a non-zero
    code and no output, and the injection reports an empty error twice and gives up.
    Measured on exactly such a cluster: the transfer succeeded, because it uses the
    connection below, and the injection that followed it failed.

    Yields a callable with `_pve_node_exec`'s signature, so the injection does not learn
    anything about which direction called it. Falls back to the shared function when no
    connection can be opened, which keeps the behaviour it had before rather than turning
    a working case into a failure.
    """
    from pegaprox.core.xhm import _connect_ssh, _resolve_pve_node_ip

    ssh = None
    try:
        node_ip = _resolve_pve_node_ip(target, task.target_node)
        if node_ip:
            ssh = _connect_ssh(node_ip,
                               getattr(target.config, 'ssh_user', '') or 'root',
                               getattr(target.config, 'pass_', ''),
                               key_path=getattr(target.config, 'ssh_key', ''),
                               port=int(getattr(target.config, 'ssh_port', 22) or 22))
    except Exception as exc:                                   # noqa: BLE001
        task.log(f'Could not open a node session for the driver injection ({exc}); '
                 f'falling back to the shared node connection.')
        ssh = None

    if ssh is None:
        yield None
        return

    node = _Node(ssh, getattr(target.config, 'ssh_user', '') or 'root')

    def run_on_node(_manager, _node, command, timeout=600, **_ignored):
        return node.run(command, timeout=timeout)

    try:
        yield run_on_node
    finally:
        try:
            ssh.close()
        except Exception:                                      # noqa: BLE001
            pass


def _move_to_compatible_hardware(task, target, new_vmid, volumes, detail):
    """Re-attach the disks on the compatible controller. True when the VM now boots.

    Only reached when the drivers cannot be made boot-critical. The volumes are already
    written and correct; what changes is the bus they hang on and the NIC model that went
    with the VirtIO choice.
    """
    config_url = (f'https://{target.host}:{target.api_port}'
                  f'/api2/json/nodes/{task.target_node}/qemu/{new_vmid}/config')

    def post(data):
        try:
            response = target._api_post(config_url, data=data)
            if response.status_code == 200:
                return True
            task.log(f'Could not change the VM hardware: {response.text[:160]}')
        except Exception as exc:                               # noqa: BLE001
            task.log(f'Could not change the VM hardware: {exc}')
        return False

    ordered = sorted(volumes, key=lambda volume: volume['index'])
    current = [f"{VIRTIO_CONTROLLER}{slot}" for slot, _ in enumerate(ordered)]
    # Detached first and in one call: Proxmox refuses a volume that is still attached
    # somewhere else on the same VM.
    if not post({'delete': ','.join(current)}):
        return False

    # Every volume is tried, and a failure does not end the loop. After the detach above,
    # a volume that is not re-attached is on no controller at all - it is gone from the VM
    # - so stopping at the first failure would leave the rest of a multi-disk guest
    # detached and unnamed, while the caller reported only that the controller could not be
    # changed. An operator following that would go looking for a disk on the wrong bus
    # instead of for a disk that is missing.
    attached, orphaned = [], []
    for slot, volume in enumerate(ordered):
        name = f'{COMPATIBLE_CONTROLLER}{slot}'
        if post({name: volume['volume']}):
            attached.append((volume['index'], name))
            task.log(f"Re-attached {volume['volume']} as {name}")
        else:
            orphaned.append(volume['volume'])

    if orphaned:
        task.log('These volumes are on the target and belong to this migration, but are '
                 'attached to no controller: ' + ', '.join(orphaned)
                 + '. Attach them by hand or clean the migration up.')

    boot_disk = _boot_disk_name(attached, detail)
    changes = {}
    if boot_disk:
        changes['boot'] = f'order={boot_disk}'
    # The NIC followed the disk controller into VirtIO, and a guest without the VirtIO
    # network driver installed comes up with no network at all.
    nic = _current_nic_settings(target, task, new_vmid)
    for name, value in nic.items():
        changes[name] = value.replace(f'{VIRTIO_NIC_MODEL}=', f'{COMPATIBLE_NIC_MODEL}=', 1)
    if changes and not post(changes):
        return False
    # Not "did every call succeed" but "can this VM start": a guest missing one of its
    # disks must not be handed over as moved.
    return not orphaned


def _current_nic_settings(target, task, new_vmid):
    """The VM's net* entries, so their model can be rewritten without losing the MAC."""
    try:
        response = target._api_get(
            f'https://{target.host}:{target.api_port}'
            f'/api2/json/nodes/{task.target_node}/qemu/{new_vmid}/config')
        if response.status_code != 200:
            return {}
        data = response.json().get('data') or {}
    except Exception:                                          # noqa: BLE001
        return {}
    return {name: value for name, value in data.items()
            if name.startswith('net') and name[3:].isdigit()
            and isinstance(value, str) and value.startswith(f'{VIRTIO_NIC_MODEL}=')}

def _create_target_vm(task, target, new_vmid, detail):
    """Create the VM shell. Returns True, or a reason."""
    generation = detail.get('generation')
    if generation not in GENERATION_BIOS:
        # Preflight blocks this, so reaching it means the inventory changed under the run.
        # Defaulting to generation 1 would build a BIOS machine for a UEFI guest, which
        # boots into a firmware shell and reads as a failed conversion.
        return f'The source reports VM generation {generation!r}, which has no target mapping'
    bios = GENERATION_BIOS[generation]
    machine = GENERATION_MACHINE[generation]
    hardware = target_hardware(task.config)

    create = {
        'vmid': new_vmid,
        'name': task.vm_name or f'hyperv-{task.source_vmid}',
        'memory': detail.get('memory_mb') or 1024,
        'cores': detail.get('cpu_count') or 1,
        'sockets': 1,
        # Nothing on this side can see inside the guest, so the honest default is 'other'.
        # Guessing Windows would set timers and devices for an operating system that may not
        # be there, and the wizard asks for this anyway.
        'ostype': task.config.get('ostype') or 'other',
        'bios': bios,
        'machine': machine,
        'scsihw': 'virtio-scsi-single',
        # Who made this, and under which migration. A cleanup reads it back before it
        # deletes anything: a VMID says nothing about ownership, and the number can have
        # been taken by somebody else's guest since this run failed.
        'description': target_vm_description(task.id, task.vm_name),
    }

    network_map = task.network_map or {}
    # Read off the request rather than the shared task object: `vlan_map` is this fork's
    # field, and the task class belongs to upstream.
    vlan_map = (task.config or {}).get('vlan_map') or {}
    for index, nic in enumerate((detail.get('network_adapters') or [])[:MAX_NETWORK_ADAPTERS]):
        key = nic.get('mac_address') or nic.get('name') or ''
        bridge = network_map.get(key)
        if not bridge:
            # Preflight blocks an unmapped adapter, so reaching this means the map changed
            # under the run. Leaving the NIC off is the safe half of the mistake: a VM
            # missing a network is visible, one on the wrong VLAN is not.
            task.log(f'Adapter {key or index} has no target network; leaving it off')
            continue
        # The MAC is carried over so licence bindings, DHCP reservations and firewall rules
        # written against it keep working. A new one turns a migration into a new machine
        # for everything that identified the guest that way.
        #
        # It has to be the colon-separated spelling: Hyper-V reports `00155D000001` and
        # Proxmox refuses that outright ("does not look like a valid unicast MAC address"),
        # which failed every VM that had an adapter at all.
        mac = nic.get('mac_address_colons') or format_mac(nic.get('mac_address'))
        if mac and _is_unset_mac(mac):
            # All zeroes is not an address, it is the absence of one: Hyper-V assigns a
            # dynamic MAC at first start, so a VM that has never run reports this. Carrying
            # it over would be refused; leaving it out lets Proxmox assign one, which is
            # what the source would have done too.
            task.log(f'Adapter {key or index} has no MAC yet; the target will assign one')
            mac = None
        # The NIC model follows the same decision as the disk controller. A VirtIO NIC on a
        # guest that was imported without VirtIO drivers has no driver either, so a
        # compatible disk controller with a VirtIO network card would still leave the guest
        # without a network until somebody installed drivers it cannot download.
        # The VLAN the operator confirmed in the wizard, falling back to what the source
        # reported. An empty entry is a decision too -- it means "no tag" -- so only a
        # missing key falls through to the source's own value.
        if key in vlan_map:
            chosen = vlan_map.get(key)
        else:
            chosen = vlan_for_adapter(nic)
        try:
            vlan = int(chosen)
        except (TypeError, ValueError):
            vlan = 0
        if vlan and not 1 <= vlan <= 4094:
            task.log(f'Adapter {key or index}: VLAN {vlan} is out of range; leaving it untagged')
            vlan = 0
        create[f'net{index}'] = (f'{hardware["nic_model"]},bridge={bridge}'
                                 + (f',macaddr={mac}' if mac else '')
                                 + (f',tag={vlan}' if vlan else ''))
        task.log(f'Adapter {key or index} -> {bridge}'
                 + (f' VLAN {vlan}' if vlan else ' untagged'))

    try:
        response = target._api_post(
            f'https://{target.host}:{target.api_port}'
            f'/api2/json/nodes/{task.target_node}/qemu', data=create)
    except Exception as exc:
        return f'Creating the target VM failed: {exc}'
    if response.status_code not in (200, 201):
        return f'Creating the target VM failed: {response.text[:200]}'

    # Creating a VM is an asynchronous task and it holds a lock while it runs. Posting the
    # disk configuration straight afterwards is refused with "VM is locked (create)", which
    # on a real node happens every time and left the VM with no disks at all. Waiting for
    # the task costs a second and is the difference between an importable VM and one that
    # boots to a network prompt.
    upid = (response.json() or {}).get('data')
    if isinstance(upid, str) and upid.startswith('UPID:'):
        if not target._wait_for_task(task.target_node, upid, timeout=_CREATE_TASK_TIMEOUT):
            return f'Creating the target VM did not finish: {upid}'

    task.log(f'Created VM {new_vmid} on {task.target_node} '
             f'({bios}, {machine}, {hardware["hardware"]} hardware)')
    return True


def _attach_disks(task, target, new_vmid, volumes, detail):
    """Attach the converted volumes and say what the VM boots from.

    Returns the names of the disks that could not be attached. The transfer is never undone
    for one of these: the volumes exist, they are correct, and they stay recorded so an
    operator can attach them or clean them up. But the import does not report success —
    a VM with no disk boots to a network prompt, and calling that "completed" sends somebody
    to a machine they think is migrated.
    """
    failed = []
    attached = []
    per_controller = {}
    for volume in sorted(volumes, key=lambda v: v['index']):
        controller = volume['controller']
        slot = per_controller.get(controller, 0)
        per_controller[controller] = slot + 1
        name = f'{controller}{slot}'
        try:
            response = target._api_post(
                f'https://{target.host}:{target.api_port}'
                f'/api2/json/nodes/{task.target_node}/qemu/{new_vmid}/config',
                data={name: volume['volume']})
            if response.status_code == 200:
                task.log(f"Attached {volume['volume']} as {name}")
                attached.append((volume['index'], name))
            else:
                failed.append(name)
                task.log(f'Could not attach {name}: {response.text[:160]}')
        except Exception as exc:
            failed.append(name)
            task.log(f'Could not attach {name}: {exc}')

    extra = {}
    boot_disk = _boot_disk_name(attached, detail)
    if boot_disk:
        extra['boot'] = f'order={boot_disk}'
    if GENERATION_BIOS.get(detail.get('generation')) == 'ovmf':
        # A Generation 2 guest boots UEFI and needs somewhere to keep its variables. Without
        # this the VM starts into the firmware shell and looks like a failed conversion.
        extra['efidisk0'] = f'{task.target_storage}:1,efitype=4m,pre-enrolled-keys=0'
    if extra:
        try:
            response = target._api_post(
                f'https://{target.host}:{target.api_port}'
                f'/api2/json/nodes/{task.target_node}/qemu/{new_vmid}/config', data=extra)
            if response.status_code != 200:
                failed.append('boot configuration')
                task.log(f'Could not finish the VM configuration: {response.text[:160]}')
        except Exception as exc:
            failed.append('boot configuration')
            task.log(f'Could not finish the VM configuration: {exc}')
    return failed


def _boot_disk_name(attached, detail):
    """Which attached disk the VM should boot from.

    The source's own boot order decides where it can. Hyper-V lists the boot entries of a
    Generation 2 VM, and its first disk entry is the one the guest's loader lives on —
    which is not always the disk that happened to attach first here. Without that
    information the lowest source index is the best available guess, and it is the same
    disk in every ordinary case.
    """
    if not attached:
        return None
    by_index = {index: name for index, name in attached}
    for index in (detail.get('boot_disk_order') or []):
        if index in by_index:
            return by_index[index]
    return by_index[min(by_index)]


def _finish(task, migration_id, new_vmid):
    """Complete, and say plainly what was deliberately not done.

    The source is left exactly as it was found — running or off, with its disks untouched.
    That is what makes the rollback for this migration "start the original again", and it
    is why nothing here deletes or disables it even when the copy is perfect.
    """
    task.progress = 100
    task.set_phase('completed')
    _update_migration_row(migration_id, phase='completed', status=hyperv_db.STATUS_COMPLETED,
                     progress=100, completed_at=time.time())
    task.log(f'Migration complete. The Hyper-V source is untouched; VMID {new_vmid} on '
             f'{task.target_node} has not been started.')


def _fail(task, migration_id, reason):
    task.set_phase('failed', reason)
    _update_migration_row(migration_id, status=hyperv_db.STATUS_FAILED, error=str(reason)[:500],
                     completed_at=time.time())
