"""Persistence for the Hyper-V migration source.

Two things have to outlive the process, for different reasons.

**Identity.** Hyper-V names a VM with a GUID; PegaProx, its API and its RBAC layer expect
an integer VMID, and refuse or silently drop anything else. So each Hyper-V VM gets a
synthetic integer, allocated once per host and never reused, exactly as XCP-ng UUIDs do.
The mapping has to be durable or a restart would renumber every VM and every access-control
entry written against the old numbers would point somewhere else.

**Migration state.** A migration creates real, costly things on the target — a VM, storage
volumes — and the existing XHM engine keeps all of that in a process-local dict. When the
process restarts mid-transfer, the record is gone and the half-built target is left with
nobody to tell you it exists. This module records what has actually been created, so after
a crash the answer to "what happened and what is safe to do now" comes from the database
rather than from a person guessing.

The tables are created here and wired into the main schema with a single call, so this
file stays the only place that knows their shape.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
import uuid

logger = logging.getLogger(__name__)

# Synthetic VMIDs start above the Proxmox convention of 100 so a Hyper-V VM never looks
# like a low-numbered local guest in a log line.
_FIRST_SYNTHETIC_VMID = 100

# How many times to retry an allocation that lost a race. Two writers picking the same
# next id is normal under concurrent inventory walks; the unique index rejects the loser,
# which then simply picks again.
_ALLOCATION_ATTEMPTS = 8

# Terminal states. A migration in any other state was interrupted rather than finished.
STATUS_RUNNING = 'running'
STATUS_COMPLETED = 'completed'
STATUS_FAILED = 'failed'
STATUS_INTERRUPTED = 'interrupted'
_TERMINAL_STATUSES = (STATUS_COMPLETED, STATUS_FAILED)


def ensure_schema(cursor) -> None:
    """Create the Hyper-V tables if they are not there yet.

    Idempotent and safe on every start, following the pattern the rest of the schema uses.
    Called once from the main schema setup so this module owns the definitions.
    """
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS hyperv_vmid_map (
            cluster_id TEXT NOT NULL,
            vm_guid TEXT NOT NULL,
            vmid INTEGER NOT NULL,
            vm_name TEXT DEFAULT '',
            first_seen REAL NOT NULL,
            PRIMARY KEY (cluster_id, vm_guid)
        )
    ''')
    # Two VMs on one host must never share a synthetic id: that would silently merge their
    # access-control entries.
    cursor.execute('''
        CREATE UNIQUE INDEX IF NOT EXISTS idx_hyperv_vmid
        ON hyperv_vmid_map(cluster_id, vmid)
    ''')
    # The allocation counter is separate from the mapping on purpose, and only ever goes
    # up. Deriving the next id from MAX(vmid) would hand a deleted VM's number to the next
    # new one, and with it every access-control entry written against that number.
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS hyperv_vmid_sequence (
            cluster_id TEXT PRIMARY KEY,
            next_vmid INTEGER NOT NULL
        )
    ''')

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS hyperv_migrations (
            migration_id TEXT PRIMARY KEY,
            source_cluster TEXT NOT NULL,
            source_vm_guid TEXT NOT NULL,
            source_vm_name TEXT DEFAULT '',
            target_cluster TEXT DEFAULT '',
            target_node TEXT DEFAULT '',
            target_storage TEXT DEFAULT '',
            target_vmid INTEGER,
            phase TEXT DEFAULT 'planning',
            status TEXT DEFAULT 'running',
            progress INTEGER DEFAULT 0,
            error TEXT DEFAULT '',
            created_resources TEXT DEFAULT '[]',
            disk_progress TEXT DEFAULT '{}',
            started_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            completed_at REAL
        )
    ''')
    cursor.execute('''
        CREATE INDEX IF NOT EXISTS idx_hyperv_migrations_status
        ON hyperv_migrations(status, updated_at)
    ''')

    # The shared cluster table has a fixed set of columns and drops anything it does not
    # know, so a Hyper-V-only setting saved there would be silently gone after the next
    # restart. These live here instead, where this patch owns the schema and upstream is
    # never asked to widen a table for a fork's feature.
    # One migration at a time per source VM. The primary key is the guarantee: two
    # requests that both believe the VM is free cannot both insert, so the loser is told
    # rather than starting a second transfer of the same disks into a second target.
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS hyperv_migration_claims (
            source_cluster TEXT NOT NULL,
            source_vm_guid TEXT NOT NULL,
            migration_id TEXT NOT NULL,
            claimed_at REAL NOT NULL,
            PRIMARY KEY (source_cluster, source_vm_guid)
        )
    ''')

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS hyperv_host_settings (
            cluster_id TEXT PRIMARY KEY,
            winrm_port INTEGER,
            verify_certificate INTEGER DEFAULT 1,
            iso_library_paths TEXT DEFAULT '[]',
            smb_share_map TEXT DEFAULT '{}',
            smb_domain TEXT DEFAULT '',
            updated_at REAL NOT NULL
        )
    ''')
    logger.debug('Ensured Hyper-V tables exist')


# ---------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------

def _next_in_sequence(conn, cluster_id: str) -> int:
    """Hand out the next never-yet-used VMID for a host.

    Monotonic by construction: the counter is stored, not derived from the rows that
    happen to exist. A VM removed from the inventory therefore takes its number out of
    circulation for good, so a later VM cannot inherit access-control entries that were
    written for the old one.

    SQLite serialises writers, so the increment-then-read pair is atomic enough for the
    concurrent inventory walks this sees; the unique index on (cluster_id, vmid) is the
    backstop if it ever is not.
    """
    cursor = conn.cursor()
    cursor.execute('INSERT OR IGNORE INTO hyperv_vmid_sequence (cluster_id, next_vmid) '
                   'VALUES (?, ?)', (cluster_id, _FIRST_SYNTHETIC_VMID))
    cursor.execute('SELECT next_vmid FROM hyperv_vmid_sequence WHERE cluster_id = ?',
                   (cluster_id,))
    candidate = int(cursor.fetchone()['next_vmid'])
    cursor.execute('UPDATE hyperv_vmid_sequence SET next_vmid = ? WHERE cluster_id = ?',
                   (candidate + 1, cluster_id))
    conn.commit()
    return candidate


def peek_next_vmid(conn, cluster_id: str) -> int:
    """Say which VMID would be handed out next, without handing it out.

    Read-only on purpose: this answers a question the UI asks while drawing a form, and a
    page load must not consume a number from a monotonic sequence.
    """
    cursor = conn.cursor()
    cursor.execute('SELECT next_vmid FROM hyperv_vmid_sequence WHERE cluster_id = ?',
                   (cluster_id,))
    row = cursor.fetchone()
    return int(row['next_vmid']) if row else _FIRST_SYNTHETIC_VMID


def get_vmid(conn, cluster_id: str, vm_guid: str, vm_name: str = '') -> int:
    """Return this VM's synthetic VMID, allocating one the first time it is seen.

    Stable for the life of the VM on that host: the GUID is what Hyper-V keeps constant,
    so renaming a VM or moving it in the inventory does not renumber it, and two VMs with
    the same name on different hosts cannot be confused because each host is its own
    cluster_id.
    """
    cursor = conn.cursor()
    cursor.execute('SELECT vmid FROM hyperv_vmid_map WHERE cluster_id = ? AND vm_guid = ?',
                   (cluster_id, vm_guid))
    row = cursor.fetchone()
    if row:
        if vm_name:
            # The name is carried only so a human reading the table can tell rows apart;
            # nothing resolves by it.
            cursor.execute('UPDATE hyperv_vmid_map SET vm_name = ? '
                           'WHERE cluster_id = ? AND vm_guid = ?',
                           (vm_name, cluster_id, vm_guid))
            conn.commit()
        return int(row['vmid'])

    for _ in range(_ALLOCATION_ATTEMPTS):
        candidate = _next_in_sequence(conn, cluster_id)
        try:
            cursor.execute(
                'INSERT INTO hyperv_vmid_map (cluster_id, vm_guid, vmid, vm_name, first_seen) '
                'VALUES (?, ?, ?, ?, ?)',
                (cluster_id, vm_guid, candidate, vm_name, time.time()))
            conn.commit()
            return candidate
        except sqlite3.IntegrityError:
            # Either another writer took this id, or it took this GUID. Re-read: if the
            # GUID now has a row, that writer did our work for us.
            conn.rollback()
            cursor.execute('SELECT vmid FROM hyperv_vmid_map WHERE cluster_id = ? AND vm_guid = ?',
                           (cluster_id, vm_guid))
            row = cursor.fetchone()
            if row:
                return int(row['vmid'])

    raise RuntimeError(
        f'Could not allocate a VMID for a Hyper-V VM on {cluster_id} after '
        f'{_ALLOCATION_ATTEMPTS} attempts.')


def resolve_vmid(conn, cluster_id: str, vmid) -> str | None:
    """Turn a synthetic VMID back into the Hyper-V GUID, or None if it is not ours."""
    try:
        numeric = int(vmid)
    except (TypeError, ValueError):
        return None
    cursor = conn.cursor()
    cursor.execute('SELECT vm_guid FROM hyperv_vmid_map WHERE cluster_id = ? AND vmid = ?',
                   (cluster_id, numeric))
    row = cursor.fetchone()
    return row['vm_guid'] if row else None


def forget_host(conn, cluster_id: str) -> int:
    """Drop every mapping for a host that is being removed. Returns the row count.

    Called when a Hyper-V connection is deleted, so the table does not accumulate rows for
    hosts nobody can reach any more.
    """
    cursor = conn.cursor()
    cursor.execute('DELETE FROM hyperv_vmid_map WHERE cluster_id = ?', (cluster_id,))
    conn.commit()
    # The sequence row stays. If the same host is added back, its VMs start above the
    # numbers the previous connection handed out rather than reusing them.
    return cursor.rowcount


# ---------------------------------------------------------------------------
# Migration state
# ---------------------------------------------------------------------------

def create_migration(conn, *, source_cluster: str, source_vm_guid: str, source_vm_name: str = '',
                     target_cluster: str = '', target_node: str = '', target_storage: str = '',
                     migration_id: str | None = None) -> str:
    """Record a migration before any work starts, and return its id.

    Written first on purpose. A migration that dies between "started" and its first
    progress update still leaves a row saying which VM was being moved and where to look.
    """
    mid = migration_id or str(uuid.uuid4())[:8]
    now = time.time()
    conn.cursor().execute(
        'INSERT INTO hyperv_migrations (migration_id, source_cluster, source_vm_guid, '
        'source_vm_name, target_cluster, target_node, target_storage, started_at, updated_at) '
        'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
        (mid, source_cluster, source_vm_guid, source_vm_name, target_cluster, target_node,
         target_storage, now, now))
    conn.commit()
    return mid


def update_migration(conn, migration_id: str, **fields) -> None:
    """Update a migration's live state. Unknown field names are refused, not ignored.

    The allowlist exists because the column names would otherwise be interpolated into SQL
    from a caller's dictionary keys.
    """
    allowed = {'phase', 'status', 'progress', 'error', 'target_vmid', 'target_cluster',
               'target_node', 'target_storage', 'completed_at'}
    unknown = set(fields) - allowed
    if unknown:
        raise ValueError(f'Not migration fields: {sorted(unknown)}')
    if not fields:
        return

    fields['updated_at'] = time.time()
    if fields.get('status') in _TERMINAL_STATUSES and 'completed_at' not in fields:
        fields['completed_at'] = fields['updated_at']

    assignments = ', '.join(f'{name} = ?' for name in fields)
    conn.cursor().execute(
        f'UPDATE hyperv_migrations SET {assignments} WHERE migration_id = ?',
        (*fields.values(), migration_id))
    conn.commit()


def record_created_resource(conn, migration_id: str, kind: str, identifier: str,
                            detail: str = '') -> None:
    """Append something this migration created on the target.

    This is the list that makes an interrupted migration recoverable by a human: it says
    what exists now that did not exist before, so a cleanup removes exactly those things
    and nothing belonging to anybody else.
    """
    cursor = conn.cursor()
    cursor.execute('SELECT created_resources FROM hyperv_migrations WHERE migration_id = ?',
                   (migration_id,))
    row = cursor.fetchone()
    if row is None:
        raise KeyError(f'No such migration: {migration_id}')

    resources = json.loads(row['created_resources'] or '[]')
    resources.append({'kind': kind, 'id': identifier, 'detail': detail, 'at': time.time()})
    cursor.execute('UPDATE hyperv_migrations SET created_resources = ?, updated_at = ? '
                   'WHERE migration_id = ?',
                   (json.dumps(resources), time.time(), migration_id))
    conn.commit()


def forget_created_resource(conn, migration_id: str, kind: str, identifier: str) -> None:
    """Drop one entry, because the thing it names has been removed again.

    A failed conversion frees its volume and starts over on a fresh one. Leaving the freed
    volume in the list would make a later cleanup look for something that is gone and would
    tell an operator that leftovers exist when they do not.
    """
    cursor = conn.cursor()
    cursor.execute('SELECT created_resources FROM hyperv_migrations WHERE migration_id = ?',
                   (migration_id,))
    row = cursor.fetchone()
    if row is None:
        return
    resources = [entry for entry in json.loads(row['created_resources'] or '[]')
                 if not (entry.get('kind') == kind and entry.get('id') == identifier)]
    cursor.execute('UPDATE hyperv_migrations SET created_resources = ?, updated_at = ? '
                   'WHERE migration_id = ?',
                   (json.dumps(resources), time.time(), migration_id))
    conn.commit()


def set_disk_progress(conn, migration_id: str, disk_key: str, copied: int, total: int) -> None:
    """Record how far one disk has got, so a restart can say where it stopped."""
    cursor = conn.cursor()
    cursor.execute('SELECT disk_progress FROM hyperv_migrations WHERE migration_id = ?',
                   (migration_id,))
    row = cursor.fetchone()
    if row is None:
        raise KeyError(f'No such migration: {migration_id}')

    progress = json.loads(row['disk_progress'] or '{}')
    pct = round(copied / total * 100, 1) if total else 0.0
    progress[disk_key] = {'copied': copied, 'total': total, 'pct': pct}
    cursor.execute('UPDATE hyperv_migrations SET disk_progress = ?, updated_at = ? '
                   'WHERE migration_id = ?',
                   (json.dumps(progress), time.time(), migration_id))
    conn.commit()


def get_migration(conn, migration_id: str) -> dict | None:
    """One migration as a plain dict, with its JSON columns already decoded."""
    cursor = conn.cursor()
    cursor.execute('SELECT * FROM hyperv_migrations WHERE migration_id = ?', (migration_id,))
    row = cursor.fetchone()
    return _row_to_dict(row) if row else None


def list_migrations(conn, limit: int = 100) -> list[dict]:
    """Recent migrations, newest first."""
    cursor = conn.cursor()
    cursor.execute('SELECT * FROM hyperv_migrations ORDER BY started_at DESC LIMIT ?',
                   (int(limit),))
    return [_row_to_dict(row) for row in cursor.fetchall()]


def mark_interrupted_migrations(conn) -> list[dict]:
    """Flag migrations that were still running when the process stopped.

    Called once at startup. A row left in 'running' has no worker behind it any more, and
    saying so is the difference between a migration that looks alive and one that is
    honestly reported as interrupted with a list of what it had already created.
    """
    stale = [row for row in list_migrations(conn, limit=1000)
             if row['status'] == STATUS_RUNNING]
    for row in stale:
        update_migration(
            conn, row['migration_id'],
            status=STATUS_INTERRUPTED,
            error='The PegaProx process stopped while this migration was running. '
                  'Nothing was rolled back; the resources it had already created are listed.',
        )
        logger.warning('Hyper-V migration %s was interrupted by a restart in phase %s',
                       row['migration_id'], row['phase'])
    return stale


def migrations_for_cluster(conn, source_cluster: str, limit: int = 100) -> list[dict]:
    """Recent migrations that started from one host, newest first."""
    cursor = conn.cursor()
    cursor.execute('SELECT * FROM hyperv_migrations WHERE source_cluster = ? '
                   'ORDER BY started_at DESC LIMIT ?', (source_cluster, int(limit)))
    return [_row_to_dict(row) for row in cursor.fetchall()]


def clear_created_resources(conn, migration_id: str) -> None:
    """Forget what a migration created, once it has actually been removed.

    Called only by a cleanup that succeeded. The list is what makes leftovers findable, so
    emptying it while they still exist would hide them instead of removing them.
    """
    conn.cursor().execute(
        'UPDATE hyperv_migrations SET created_resources = ?, updated_at = ? '
        'WHERE migration_id = ?', (json.dumps([]), time.time(), migration_id))
    conn.commit()


# ---------------------------------------------------------------------------
# One migration at a time per source VM
# ---------------------------------------------------------------------------

def claim_source(conn, source_cluster: str, vm_guid: str, migration_id: str) -> str | None:
    """Take the right to migrate this VM, or say who already holds it.

    Returns None when the claim is now held by `migration_id`, and the holder's migration
    id when somebody else has it. The insert is the decision, not the check before it: two
    callers asking "is it free?" at the same moment both get yes, and only one of them can
    then insert the primary key.
    """
    _drop_finished_claims(conn, source_cluster, vm_guid)
    try:
        conn.cursor().execute(
            'INSERT INTO hyperv_migration_claims '
            '(source_cluster, source_vm_guid, migration_id, claimed_at) VALUES (?, ?, ?, ?)',
            (source_cluster, vm_guid, migration_id, time.time()))
        conn.commit()
        return None
    except sqlite3.IntegrityError:
        holder = active_claim(conn, source_cluster, vm_guid)
        return holder['migration_id'] if holder else None


def release_source(conn, migration_id: str) -> None:
    """Give the claim back. Safe to call when there is none, and on every exit path."""
    conn.cursor().execute('DELETE FROM hyperv_migration_claims WHERE migration_id = ?',
                          (migration_id,))
    conn.commit()


def active_claim(conn, source_cluster: str, vm_guid: str) -> dict | None:
    """The live claim on this VM, or None. A claim whose migration ended is not live."""
    _drop_finished_claims(conn, source_cluster, vm_guid)
    cursor = conn.cursor()
    cursor.execute('SELECT * FROM hyperv_migration_claims '
                   'WHERE source_cluster = ? AND source_vm_guid = ?',
                   (source_cluster, vm_guid))
    row = cursor.fetchone()
    return dict(row) if row else None


def _drop_finished_claims(conn, source_cluster: str, vm_guid: str) -> None:
    """Remove a claim whose migration is over, however it ended.

    A claim outlives its migration when the process dies between the last write and the
    release. Deciding liveness from the migration's own status rather than from the claim
    row means a restart cannot leave a VM permanently unmigratable.
    """
    cursor = conn.cursor()
    cursor.execute('SELECT migration_id FROM hyperv_migration_claims '
                   'WHERE source_cluster = ? AND source_vm_guid = ?',
                   (source_cluster, vm_guid))
    row = cursor.fetchone()
    if row is None:
        return
    migration = get_migration(conn, row['migration_id'])
    if migration is None or migration['status'] != STATUS_RUNNING:
        release_source(conn, row['migration_id'])


def _row_to_dict(row) -> dict:
    """sqlite3.Row to dict, decoding the two JSON columns."""
    data = dict(row)
    for column, empty in (('created_resources', []), ('disk_progress', {})):
        try:
            data[column] = json.loads(data.get(column) or json.dumps(empty))
        except (TypeError, ValueError):
            # A corrupt cell must not make the whole migration unreadable; the rest of the
            # row is still the useful part.
            logger.warning('Unreadable %s on Hyper-V migration %s', column,
                           data.get('migration_id'))
            data[column] = empty
    return data


# ---------------------------------------------------------------------------
# Host settings the shared cluster table has no column for
# ---------------------------------------------------------------------------

def save_host_settings(conn, cluster_id: str, *, winrm_port: int | None = None,
                       verify_certificate: bool = True,
                       iso_library_paths: list | None = None,
                       smb_share_map: dict | None = None,
                       smb_domain: str = '') -> None:
    """Persist the Hyper-V-only part of a host's configuration.

    No credential is ever written here. This table is not encrypted, and the account that
    reaches a Hyper-V host already lives in the shared cluster row, which is.
    """
    conn.execute(
        'INSERT OR REPLACE INTO hyperv_host_settings '
        '(cluster_id, winrm_port, verify_certificate, iso_library_paths, smb_share_map, '
        'smb_domain, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)',
        (cluster_id, winrm_port, 1 if verify_certificate else 0,
         json.dumps(list(iso_library_paths or [])), json.dumps(dict(smb_share_map or {})),
         smb_domain or '', time.time()))
    conn.commit()


def load_host_settings(conn, cluster_id: str) -> dict:
    """What was saved for this host, or empty defaults when nothing was.

    Empty defaults rather than None: a caller merging this into a config dict should not
    have to distinguish "never configured" from "configured as empty", because both mean
    the same thing to everything downstream.
    """
    row = conn.execute(
        'SELECT winrm_port, verify_certificate, iso_library_paths, smb_share_map, smb_domain '
        'FROM hyperv_host_settings WHERE cluster_id = ?', (cluster_id,)).fetchone()
    if not row:
        return {}
    return {
        'port': row['winrm_port'],
        'ssl_verification': bool(row['verify_certificate']),
        'iso_library_paths': _decode_json(row['iso_library_paths'], [], cluster_id,
                                          'ISO library'),
        'smb_share_map': _decode_json(row['smb_share_map'], {}, cluster_id, 'share map'),
        'smb_domain': row['smb_domain'] or '',
    }


def _decode_json(raw, fallback, cluster_id: str, label: str):
    """Decode a stored JSON setting, falling back rather than taking the host offline.

    An empty library or an unmapped drive is visible in the UI and fixable. An exception
    during start-up is neither, and it would take down every other host in the list with it.
    """
    try:
        return json.loads(raw or json.dumps(fallback))
    except (TypeError, ValueError):
        logger.warning('Unreadable %s setting for Hyper-V host %s', label, cluster_id)
        return fallback


def forget_host_settings(conn, cluster_id: str) -> None:
    """Drop a removed host's settings."""
    conn.execute('DELETE FROM hyperv_host_settings WHERE cluster_id = ?', (cluster_id,))
    conn.commit()
