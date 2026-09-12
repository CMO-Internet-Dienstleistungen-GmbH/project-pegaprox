# -*- coding: utf-8 -*-
"""Who created a snapshot — the one fact PVE does not keep for us.

A PVE snapshot carries a name, a timestamp, a parent and a description. It
does not carry an author: the API call arrives as the cluster's own root
ticket, so by the time a snapshot exists there is nothing left that says which
PegaProx account asked for it. This module keeps that link on our side, in a
table of its own, and hands it back when a snapshot list is rendered.

The identity of a snapshot is (cluster, guest type, guest, name). Names are
reusable, so a stored row is bound to the snapshot's `snaptime` the first time
we see the snapshot itself:

  * A row is written when a creation is requested, with `snaptime` still unset
    and `requested_at` holding our clock.
  * The first listing that shows a snapshot of that name claims the row, but
    only when its `snaptime` falls inside the window around `requested_at` —
    an older snapshot that merely shares the name is not ours.
  * Once claimed, the row answers only for that exact `snaptime`. Delete the
    snapshot outside PegaProx and create another one under the same name and
    the times differ, so the old author is not inherited.
  * A row that is never claimed inside the window describes a creation that
    failed, and is dropped rather than left to be adopted by a later snapshot.

The one case this cannot separate is a snapshot deleted and recreated under
the same name within the same second, which PVE timestamps identically. That
is accepted: the alternative is an identity PVE does not offer us.

Deliberately additive — the fork's patch for issue #39 owns this file, so an
upstream release can move the code around it without a conflict.
"""
from __future__ import annotations

import time
import logging
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from pegaprox.core.db import get_db

# Origin of a recorded creation. `user` means an authenticated account asked
# for it; `automatic` a scheduled policy, which has no account behind it but a
# provenance that is just as documented. Anything we cannot prove gets no row
# at all and therefore no origin — never a guess.
ORIGIN_USER = 'user'
ORIGIN_AUTOMATIC = 'automatic'

# PegaProx and the PVE node keep their own clocks, and `snaptime` comes from
# the node. Allow for drift in both directions, plus the time a snapshot may
# legitimately take to appear (a VM-state snapshot writes RAM to disk first).
_CLOCK_SKEW_SECONDS = 300
_CREATION_WINDOW_SECONDS = 6 * 3600

_TABLE = 'snapshot_authors'

_Key = Tuple[str, str, int, str]

_log = logging.getLogger(__name__)
_schema_ready = False


def _connection():
    return get_db().conn


def _ensure_schema() -> None:
    """Create our table on first use.

    Lazily rather than in db.py's schema block: this keeps the patch out of a
    file every upstream release touches, and the table is needed only once a
    snapshot is actually created or listed.
    """
    global _schema_ready
    if _schema_ready:
        return
    cursor = _connection().cursor()
    cursor.execute(f'''
        CREATE TABLE IF NOT EXISTS {_TABLE} (
            cluster_id   TEXT    NOT NULL,
            vm_type      TEXT    NOT NULL,
            vmid         INTEGER NOT NULL,
            snapname     TEXT    NOT NULL,
            author       TEXT    NOT NULL DEFAULT '',
            origin       TEXT    NOT NULL DEFAULT '{ORIGIN_USER}',
            requested_at INTEGER NOT NULL,
            snaptime     INTEGER,
            PRIMARY KEY (cluster_id, vm_type, vmid, snapname)
        )
    ''')
    cursor.execute(
        f'CREATE INDEX IF NOT EXISTS idx_snapshot_authors_guest '
        f'ON {_TABLE}(cluster_id, vmid)'
    )
    _connection().commit()
    _schema_ready = True


def reset_schema_cache() -> None:
    """Forget that the table was created — for tests that swap the database."""
    global _schema_ready
    _schema_ready = False


def _normalise(cluster_id: str, vm_type: str, vmid: Any, snapname: str) -> Optional[_Key]:
    try:
        vmid_int = int(vmid)
    except (TypeError, ValueError):
        return None
    if not cluster_id or not vm_type or not snapname:
        return None
    return (str(cluster_id), str(vm_type), vmid_int, str(snapname))


def record_creation(cluster_id: str, vm_type: str, vmid: Any, snapname: str,
                    author: str, origin: str = ORIGIN_USER) -> None:
    """Remember that `author` asked for this snapshot.

    Called when the creation is requested, not when it completes — PVE answers
    with a task and the snapshot appears later. An unclaimed row is discarded
    by `resolve()` once the window has passed, so a failed creation leaves
    nothing behind.
    """
    key = _normalise(cluster_id, vm_type, vmid, snapname)
    if key is None:
        return
    if origin not in (ORIGIN_USER, ORIGIN_AUTOMATIC):
        origin = ORIGIN_USER
    try:
        _ensure_schema()
        cursor = _connection().cursor()
        cursor.execute(
            f'INSERT OR REPLACE INTO {_TABLE} '
            f'(cluster_id, vm_type, vmid, snapname, author, origin, requested_at, snaptime) '
            f'VALUES (?, ?, ?, ?, ?, ?, ?, NULL)',
            (key[0], key[1], key[2], key[3], (author or '').strip(), origin, int(time.time()))
        )
        _connection().commit()
    except Exception as e:
        # Metadata is an addition to the snapshot, never a precondition for it.
        _log.warning("[SnapshotMeta] could not record author for %s/%s: %s", vmid, snapname, e)


def forget(cluster_id: str, vm_type: str, vmid: Any, snapname: str) -> None:
    """Drop the row for a snapshot that is being deleted."""
    key = _normalise(cluster_id, vm_type, vmid, snapname)
    if key is None:
        return
    try:
        _ensure_schema()
        cursor = _connection().cursor()
        cursor.execute(
            f'DELETE FROM {_TABLE} '
            f'WHERE cluster_id = ? AND vm_type = ? AND vmid = ? AND snapname = ?',
            key
        )
        _connection().commit()
    except Exception as e:
        _log.warning("[SnapshotMeta] could not drop author for %s/%s: %s", vmid, snapname, e)


def _claimable(requested_at: int, snaptime: int) -> bool:
    return (requested_at - _CLOCK_SKEW_SECONDS) <= snaptime <= (requested_at + _CREATION_WINDOW_SECONDS)


def resolve(entries: Sequence[Tuple[str, str, Any, str, Any]]) -> Dict[_Key, Dict[str, str]]:
    """Look up author and origin for existing snapshots.

    `entries` are (cluster_id, vm_type, vmid, snapname, snaptime) tuples of
    snapshots that were just read from the hypervisor. One query for the whole
    batch, so a list of any length costs the same as a single row.
    """
    wanted: Dict[_Key, int] = {}
    for cluster_id, vm_type, vmid, snapname, snaptime in entries:
        key = _normalise(cluster_id, vm_type, vmid, snapname)
        if key is None:
            continue
        try:
            wanted[key] = int(snaptime)
        except (TypeError, ValueError):
            continue
    if not wanted:
        return {}

    try:
        _ensure_schema()
        cursor = _connection().cursor()
        clusters = sorted({key[0] for key in wanted})
        placeholders = ','.join('?' for _ in clusters)
        cursor.execute(
            f'SELECT cluster_id, vm_type, vmid, snapname, author, origin, requested_at, snaptime '
            f'FROM {_TABLE} WHERE cluster_id IN ({placeholders})',
            clusters
        )
        rows = cursor.fetchall()
    except Exception as e:
        _log.warning("[SnapshotMeta] could not read snapshot authors: %s", e)
        return {}

    now = int(time.time())
    found: Dict[_Key, Dict[str, str]] = {}
    claims: List[Tuple[int, str, str, int, str]] = []
    stale: List[_Key] = []

    for row in rows:
        key = _normalise(row[0], row[1], row[2], row[3])
        if key is None:
            continue
        author, origin, requested_at, bound = (row[4] or ''), row[5], int(row[6]), row[7]
        seen = wanted.get(key)

        if bound is None:
            # Not bound to a snapshot yet: bind it now if this is plausibly the
            # snapshot we asked for, drop it once the window has closed.
            if seen is not None and _claimable(requested_at, seen):
                claims.append((seen, key[0], key[1], key[2], key[3]))
                found[key] = {'author': author, 'origin': origin}
            elif now > requested_at + _CREATION_WINDOW_SECONDS:
                stale.append(key)
            continue

        if seen is not None and int(bound) == seen:
            found[key] = {'author': author, 'origin': origin}

    if claims or stale:
        try:
            cursor = _connection().cursor()
            for snaptime, cluster_id, vm_type, vmid, snapname in claims:
                cursor.execute(
                    f'UPDATE {_TABLE} SET snaptime = ? '
                    f'WHERE cluster_id = ? AND vm_type = ? AND vmid = ? AND snapname = ? '
                    f'AND snaptime IS NULL',
                    (snaptime, cluster_id, vm_type, vmid, snapname)
                )
            for key in stale:
                cursor.execute(
                    f'DELETE FROM {_TABLE} '
                    f'WHERE cluster_id = ? AND vm_type = ? AND vmid = ? AND snapname = ? '
                    f'AND snaptime IS NULL',
                    key
                )
            _connection().commit()
        except Exception as e:
            _log.warning("[SnapshotMeta] could not bind snapshot authors: %s", e)

    return found


def annotate_snapshots(cluster_id: str, vm_type: str, vmid: Any,
                       snapshots: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Add `author` / `author_origin` to PVE snapshot dicts of one guest."""
    items = [s for s in (snapshots or []) if isinstance(s, dict)]
    if not items:
        return items
    meta = resolve([
        (cluster_id, vm_type, vmid, s.get('name'), s.get('snaptime'))
        for s in items
    ])
    for snap in items:
        key = _normalise(cluster_id, vm_type, vmid, snap.get('name'))
        entry = meta.get(key) if key else None
        snap['author'] = entry['author'] if entry else ''
        snap['author_origin'] = entry['origin'] if entry else ''
    return items


def annotate_rows(rows: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Add `author` / `author_origin` to aggregated overview rows.

    Rows carry the guest they belong to, so an overview spanning clusters is
    still one query — the overview must not grow a lookup per table row.
    """
    items = [r for r in (rows or []) if isinstance(r, dict)]
    if not items:
        return items
    meta = resolve([
        (r.get('cluster_id'), r.get('vm_type', 'qemu'), r.get('vmid'),
         r.get('snapshot_name'), r.get('snaptime'))
        for r in items
    ])
    for row in items:
        key = _normalise(row.get('cluster_id'), row.get('vm_type', 'qemu'),
                         row.get('vmid'), row.get('snapshot_name'))
        entry = meta.get(key) if key else None
        row['author'] = entry['author'] if entry else ''
        row['author_origin'] = entry['origin'] if entry else ''
    return items


def annotate_efficient(rows: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Expose an efficient snapshot's own `created_by` under the shared keys.

    Efficient snapshots are ours from the start and already store who made
    them, so there is nothing to look up — they only need to answer to the
    same two keys as a PVE snapshot, so one display rule covers both kinds.
    """
    items = [r for r in (rows or []) if isinstance(r, dict)]
    for row in items:
        author = (row.get('created_by') or '').strip()
        row['author'] = author
        row['author_origin'] = ORIGIN_USER if author else ''
    return items
