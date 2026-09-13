# -*- coding: utf-8 -*-
"""HTTP routes for a Hyper-V migration source.

A Hyper-V host is registered like any other cluster and therefore already answers the
generic `/api/clusters/...` routes. What lives here is the part those routes have no
vocabulary for: disk chains, checkpoints, the merge state of a VM that was just edited,
the preflight verdict, and the four actions this product is allowed to perform on a
source it does not manage.

Two rules shape every handler below.

Nothing here does more to the source than preparing it for a migration. The host is a
customer's running hypervisor, not a PegaProx-managed one, so there is no create, no
delete, no hardware change and no reconfiguration — only start, orderly shutdown,
checkpoint removal, and mounting an ISO the operator points at.

Every VM-scoped route asks twice. `check_cluster_access` decides whether the caller may
reach this host at all; `_require_vm_access` decides whether they may touch this
particular VM. The second one is what a tenant- or pool-scoped account runs into, and
leaving it out is the cross-tenant class of bug the route contract test exists to catch.
"""

import logging

from flask import Blueprint, jsonify, request

from pegaprox.globals import cluster_managers
from pegaprox.utils.auth import require_auth, build_authz_user
from pegaprox.utils.audit import log_audit
from pegaprox.utils.rbac import user_can_access_vm
from pegaprox.utils.sanitization import sanitize_log_message as _sl
from pegaprox.api.helpers import check_cluster_access, caller_is_scoped
from pegaprox.core import hyperv_preflight
from pegaprox.core.hyperv_errors import (
    HyperVError, KIND_AUTHENTICATION, KIND_AUTHORIZATION, KIND_CERTIFICATE,
    KIND_CLIENT_DEPENDENCY, KIND_MISSING_FEATURE, KIND_REFUSED, KIND_TIMEOUT,
    KIND_UNREACHABLE,
)

bp = Blueprint('hyperv', __name__)

logger = logging.getLogger(__name__)

CLUSTER_TYPE = 'hyperv'

# How a source-side failure reaches the caller. None of these are the caller's own
# authorization — a 401 or 403 here would tell a browser its session had expired and
# send the operator to the wrong login screen. Everything that happened on the far side
# of the WinRM connection is reported as a gateway failure, and the body carries `kind`
# and `remedy` so the four cases the epic requires stay distinguishable.
_HTTP_STATUS_FOR_KIND = {
    KIND_UNREACHABLE: 502,
    KIND_CERTIFICATE: 502,
    KIND_AUTHENTICATION: 502,
    KIND_AUTHORIZATION: 502,
    KIND_MISSING_FEATURE: 502,
    KIND_TIMEOUT: 504,
    # This side is missing pypsrp, or this side refused to send the script. Both are
    # PegaProx's own state, and neither is fixed on the Hyper-V host.
    KIND_CLIENT_DEPENDENCY: 503,
    KIND_REFUSED: 500,
}
_DEFAULT_ERROR_STATUS = 502

# The default target controller when nothing else is known. Hyper-V's IDE disks map to
# SATA and its SCSI disks to VirtIO SCSI; the preflight only needs to know which driver
# question to ask about.
_DEFAULT_TARGET_CONTROLLER = 'scsi'

# A Hyper-V VM is a full machine, so it is a 'qemu' guest in the vocabulary of the pool
# lookup inside the access-control layer, never an 'lxc' one.
_GUEST_TYPE = 'qemu'


# =============================================================================
# Shared plumbing
# =============================================================================

def _error_response(exc: HyperVError):
    """One classified failure, in the shape the UI renders for every source problem."""
    status = _HTTP_STATUS_FOR_KIND.get(exc.kind, _DEFAULT_ERROR_STATUS)
    body = exc.to_dict()
    body['error'] = exc.message
    return jsonify(body), status


def _hyperv_host(cluster_id):
    """Resolve a cluster id to a Hyper-V manager the caller may reach.

    Returns (manager, None) or (None, error_response). The cluster-type check is not
    cosmetic: without it these routes would call Hyper-V-only methods on a Proxmox
    manager and fail with an AttributeError instead of saying what is wrong.
    """
    ok, err = check_cluster_access(cluster_id)
    if not ok:
        return None, err

    mgr = cluster_managers.get(cluster_id)
    if mgr is None:
        return None, (jsonify({'error': 'Cluster not found'}), 404)
    if getattr(mgr, 'cluster_type', 'proxmox') != CLUSTER_TYPE:
        return None, (jsonify({'error': 'This cluster is not a Hyper-V host'}), 400)
    return mgr, None


def _require_vm_access(cluster_id, vmid, acl_permission):
    """Per-VM gate. Returns None when allowed, else the 403 the caller must return.

    The permission passed here is one of the product's generic `vm.*` verbs, not the
    `hyperv.*` one on the route. Those are two different questions asked of two different
    layers: the route's permission decides whether this account may work with Hyper-V
    sources at all, and this one decides whether it may touch this particular VM. A VM
    access-control entry is written in `vm.*` verbs and grants nothing for a name it does
    not know, so asking it about `hyperv.vm.view` would silently deny every scoped user
    the entry was created for.

    build_authz_user applies the token's effective role, so an admin-owned but scoped API
    token does not inherit the admin bypass inside user_can_access_vm.
    """
    user = build_authz_user(request.session.get('user', ''), request.session)
    if not user_can_access_vm(user, cluster_id, vmid, acl_permission, _GUEST_TYPE):
        return jsonify({'error': f'Access denied to this VM ({acl_permission})'}), 403
    return None


def _guid_for(mgr, vmid):
    """The Hyper-V GUID behind a synthetic VMID, or (None, 404) if it is not this host's.

    Resolving through the durable map rather than trusting a GUID from the request is
    what keeps a VMID-based ACL meaningful: a caller cannot name a VM the ACL was never
    written for by sending its GUID directly.
    """
    guid = mgr.guid_for(vmid)
    if not guid:
        return None, (jsonify({'error': f'No Hyper-V VM is known here as {vmid}'}), 404)
    return guid, None


def _acting_user():
    return request.session.get('user', 'unknown')


# =============================================================================
# Host
# =============================================================================

@bp.route('/api/hyperv/<cluster_id>/host', methods=['GET'])
@require_auth(perms=['hyperv.view'])
def get_hyperv_host(cluster_id):
    """What the host is, and which of the properties this product reads it actually has.

    The property report is part of the answer rather than a log line. Hyper-V documents
    its cmdlets' parameters but almost never the objects they return, so a property that
    was renamed or is absent on an older host reads as null instead of raising — and a
    VM would then look like it had no generation and no checkpoints. Naming the missing
    properties here is what turns that into something an operator can see.
    """
    mgr, err = _hyperv_host(cluster_id)
    if err:
        return err

    connected = mgr.is_connected or mgr.connect()
    body = {
        'id': mgr.id,
        'name': mgr.name,
        'connected': connected,
        'connection_error': mgr.connection_error,
        'properties': mgr.property_report,
    }
    if not connected:
        return jsonify(body), 200

    try:
        body['facts'] = mgr.manager.host_facts()
    except HyperVError as exc:
        return _error_response(exc)
    return jsonify(body)


@bp.route('/api/hyperv/<cluster_id>/isos', methods=['GET'])
@require_auth(perms=['hyperv.vm.media'])
def list_hyperv_isos(cluster_id):
    """The ISOs this host can offer a VM, from the library paths its config names.

    PegaProx does not upload an ISO to a customer's Hyper-V host and does not browse its
    filesystem. It reads the paths an administrator configured for this source and
    nothing else.
    """
    mgr, err = _hyperv_host(cluster_id)
    if err:
        return err

    paths = mgr.config.iso_library_paths
    if not paths:
        return jsonify({
            'isos': [],
            'message': 'No ISO library path is configured for this Hyper-V host.',
        })

    try:
        return jsonify({'isos': mgr.manager.list_isos(paths)})
    except HyperVError as exc:
        return _error_response(exc)


# =============================================================================
# VM reads
# =============================================================================

@bp.route('/api/hyperv/<cluster_id>/vms', methods=['GET'])
@require_auth(perms=['hyperv.vm.view'])
def list_hyperv_vms(cluster_id):
    """Every VM on the host the caller is allowed to see.

    The list is filtered per VM rather than gated only at the host, because reaching a
    host through a single VM-ACL entry must not hand back the whole inventory.
    """
    mgr, err = _hyperv_host(cluster_id)
    if err:
        return err

    try:
        vms = mgr.get_vms()
    except HyperVError as exc:
        return _error_response(exc)

    user = build_authz_user(request.session.get('user', ''), request.session)
    visible = [vm for vm in vms
               if user_can_access_vm(user, cluster_id, vm['vmid'], 'vm.view', _GUEST_TYPE)]
    return jsonify({'vms': visible})


@bp.route('/api/hyperv/<cluster_id>/vms/<int:vmid>', methods=['GET'])
@require_auth(perms=['hyperv.vm.view'])
def get_hyperv_vm(cluster_id, vmid):
    """One VM's migration-relevant hardware, normalised.

    Everything this returns is what somebody needs to decide whether the VM can move:
    generation and firmware, CPU and the three memory figures, disks with their
    controller positions, adapters with their MACs and switches, and the checkpoints.
    A value the host did not report stays null — never a zero, never an empty list that
    would read as "none".
    """
    mgr, err = _hyperv_host(cluster_id)
    if err:
        return err
    denied = _require_vm_access(cluster_id, vmid, 'vm.view')
    if denied:
        return denied

    try:
        detail = mgr.get_vm_config(vmid)
    except HyperVError as exc:
        return _error_response(exc)
    if 'error' in detail:
        return jsonify(detail), 404
    return jsonify(detail)


@bp.route('/api/hyperv/<cluster_id>/vms/<int:vmid>/disks', methods=['GET'])
@require_auth(perms=['hyperv.vm.view'])
def get_hyperv_vm_disks(cluster_id, vmid):
    """The disk chain behind each of a VM's disks.

    A differencing chain is the reason a VHDX that looks complete is not: the file the VM
    attaches may hold only the changes since a parent it points at by path. The chain is
    read so preflight can refuse it rather than copying one link and calling it a disk.
    """
    mgr, err = _hyperv_host(cluster_id)
    if err:
        return err
    denied = _require_vm_access(cluster_id, vmid, 'vm.view')
    if denied:
        return denied

    guid, err = _guid_for(mgr, vmid)
    if err:
        return err

    try:
        return jsonify({'vmid': vmid, 'chains': mgr.manager.get_disk_chains(guid)})
    except HyperVError as exc:
        return _error_response(exc)


@bp.route('/api/hyperv/<cluster_id>/vms/<int:vmid>/state', methods=['GET'])
@require_auth(perms=['hyperv.vm.view'])
def get_hyperv_vm_state(cluster_id, vmid):
    """The VM's power state, and whether its disks are settled enough to be read.

    Deleting a checkpoint returns immediately and the merge runs on afterwards in the
    background, so a VM that reports "Off" with no checkpoints can still be rewriting its
    own disk files. This asks the host's own WMI view for that, which is the only place
    it is visible, and `safe_to_read` is the answer a migration is allowed to act on.
    """
    mgr, err = _hyperv_host(cluster_id)
    if err:
        return err
    denied = _require_vm_access(cluster_id, vmid, 'vm.view')
    if denied:
        return denied

    guid, err = _guid_for(mgr, vmid)
    if err:
        return err

    try:
        state = mgr.manager.get_vm_state(guid)
        merge = mgr.manager.get_merge_state(guid)
        safe, reason = mgr.manager.disks_are_safe_to_read(guid)
    except HyperVError as exc:
        return _error_response(exc)

    return jsonify({
        'vmid': vmid,
        'state': state,
        'merge_state': merge,
        'safe_to_read': safe,
        'reason': reason,
    })


# =============================================================================
# Preflight
# =============================================================================

def _target_available_bytes(target_mgr, node, storage):
    """Free bytes on the chosen Proxmox storage, or None when it cannot be read.

    None is a real answer here and the capacity check treats it as blocking. Starting a
    copy without knowing whether it fits risks filling a storage that other guests are
    already running on, and that damage lands on VMs nobody was migrating.
    """
    if not (target_mgr and node and storage):
        return None
    try:
        response = target_mgr._api_get(
            f'https://{target_mgr.host}:{target_mgr.api_port}'
            f'/api2/json/nodes/{node}/storage/{storage}/status')
        if response.status_code != 200:
            return None
        return response.json().get('data', {}).get('avail')
    except Exception:
        logger.debug('Could not read free space on target storage %s', _sl(str(storage)),
                     exc_info=True)
        return None


@bp.route('/api/hyperv/<cluster_id>/vms/<int:vmid>/preflight', methods=['POST'])
@require_auth(perms=['hyperv.vm.view'])
def hyperv_vm_preflight(cluster_id, vmid):
    """Every migration check for one VM against one chosen target.

    It is a POST because the verdict depends on choices the caller makes — which target,
    which storage, which network each adapter lands on — not because it changes anything.
    Nothing on either side is modified.

    All checks run; none short-circuits. Somebody preparing a VM wants the whole list of
    what is wrong, not one item per attempt across four rounds.
    """
    mgr, err = _hyperv_host(cluster_id)
    if err:
        return err
    denied = _require_vm_access(cluster_id, vmid, 'vm.view')
    if denied:
        return denied

    data = request.json or {}

    try:
        vm = mgr.get_vm_config(vmid)
    except HyperVError as exc:
        return _error_response(exc)
    if 'error' in vm:
        return jsonify(vm), 404

    target_cluster = data.get('target_cluster')
    target_mgr = cluster_managers.get(target_cluster) if target_cluster else None
    if target_cluster:
        if target_mgr is None:
            return jsonify({'error': 'Target cluster not found'}), 404
        if getattr(target_mgr, 'cluster_type', 'proxmox') != 'proxmox':
            return jsonify({'error': 'A Hyper-V source can only migrate to Proxmox'}), 400
        ok, target_err = check_cluster_access(target_cluster)
        if not ok:
            return target_err

    target = {'available_bytes': _target_available_bytes(
        target_mgr, data.get('target_node'), data.get('target_storage'))}

    options = {
        'network_map': data.get('network_map') or {},
        'controller': data.get('controller') or _DEFAULT_TARGET_CONTROLLER,
        # The file-share transport is not wired into this route. Saying so explicitly is
        # what keeps the result honest: the check becomes a warning somebody has to
        # confirm, rather than an OK that was never earned or a blocker on a VM that is
        # otherwise ready. A runner never sets this and stays blocked without the proof.
        'source_access_probed': False,
        'reachable_paths': {},
    }

    report = hyperv_preflight.run_preflight(vm, target, options)
    body = report.to_dict()
    body['vmid'] = vmid
    body['direction'] = 'hyperv_to_pve'
    return jsonify(body)


# =============================================================================
# Actions on the source
# =============================================================================

@bp.route('/api/hyperv/<cluster_id>/vms/<int:vmid>/start', methods=['POST'])
@require_auth(perms=['hyperv.vm.power'])
def start_hyperv_vm(cluster_id, vmid):
    """Start the VM, so a guest can be prepared before it is migrated."""
    mgr, err = _hyperv_host(cluster_id)
    if err:
        return err
    denied = _require_vm_access(cluster_id, vmid, 'vm.start')
    if denied:
        return denied

    guid, err = _guid_for(mgr, vmid)
    if err:
        return err

    # After a migration the original and the copy are one machine twice, down to the MAC.
    # Starting this one while the copy runs is the mistake this refuses to help with.
    from pegaprox.core.hyperv_xhm import refuse_source_start
    refused = refuse_source_start(cluster_id, vmid)
    if refused:
        return jsonify({'error': refused}), 409

    try:
        result = mgr.manager.start_vm(guid)
    except HyperVError as exc:
        return _error_response(exc)

    log_audit(_acting_user(), 'hyperv.vm.start',
              f'Started Hyper-V VM {vmid} on host {_sl(mgr.name)}')
    return jsonify(result)


@bp.route('/api/hyperv/<cluster_id>/vms/<int:vmid>/shutdown', methods=['POST'])
@require_auth(perms=['hyperv.vm.power'])
def shutdown_hyperv_vm(cluster_id, vmid):
    """Ask the guest to shut itself down, and wait for it to finish.

    Only the orderly shutdown exists here. Hyper-V can also cut the power, and that would
    leave the disks in the state an unexpected outage leaves them in — the one state a
    migration must not start from. A guest that refuses is reported as refusing; it is
    not overruled.
    """
    mgr, err = _hyperv_host(cluster_id)
    if err:
        return err
    denied = _require_vm_access(cluster_id, vmid, 'vm.stop')
    if denied:
        return denied

    guid, err = _guid_for(mgr, vmid)
    if err:
        return err

    data = request.json or {}
    try:
        timeout = int(data.get('timeout_seconds', 300))
    except (TypeError, ValueError):
        return jsonify({'error': 'timeout_seconds must be a number'}), 400
    if timeout <= 0:
        return jsonify({'error': 'timeout_seconds must be positive'}), 400

    try:
        result = mgr.manager.shutdown_vm(guid, timeout_seconds=timeout)
    except HyperVError as exc:
        return _error_response(exc)

    log_audit(_acting_user(), 'hyperv.vm.shutdown',
              f'Requested guest shutdown of Hyper-V VM {vmid} on host {_sl(mgr.name)}')
    return jsonify(result)


@bp.route('/api/hyperv/<cluster_id>/vms/<int:vmid>/checkpoints', methods=['DELETE'])
@require_auth(perms=['hyperv.vm.checkpoint'])
def remove_hyperv_checkpoints(cluster_id, vmid):
    """Delete checkpoints so the VM's disks become a single readable file per disk.

    This is destructive on the customer's source: the states those checkpoints could be
    rolled back to are gone afterwards, and Hyper-V offers no undo. The route therefore
    requires its own permission and writes an audit entry naming what was removed.

    It returns as soon as Hyper-V accepts the request. The merge that follows runs in the
    background on the host, and whether it has finished is what the state route reports.
    """
    mgr, err = _hyperv_host(cluster_id)
    if err:
        return err
    denied = _require_vm_access(cluster_id, vmid, 'vm.snapshot')
    if denied:
        return denied

    guid, err = _guid_for(mgr, vmid)
    if err:
        return err

    data = request.json or {}
    name = data.get('checkpoint_name')
    if name is not None and not str(name).strip():
        return jsonify({'error': 'checkpoint_name must not be empty'}), 400

    try:
        result = mgr.manager.remove_checkpoints(guid, checkpoint_name=name)
    except HyperVError as exc:
        return _error_response(exc)

    scope = f'checkpoint {_sl(str(name))}' if name else 'all checkpoints'
    log_audit(_acting_user(), 'hyperv.vm.checkpoint.remove',
              f'Removed {scope} of Hyper-V VM {vmid} on host {_sl(mgr.name)}')
    return jsonify(result)


@bp.route('/api/hyperv/<cluster_id>/vms/<int:vmid>/iso', methods=['POST'])
@require_auth(perms=['hyperv.vm.media'])
def mount_hyperv_iso(cluster_id, vmid):
    """Attach an ISO from the host's configured library to the VM's DVD drive.

    The path has to be one the library paths already contain. Taking an arbitrary path
    from the request would let a caller mount any file the service account can reach on
    the customer's host, which is a filesystem read primitive, not a media action.
    """
    mgr, err = _hyperv_host(cluster_id)
    if err:
        return err
    denied = _require_vm_access(cluster_id, vmid, 'vm.config')
    if denied:
        return denied

    guid, err = _guid_for(mgr, vmid)
    if err:
        return err

    iso_path = (request.json or {}).get('path')
    if not iso_path:
        return jsonify({'error': 'path is required'}), 400

    paths = mgr.config.iso_library_paths
    if not paths:
        return jsonify({'error': 'No ISO library path is configured for this Hyper-V host'}), 400

    try:
        available = {iso.get('path') for iso in mgr.manager.list_isos(paths)}
    except HyperVError as exc:
        return _error_response(exc)
    if iso_path not in available:
        return jsonify({'error': 'That ISO is not in this host\'s configured library'}), 400

    try:
        result = mgr.manager.mount_iso(guid, iso_path)
    except HyperVError as exc:
        return _error_response(exc)

    log_audit(_acting_user(), 'hyperv.vm.iso.mount',
              f'Mounted an ISO on Hyper-V VM {vmid} on host {_sl(mgr.name)}')
    return jsonify(result)


@bp.route('/api/hyperv/<cluster_id>/vms/<int:vmid>/iso', methods=['DELETE'])
@require_auth(perms=['hyperv.vm.media'])
def eject_hyperv_iso(cluster_id, vmid):
    """Empty the VM's DVD drive again."""
    mgr, err = _hyperv_host(cluster_id)
    if err:
        return err
    denied = _require_vm_access(cluster_id, vmid, 'vm.config')
    if denied:
        return denied

    guid, err = _guid_for(mgr, vmid)
    if err:
        return err

    try:
        result = mgr.manager.eject_iso(guid)
    except HyperVError as exc:
        return _error_response(exc)

    log_audit(_acting_user(), 'hyperv.vm.iso.eject',
              f'Ejected the ISO from Hyper-V VM {vmid} on host {_sl(mgr.name)}')
    return jsonify(result)


# =============================================================================
# The VM's own console
# =============================================================================

@bp.route('/api/hyperv/<cluster_id>/vms/<int:vmid>/console', methods=['POST'])
@require_auth(perms=['hyperv.vm.console'])
def open_hyperv_console(cluster_id, vmid):
    """Mint a short-lived ticket for this VM's VMConnect console.

    What comes back is a token and an address. Not the host, not the account, not the VM's
    GUID: the browser is given only something that expires, is spent by the first
    connection that uses it, and means nothing on its own. Everything the console actually
    needs is read on the server when the websocket arrives.

    The ticket says nothing about whether the console will open. Whether guacd is running
    and whether the Hyper-V host answers on its Virtual Machine Connection port are
    questions the relay asks, and their answers reach the browser through the console
    itself rather than through this route — a ticket that succeeded and a console that
    failed are different facts, and reporting them together would hide one of them.
    """
    mgr, err = _hyperv_host(cluster_id)
    if err:
        return err
    denied = _require_vm_access(cluster_id, vmid, 'vm.console')
    if denied:
        return denied

    guid, err = _guid_for(mgr, vmid)
    if err:
        return err

    from pegaprox.core import hyperv_console
    from pegaprox.utils.realtime import create_ws_token, WS_TOKEN_TTL

    user = _acting_user()
    token = create_ws_token(user, request.session.get('role', ''))

    log_audit(user, 'hyperv.vm.console.open',
              f'Opened the console of Hyper-V VM {vmid} on host {_sl(mgr.name)}')
    return jsonify({
        'path': f'/api/hyperv/{cluster_id}/vms/{vmid}/console',
        'token': token,
        'expires_in': WS_TOKEN_TTL,
        'protocol': 'guacamole',
        # So the UI can say why nothing is there before it opens a black rectangle.
        'guacd': {'configured': True, 'host_env': hyperv_console.GUACD_HOST_ENV,
                  'port_env': hyperv_console.GUACD_PORT_ENV},
    })


# =============================================================================
# What a migration left behind
# =============================================================================

@bp.route('/api/hyperv/<cluster_id>/migrations', methods=['GET'])
@require_auth(perms=['hyperv.vm.view'])
def list_hyperv_migrations(cluster_id):
    """Every migration started from this host, from the database rather than from memory.

    The shared migration list is process-local, so a restart empties it while the half-built
    target it describes is still there. This one answers from the durable record and says,
    per migration, what it created and whether that is still lying around.
    """
    mgr, err = _hyperv_host(cluster_id)
    if err:
        return err

    from pegaprox.core.db import get_db
    from pegaprox.core import hyperv_db

    rows = hyperv_db.migrations_for_cluster(get_db().conn, cluster_id)
    migrations = []
    for row in rows:
        vmid = mgr.vmid_for(row['source_vm_guid']) if row.get('source_vm_guid') else None
        if vmid is not None and _require_vm_access(cluster_id, vmid, 'vm.view'):
            # A scoped operator sees the migrations of the VMs they may see, and is not
            # told that the others exist.
            continue
        leftovers = row.get('created_resources') or []
        migrations.append({
            'migration_id': row['migration_id'],
            'source_vmid': vmid,
            'source_vm_name': row.get('source_vm_name') or '',
            'target_cluster': row.get('target_cluster') or '',
            'target_node': row.get('target_node') or '',
            'target_vmid': row.get('target_vmid'),
            'phase': row.get('phase'),
            'status': row.get('status'),
            'progress': row.get('progress'),
            'error': row.get('error') or '',
            'started_at': row.get('started_at'),
            'completed_at': row.get('completed_at'),
            'disk_progress': row.get('disk_progress') or {},
            'leftovers': leftovers,
            # Whether starting this VM again would create a second copy of what is already
            # there. The UI needs this to know whether to offer a retry or a cleanup.
            'blocks_retry': bool(leftovers) and row.get('status') in ('failed', 'interrupted'),
        })
    return jsonify({'migrations': migrations})


@bp.route('/api/hyperv/<cluster_id>/migrations/<migration_id>/cleanup', methods=['POST'])
@require_auth(perms=['hyperv.vm.migrate'])
def cleanup_hyperv_migration(cluster_id, migration_id):
    """Remove what one failed migration created on the target, after confirmation.

    Three gates, and each of them has a failure it prevents. The confirmation has to name
    this migration, so a click on the wrong row cannot delete a VM. The caller must reach
    the target cluster in their own right, because deleting a guest there is a target-side
    action that a Hyper-V permission has no business granting. And the runner must be gone,
    which the core checks again against the claim table.

    The Hyper-V source is never touched by any of this.
    """
    mgr, err = _hyperv_host(cluster_id)
    if err:
        return err

    from pegaprox.core.db import get_db
    from pegaprox.core import hyperv_db
    from pegaprox.core.hyperv_xhm import cleanup_migration

    migration = hyperv_db.get_migration(get_db().conn, migration_id)
    if migration is None or migration.get('source_cluster') != cluster_id:
        return jsonify({'error': 'No such migration on this host'}), 404

    vmid = mgr.vmid_for(migration['source_vm_guid']) if migration.get('source_vm_guid') else None
    if vmid is not None:
        denied = _require_vm_access(cluster_id, vmid, 'vm.migrate')
        if denied:
            return denied

    target_cluster = migration.get('target_cluster') or ''
    if target_cluster:
        ok, target_err = check_cluster_access(target_cluster)
        if not ok:
            return target_err
        user = build_authz_user(request.session.get('user', ''), request.session)
        if caller_is_scoped(user, target_cluster):
            return jsonify({'error': 'Access denied to the target cluster'}), 403

    data = request.json or {}
    if data.get('confirm') != migration_id:
        return jsonify({
            'error': 'Cleanup deletes a VM and its disks on the target and cannot be '
                     'undone. Send {"confirm": "<migration_id>"} to perform it.',
            'leftovers': migration.get('created_resources') or [],
        }), 400

    result = cleanup_migration(migration_id, confirmed=True)
    if not result.get('success'):
        log_audit(_acting_user(), 'hyperv.migration.cleanup.refused',
                  f'Cleanup of Hyper-V migration {_sl(migration_id)} was refused: '
                  f'{_sl(str(result.get("error", "")))[:200]}')
        return jsonify(result), 409

    log_audit(_acting_user(), 'hyperv.migration.cleanup',
              f'Removed {len(result.get("removed", []))} target resource(s) left by '
              f'Hyper-V migration {_sl(migration_id)}; the source was not touched')
    return jsonify(result)
