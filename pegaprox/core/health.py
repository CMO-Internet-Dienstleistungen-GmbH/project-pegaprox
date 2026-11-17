# -*- coding: utf-8 -*-
"""
PegaProx Cluster Health - rollup computation and cache.

The health score used to be computed inline in the /health request handler, once
per request per tab. It is a cluster-wide number — the same for everyone looking
at the cluster — but every viewer paid for their own fan-out: get_node_status()
plus one uncached GET /nodes/<node>/storage per online node.

Two things live here so that stops being per-viewer:

  compute_cluster_health()  the rollup itself, lifted out of the Flask handler so
                            the SSE broadcaster can call it too. No auth — the
                            caller owns it. Since upstream 1.5.0 (#1218) the
                            number is per caller for a confined account; only the
                            unconfined rollup is shared, cached and broadcast.
  get_cluster_health()      the same, memoised per cluster for _HEALTH_TTL_S.

Why the rollup is cached rather than get_storage_list(): that method has seven
callers, several of them decision paths (ISO selection, migration target choice).
A stale storage list there is worse than an extra API call. Caching the rollup
gets the same relief without putting a cache in a write path.
"""

import hashlib
import json
import logging
import os
import threading
import time
from datetime import datetime, timezone

# How long a computed rollup stays good.
#
# This MUST exceed the broadcast refresher's interval (background/health.py,
# 60s). The refresher is what keeps the cache warm; if entries expire before it
# comes round again, the cache is cold for most of every cycle and whoever asks
# in that window pays the full storage fan-out. Measured on a live instance with
# a 15s TTL: requests came back in either 100ms (hit) or 4s (miss), which is that
# gap made visible.
#
# The staleness this allows is bounded by the refresher, not by this number: it
# rewrites the entry every 60s regardless. The TTL only decides what happens to a
# cluster the refresher is NOT servicing — one nobody is watching — and there a
# recomputation on first request is the right answer anyway.
_HEALTH_TTL_S = float(os.environ.get('PEGAPROX_HEALTH_TTL', '90'))

# cluster_id -> {'payload': dict, 'etag': str, 'at': float, 'ceph': _ceph_mark()}
_cache = {}
_cache_lock = threading.Lock()


def _band(score):
    if score >= 90:
        return 'excellent'
    if score >= 70:
        return 'good'
    if score >= 50:
        return 'warning'
    if score >= 30:
        return 'degraded'
    return 'critical'


def compute_cluster_health(mgr, cluster_id, mine=None):
    """Compute the health rollup for one cluster. Returns the response payload.

    `mine` is upstream's caller scope (#1218, api.helpers.caller_guest_nodes): None for a
    caller who sees the whole cluster, else the set of nodes their own guests run on. Only
    the None rollup is the cluster-wide number this module caches and broadcasts; a scoped
    one is computed per request by the route and never stored here. Scoping rows needs the
    request context, which is why only the route asks for a scoped rollup.

    The factor block below is upstream's /health handler as of v1.5.0, verbatim apart from
    its indentation, so the next release can be diffed against it line by line. The names
    it uses are bound to the API module's own objects first: that module imports this one,
    so they are imported at call time.
    """
    from pegaprox.api import clusters as _clusters
    from pegaprox.api.helpers import scope_vm_rows
    from pegaprox.core.db import get_db
    _health_storage_cache = _clusters._health_storage_cache
    _health_storage_rows = _clusters._health_storage_rows
    _HEALTH_STORAGE_TTL = _clusters._HEALTH_STORAGE_TTL
    _health_ceph_status = _clusters._health_ceph_status
    _health_ceph_factor = _clusters._health_ceph_factor

    score = 100
    factors = []
    issues = []

    # Connectivity gate — if the API isn't reachable, everything else is moot
    if not mgr.is_connected:
        return {
            'score': 0,
            'band': 'critical',
            'factors': [{'key': 'api', 'label': 'API connectivity',
                         'value': 'disconnected', 'delta': -100}],
            'issues': ['Cluster API not reachable'],
            'computed_at': None,
        }


    # 1) Nodes online
    try:
        ns = mgr.get_node_status() or {}
    except Exception:
        ns = {}
    if mine is not None:
        ns = {name: d for name, d in ns.items() if name in mine}
    total_nodes = len(ns)
    online_nodes = sum(1 for n in ns.values() if (n.get('status') in ('online', 'running') or not n.get('offline')))
    if total_nodes:
        offline = total_nodes - online_nodes
        delta = -25 * offline
        score += delta
        factors.append({
            'key': 'nodes', 'label': 'Nodes online',
            'value': f'{online_nodes}/{total_nodes}', 'delta': delta,
            'severity': 'critical' if offline else 'ok',
        })
        if offline:
            offline_names = [name for name, d in ns.items()
                             if d.get('status') == 'offline' or d.get('offline')]
            issues.append(f'{offline} node(s) offline: {", ".join(offline_names) or "?"}')

    # 2) Storage pressure — worst-offender across all nodes
    worst_pct = 0.0
    worst_label = None
    try:
        rows, hit = _health_storage_cache.get(cluster_id, 'storage')
        if not hit:
            rows = _health_storage_rows(cluster_id, mgr, ns)
            if rows is not None:
                _health_storage_cache.set(cluster_id, 'storage', rows, ttl_seconds=_HEALTH_STORAGE_TTL)
        for node_name, stor_name, used, total in (rows or []):
            if total <= 0 or (mine is not None and node_name not in mine):
                continue
            pct = (used / total) * 100.0
            if pct > worst_pct:
                worst_pct = pct
                worst_label = f"{stor_name} @ {node_name}"
    except Exception as e:
        logging.debug(f"[health] storage scan failed: {e}")
    if worst_label is not None:
        if worst_pct >= 95:
            d = -25
        elif worst_pct >= 90:
            d = -15
        elif worst_pct >= 80:
            d = -5
        else:
            d = 0
        score += d
        factors.append({
            'key': 'storage', 'label': 'Worst storage',
            'value': f'{worst_label} ({worst_pct:.0f}%)', 'delta': d,
            'severity': 'critical' if worst_pct >= 95 else 'warning' if worst_pct >= 80 else 'ok',
        })
        if worst_pct >= 90:
            issues.append(f'Storage near full: {worst_label} at {worst_pct:.0f}%')

    # 3) Replication — failed jobs hurt
    try:
        repl = mgr.get_replication_status() or []
    except Exception:
        repl = []
    if mine is not None:
        # a job's 'type' is the replication type (local), not the guest's: asked as either
        repl = scope_vm_rows(cluster_id, repl, vmid_key='guest', type_key='guest_type') if mine else []
    if repl:
        # PVE flags failures via 'fail_count' or non-zero error
        failed = sum(1 for r in repl if (r.get('fail_count') or 0) > 0 or r.get('error'))
        d = max(-20, -5 * failed)
        score += d
        factors.append({
            'key': 'replication', 'label': 'Replication',
            'value': f'{failed} failing / {len(repl)} jobs',
            'delta': d,
            'severity': 'warning' if failed else 'ok',
        })
        if failed:
            issues.append(f'{failed} replication job(s) failing')

    # 4) Backup-SLA — only if admin set a max-age threshold on the cluster
    try:
        db = get_db()
        row = db.conn.cursor().execute(
            "SELECT backup_sla_max_age_hours FROM clusters WHERE id = ?", (cluster_id,)
        ).fetchone()
        max_age = (dict(row).get('backup_sla_max_age_hours') if row else None) or 0
    except Exception:
        max_age = 0
    if max_age and max_age > 0:
        # Pull the most-recent backup timestamp via cluster/backup-info — cheap call
        try:
            import time as _t
            now = _t.time()
            url = f"https://{mgr.host}:{mgr.api_port}/api2/json/cluster/backup-info/not-backed-up"
            r = mgr._api_get(url)
            stale = 0
            if r is not None and r.status_code == 200:
                _rows = r.json().get('data') or []
                if mine is not None:
                    _rows = scope_vm_rows(cluster_id, _rows) if mine else []
                stale = len(_rows)
            d = -10 if stale else 0
            score += d
            factors.append({
                'key': 'backup_sla', 'label': 'Backup SLA',
                'value': f'{stale} VM(s) past RPO ({max_age}h)' if stale else 'within RPO',
                'delta': d,
                'severity': 'warning' if stale else 'ok',
            })
            if stale:
                issues.append(f'{stale} VMs past backup RPO of {max_age}h')
        except Exception as e:
            logging.debug(f"[health] backup-sla check failed: {e}")

    # 5) Ceph - last, its cap holds whatever the factors above left. Not for a caller confined
    # to a pool or to guests: Ceph is the cluster's storage, and the Ceph overview answers
    # 'confined' to them as well
    try:
        ceph, has_ceph = (None, False) if mine is not None else _health_ceph_status(cluster_id, mgr)
        if has_ceph:
            f, issue = _health_ceph_factor(ceph, score)
            score += f['delta']
            factors.append(f)
            if issue:
                issues.append(issue)
    except Exception as e:
        logging.debug(f"[health] ceph check failed: {e}")

    score = max(0, min(100, score))
    return {
        'score': score,
        'band': _band(score),
        'factors': factors,
        'issues': issues,
        'computed_at': datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'),
    }


def health_etag(payload):
    """A stable ETag for a rollup.

    computed_at is deliberately excluded: it changes on every computation, so
    including it would make every ETag unique and the conditional request
    pointless. Two rollups with the same score, factors and issues are the same
    answer regardless of when they were worked out.
    """
    material = {k: payload.get(k) for k in ('score', 'band', 'factors', 'issues')}
    blob = json.dumps(material, sort_keys=True, default=str).encode('utf-8')
    return 'W/"' + hashlib.sha256(blob).hexdigest()[:32] + '"'


def _storage_part_is_live(cluster_id, payload):
    """The storage figure inside a cached rollup is only as fresh as the shared storage
    read it came from (#946: a short TTL, and a failed read is not kept). A rollup built
    on a read that has since expired, or that never succeeded, is not served again; the
    rollup of a cluster that was never asked for storage (not connected) has no such part."""
    if not payload.get('computed_at'):
        return True
    try:
        from pegaprox.api import clusters as _clusters
        return _clusters._health_storage_cache.get(cluster_id, 'storage')[1]
    except Exception:
        return True


def _ceph_mark(cluster_id):
    """Which Ceph state a rollup was scored on: the time of the last Ceph read and since
    when Ceph has been silent (#948, background/alert_events). Upstream reads Ceph in the
    background and scores the status it has, so a rollup computed before a read landed
    carries no Ceph factor; serving it again after the read would hide Ceph for as long
    as the cache lives."""
    try:
        from pegaprox.background import alert_events
        last = alert_events.ceph_reading(cluster_id)
        return (last[0] if last else None, alert_events.ceph_silent_since(cluster_id))
    except Exception:
        return None


def _ceph_read_outstanding(cluster_id, mark):
    """Whether a rollup was scored before Ceph had ever answered on a cluster that has Ceph
    storage. Upstream asks again on the next poll when such a read failed; a rollup kept
    for that cluster would stop it asking until the cache expires, so none is kept."""
    if mark is None or mark[0] is not None:
        return False
    try:
        from pegaprox.api import clusters as _clusters
        rows, hit = _clusters._health_storage_cache.get(cluster_id, 'resources')
        return bool(hit) and any(r.get('plugintype') in _clusters._HEALTH_CEPH_STORAGE
                                 for r in (rows or []))
    except Exception:
        return False


def get_cluster_health(cluster_id, mgr, max_age=None, force=False):
    """Cached rollup. Returns (payload, etag, from_cache).

    Not single-flight: concurrent misses each compute. That is deliberate — a
    lock held across the storage fan-out would serialise every viewer behind one
    slow node, which is the failure this change exists to avoid. The window is
    narrow and the cost of a duplicate computation is bounded.
    """
    ttl = _HEALTH_TTL_S if max_age is None else max_age
    now = time.time()

    if not force and ttl > 0:
        with _cache_lock:
            entry = _cache.get(cluster_id)
        if entry and (now - entry['at']) <= ttl and _storage_part_is_live(cluster_id, entry['payload']) \
                and entry.get('ceph') == _ceph_mark(cluster_id) \
                and not _ceph_read_outstanding(cluster_id, entry.get('ceph')):
            return entry['payload'], entry['etag'], True

    payload = compute_cluster_health(mgr, cluster_id)
    etag = health_etag(payload)
    with _cache_lock:
        _cache[cluster_id] = {'payload': payload, 'etag': etag, 'at': time.time(),
                              'ceph': _ceph_mark(cluster_id)}
    return payload, etag, False


def peek_cluster_health(cluster_id):
    """The cached rollup without computing anything, or None. For the broadcaster."""
    with _cache_lock:
        entry = _cache.get(cluster_id)
    return (entry['payload'], entry['etag']) if entry else None


def invalidate_cluster_health(cluster_id=None):
    """Drop one cluster's rollup, or all of them."""
    with _cache_lock:
        if cluster_id is None:
            _cache.clear()
        else:
            _cache.pop(cluster_id, None)
