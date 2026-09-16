"""What happens to an imported VM after it arrives, on the Proxmox side only.

An import lands a guest on hardware it already has drivers for — SATA and e1000 — because
a Windows guest coming off Hyper-V has no VirtIO drivers and would not find its disk. That
makes the VM bootable immediately and slower than it needs to be, and the two steps here
are how it gets the rest of the way:

  1. Offer the VirtIO driver ISO to the guest, so somebody can install the drivers inside
     it. Nothing here enters the guest: this attaches a CD and stops.
  2. Once the drivers are in, move the VM onto the faster hardware.

The order matters and cannot be checked from outside. Nothing on this side can see whether
a driver is installed — there is no agent, no network into the guest, and the disk is not
inspected. So "the ISO is attached" and "the drivers are installed" are two separate facts
here, and the second one only ever comes from a person saying so. Treating the first as the
second is how a VM gets switched to VirtIO and stops booting.

Everything acts on the VM a recorded migration created, verified by the mark in its
description, for the same reason the cleanup does: a VMID says nothing about ownership and
the number may belong to somebody else by now.
"""

from __future__ import annotations

import functools
import itertools
import logging
import time

from pegaprox.core import hyperv_db
from pegaprox.core.hyperv_xhm import (VIRTIO_CONTROLLER, VIRTIO_NIC_MODEL,
                                      _describes_migration, _conn)
from pegaprox.globals import cluster_managers

logger = logging.getLogger(__name__)

#: Where the driver ISO is offered to the guest. A separate drive from any the import
#: created, so an ISO that was already mounted is never silently displaced.
VIRTIO_DRIVE = 'ide2'

#: How the ISO is recognised among a storage's contents. Matched loosely because the file
#: is downloaded by an operator and its name carries a version.
VIRTIO_ISO_HINTS = ('virtio-win', 'virtio_win')

#: What "switch to the standard" changes. Only these keys are touched; everything else on
#: the VM — its name, its memory, its network assignment, anything an operator set by hand —
#: is left exactly as it is.
#:
#: The values are the ones the manual migration procedure sets by hand, so the button
#: produces the VM an operator would have built. Nothing here is inferred from what reads
#: well: a guessed CPU model boots fine on the node it was chosen on and fails to
#: live-migrate months later, which is why this stayed empty until the procedure was on
#: the table.
#:
#: Three settings from that procedure are deliberately *not* here, because this button
#: changes a VM that already has an operating system on it:
#:
#: * **Firmware and machine type.** They follow the source VM's generation and are decided
#:   at import. Moving an installed guest from SeaBIOS to OVMF does not boot it.
#: * **A TPM.** It needs a new state volume, and a guest imported without one gains
#:   nothing from being given one afterwards.
#: * **Firewall and HA.** Cluster policy, not the VM's hardware.
STANDARD_PROFILE = {
    'name': 'virtio',
    'controller': VIRTIO_CONTROLLER,
    'nic_model': VIRTIO_NIC_MODEL,
    #: Applied to every disk this switch moves; options the disk already carries are kept.
    'disk_options': {'cache': 'writeback', 'discard': 'on', 'ssd': '1'},
    #: `cpu` is the one value with a known exception: an older Windows Server 2016 or 2019
    #: can bluescreen (DXGKRNL_FATAL_ERROR, roughly every eleven minutes) on a modern CPU
    #: model, and `kvm64` is what settles it. That is a per-guest override, not a reason to
    #: hold every other VM back.
    'extra': {'cpu': 'x86-64-v2-AES', 'numa': '1', 'balloon': '0', 'agent': '1'},
}

#: Disk keys an import can have created, in the order a boot entry would name them.
_DISK_PREFIXES = ('sata', 'scsi', 'virtio', 'ide')


class PostImportError(Exception):
    """Something about the request is wrong, and the message says what to do about it."""


def _as_result(function):
    """Turn a refusal into a result dict, the way the cleanup path reports one.

    Every refusal here is something an operator has to read and act on -- a VM that is not
    this migration's, a cluster that is not connected -- not a programming error. The web
    layer above reports the message rather than a stack trace.
    """
    @functools.wraps(function)
    def wrapper(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except PostImportError as exc:
            return {'success': False, 'error': str(exc)}
    return wrapper


def _target_and_vmid(migration_id):
    """The target manager and VMID of a recorded migration, or a reason there is none."""
    migration = hyperv_db.get_migration(_conn(), migration_id)
    if migration is None:
        raise PostImportError(f'No such migration: {migration_id}')
    vmid = migration.get('target_vmid')
    if not vmid:
        raise PostImportError('This migration never created a VM on the target.')
    target = cluster_managers.get(migration['target_cluster'])
    if not target or not getattr(target, 'is_connected', False):
        raise PostImportError(
            f'Target cluster {migration["target_cluster"]} is not connected.')
    return migration, target, int(vmid)


def _read_config(target, node, vmid, *, live=False):
    """The VM's configuration. `live` asks for what QEMU is running right now.

    The two differ while a VM runs. Proxmox answers with pending values merged in by
    default, so a drive that was added to a running VM reads as present here and does not
    exist inside the guest. Which of the two a caller wants depends on the question: what
    the VM *is* configured as, or what the guest can currently see.
    """
    url = (f'https://{target.host}:{target.api_port}'
           f'/api2/json/nodes/{node}/qemu/{vmid}/config'
           + ('?current=1' if live else ''))
    response = target._api_get(url)
    if response.status_code == 404:
        raise PostImportError(f'VM {vmid} no longer exists on {node}.')
    if response.status_code != 200:
        raise PostImportError(f'Could not read VM {vmid}: {response.text[:160]}')
    return (response.json().get('data') or {})


def _our_vm(migration_id, target, node, vmid):
    """Read the VM's config, refusing one that is not this migration's."""
    config = _read_config(target, node, vmid)
    if not _describes_migration(config.get('description', ''), migration_id):
        raise PostImportError(
            f'VM {vmid} on {node} does not carry this migration\'s mark. The VMID belongs '
            f'to something else now, and nothing about it will be changed here.')
    return config


def _post_config(target, node, vmid, data):
    response = target._api_post(
        f'https://{target.host}:{target.api_port}'
        f'/api2/json/nodes/{node}/qemu/{vmid}/config', data=data)
    if response.status_code not in (200, 201):
        raise PostImportError(f'Proxmox refused the change: {response.text[:200]}')
    return response


def _is_running(target, node, vmid):
    response = target._api_get(
        f'https://{target.host}:{target.api_port}'
        f'/api2/json/nodes/{node}/qemu/{vmid}/status/current')
    if response.status_code != 200:
        return None
    return (response.json().get('data') or {}).get('status') == 'running'


# ---------------------------------------------------------------------------
# The driver ISO
# ---------------------------------------------------------------------------

def find_virtio_isos(target, node):
    """Every VirtIO driver ISO the node can see, newest-looking first.

    The ISO is not shipped and not downloaded by PegaProx: an operator puts it on a storage
    the way they put any other ISO there. If none is found that is a fact to report, not a
    reason to fetch something from the internet onto a customer's node.
    """
    found = []
    response = target._api_get(
        f'https://{target.host}:{target.api_port}/api2/json/nodes/{node}/storage')
    if response.status_code != 200:
        return found
    for storage in (response.json().get('data') or []):
        if 'iso' not in (storage.get('content') or ''):
            continue
        name = storage.get('storage')
        listing = target._api_get(
            f'https://{target.host}:{target.api_port}'
            f'/api2/json/nodes/{node}/storage/{name}/content?content=iso')
        if listing.status_code != 200:
            continue
        for item in (listing.json().get('data') or []):
            volid = item.get('volid') or ''
            if any(hint in volid.lower() for hint in VIRTIO_ISO_HINTS):
                found.append({'volid': volid, 'size': item.get('size'), 'storage': name})
    return sorted(found, key=lambda entry: entry['volid'], reverse=True)


@_as_result
def describe_driver_state(migration_id):
    """What is true about this VM's drivers, with the two facts kept apart.

    `iso_attached` is observed. `drivers_confirmed` is somebody's word, recorded with the
    migration, and nothing here ever infers it from the first.
    """
    migration, target, vmid = _target_and_vmid(migration_id)
    node = migration.get('target_node') or ''
    config = _our_vm(migration_id, target, node, vmid)
    return _driver_state(migration, target, node, vmid, config)


def _driver_state(migration, target, node, vmid, config):
    """The same answer, for callers that have already read the config and the row."""
    attached = config.get(VIRTIO_DRIVE) or ''
    # What the guest can see, which is not the same as what the VM is configured with while
    # it runs. A pending drive is in the config and in no guest.
    live = _read_config(target, node, vmid, live=True) if _is_running(target, node, vmid) \
        else config
    confirmation = (migration.get('post_import') or {}).get('drivers') or {}
    return {
        'vmid': vmid,
        'node': node,
        'iso_attached': bool(attached) and 'media=cdrom' in attached,
        'iso': attached or None,
        'iso_pending': bool(attached) and not (live or {}).get(VIRTIO_DRIVE),
        'drivers_confirmed': bool(confirmation.get('confirmed')),
        'confirmed_by': confirmation.get('by'),
        'confirmed_at': confirmation.get('at'),
        'running': _is_running(target, node, vmid),
        'hardware': _current_hardware(config),
        # Said plainly, because the difference is the whole point: an attached ISO is a
        # disc in a drive. Whether anybody installed from it cannot be seen from here.
        'note': ('An attached ISO is a disc in a drive. Whether the drivers were installed '
                 'inside the guest cannot be seen from outside it and has to be confirmed '
                 'by whoever installed them.'),
    }


@_as_result
def attach_virtio_iso(migration_id, volid=None, *, replace=False):
    """Put the driver ISO in this VM's drive. Never enters the guest.

    Needs no guest network and no guest agent: it is a change to the VM's hardware, made
    through the Proxmox API, and the guest sees a CD appear.
    """
    migration, target, vmid = _target_and_vmid(migration_id)
    node = migration.get('target_node') or ''
    config = _our_vm(migration_id, target, node, vmid)

    present = config.get(VIRTIO_DRIVE) or ''
    if present and 'none' not in present and not replace:
        return {'success': False, 'replaced': False, 'current': present,
                'error': f'{VIRTIO_DRIVE} already holds {present}. Replacing a mounted '
                         f'medium can interrupt whatever is using it, so it is not done '
                         f'without saying so explicitly.'}

    if not volid:
        candidates = find_virtio_isos(target, node)
        if not candidates:
            return {'success': False,
                    'error': 'No VirtIO driver ISO was found on this node. Upload one to '
                             'an ISO storage first; PegaProx does not download it.'}
        volid = candidates[0]['volid']

    # Whether the guest will actually see a disc depends on there already being a drive to
    # put it in. Proxmox can swap the medium in an existing drive while the VM runs, but a
    # drive that did not exist at boot is added to the pending configuration and appears
    # only at the next start -- QEMU reports no such device in the meantime. Saying
    # "install the drivers now" in that state sends somebody looking for a disc that is
    # not there.
    running = _is_running(target, node, vmid)
    live_drive = bool((_read_config(target, node, vmid, live=True) or {}).get(VIRTIO_DRIVE))
    pending = bool(running) and not live_drive

    _post_config(target, node, vmid, {VIRTIO_DRIVE: f'{volid},media=cdrom'})
    logger.info('Attached %s to VM %s for migration %s%s', volid, vmid, migration_id,
                ' (takes effect at the next start)' if pending else '')
    message = (f'{volid} is in {VIRTIO_DRIVE} of VM {vmid}. Install the drivers inside the '
               f'guest, then confirm that here.')
    if pending:
        message = (f'{volid} is configured as {VIRTIO_DRIVE} of VM {vmid}, but the VM is '
                   f'running and had no CD drive when it started. The guest will not see '
                   f'the disc until the VM is stopped and started again.')
    return {'success': True, 'replaced': bool(present), 'vmid': vmid, 'iso': volid,
            'drivers_confirmed': False, 'pending': pending, 'running': bool(running),
            'message': message}


@_as_result
def confirm_drivers(migration_id, by, *, confirmed=True):
    """Record that a person says the drivers are installed in the guest.

    This is a statement, not a measurement, and it is stored as one — with who made it and
    when, so the profile switch afterwards can say what it is relying on.
    """
    _target_and_vmid(migration_id)
    hyperv_db.set_post_import(
        _conn(), migration_id,
        drivers=({'confirmed': True, 'by': by, 'at': time.time()} if confirmed else None))
    return {'success': True, 'drivers_confirmed': bool(confirmed), 'by': by,
            'message': 'Recorded as a manual confirmation. Nothing was verified inside '
                       'the guest.'}


# ---------------------------------------------------------------------------
# The standard profile
# ---------------------------------------------------------------------------

def _occupied_slots(config, controller):
    """The indices already taken on one controller, CD drives included.

    A key holds one thing. Handing a moved disk the number of a drive that is already
    there replaces that drive in the config and detaches whatever was in it -- which on a
    VM that was imported to VirtIO and later had a SATA disk added by hand is the second
    disk disappearing, quietly, during what was announced as a controller change.
    """
    return {int(key[len(controller):]) for key in config
            if key.startswith(controller) and key[len(controller):].isdigit()}


def _disk_options(value, wanted):
    """What a moved disk carries on its new controller: its own options, plus the profile's.

    `size=` is dropped -- it describes the volume and Proxmox writes it back itself. An
    option the disk already has wins over the profile's, because somebody set it on this
    disk on purpose and a hardware switch is not the place to overrule them.
    """
    kept = [part for part in str(value).split(',')[1:] if not part.startswith('size=')]
    present = {part.split('=')[0] for part in kept}
    added = [f'{key}={setting}' for key, setting in (wanted or {}).items()
             if key not in present]
    return ','.join(kept + added), added


def _already_set(key, current, wanted):
    """Whether a setting already says what the profile wants, in Proxmox's own spelling.

    `agent` is the one that has to be read rather than compared: an enabled guest agent is
    written both as `1` and as `enabled=1,...`, and the long form carries settings somebody
    chose. Replacing it with a bare `1` would drop them.
    """
    if current is None:
        return False
    current = str(current)
    if key == 'agent':
        return current.startswith('1') or 'enabled=1' in current
    return current == str(wanted)


def _current_hardware(config):
    """The disks and network cards a VM has now, in the shape a change is described in."""
    disks, nics = [], []
    for key, value in sorted(config.items()):
        if key.startswith('net') and key[3:].isdigit():
            nics.append({'key': key, 'model': str(value).split(',')[0].split('=')[0],
                         'value': value})
        elif any(key.startswith(p) and key[len(p):].isdigit() for p in _DISK_PREFIXES):
            if 'media=cdrom' in str(value):
                continue
            disks.append({'key': key, 'controller': key.rstrip('0123456789'),
                          'value': value})
    return {'disks': disks, 'nics': nics, 'boot': config.get('boot'),
            'scsihw': config.get('scsihw')}


@_as_result
def preview_profile(migration_id, profile=None):
    """What switching to the profile would change, before anybody agrees to it.

    The public shape: what an operator has to read, and nothing this module keeps for
    itself. `_preview` carries the parts that describe *how* to make the change, and those
    are working notes rather than an answer to anybody's question.
    """
    return _public(_preview(migration_id, profile))


def _preview(migration_id, profile=None):
    """The same, plus the internals the change itself is built from."""
    profile = profile or STANDARD_PROFILE
    migration, target, vmid = _target_and_vmid(migration_id)
    node = migration.get('target_node') or ''
    config = _our_vm(migration_id, target, node, vmid)
    hardware = _current_hardware(config)
    running = _is_running(target, node, vmid)
    state = _driver_state(migration, target, node, vmid, config)

    changes = []
    taken = _occupied_slots(config, profile['controller'])
    for disk in hardware['disks']:
        volume = str(disk['value']).split(',')[0]
        options, added = _disk_options(disk['value'], profile.get('disk_options'))
        if disk['controller'] == profile['controller']:
            # Already on the right bus. It is still not on the profile while it is missing
            # the options the profile sets, and a disk nobody has to move is the cheapest
            # place to notice that.
            if not added:
                continue
            changes.append({'kind': 'disk', 'from': disk['key'], 'to': disk['key'],
                            'detail': f"{disk['key']} gains {', '.join(added)}",
                            '_volume': volume, '_options': options})
            continue
        slot = next(candidate for candidate in itertools.count() if candidate not in taken)
        taken.add(slot)
        target_key = f"{profile['controller']}{slot}"
        detail = f"{volume} moves from {disk['key']} to {target_key}"
        if added:
            detail += f" and gains {', '.join(added)}"
        changes.append({'kind': 'disk', 'from': disk['key'], 'to': target_key,
                        'detail': detail, '_volume': volume, '_options': options})
    for nic in hardware['nics']:
        if nic['model'] == profile['nic_model']:
            continue
        changes.append({'kind': 'nic', 'from': nic['key'], 'to': nic['key'],
                        'detail': f"{nic['key']} changes from {nic['model']} to "
                                  f"{profile['nic_model']}",
                        '_value': nic['value'], '_model': nic['model']})
    for key, value in (profile.get('extra') or {}).items():
        if not _already_set(key, config.get(key), value):
            changes.append({'kind': 'setting', 'from': key, 'to': key,
                            'detail': f'{key}: {config.get(key)!r} becomes {value!r}',
                            '_value': value})

    requirements = []
    if not state['drivers_confirmed']:
        requirements.append(
            'The VirtIO drivers have to be installed inside the guest and confirmed here '
            'first. Without them the guest will not find its disk after this change.')
    if running:
        requirements.append(
            f'VM {vmid} is running. This change needs it powered off; it is not shut down '
            f'automatically.')

    return {
        'vmid': vmid, 'node': node, 'profile': profile['name'],
        'running': running,
        'drivers_confirmed': state['drivers_confirmed'],
        'changes': [{k: v for k, v in change.items() if not k.startswith('_')}
                    for change in changes],
        'unchanged': ('Everything not listed stays as it is, including the VM\'s name, its '
                      'memory, its network assignment and anything set by hand.'),
        'requirements': requirements,
        'can_apply': not requirements and bool(changes),
        'already_applied': not changes,
        '_changes': changes,
    }


@_as_result
def apply_profile(migration_id, profile=None, *, confirmed=False, force=False):
    """Move the VM onto the profile's hardware. Nothing else about it is touched.

    Refuses while the VM runs and refuses while the drivers are unconfirmed, because both
    produce a guest that does not come back up. `force` exists for the operator who knows
    their guest already had the drivers before it was imported.
    """
    profile = profile or STANDARD_PROFILE
    preview = _preview(migration_id, profile)
    if preview.get('success') is False:
        # The VM could not be read at all -- deleted, renumbered, or the target went away
        # between the preview somebody looked at and this call. Its reason is the answer.
        return preview
    if not confirmed:
        return {'success': False, 'preview': _public(preview),
                'error': 'This changes the VM\'s hardware and needs an explicit '
                         'confirmation. The preview says what would change.'}
    if preview['already_applied']:
        return {'success': True, 'changed': [], 'preview': _public(preview),
                'message': 'This VM is already on the profile.'}
    if preview['requirements'] and not force:
        return {'success': False, 'preview': _public(preview),
                'error': ' '.join(preview['requirements'])}

    migration, target, vmid = _target_and_vmid(migration_id)
    node = migration.get('target_node') or ''
    # Read again rather than trusting the preview: between the preview an operator read and
    # this call, somebody may have started the VM or attached a disk.
    config = _our_vm(migration_id, target, node, vmid)
    if not force and _is_running(target, node, vmid):
        return {'success': False,
                'error': f'VM {vmid} is running now. It is not shut down automatically.'}

    changed, payload, delete = [], {}, []
    for change in preview['_changes']:
        if change['kind'] == 'disk':
            options = f",{change['_options']}" if change['_options'] else ''
            payload[change['to']] = f"{change['_volume']}{options}"
            if change['from'] != change['to']:
                delete.append(change['from'])
        elif change['kind'] == 'nic':
            rest = ','.join(str(change['_value']).split(',')[1:])
            mac = str(change['_value']).split(',')[0]
            mac = mac.split('=')[1] if '=' in mac else ''
            model = f"{profile['nic_model']}={mac}" if mac else profile['nic_model']
            payload[change['to']] = f'{model},{rest}' if rest else model
        else:
            payload[change['from']] = change['_value']
        changed.append(change['detail'])

    if delete:
        # The disk moves to another bus in one call: writing the new key and removing the
        # old one separately would leave the volume attached twice, or not at all.
        payload['delete'] = ','.join(delete)
    moved = {change['from']: change['to'] for change in preview['_changes']
             if change['kind'] == 'disk' and change['from'] != change['to']}
    boot = _rewritten_boot_order(config.get('boot'), moved)
    if boot:
        payload['boot'] = boot
    if profile['controller'] == VIRTIO_CONTROLLER:
        payload.setdefault('scsihw', 'virtio-scsi-single')

    _post_config(target, node, vmid, payload)
    hyperv_db.set_post_import(_conn(), migration_id,
                              profile={'name': profile['name'], 'at': time.time(),
                                       'changed': changed})
    logger.info('Applied profile %s to VM %s (migration %s)',
                profile['name'], vmid, migration_id)
    return {'success': True, 'vmid': vmid, 'profile': profile['name'], 'changed': changed,
            'message': f'VM {vmid} is on the {profile["name"]} profile. Its disks and its '
                       f'data are unchanged; only how they are attached has changed.'}


def _rewritten_boot_order(boot, moved):
    """The VM's own boot order with the moved disks renamed, or nothing to say.

    Not "boot from the first disk we happened to move": a VM with two disks boots from the
    one it booted from, and picking another is how a guest comes up on its data volume.
    Entries that did not move are left in place, and so is their order.
    """
    if not moved:
        return None
    entries = [part.strip() for part in
               str(boot or '').partition('order=')[2].split(';') if part.strip()]
    if not entries:
        # No recorded order. The lowest moved key is the only defensible guess, and it is
        # the one the import itself used.
        return f'order={sorted(moved.values())[0]}'
    return 'order=' + ';'.join(moved.get(entry, entry) for entry in entries)


def _public(preview):
    return {k: v for k, v in preview.items() if not k.startswith('_')}
