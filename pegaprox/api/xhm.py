# -*- coding: utf-8 -*-
"""Cross-Hypervisor Migration API - LW Mar 2026
Endpoints for Proxmox <-> XCP-ng <-> ESXi migration.
"""

import logging
import threading
import uuid
from datetime import datetime, timedelta
from flask import Blueprint, jsonify, request

from pegaprox.globals import cluster_managers, _xhm_migrations
from pegaprox.utils.auth import require_auth, load_users, build_authz_user
from pegaprox.utils.audit import log_audit
from pegaprox.utils.rbac import user_can_access_vm
from pegaprox.api.helpers import check_cluster_access, caller_is_scoped
from pegaprox.core.xhm import (
    XHMigrationTask, plan_xcpng_to_pve, plan_pve_to_xcpng,
    _run_xcpng_to_pve, _run_pve_to_xcpng,
    plan_esxi_to_pve, plan_esxi_to_xcpng,
    _run_esxi_to_pve, _run_esxi_to_xcpng,
)

bp = Blueprint('xhm', __name__)

_xhm_lock = threading.Lock()

# How long a finished migration stays in the in-memory registry, and how many we keep at all.
# The UI reads the list to show recent results, so this is a retention window, not a cleanup.
_XHM_RETENTION_SECONDS = 6 * 3600
_XHM_MAX_FINISHED = 100


def _prune_finished_migrations():
    """Drop old finished migrations. Call with _xhm_lock held.

    Nothing ever removed from this dict, so every migration a server had ever run stayed
    resident for the process lifetime — each one holding its full log — and the list endpoint
    re-authorized all of them on every call. Running tasks are never touched."""
    finished = [(t.completed_at, mid) for mid, t in _xhm_migrations.items()
                if t.status in ('completed', 'failed') and t.completed_at]
    if not finished:
        return
    cutoff = datetime.now() - timedelta(seconds=_XHM_RETENTION_SECONDS)
    stale = {mid for ts, mid in finished if ts < cutoff}
    # plus the oldest beyond the cap, so a burst of migrations can't outrun the window
    if len(finished) - len(stale) > _XHM_MAX_FINISHED:
        keep = sorted((f for f in finished if f[1] not in stale), reverse=True)
        stale.update(mid for _, mid in keep[_XHM_MAX_FINISHED:])
    for mid in stale:
        _xhm_migrations.pop(mid, None)


@bp.route('/api/xhm/plan', methods=['GET'])
@require_auth(perms=['vm.migrate'])
def xhm_plan():
    """Get migration plan - analyzes source VM and lists available targets."""
    source_cluster = request.args.get('source_cluster', '')
    source_vmid = request.args.get('source_vmid', '')
    target_cluster = request.args.get('target_cluster', '')
    direction = request.args.get('direction', '')

    if not source_cluster or not source_vmid or not target_cluster:
        return jsonify({'error': 'source_cluster, source_vmid, and target_cluster are required'}), 400

    # Authorization: check source cluster access
    ok, err = check_cluster_access(source_cluster)
    if not ok:
        return err

    # Authorization: check target cluster access
    ok, err = check_cluster_access(target_cluster)
    if not ok:
        return err

    # Authorization: check source VM access
    user = build_authz_user(request.session['user'], request.session)
    try:
        vmid_int = int(source_vmid)
    except (ValueError, TypeError):
        return jsonify({'error': 'Invalid source_vmid'}), 400
    
    if not user_can_access_vm(user, source_cluster, vmid_int, 'vm.migrate'):
        return jsonify({'error': 'Access denied to source VM'}), 403
    # sec (audit): the target got check_cluster_access only — which admits a pool-/ACL-scoped
    # caller — yet this creates a BRAND-NEW guest there on a caller-chosen node and storage.
    # A new vmid matches no per-object grant, so confinement is the right question to ask.
    if caller_is_scoped(user, target_cluster):
        return jsonify({'error': 'Access denied to target cluster'}), 403

    # auto-detect direction from cluster types
    src_mgr = cluster_managers.get(source_cluster)
    tgt_mgr = cluster_managers.get(target_cluster)
    if not src_mgr:
        return jsonify({'error': 'Source cluster not found'}), 404
    if not tgt_mgr:
        return jsonify({'error': 'Target cluster not found'}), 404

    src_type = getattr(src_mgr, 'cluster_type', 'proxmox')
    tgt_type = getattr(tgt_mgr, 'cluster_type', 'proxmox')

    if src_type == tgt_type:
        return jsonify({'error': f'Both clusters are {src_type} - use native migration instead'}), 400

    # route to correct plan function based on cluster types
    if src_type == 'esxi' and tgt_type == 'proxmox':
        result = plan_esxi_to_pve(source_cluster, source_vmid, target_cluster)
    elif src_type == 'esxi' and tgt_type == 'xcpng':
        result = plan_esxi_to_xcpng(source_cluster, source_vmid, target_cluster)
    elif src_type == 'hyperv':
        from pegaprox.core.hyperv_xhm import plan_hyperv_to_pve
        result = plan_hyperv_to_pve(source_cluster, source_vmid, target_cluster)
    elif src_type == 'xcpng':
        result = plan_xcpng_to_pve(source_cluster, source_vmid, target_cluster)
    elif tgt_type == 'xcpng':
        source_node = request.args.get('source_node', '')
        if not source_node:
            return jsonify({'error': 'source_node required for Proxmox source'}), 400
        result = plan_pve_to_xcpng(source_cluster, source_node, source_vmid, target_cluster)
    else:
        return jsonify({'error': f'Unsupported migration: {src_type} -> {tgt_type}'}), 400

    if 'error' in result:
        return jsonify(result), 400
    return jsonify(result)


@bp.route('/api/xhm/migrate', methods=['POST'])
@require_auth(perms=['vm.migrate'])
def xhm_start():
    """Start cross-hypervisor migration."""
    data = request.json or {}
    required = ['source_cluster', 'source_vmid', 'target_cluster', 'target_storage']
    for f in required:
        if not data.get(f):
            return jsonify({'error': f'{f} is required'}), 400

    # MK May 2026 (#481 port) — target_storage is embedded in pvesm alloc cmds
    # in core/xhm.py. Validate at api boundary.
    from pegaprox.utils.sanitization import validate_storage_name
    if not validate_storage_name(data['target_storage']):
        return jsonify({'error': 'Invalid target_storage name. Must be alphanumeric with hyphens, underscores, or dots only.'}), 400

    # Authorization: check source cluster access
    ok, err = check_cluster_access(data['source_cluster'])
    if not ok:
        return err

    # Authorization: check target cluster access
    ok, err = check_cluster_access(data['target_cluster'])
    if not ok:
        return err

    # Authorization: check source VM access
    user = build_authz_user(request.session['user'], request.session)
    try:
        vmid_int = int(data['source_vmid'])
    except (ValueError, TypeError):
        return jsonify({'error': 'Invalid source_vmid'}), 400
    
    if not user_can_access_vm(user, data['source_cluster'], vmid_int, 'vm.migrate'):
        return jsonify({'error': 'Access denied to source VM'}), 403
    if caller_is_scoped(user, data['target_cluster']):
        return jsonify({'error': 'Access denied to target cluster'}), 403

    src_mgr = cluster_managers.get(data['source_cluster'])
    tgt_mgr = cluster_managers.get(data['target_cluster'])
    if not src_mgr:
        return jsonify({'error': 'Source cluster not found'}), 404
    if not tgt_mgr:
        return jsonify({'error': 'Target cluster not found'}), 404

    src_type = getattr(src_mgr, 'cluster_type', 'proxmox')
    tgt_type = getattr(tgt_mgr, 'cluster_type', 'proxmox')

    if src_type == 'esxi' and tgt_type == 'proxmox':
        direction = 'esxi_to_pve'
    elif src_type == 'esxi' and tgt_type == 'xcpng':
        direction = 'esxi_to_xcpng'
    elif src_type == 'hyperv' and tgt_type == 'proxmox':
        direction = 'hyperv_to_pve'
    elif src_type == 'xcpng' and tgt_type != 'xcpng':
        direction = 'xcpng_to_pve'
    elif src_type != 'xcpng' and tgt_type == 'xcpng':
        direction = 'pve_to_xcpng'
    else:
        return jsonify({'error': 'Invalid cluster combination for cross-hypervisor migration'}), 400

    if direction in ('xcpng_to_pve', 'esxi_to_pve', 'hyperv_to_pve') and not data.get('target_node'):
        return jsonify({'error': 'target_node is required for migration to Proxmox'}), 400

    # CMO fork patch #15: one import at a time per Hyper-V source, and none at all while
    # a failed one's leftovers are still on the target.
    if direction == 'hyperv_to_pve':
        from pegaprox.core.hyperv_xhm import refuse_hyperv_start
        refused = refuse_hyperv_start(data['source_cluster'], vmid_int, data)
        if refused:
            return jsonify({'error': refused}), 409
        # Named in the imported VM's description. Set here from the session, never taken
        # from the request body, which anybody can fill in with any name.
        data['started_by'] = request.session['user']

    mid = str(uuid.uuid4())[:8]
    task = XHMigrationTask(
        mid=mid,
        direction=direction,
        source_cluster=data['source_cluster'],
        source_node=data.get('source_node', ''),
        source_vmid=data['source_vmid'],
        target_cluster=data['target_cluster'],
        target_node=data['target_node'],
        target_storage=data['target_storage'],
        vm_name=data.get('vm_name', ''),
        config=data,
    )

    with _xhm_lock:
        _prune_finished_migrations()
        _xhm_migrations[mid] = task

    _runners = {
        'xcpng_to_pve': _run_xcpng_to_pve,
        'pve_to_xcpng': _run_pve_to_xcpng,
        'esxi_to_pve': _run_esxi_to_pve,
        'esxi_to_xcpng': _run_esxi_to_xcpng,
    }
    if direction == 'hyperv_to_pve':
        from pegaprox.core.hyperv_xhm import _run_hyperv_to_pve
        _runners['hyperv_to_pve'] = _run_hyperv_to_pve
    runner = _runners.get(direction)
    if not runner:
        return jsonify({'error': f'No runner for direction {direction}'}), 400
    t = threading.Thread(target=runner, args=(task,), daemon=True)
    t.start()

    user = request.session.get('user', 'admin') if hasattr(request, 'session') else 'admin'
    log_audit(user, 'xhm.migration.started',
              f"XHM {direction}: {data.get('vm_name', data['source_vmid'])} -> "
              f"{data['target_cluster']}/{data['target_node']}")

    return jsonify({
        'migration_id': mid,
        'message': f'Migration started ({direction})',
        'task': task.to_dict(),
    }), 202


def _xhm_reachable(t):
    # NS Jul 2026 (CodeAnt IDOR) — show a migration only if the caller reaches one of its clusters.
    from pegaprox.api.helpers import check_cluster_access
    cids = [c for c in (getattr(t, 'target_cluster', None), getattr(t, 'source_cluster', None)) if c]
    if cids and not any(check_cluster_access(c)[0] for c in cids):
        return False
    # NS Aug 2026 (Aikido #469089253) — and only if the caller can access the source VM itself,
    # matching the plan/start gate; cluster reach alone leaked other VMs' migration records.
    svmid, scluster = getattr(t, 'source_vmid', None), getattr(t, 'source_cluster', None)
    if svmid and scluster:
        try:
            _u = build_authz_user(request.session.get('user', ''), request.session)
            return user_can_access_vm(_u, scluster, int(svmid), 'vm.migrate')
        except Exception:
            return False
    return True


# NS Aug 2026 (#654) — same slip as the vmware list route: decorators were on _xhm_reachable
# instead of this handler, so GET /api/xhm/migrations 500'd with a missing-arg TypeError.
@bp.route('/api/xhm/migrations', methods=['GET'])
@require_auth(perms=['vm.migrate'])
def xhm_list():
    live = [t.to_dict() for t in _xhm_migrations.values() if _xhm_reachable(t)]
    # Fork patch #15 — plus the Hyper-V migrations only the database still knows.
    return jsonify(live + _recorded_hyperv_migrations({m.get('id') for m in live}))


@bp.route('/api/xhm/migrations', methods=['DELETE'])
@require_auth(perms=['vm.migrate'])
def xhm_dismiss_finished():
    """Take every finished migration off the list. Running ones are left alone.

    The list is where an operator reads what happened, and the log of the one they picked
    is rendered under it. An entry they have already read and acted on is in the way of
    the next one, and nothing in the product could put it away — it left on a timer, six
    hours later.

    This removes a record of a run that is over. It removes nothing on either hypervisor:
    no VM, no disk, no volume. What a failed run left behind on the target is a separate
    question with its own answer, and dropping the entry here does not touch it.
    """
    with _xhm_lock:
        finished = [mid for mid, task in _xhm_migrations.items()
                    if task.status != 'running' and _xhm_reachable(task)]

    # A migration can also be recorded somewhere that outlives this process, and then the
    # entry comes back on the next page load unless that record goes too. The record is
    # asked first: the log lives only in the in-memory entry, so dropping that entry for a
    # record that stays would bring the row back without the log it had a moment ago.
    removed, refused = [], {}
    for mid, why in _forget_recorded(None):
        if why is None:
            removed.append(mid)
        else:
            refused[mid] = why

    with _xhm_lock:
        for mid in finished:
            task = _xhm_migrations.get(mid)
            if mid in refused or task is None or task.status == 'running':
                continue
            del _xhm_migrations[mid]
            if mid not in removed:
                removed.append(mid)

    kept = [{'id': mid, 'reason': why} for mid, why in refused.items()]
    return jsonify({'removed': removed, 'count': len(removed), 'kept': kept})


@bp.route('/api/xhm/migrations/<mid>', methods=['DELETE'])
@require_auth(perms=['vm.migrate'])
def xhm_dismiss(mid):
    """Take one finished migration off the list.

    A running migration is refused rather than ignored: dropping its record would leave a
    transfer writing to a target with nothing on screen saying so.
    """
    if mid not in _xhm_migrations:
        # After a restart a migration can be on the list without being in this process:
        # the list also shows what a durable record remembers. Answering 404 here made
        # exactly those rows impossible to dismiss.
        answers = list(_forget_recorded(mid))
        if not answers:
            return jsonify({'error': 'Migration not found'}), 404
        why = answers[0][1]
        if why is not None:
            return jsonify({'error': why, 'kept': [{'id': mid, 'reason': why}]}), 409
        return jsonify({'removed': [mid], 'count': 1})
    if not _xhm_reachable(_xhm_migrations[mid]):
        return jsonify({'error': 'Migration not found'}), 404
    with _xhm_lock:
        task = _xhm_migrations.get(mid)
        if task is None:
            return jsonify({'error': 'Migration not found'}), 404
        if task.status == 'running':
            return jsonify({'error': 'This migration is still running. Cancel it first, or '
                                     'wait for it to finish.'}), 409

    for _, why in _forget_recorded(mid):
        if why is not None:
            # The entry stays, and with it the log: that lives only here, and a record
            # that stayed would bring the row back on the next load without it.
            return jsonify({'error': why, 'kept': [{'id': mid, 'reason': why}]}), 409
    with _xhm_lock:
        _xhm_migrations.pop(mid, None)
    return jsonify({'removed': [mid], 'count': 1})


def _forget_recorded(mid):
    """Ask whoever keeps durable migration records to drop one, or all finished ones.

    Yields `(migration_id, reason_it_stayed)` — `None` as the reason means it went. A
    product without such records answers with nothing at all, which is why this is written
    as an optional import rather than a dependency.
    """
    try:
        from pegaprox.core.hyperv_xhm import forget_recorded_migration, recorded_migrations
    except ImportError:
        return

    # Only what the caller may see on the list may be dismissed from it — the same rule
    # for one entry and for all of them, so "clear finished" cannot reach records of a
    # cluster this account has no access to.
    try:
        rows = [row for row in recorded_migrations(()) if _may_dismiss_record(row)]
    except Exception:
        logging.warning('Could not read the recorded migrations', exc_info=True)
        return
    if mid:
        targets = [row.get('id') for row in rows if row.get('id') == mid]
    else:
        targets = [row.get('id') for row in rows if row.get('status') != 'running']
    for target in targets:
        if not target:
            continue
        try:
            answer = forget_recorded_migration(target)
        except Exception:
            logging.warning('Could not forget the record of migration %s', target,
                            exc_info=True)
            # No answer is not a yes: the record may well still be there.
            yield target, 'The migration record could not be removed.'
            continue
        yield target, (None if answer.get('forgotten') else answer.get('error'))


def _may_dismiss_record(row):
    """Whether the caller may take a recorded migration off the list.

    A record carries no live task to ask `_xhm_reachable`, so the cluster-level check on
    its source is asked instead. Anything that goes wrong answers no: a record nobody can
    place is not one to delete.
    """
    try:
        allowed, _ = check_cluster_access(row.get('source_cluster') or '')
        return bool(allowed)
    except Exception:
        return False


@bp.route('/api/xhm/migrations/<mid>', methods=['GET'])
@require_auth(perms=['vm.migrate'])
def xhm_detail(mid):
    if mid not in _xhm_migrations:
        # Fork patch #15 — a restart empties the registry; the database does not.
        recorded = _one_recorded_hyperv_migration(mid)
        if recorded is not None:
            return jsonify(recorded)
        return jsonify({'error': 'Migration not found'}), 404
    if not _xhm_reachable(_xhm_migrations[mid]):
        return jsonify({'error': 'Migration not found'}), 404
    return jsonify(_xhm_migrations[mid].to_dict())


# Fork patch #15 — the migration list is built from a dict in this process, and a restart
# empties it. The check that refuses to start the same VM again reads the database, which
# does not. Between the two, an operator saw nothing and could start nothing: without a
# row there is no "clean up target" and no way to dismiss the entry, while the refusal
# went on naming resources. These two helpers are the bridge; the logic is in the fork's
# own module so this file keeps one call each.
def _recorded_hyperv_migrations(already_listed):
    from pegaprox.core.hyperv_xhm import recorded_migrations

    try:
        rows = recorded_migrations(already_listed)
    except Exception:
        logging.warning('Could not read recorded Hyper-V migrations', exc_info=True)
        return []
    # Same access rule as a live one: whoever may not see the source VM may not see its
    # migration either.
    return [row for row in rows if _recorded_reachable(row)]


def _one_recorded_hyperv_migration(mid):
    for row in _recorded_hyperv_migrations(()):
        if row.get('id') == mid:
            return row
    return None


def _recorded_reachable(row):
    """Whether the caller may see this recorded migration.

    A recorded row carries the source VM's GUID where a live task carries a VMID, so the
    per-VM check cannot be asked in the same way. The cluster-level check is asked
    instead, which is the same gate the Hyper-V routes use.
    """
    try:
        from pegaprox.api.helpers import check_cluster_access

        allowed, _ = check_cluster_access(row.get('source_cluster') or '')
        return bool(allowed)
    except Exception:
        return False
