# -*- coding: utf-8 -*-
"""
Cross-cluster EVPN SDN orchestration (#612, Phase 1) — MK Jul 2026.

PVE has no cross-cluster SDN primitive: each cluster's /etc/pve/sdn config is
entirely local (distributed only within that cluster's pmxcfs). To make one
logical EVPN vNet "span" several clusters that share a BGP ASN, PegaProx has to
create the *same* EVPN controller (asn/peers), the *same* EVPN zone (vrf-vxlan),
and the *same* vnet (tag/VNI/alias) on **every** member cluster and apply each.

This blueprint composes the existing per-cluster SDN passthrough (api/datacenter.py
→ PVE /cluster/sdn/*) into a multi-cluster create/read layer, and keeps the
authoritative record PDM lacks in the `multi_cluster_vnets` table.

Phase 1 = create + read across clusters, with collision pre-flight, bounded
concurrent fan-out, per-cluster status, and atomic rollback on partial failure.
Phase 2 (edit/alias fan-out + a background drift-detect/reconcile scanner) is
intentionally out of scope here.

Blast-radius note: `PUT /cluster/sdn` (apply) is a cluster-wide reload of ALL SDN
on every node of that cluster, not just our vnet. Doing it across N clusters is a
real blast radius, so the write routes are gated on sdn.manage + admin.settings.

We DO NOT build the physical underlay (BGP-EVPN peering / route reflectors between
clusters) — that is the operator's network. We orchestrate the SDN config objects
and assume the shared-ASN fabric already peers.
"""
import ipaddress
import json
import logging
import re
import uuid
from datetime import datetime

from flask import Blueprint, jsonify, request

from pegaprox.globals import cluster_managers
from pegaprox.core.db import get_db
from pegaprox.utils.auth import require_auth
from pegaprox.utils.audit import log_audit
from pegaprox.utils.concurrent import run_per_node
from pegaprox.api.helpers import check_cluster_access, parse_pve_error

bp = Blueprint('multi_sdn', __name__)

# PVE SDN id constraints. Zone + vnet ids are limited to 8 alphanumerics (PVE
# schema); controllers are looser. We validate strictly at the boundary — these
# ids are also interpolated into PVE API paths, so a strict allowlist doubles as
# an injection guard even though the bodies are form-encoded, not shelled.
_ID8_RE = re.compile(r'^[A-Za-z][A-Za-z0-9]{0,7}$')          # zone / vnet
_IDLONG_RE = re.compile(r'^[A-Za-z][A-Za-z0-9_-]{0,62}$')    # controller
_VNI_MAX = 16777215      # 24-bit VXLAN VNI
_ASN_MAX = 4294967295    # 32-bit BGP ASN


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------
def _sdn_base(mgr):
    return f"https://{mgr.host}:{mgr.api_port}/api2/json/cluster/sdn"


def _resolve_member(cid):
    """(mgr, reason). reason is None if usable, else 'not_found' / 'offline'."""
    mgr = cluster_managers.get(cid)
    if mgr is None:
        return None, 'not_found'
    if not getattr(mgr, 'is_connected', False):
        return None, 'offline'
    return mgr, None


def _require_members_access(cluster_ids):
    """Gate every member cluster the operation touches; return the first deny's
    Flask error response, or None if the caller may reach them all. Modeled on
    dr_drill._require_plan_access — a caller who can reach cluster A but not B
    must not be able to mutate/read B's SDN through the aggregate."""
    for cid in cluster_ids:
        if not cid:
            continue
        ok, err = check_cluster_access(cid)
        if not ok:
            return err
    return None


def _sdn_list(mgr, suffix):
    """GET a /cluster/sdn/<suffix> collection → (list, err). 501 => SDN not
    installed on this cluster (err='sdn_not_installed')."""
    try:
        resp = mgr._api_get(f"{_sdn_base(mgr)}/{suffix}", timeout=10)
    except Exception as e:
        return None, f"request failed: {e}"
    if resp.status_code == 501:
        return None, 'sdn_not_installed'
    if resp.status_code != 200:
        return None, parse_pve_error(resp.text)
    try:
        return (resp.json().get('data') or []), None
    except Exception as e:
        return None, f"bad response: {e}"


def _sdn_post(mgr, suffix, body):
    """POST a create; (ok, err). 200/201 => ok."""
    try:
        resp = mgr._api_post(f"{_sdn_base(mgr)}/{suffix}", data=body, timeout=15)
    except Exception as e:
        return False, f"request failed: {e}"
    if resp.status_code == 501:
        return False, 'sdn_not_installed'
    if resp.status_code in (200, 201):
        return True, None
    return False, parse_pve_error(resp.text)


def _sdn_delete(mgr, suffix):
    try:
        resp = mgr._api_delete(f"{_sdn_base(mgr)}/{suffix}", timeout=15)
    except Exception as e:
        return False, f"request failed: {e}"
    if resp.status_code in (200, 201):
        return True, None
    return False, parse_pve_error(resp.text)


def _sdn_apply(mgr):
    """PUT /cluster/sdn — cluster-wide SDN reload (slow, 30s). No body, no digest."""
    try:
        resp = mgr._api_put(_sdn_base(mgr), timeout=30)
    except Exception as e:
        return False, f"request failed: {e}"
    if resp.status_code == 200:
        return True, None
    return False, parse_pve_error(resp.text)


# ---------------------------------------------------------------------------
# validation + desired-state
# ---------------------------------------------------------------------------
def _validate_definition(body):
    """Return (defn, error_message). defn is the normalized cross-cluster vnet
    definition; error_message is a user-facing string on invalid input."""
    name = str(body.get('name', '')).strip()
    zone = str(body.get('zone', '')).strip()
    controller = str(body.get('controller', '')).strip()
    alias = str(body.get('alias', '')).strip()

    if not _ID8_RE.match(name):
        return None, "vnet name must start with a letter and be 1–8 alphanumeric chars"
    if not _ID8_RE.match(zone):
        return None, "zone id must start with a letter and be 1–8 alphanumeric chars"
    if not _IDLONG_RE.match(controller):
        return None, "controller id must start with a letter (letters, digits, - and _; max 63)"

    def _int(field, lo, hi, required=True, default=None):
        raw = body.get(field, None)
        if raw in (None, ''):
            if required:
                return None, f"{field} is required"
            return default, None
        try:
            v = int(raw)
        except (ValueError, TypeError):
            return None, f"{field} must be an integer"
        if not (lo <= v <= hi):
            return None, f"{field} must be between {lo} and {hi}"
        return v, None

    vni, err = _int('vni', 1, _VNI_MAX)
    if err:
        return None, err
    asn, err = _int('asn', 1, _ASN_MAX)
    if err:
        return None, err
    # vrf_vxlan (zone L3 VNI) defaults to the vnet VNI when unset.
    vrf_vxlan, err = _int('vrf_vxlan', 1, _VNI_MAX, required=False, default=vni)
    if err:
        return None, err

    # peers: optional comma/space-separated IPs (EVPN controller BGP peers).
    peers_raw = str(body.get('peers', '') or '').strip()
    peers = []
    for tok in re.split(r'[\s,]+', peers_raw):
        if not tok:
            continue
        try:
            ipaddress.ip_address(tok)
        except ValueError:
            return None, f"invalid peer IP: {tok}"
        peers.append(tok)

    # subnets: optional list of CIDRs (with optional per-subnet gateway/snat).
    subnets = []
    for s in (body.get('subnets') or []):
        if isinstance(s, str):
            s = {'cidr': s}
        cidr = str(s.get('cidr', '')).strip()
        try:
            ipaddress.ip_network(cidr, strict=False)
        except (ValueError, TypeError):
            return None, f"invalid subnet CIDR: {cidr!r}"
        entry = {'cidr': cidr}
        gw = str(s.get('gateway', '') or '').strip()
        if gw:
            try:
                ipaddress.ip_address(gw)
            except ValueError:
                return None, f"invalid subnet gateway: {gw}"
            entry['gateway'] = gw
        if s.get('snat'):
            entry['snat'] = True
        subnets.append(entry)

    members = [str(c).strip() for c in (body.get('cluster_ids') or []) if str(c).strip()]
    # de-dupe, preserve order
    members = list(dict.fromkeys(members))
    if len(members) < 1:
        return None, "at least one member cluster is required"

    defn = {
        'name': name, 'zone': zone, 'controller': controller, 'alias': alias,
        'vni': vni, 'asn': asn, 'vrf_vxlan': vrf_vxlan, 'peers': peers,
        'subnets': subnets, 'member_clusters': members,
    }
    return defn, None


def _controller_body(defn):
    b = {'controller': defn['controller'], 'type': 'evpn', 'asn': defn['asn']}
    if defn['peers']:
        b['peers'] = ','.join(defn['peers'])
    return b


def _zone_body(defn):
    return {'zone': defn['zone'], 'type': 'evpn',
            'controller': defn['controller'], 'vrf-vxlan': defn['vrf_vxlan']}


def _vnet_body(defn):
    b = {'vnet': defn['name'], 'zone': defn['zone'], 'tag': defn['vni']}
    if defn['alias']:
        b['alias'] = defn['alias']
    return b


# ---------------------------------------------------------------------------
# per-cluster: collision pre-flight, apply (idempotent), rollback, live read
# ---------------------------------------------------------------------------
def _collisions_on_cluster(mgr, defn):
    """Return (conflicts, err). conflicts is a list of human strings for objects
    that already exist with a DIFFERENT definition (or a VNI/ASN clash). An object
    that already exists with the SAME definition is fine (idempotent create)."""
    conflicts = []
    controllers, err = _sdn_list(mgr, 'controllers')
    if err:
        return None, err
    zones, err = _sdn_list(mgr, 'zones')
    if err:
        return None, err
    vnets, err = _sdn_list(mgr, 'vnets')
    if err:
        return None, err

    # controller id already used?
    for c in controllers:
        if c.get('controller') == defn['controller']:
            if str(c.get('type')) != 'evpn' or str(c.get('asn')) != str(defn['asn']):
                conflicts.append(
                    f"controller '{defn['controller']}' exists with a different type/ASN "
                    f"(type={c.get('type')}, asn={c.get('asn')})")
    # an EVPN controller on a DIFFERENT id but same-ASN is fine (shared ASN is the point);
    # but a different controller carrying our ASN is not a conflict per se — skip.

    # zone id already used with different controller/vrf-vxlan?
    for z in zones:
        if z.get('zone') == defn['zone']:
            zc = str(z.get('controller') or '')
            zv = str(z.get('vrf-vxlan') or z.get('vrfvxlan') or '')
            if str(z.get('type')) != 'evpn' or (zc and zc != defn['controller']) or (zv and zv != str(defn['vrf_vxlan'])):
                conflicts.append(
                    f"zone '{defn['zone']}' exists with a different definition "
                    f"(type={z.get('type')}, controller={zc}, vrf-vxlan={zv})")

    # vnet id used with different zone/tag, OR our VNI(tag) used by a DIFFERENT vnet?
    for v in vnets:
        same_name = v.get('vnet') == defn['name']
        vtag = str(v.get('tag') or '')
        if same_name:
            if (str(v.get('zone') or '') not in ('', defn['zone'])) or (vtag and vtag != str(defn['vni'])):
                conflicts.append(
                    f"vnet '{defn['name']}' exists with a different zone/tag "
                    f"(zone={v.get('zone')}, tag={vtag})")
        elif vtag and vtag == str(defn['vni']):
            conflicts.append(
                f"VNI {defn['vni']} is already used by a different vnet '{v.get('vnet')}'")

    return conflicts, None


def _apply_on_cluster(cid, defn):
    """Idempotently build the EVPN controller → zone → vnet → subnet(s) on ONE
    cluster, then apply. Returns a per-cluster status dict. Runs inside a greenlet
    (run_per_node) — no Flask context, PVE calls only."""
    result = {'cluster_id': cid, 'status': 'failed', 'steps': [], 'error': None}
    mgr, reason = _resolve_member(cid)
    if reason:
        result['status'] = 'offline' if reason == 'offline' else 'not_found'
        result['error'] = reason
        return result

    # snapshot existing objects once so we skip re-creating what's already there
    controllers, err = _sdn_list(mgr, 'controllers')
    if err:
        result['error'] = f"read controllers: {err}"
        return result
    zones, err = _sdn_list(mgr, 'zones')
    if err:
        result['error'] = f"read zones: {err}"
        return result
    vnets, err = _sdn_list(mgr, 'vnets')
    if err:
        result['error'] = f"read vnets: {err}"
        return result
    have_ctrl = any(c.get('controller') == defn['controller'] for c in controllers)
    have_zone = any(z.get('zone') == defn['zone'] for z in zones)
    have_vnet = any(v.get('vnet') == defn['name'] for v in vnets)

    def _step(label, ok, e=None):
        result['steps'].append({'step': label, 'ok': ok, 'error': e})
        return ok

    # 1) controller
    if have_ctrl:
        _step('controller (exists)', True)
    else:
        ok, e = _sdn_post(mgr, 'controllers', _controller_body(defn))
        if not _step('controller', ok, e):
            result['error'] = f"controller: {e}"
            return result
    # 2) zone (depends on controller)
    if have_zone:
        _step('zone (exists)', True)
    else:
        ok, e = _sdn_post(mgr, 'zones', _zone_body(defn))
        if not _step('zone', ok, e):
            result['error'] = f"zone: {e}"
            return result
    # 3) vnet (depends on zone)
    if have_vnet:
        _step('vnet (exists)', True)
    else:
        ok, e = _sdn_post(mgr, 'vnets', _vnet_body(defn))
        if not _step('vnet', ok, e):
            result['error'] = f"vnet: {e}"
            return result
    # 4) subnets (nested under vnet) — best-effort idempotent
    if defn['subnets']:
        existing_subs, _serr = _sdn_list(mgr, f"vnets/{defn['name']}/subnets")
        # Compare by CANONICAL network equality, not substring — a bare string test
        # false-positives (e.g. desired '10.0.0.0/2' is a substring of existing
        # '10.0.0.0/24') and would silently skip creating a subnet that isn't there.
        existing_networks = set()
        for es in (existing_subs or []):
            raw = str(es.get('cidr') or es.get('subnet') or '')
            try:
                existing_networks.add(str(ipaddress.ip_network(raw, strict=False)))
            except (ValueError, TypeError):
                # PVE subnet ids can be "<zone>-<cidr>" (slash→dash), not a bare CIDR — skip
                pass
        for sub in defn['subnets']:
            if str(ipaddress.ip_network(sub['cidr'], strict=False)) in existing_networks:
                _step(f"subnet {sub['cidr']} (exists)", True)
                continue
            body = {'subnet': sub['cidr'], 'type': 'subnet'}
            if sub.get('gateway'):
                body['gateway'] = sub['gateway']
            if sub.get('snat'):
                body['snat'] = 1
            ok, e = _sdn_post(mgr, f"vnets/{defn['name']}/subnets", body)
            if not _step(f"subnet {sub['cidr']}", ok, e):
                result['error'] = f"subnet {sub['cidr']}: {e}"
                return result
    # 5) apply (cluster-wide reload)
    ok, e = _sdn_apply(mgr)
    if not _step('apply', ok, e):
        result['error'] = f"apply: {e}"
        return result

    result['status'] = 'applied'
    return result


def _rollback_on_cluster(cid, defn):
    """Best-effort teardown of what a create staged on ONE cluster: delete vnet →
    zone → controller (reverse dependency order), then apply. Used for atomic-
    create rollback and for purge-delete. Never raises."""
    out = {'cluster_id': cid, 'deleted': [], 'errors': []}
    mgr, reason = _resolve_member(cid)
    if reason:
        out['errors'].append(reason)
        return out
    # reverse order; ignore "does not exist" errors
    for suffix, label in (
        (f"vnets/{defn['name']}", 'vnet'),
        (f"zones/{defn['zone']}", 'zone'),
        (f"controllers/{defn['controller']}", 'controller'),
    ):
        ok, e = _sdn_delete(mgr, suffix)
        if ok:
            out['deleted'].append(label)
        elif e and 'does not exist' not in str(e).lower():
            out['errors'].append(f"{label}: {e}")
    _sdn_apply(mgr)
    return out


def _live_status_on_cluster(cid, defn):
    """Read live SDN state on ONE cluster and classify vs the desired definition:
    in_sync / drift / missing / offline / sdn_not_installed / error."""
    st = {'cluster_id': cid, 'state': 'error', 'detail': ''}
    mgr, reason = _resolve_member(cid)
    if reason:
        st['state'] = 'offline' if reason == 'offline' else 'not_found'
        return st
    vnets, err = _sdn_list(mgr, 'vnets')
    if err == 'sdn_not_installed':
        st['state'] = 'sdn_not_installed'
        return st
    if err:
        st['detail'] = err
        return st
    v = next((x for x in vnets if x.get('vnet') == defn['name']), None)
    if not v:
        st['state'] = 'missing'
        return st
    diffs = []
    if str(v.get('zone') or '') not in ('', defn['zone']):
        diffs.append(f"zone={v.get('zone')}≠{defn['zone']}")
    if str(v.get('tag') or '') not in ('', str(defn['vni'])):
        diffs.append(f"tag={v.get('tag')}≠{defn['vni']}")
    st['state'] = 'drift' if diffs else 'in_sync'
    st['detail'] = '; '.join(diffs)
    return st


# ---------------------------------------------------------------------------
# record helpers
# ---------------------------------------------------------------------------
def _row_to_dict(row):
    d = dict(row)
    for k, default in (('member_clusters', '[]'), ('subnets', '[]'),
                       ('desired_state', '{}'), ('per_cluster_status', '{}')):
        try:
            d[k] = json.loads(d.get(k) or default)
        except (json.JSONDecodeError, TypeError):
            d[k] = json.loads(default)
    d['enabled'] = bool(d.get('enabled', 1))
    return d


def _caller_can_access_all(member_ids):
    for cid in member_ids:
        ok, _err = check_cluster_access(cid)
        if not ok:
            return False
    return True


def _rollup_status(per_cluster):
    """applied / partial / failed from the per-cluster result map."""
    states = [v.get('status') for v in per_cluster.values()]
    if not states:
        return 'pending'
    good = [s for s in states if s == 'applied']
    if len(good) == len(states):
        return 'applied'
    if not good:
        return 'failed'
    return 'partial'


# ---------------------------------------------------------------------------
# routes
# ---------------------------------------------------------------------------
@bp.route('/api/multi-sdn/vnets', methods=['GET'])
@require_auth(perms=['node.view'])
def list_multi_vnets():
    """List cross-cluster EVPN vnets the caller can reach (must be able to access
    ALL member clusters — it's a cross-cluster object)."""
    db = get_db()
    rows = db.query('SELECT * FROM multi_cluster_vnets ORDER BY created_at DESC')
    out = []
    for row in rows:
        rec = _row_to_dict(row)
        if _caller_can_access_all(rec.get('member_clusters', [])):
            out.append(rec)
    return jsonify(out)


@bp.route('/api/multi-sdn/vnets/<vid>', methods=['GET'])
@require_auth(perms=['node.view'])
def get_multi_vnet(vid):
    """Detail for one aggregate vnet. ?refresh=1 re-reads live per-cluster state."""
    db = get_db()
    row = db.query_one('SELECT * FROM multi_cluster_vnets WHERE id = ?', (vid,))
    if not row:
        return jsonify({'error': 'not found'}), 404
    rec = _row_to_dict(row)
    members = rec.get('member_clusters', [])
    # deny → 404 (don't confirm existence of a record on clusters you can't see)
    if not _caller_can_access_all(members):
        return jsonify({'error': 'not found'}), 404

    if request.args.get('refresh') in ('1', 'true', 'yes'):
        defn = rec.get('desired_state') or {}
        if defn:
            # a handful of quick GETs per member — read them sequentially
            rec['live_status'] = {cid: _live_status_on_cluster(cid, defn) for cid in members}
    return jsonify(rec)


@bp.route('/api/multi-sdn/vnets/validate', methods=['POST'])
@require_auth(perms=['sdn.manage'])
def validate_multi_vnet():
    """Dry pre-flight: validate the definition + check every member for
    reachability and collisions. No writes. Feeds the wizard's preview step."""
    body = request.get_json(force=True, silent=True) or {}
    defn, err = _validate_definition(body)
    if err:
        return jsonify({'ok': False, 'error': err}), 400
    denied = _require_members_access(defn['member_clusters'])
    if denied:
        return denied

    plan = {}
    reachable = True
    for cid in defn['member_clusters']:
        mgr, reason = _resolve_member(cid)
        if reason:
            plan[cid] = {'reachable': False, 'reason': reason, 'conflicts': []}
            reachable = False
            continue
        conflicts, cerr = _collisions_on_cluster(mgr, defn)
        if cerr == 'sdn_not_installed':
            plan[cid] = {'reachable': True, 'sdn_installed': False, 'conflicts': []}
            reachable = False
        elif cerr:
            plan[cid] = {'reachable': True, 'error': cerr, 'conflicts': []}
            reachable = False
        else:
            plan[cid] = {'reachable': True, 'sdn_installed': True, 'conflicts': conflicts}
    has_conflicts = any(p.get('conflicts') for p in plan.values())
    return jsonify({'ok': reachable and not has_conflicts, 'defn': defn, 'plan': plan})


@bp.route('/api/multi-sdn/vnets', methods=['POST'])
@require_auth(perms=['sdn.manage', 'admin.settings'])
def create_multi_vnet():
    """Create a cross-cluster EVPN vnet: validate → per-member collision + reach
    pre-flight → concurrent fan-out (controller→zone→vnet→subnets→apply) → record.

    Body: name, zone, controller, vni, asn[, vrf_vxlan, alias, peers, subnets],
          cluster_ids[]; optional atomic (default true) = roll back all members on
          any failure so you never get a half-built L2 span.
    """
    body = request.get_json(force=True, silent=True) or {}
    defn, err = _validate_definition(body)
    if err:
        return jsonify({'error': err}), 400
    members = defn['member_clusters']
    denied = _require_members_access(members)
    if denied:
        return denied
    atomic = body.get('atomic', True) is not False

    # --- pre-flight: every member must be reachable, SDN-installed, conflict-free.
    # A create must not build a partial/inconsistent span, so bail before writing.
    preflight = {}
    for cid in members:
        mgr, reason = _resolve_member(cid)
        if reason:
            return jsonify({'error': f"member cluster '{cid}' is {reason}; "
                            f"cannot build a consistent span", 'cluster_id': cid}), 409
        conflicts, cerr = _collisions_on_cluster(mgr, defn)
        if cerr == 'sdn_not_installed':
            return jsonify({'error': f"SDN is not installed on member cluster '{cid}'",
                            'cluster_id': cid}), 409
        if cerr:
            return jsonify({'error': f"pre-flight read failed on '{cid}': {cerr}",
                            'cluster_id': cid}), 502
        if conflicts:
            return jsonify({'error': f"conflicting SDN objects on '{cid}'",
                            'cluster_id': cid, 'conflicts': conflicts}), 409
        preflight[cid] = 'ok'

    # --- fan out (bounded concurrency); each member builds sequentially internally
    results = run_per_node(
        {cid: (lambda c=cid: _apply_on_cluster(c, defn)) for cid in members},
        max_concurrent=8, timeout=180) or {}
    per_cluster = {}
    for cid in members:
        r = results.get(cid)
        per_cluster[cid] = r if isinstance(r, dict) else {
            'cluster_id': cid, 'status': 'failed', 'error': 'no result (timeout?)', 'steps': []}
    rollup = _rollup_status(per_cluster)

    user = getattr(request, 'session', {}).get('user', 'system')

    # --- atomic: on any non-'applied' member, tear the successful ones back down
    #     and do NOT persist a record (no half-built span left behind).
    if atomic and rollup != 'applied':
        # Roll back EVERY member, not just the fully-'applied' ones: a member that failed
        # mid-build (e.g. controller + zone created, then the vnet POST failed) is the one
        # MOST likely to hold orphaned objects, yet its status is 'failed'. _rollback_on_cluster
        # is best-effort and ignores "does not exist", so over-rolling a member that created
        # nothing is a harmless no-op — this is what actually honors "no half-built span".
        rollbacks = {c: _rollback_on_cluster(c, defn) for c in members}
        log_audit(user, 'multi_sdn.vnet_create_failed',
                  f"Cross-cluster EVPN vnet '{defn['name']}' failed ({rollup}); "
                  f"rolled back all {len(members)} member(s)")
        return jsonify({'error': 'fan-out failed; rolled back (atomic)',
                        'status': rollup, 'per_cluster': per_cluster,
                        'rolled_back': rollbacks}), 502

    # --- persist the authoritative record
    vid = str(uuid.uuid4())[:8]
    now = datetime.now().isoformat()
    db = get_db()
    db.execute('''
        INSERT INTO multi_cluster_vnets
        (id, name, alias, zone, vni, asn, vrf_vxlan, controller, peers,
         member_clusters, subnets, desired_state, per_cluster_status, status,
         enabled, created_by, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?)
    ''', (
        vid, defn['name'], defn['alias'], defn['zone'], defn['vni'], defn['asn'],
        defn['vrf_vxlan'], defn['controller'], ','.join(defn['peers']),
        json.dumps(members), json.dumps(defn['subnets']), json.dumps(defn),
        json.dumps(per_cluster), rollup, user, now, now,
    ))
    log_audit(user, 'multi_sdn.vnet_created',
              f"Cross-cluster EVPN vnet '{defn['name']}' (VNI {defn['vni']}, ASN "
              f"{defn['asn']}) across {len(members)} clusters → {rollup}")
    row = db.query_one('SELECT * FROM multi_cluster_vnets WHERE id = ?', (vid,))
    return jsonify(_row_to_dict(row)), 201


@bp.route('/api/multi-sdn/vnets/<vid>/apply', methods=['POST'])
@require_auth(perms=['sdn.manage', 'admin.settings'])
def reapply_multi_vnet(vid):
    """Re-apply (retry) the vnet on members that aren't yet 'applied' — idempotent,
    so it's safe to re-run after fixing an offline member. Refreshes the record's
    per-cluster status + rollup."""
    db = get_db()
    row = db.query_one('SELECT * FROM multi_cluster_vnets WHERE id = ?', (vid,))
    if not row:
        return jsonify({'error': 'not found'}), 404
    rec = _row_to_dict(row)
    members = rec.get('member_clusters', [])
    denied = _require_members_access(members)
    if denied:
        return denied
    defn = rec.get('desired_state') or {}
    if not defn:
        return jsonify({'error': 'record has no stored definition'}), 500

    prev = rec.get('per_cluster_status', {})
    todo = [c for c in members if (prev.get(c) or {}).get('status') != 'applied']
    if not todo:
        todo = members  # allow a full re-apply if everything already applied
    results = run_per_node(
        {cid: (lambda c=cid: _apply_on_cluster(c, defn)) for cid in todo},
        max_concurrent=8, timeout=180) or {}
    merged = dict(prev)
    for cid in todo:
        r = results.get(cid)
        merged[cid] = r if isinstance(r, dict) else {
            'cluster_id': cid, 'status': 'failed', 'error': 'no result', 'steps': []}
    rollup = _rollup_status({c: merged.get(c, {}) for c in members})
    now = datetime.now().isoformat()
    db.execute('UPDATE multi_cluster_vnets SET per_cluster_status = ?, status = ?, updated_at = ? WHERE id = ?',
               (json.dumps(merged), rollup, now, vid))
    log_audit(getattr(request, 'session', {}).get('user', 'system'),
              'multi_sdn.vnet_reapplied', f"Re-applied cross-cluster vnet '{rec.get('name')}' → {rollup}")
    row = db.query_one('SELECT * FROM multi_cluster_vnets WHERE id = ?', (vid,))
    return jsonify(_row_to_dict(row))


@bp.route('/api/multi-sdn/vnets/<vid>', methods=['DELETE'])
@require_auth(perms=['sdn.manage', 'admin.settings'])
def delete_multi_vnet(vid):
    """Remove the aggregate record. Default only forgets our bookkeeping and leaves
    the SDN objects on the clusters intact; ?purge=1 ALSO tears the vnet/zone/
    controller down on every member (delete fan-out + apply)."""
    db = get_db()
    row = db.query_one('SELECT * FROM multi_cluster_vnets WHERE id = ?', (vid,))
    if not row:
        return jsonify({'error': 'not found'}), 404
    rec = _row_to_dict(row)
    members = rec.get('member_clusters', [])
    denied = _require_members_access(members)
    if denied:
        return denied
    purge = request.args.get('purge') in ('1', 'true', 'yes')
    purged = {}
    if purge:
        defn = rec.get('desired_state') or {}
        if defn:
            purged = {c: _rollback_on_cluster(c, defn) for c in members}
    db.execute('DELETE FROM multi_cluster_vnets WHERE id = ?', (vid,))
    log_audit(getattr(request, 'session', {}).get('user', 'system'),
              'multi_sdn.vnet_deleted',
              f"Deleted cross-cluster vnet record '{rec.get('name')}'"
              + (f" + purged from {len(members)} clusters" if purge else " (record only)"))
    return jsonify({'ok': True, 'purged': purged})
