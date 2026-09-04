# -*- coding: utf-8 -*-
"""
PegaProx Cluster Health - rollup computation and cache.

The health score used to be computed inline in the /health request handler, once
per request per tab. It is a cluster-wide number — the same for everyone looking
at the cluster — but every viewer paid for their own fan-out: get_node_status()
plus one uncached GET /nodes/<node>/storage per online node.

Two things live here so that stops being per-viewer:

  compute_cluster_health()  the rollup itself, lifted out of the Flask handler so
                            the SSE broadcaster can call it too. No request
                            context, no auth — the caller owns both.
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
import re
import threading
import time
from datetime import datetime, timezone

# How long a computed rollup stays good. Short on purpose: this is a health
# readout, and a viewer opening the dashboard should not see a minute-old number.
# The point is that N viewers within one window cost one computation, not N.
_HEALTH_TTL_S = float(os.environ.get('PEGAPROX_HEALTH_TTL', '15'))

# cluster_id -> {'payload': dict, 'etag': str, 'at': float}
_cache = {}
_cache_lock = threading.Lock()

# PVE controls node names, but a crafted one would be interpolated into a storage
# URL. Same guard the inline version carried.
_SAFE_NODE = re.compile(r'^[a-zA-Z][a-zA-Z0-9.\-]{0,62}$')


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


def compute_cluster_health(mgr, cluster_id):
    """Compute the health rollup for one cluster. Returns the response payload.

    Lifted verbatim from the /health handler; the scoring, the factor keys and
    the response shape are unchanged, because the UI and the API contract both
    depend on them.
    """
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
    total_nodes = len(ns)
    online_nodes = sum(1 for n in ns.values()
                       if (n.get('status') in ('online', 'running') or not n.get('offline')))
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

    # 2) Storage pressure — worst offender across all online nodes.
    # Only ONLINE nodes: a dead node's storage call parks the whole parallel
    # batch at the pool timeout, which made /health slower on degraded clusters
    # than the sequential version had been.
    worst_pct = 0.0
    worst_label = None
    try:
        from pegaprox.utils.concurrent import run_concurrent_dict
        online_node_names = [
            name for name, d in ns.items()
            if (d.get('status') in ('online', 'running') or not d.get('offline'))
            and name and _SAFE_NODE.match(name)
        ]
        if online_node_names:
            tasks = {n: (lambda nn=n: mgr.get_storage_list(nn) or []) for n in online_node_names}
            per_node_stors = run_concurrent_dict(tasks, timeout=8)
        else:
            per_node_stors = {}
        for node_name, stors in per_node_stors.items():
            for s in (stors or []):
                if not s.get('active'):
                    continue
                total = s.get('total') or 0
                used = s.get('used') or 0
                if total <= 0:
                    continue
                pct = (used / total) * 100.0
                if pct > worst_pct:
                    worst_pct = pct
                    worst_label = f"{s.get('storage', '?')} @ {node_name}"
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
    if repl:
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

    # 4) Backup-SLA — only if an admin set a max-age threshold on the cluster
    try:
        from pegaprox.core.db import get_db
        db = get_db()
        row = db.conn.cursor().execute(
            "SELECT backup_sla_max_age_hours FROM clusters WHERE id = ?", (cluster_id,)
        ).fetchone()
        max_age = (dict(row).get('backup_sla_max_age_hours') if row else None) or 0
    except Exception:
        max_age = 0
    if max_age and max_age > 0:
        try:
            url = f"https://{mgr.host}:{mgr.api_port}/api2/json/cluster/backup-info/not-backed-up"
            r = mgr._api_get(url)
            stale = 0
            if r is not None and r.status_code == 200:
                stale = len(r.json().get('data') or [])
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
        if entry and (now - entry['at']) <= ttl:
            return entry['payload'], entry['etag'], True

    payload = compute_cluster_health(mgr, cluster_id)
    etag = health_etag(payload)
    with _cache_lock:
        _cache[cluster_id] = {'payload': payload, 'etag': etag, 'at': time.time()}
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
