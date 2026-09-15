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
import uuid

from flask import Blueprint, jsonify, request

from pegaprox.globals import cluster_managers
from pegaprox.utils.auth import require_auth, build_authz_user
from pegaprox.utils.audit import log_audit
from pegaprox.utils.rbac import user_can_access_vm
from pegaprox.utils.sanitization import sanitize_log_message as _sl
from pegaprox.api.helpers import check_cluster_access, caller_is_scoped
from pegaprox.core import hyperv_inventory, hyperv_preflight
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

# =============================================================================
# The registered hosts
#
# A Hyper-V host is a migration source, not a cluster (docs/adr/0001), so it is created,
# edited and removed here rather than through /api/clusters. The shape follows
# /api/vmware, which does the same job for an ESXi host.
# =============================================================================

def _unsupported_auth(data: dict):
    """A 400 naming the accepted methods when the submitted one is not among them.

    Checked here rather than left to the connection test, because pypsrp would report an
    unknown provider as a client-side ValueError and the operator would read that as the
    host refusing them.
    """
    from pegaprox.core.hyperv_client import SUPPORTED_AUTH_METHODS

    method = data.get('auth')
    if method and method not in SUPPORTED_AUTH_METHODS:
        return jsonify({'error': f'Unsupported WinRM authentication method "{_sl(method)}". '
                                 f'Expected one of: {", ".join(SUPPORTED_AUTH_METHODS)}'}), 400
    return None


@bp.route('/api/hyperv/hosts', methods=['GET'])
@require_auth(perms=['hyperv.view'])
def list_hyperv_hosts():
    """Every registered host, without credentials."""
    from pegaprox.core.db import get_db
    from pegaprox.core import hyperv_db

    db = get_db()
    hosts = []
    for record in hyperv_db.load_hosts(db.conn, db._decrypt):
        manager = cluster_managers.get(record['id'])
        hosts.append({
            'id': record['id'],
            'name': record['name'],
            'host': record['host'],
            'user': record['user'],
            'port': record['port'],
            'use_ssl': record['use_ssl'],
            'auth': record['auth'],
            'encrypt_messages': record['encrypt_messages'],
            'ssl_verification': record['ssl_verification'],
            'iso_library_paths': record['iso_library_paths'],
            'smb_share_map': record['smb_share_map'],
            'smb_domain': record['smb_domain'],
            'transfer_host': record.get('transfer_host', ''),
            # What a target node last measured. The host view renders it, and its absence
            # is why the migration wizard would otherwise ask about disk access per VM.
            'transfer_check': record.get('transfer_check') or {},
            # Never the password, not even its length.
            'has_password': bool(record['pass']),
            'connected': bool(manager and manager.is_connected),
            'connection_error': getattr(manager, 'connection_error', '') if manager else '',
        })
    return jsonify({'hosts': hosts})


@bp.route('/api/hyperv/hosts', methods=['POST'])
@require_auth(perms=['hyperv.config'])
def create_hyperv_host():
    """Register a host, after proving it answers.

    The connection is tested before anything is written. Saving a source that was never
    reachable only moves the failure to a place where it is harder to connect to what the
    operator just typed.
    """
    from pegaprox.core.db import get_db
    from pegaprox.core import hyperv_db
    from pegaprox.core.hyperv_cluster import connect_hyperv_source, register_hyperv_source

    data = request.json or {}
    if not data.get('host'):
        return jsonify({'error': 'A host address is required'}), 400
    auth_error = _unsupported_auth(data)
    if auth_error:
        return auth_error

    host_id = uuid.uuid4().hex[:8]
    manager, hv_error = connect_hyperv_source(host_id, data)
    if hv_error:
        return jsonify({'error': f"Failed to connect: {hv_error['message']}", **hv_error}), 400

    db = get_db()
    hyperv_db.save_host(db.conn, db._encrypt, host_id, data)
    register_hyperv_source(host_id, hyperv_db.load_host(db.conn, db._decrypt, host_id),
                           cluster_managers)
    log_audit(_acting_user(), 'hyperv.host.create',
              f'Registered Hyper-V migration source {_sl(data.get("name") or host_id)}')
    return jsonify({'id': host_id, 'name': data.get('name') or data.get('host')}), 201


@bp.route('/api/hyperv/hosts/<host_id>', methods=['PUT'])
@require_auth(perms=['hyperv.config'])
def update_hyperv_host(host_id):
    """Change a host's settings. An empty password field keeps the stored one."""
    from pegaprox.core.db import get_db
    from pegaprox.core import hyperv_db
    from pegaprox.core.hyperv_cluster import connect_hyperv_source, register_hyperv_source

    db = get_db()
    existing = hyperv_db.load_host(db.conn, db._decrypt, host_id)
    if existing is None:
        return jsonify({'error': 'Hyper-V host not found'}), 404

    data = {**existing, **(request.json or {})}
    # Whether a password was actually typed. The stored one is filled in below so the
    # connection test can run, which makes `pass` truthy either way — and the measured
    # transfer check must only be discarded when the credential really changed. Read here
    # rather than guessed in the database layer, which cannot tell the two apart.
    data['_password_submitted'] = bool((request.json or {}).get('pass'))
    if not (request.json or {}).get('pass'):
        data['pass'] = existing['pass']
    auth_error = _unsupported_auth(data)
    if auth_error:
        return auth_error

    manager, hv_error = connect_hyperv_source(host_id, data)
    if hv_error:
        return jsonify({'error': f"Connection failed: {hv_error['message']}", **hv_error}), 400

    hyperv_db.save_host(db.conn, db._encrypt, host_id, data)
    register_hyperv_source(host_id, hyperv_db.load_host(db.conn, db._decrypt, host_id),
                           cluster_managers)
    # A host reached under new settings may be a different host. What the old settings
    # returned is dropped rather than shown with a fresh timestamp on the next read.
    hyperv_inventory.invalidate(host_id)
    log_audit(_acting_user(), 'hyperv.host.update',
              f'Updated Hyper-V migration source {_sl(data.get("name") or host_id)}')
    return jsonify({'success': True})


@bp.route('/api/hyperv/hosts/<host_id>', methods=['DELETE'])
@require_auth(perms=['hyperv.config'])
def delete_hyperv_host(host_id):
    """Unregister a host.

    Its VMID mapping and migration record stay. Those describe what was done to targets
    that still exist, and unregistering a source undoes none of it.
    """
    from pegaprox.core.db import get_db
    from pegaprox.core import hyperv_db

    db = get_db()
    if hyperv_db.load_host(db.conn, db._decrypt, host_id) is None:
        return jsonify({'error': 'Hyper-V host not found'}), 404

    manager = cluster_managers.pop(host_id, None)
    if manager is not None:
        try:
            manager.stop()
        except Exception:                                    # noqa: BLE001
            logging.warning('Hyper-V source %s did not stop cleanly', host_id)

    hyperv_db.delete_host(db.conn, host_id)
    hyperv_inventory.invalidate(host_id)
    log_audit(_acting_user(), 'hyperv.host.delete',
              f'Removed Hyper-V migration source {_sl(host_id)}')
    return jsonify({'success': True})


@bp.route('/api/hyperv/<cluster_id>/host', methods=['GET'])
@require_auth(perms=['hyperv.view'])
def get_hyperv_host(cluster_id):
    """What the host is, and which of the properties this product reads it actually has.

    The property report is part of the answer rather than a log line. Hyper-V documents
    its cmdlets' parameters but almost never the objects they return, so a property that
    was renamed or is absent on an older host reads as null instead of raising — and a
    VM would then look like it had no generation and no checkpoints. Naming the missing
    properties here is what turns that into something an operator can see.

    Facts and property report come from the same background read as the VM list, for the
    same reason: connecting to a host that does not answer costs a full WinRM timeout,
    and the page that shows this must not spend it before it renders.
    """
    mgr, err = _hyperv_host(cluster_id)
    if err:
        return err

    hyperv_inventory.request_refresh(cluster_id, mgr, force=_wants_forced_refresh())
    entry, freshness = hyperv_inventory.answer(cluster_id)
    body = {
        'id': mgr.id,
        'name': mgr.name,
        'connected': mgr.is_connected,
        'connection_error': mgr.connection_error,
        # The report the last read brought back; falling back to the manager's own covers
        # the window before the first read has finished.
        'properties': entry.get('properties') or mgr.property_report,
        **freshness,
    }
    if entry.get('facts'):
        body['facts'] = entry['facts']
    return jsonify(body)


@bp.route('/api/hyperv/<cluster_id>/transfer-check', methods=['POST'])
@require_auth(perms=['hyperv.config'])
def check_hyperv_transfer(cluster_id):
    """Ask a target node whether it can read this host's disk share, and remember.

    A host fact, measured once: cifs-utils on the node, a route to TCP 445, and an account
    the share lets read. The preflight used to put the same unanswerable question in front
    of every VM and ask for a confirmation it could not inform. Now it reports what this
    found, with the date it found it.

    Read-only throughout — mount, list, unmount. `hyperv.config` rather than a view
    permission because it opens a connection from a Proxmox node to a customer's host
    using the stored credentials, which is a configuration act, not a look.
    """
    from pegaprox.core import hyperv_db, hyperv_transfer_check
    from pegaprox.core.db import get_db
    from pegaprox.core.hyperv_xhm import open_target_node_session

    mgr, err = _hyperv_host(cluster_id)
    if err:
        return err

    data = request.json or {}
    target_cluster = (data.get('target_cluster') or '').strip()
    target_node = (data.get('target_node') or '').strip()
    if not target_cluster or not target_node:
        return jsonify({'error': 'target_cluster and target_node are required — the check '
                                 'measures what one specific node can reach.'}), 400

    ok, denied = check_cluster_access(target_cluster)
    if not ok:
        return denied
    target = cluster_managers.get(target_cluster)
    if target is None or getattr(target, 'cluster_type', 'proxmox') != 'proxmox':
        return jsonify({'error': 'The target has to be a Proxmox cluster.'}), 400

    result = hyperv_transfer_check.run_check(
        mgr, target, target_node,
        lambda: open_target_node_session(target, target_node))

    try:
        hyperv_db.save_transfer_check(get_db().conn, cluster_id, result)
    except Exception as exc:                                     # noqa: BLE001
        logger.warning('Could not store the transfer check for %s: %s', _sl(cluster_id), exc)

    log_audit(_acting_user(), 'hyperv.transfer.check',
              f'Checked SMB access to {_sl(mgr.name)} from node {_sl(target_node)}: '
              f'{"ok" if result.get("ok") else "failed"}')
    return jsonify(result)


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

def _wants_forced_refresh() -> bool:
    """Whether the caller asked for the host to be read again rather than recalled.

    A person pressing the refresh button is a reason to spend a minute of a customer's
    hypervisor; a component re-rendering is not. Only the button sets this.
    """
    return str(request.args.get('refresh', '')).lower() in ('1', 'true', 'yes')


@bp.route('/api/hyperv/<cluster_id>/vms', methods=['GET'])
@require_auth(perms=['hyperv.vm.view'])
def list_hyperv_vms(cluster_id):
    """Every VM on the host the caller is allowed to see, as last read.

    The answer comes from `hyperv_inventory` and therefore comes back immediately, with
    the age of what it contains. Reading a host takes tens of seconds, so doing it inside
    this request meant the view showing the previous host's VMs for that whole time; the
    read now runs in the background and announces itself over SSE (frame
    `hyperv_inventory`), and this route is what the client then asks again.

    The list is filtered per VM rather than gated only at the host, because reaching a
    host through a single VM-ACL entry must not hand back the whole inventory. That
    filter is why the SSE frame carries no VMs: it would have to be repeated there.
    """
    mgr, err = _hyperv_host(cluster_id)
    if err:
        return err

    hyperv_inventory.request_refresh(cluster_id, mgr, force=_wants_forced_refresh())
    # One snapshot for both halves of the answer. The authorization loop below yields
    # under gevent, so reading the list and its age separately can pair the rows from
    # before a background read with the timestamp from after it.
    entry, freshness = hyperv_inventory.answer(cluster_id)
    user = build_authz_user(request.session.get('user', ''), request.session)
    visible = [vm for vm in (entry.get('vms') or [])
               if user_can_access_vm(user, cluster_id, vm['vmid'], 'vm.view', _GUEST_TYPE)]
    return jsonify({'vms': visible, **freshness})


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
        detail = mgr.vm_detail(vmid)
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
        vm = mgr.vm_detail(vmid)
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

    # The controller is not defaulted here. `target_hardware` is the one place that
    # decides what an import creates, and the runner and the planner both ask it; a second
    # default in this route is how the preflight came to warn about VirtIO drivers for a
    # migration that attaches the disk to SATA.
    from pegaprox.core.hyperv_xhm import target_hardware

    options = {
        'network_map': data.get('network_map') or {},
        'vlan_map': data.get('vlan_map') or {},
        'controller': target_hardware(data)['controller'],
        # Whether the migration will write the drivers in. Read from the same function, so
        # the box the operator ticked and the risk the list names cannot disagree.
        'drivers_injected': target_hardware(data)['hardware'] == 'virtio',
        # The file-share transport is not wired into this route. Saying so explicitly is
        # what keeps the result honest: the check becomes a warning somebody has to
        # confirm, rather than an OK that was never earned or a blocker on a VM that is
        # otherwise ready. A runner never sets this and stays blocked without the proof.
        'source_access_probed': False,
        # The host-wide measurement, so this route answers the file-access question the
        # same way the plan does instead of asking for a confirmation beside it.
        'host_transfer_check': getattr(mgr, 'transfer_check', None) or None,
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
    # The cached inventory now says this VM is off. A person who just started it is a
    # reason to spend a read on the host; a re-render is not, which is why this is the
    # forced variant and the list route's is not.
    hyperv_inventory.request_refresh(cluster_id, mgr, force=True)
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
    hyperv_inventory.request_refresh(cluster_id, mgr, force=True)
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


# ---------------------------------------------------------------------------
# After the import: the driver ISO, and the switch to the standard hardware
# ---------------------------------------------------------------------------
#
# These act on the VM the migration created on **Proxmox**, not on the Hyper-V source.
# The `/vms/<vmid>/iso` routes above are the source's CD drive and are a different thing
# entirely; putting the driver ISO through them would mount it in the guest that is still
# running on Hyper-V.
#
# They sit under `/migrations/<id>/` rather than under a target VMID because a VMID is not
# an identity: the migration row is what says which VM on which node this product created,
# and the core verifies the mark in its description before changing anything.

def _post_import_gate(cluster_id, migration_id, permission='vm.migrate'):
    """The access the two post-import buttons need. Returns (migration, error_response)."""
    mgr, err = _hyperv_host(cluster_id)
    if err:
        return None, err

    from pegaprox.core.db import get_db
    from pegaprox.core import hyperv_db

    migration = hyperv_db.get_migration(get_db().conn, migration_id)
    if migration is None or migration.get('source_cluster') != cluster_id:
        return None, (jsonify({'error': 'No such migration on this host'}), 404)

    guid = migration.get('source_vm_guid')
    vmid = mgr.vmid_for(guid) if guid else None
    if vmid is not None:
        denied = _require_vm_access(cluster_id, vmid, permission)
        if denied:
            return None, denied

    # Changing a VM on the target is a target-side action. A Hyper-V permission does not
    # grant it, for the same reason the cleanup asks separately.
    target_cluster = migration.get('target_cluster') or ''
    if target_cluster:
        ok, target_err = check_cluster_access(target_cluster)
        if not ok:
            return None, target_err
        user = build_authz_user(request.session.get('user', ''), request.session)
        if caller_is_scoped(user, target_cluster):
            return None, (jsonify({'error': 'Access denied to the target cluster'}), 403)
    return migration, None


@bp.route('/api/hyperv/<cluster_id>/migrations/<migration_id>/post-import', methods=['GET'])
@require_auth(perms=['hyperv.vm.view'])
def get_hyperv_post_import_state(cluster_id, migration_id):
    """What is true about the imported VM's drivers and hardware right now.

    Reports the attached ISO and the recorded driver confirmation as two separate facts,
    because they are two separate facts: nothing outside a guest can see what is installed
    inside it.
    """
    _migration, err = _post_import_gate(cluster_id, migration_id, 'vm.view')
    if err:
        return err

    from pegaprox.core import hyperv_postimport

    state = hyperv_postimport.describe_driver_state(migration_id)
    if state.get('success') is False:
        return jsonify(state), 409
    state['profile_preview'] = hyperv_postimport.preview_profile(migration_id)
    return jsonify(state)


@bp.route('/api/hyperv/<cluster_id>/migrations/<migration_id>/virtio-iso', methods=['POST'])
@require_auth(perms=['hyperv.vm.migrate'])
def attach_hyperv_virtio_iso(cluster_id, migration_id):
    """Put the VirtIO driver ISO in the imported VM's CD drive. The source is not touched.

    Needs no guest network and no guest agent, and installs nothing: it makes the drivers
    reachable from inside the guest and stops there.
    """
    _migration, err = _post_import_gate(cluster_id, migration_id)
    if err:
        return err

    from pegaprox.core import hyperv_postimport

    data = request.json or {}
    result = hyperv_postimport.attach_virtio_iso(
        migration_id, data.get('volid'), replace=bool(data.get('replace')))
    if not result.get('success'):
        return jsonify(result), 409

    log_audit(_acting_user(), 'hyperv.migration.virtio_iso',
              f'Attached {_sl(str(result.get("iso", "")))} to the VM imported by '
              f'migration {_sl(migration_id)}; nothing was installed in the guest')
    return jsonify(result)


@bp.route('/api/hyperv/<cluster_id>/migrations/<migration_id>/drivers',
          methods=['POST'])
@require_auth(perms=['hyperv.vm.migrate'])
def confirm_hyperv_drivers(cluster_id, migration_id):
    """Record that somebody installed the VirtIO drivers in the guest.

    Deliberately a statement and not a check. The alternative would be to guess from the
    outside — an attached ISO, an elapsed time, a successful boot — and every one of those
    guesses is wrong for some guest, in the direction that leaves it unbootable.
    """
    _migration, err = _post_import_gate(cluster_id, migration_id)
    if err:
        return err

    from pegaprox.core import hyperv_postimport

    data = request.json or {}
    confirmed = data.get('confirmed', True)
    result = hyperv_postimport.confirm_drivers(
        migration_id, _acting_user(), confirmed=bool(confirmed))
    if not result.get('success'):
        return jsonify(result), 409

    log_audit(_acting_user(), 'hyperv.migration.drivers',
              f'{"Confirmed" if confirmed else "Withdrew the confirmation"} that the '
              f'VirtIO drivers are installed in the guest imported by migration '
              f'{_sl(migration_id)}')
    return jsonify(result)


@bp.route('/api/hyperv/<cluster_id>/migrations/<migration_id>/profile', methods=['POST'])
@require_auth(perms=['hyperv.vm.migrate'])
def apply_hyperv_vm_profile(cluster_id, migration_id):
    """Switch the imported VM to the VM standard, after confirmation.

    Without `confirm` this answers with the preview and changes nothing, so the differences
    and the required downtime are on screen before anybody agrees to them. The VM is never
    powered off to make the change, and a guest whose drivers nobody confirmed is refused.
    """
    _migration, err = _post_import_gate(cluster_id, migration_id)
    if err:
        return err

    from pegaprox.core import hyperv_postimport

    data = request.json or {}
    if data.get('confirm') != migration_id:
        preview = hyperv_postimport.preview_profile(migration_id)
        if preview.get('success') is False:
            return jsonify(preview), 409
        return jsonify({
            'error': 'This changes the imported VM\'s hardware and needs the VM powered '
                     'off. Send {"confirm": "<migration_id>"} to perform it.',
            'preview': preview,
        }), 400

    result = hyperv_postimport.apply_profile(
        migration_id, confirmed=True, force=bool(data.get('force')))
    if not result.get('success'):
        log_audit(_acting_user(), 'hyperv.migration.profile.refused',
                  f'Switching the VM imported by migration {_sl(migration_id)} to the VM '
                  f'standard was refused: {_sl(str(result.get("error", "")))[:200]}')
        return jsonify(result), 409

    log_audit(_acting_user(), 'hyperv.migration.profile',
              f'Switched the VM imported by migration {_sl(migration_id)} to the '
              f'{_sl(str(result.get("profile", "")))} profile: '
              f'{len(result.get("changed", []))} setting(s) changed')
    return jsonify(result)
