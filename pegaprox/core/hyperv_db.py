"""Persistence for the Hyper-V migration source.

Three things have to outlive the process, for different reasons.

**The hosts themselves.** A Hyper-V host is a migration source, not a cluster, so it is
not written into the cluster configuration — exactly as an ESXi host is held in its own
table rather than among the clusters (see docs/adr/0001). Keeping it here rather than in
`vmware_servers` is deliberate: that table is read by `load_vmware_servers()`, which turns
every row it finds into a pyVmomi-backed manager, and its columns are cut for vSphere.

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
import time
import uuid

# Deliberately not the one from sqlite3. PegaProx opens its database through dbcrypto,
# which is sqlcipher3 where that is available and plain SQLite otherwise — and the two
# raise different classes for the same constraint violation. Catching the sqlite3 one
# therefore misses every violation on an encrypted database, which is the configuration
# that actually ships. Measured: the guard against two migrations of one VM let the raw
# error through instead of naming the migration that already holds the source.
from pegaprox.core.dbcrypto import IntegrityError
from pegaprox.core.hyperv_client import (
    DEFAULT_AUTH_METHOD, DEFAULT_MAX_SESSIONS, default_winrm_port, parse_max_sessions,
)

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
#: The VM exists and its disks are complete, but a step after the copy did not do what was
#: asked — the driver injection, typically. Not `completed`: that reads as "nothing left to
#: do" and sent an operator to a guest that booted into recovery. Not `failed` either: the
#: expensive half is done and nothing may be thrown away over it.
STATUS_COMPLETED_WITH_ERRORS = 'completed_with_errors'
_TERMINAL_STATUSES = (STATUS_COMPLETED, STATUS_COMPLETED_WITH_ERRORS, STATUS_FAILED)


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
            post_import TEXT DEFAULT '{}',
            started_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            completed_at REAL
        )
    ''')
    cursor.execute('''
        CREATE INDEX IF NOT EXISTS idx_hyperv_migrations_status
        ON hyperv_migrations(status, updated_at)
    ''')

    # What happened to the VM after it arrived: the driver ISO, somebody's confirmation
    # that the drivers went in, the profile it was switched to. Added after the table
    # existed, so a database from before this feature needs the column adding rather than
    # the table creating -- CREATE TABLE IF NOT EXISTS would leave it untouched.
    cursor.execute('PRAGMA table_info(hyperv_migrations)')
    if 'post_import' not in [column[1] for column in cursor.fetchall()]:
        cursor.execute("ALTER TABLE hyperv_migrations ADD COLUMN post_import TEXT DEFAULT '{}'")
        logger.info('Added post_import column to hyperv_migrations')

    # The migration's own log. It used to live only in the process, which meant a
    # restart took it with it — and the log is the part an operator reads to find out what
    # happened, long after the run. Same shape of addition as post_import above: a
    # database from before this needs the column adding, not the table creating.
    cursor.execute('PRAGMA table_info(hyperv_migrations)')
    if 'log_lines' not in [column[1] for column in cursor.fetchall()]:
        cursor.execute("ALTER TABLE hyperv_migrations ADD COLUMN log_lines TEXT DEFAULT '[]'")
        logger.info('Added log_lines column to hyperv_migrations')

    # Everything else the migration list shows about a run: the phase timeline, what was
    # chosen in the wizard, how far it got. Without it a record read back after a restart
    # was a name, a date and a status - a failed run could not be told from a finished one
    # by anything but its colour. Kept as the run's own snapshot rather than a column per
    # field, so a field the list gains later is recorded without another migration.
    cursor.execute('PRAGMA table_info(hyperv_migrations)')
    if 'snapshot' not in [column[1] for column in cursor.fetchall()]:
        cursor.execute("ALTER TABLE hyperv_migrations ADD COLUMN snapshot TEXT DEFAULT '{}'")
        logger.info('Added snapshot column to hyperv_migrations')

    # Same reasoning, one step further: an early build of this patch named the key column
    # host_id, and CREATE TABLE IF NOT EXISTS leaves an existing table alone. Without this
    # the host routes fail with "no such column: id" on any database created by that build.
    cursor.execute('PRAGMA table_info(hyperv_hosts)')
    host_columns = [column[1] for column in cursor.fetchall()]
    if host_columns and 'id' not in host_columns and 'host_id' in host_columns:
        cursor.execute('ALTER TABLE hyperv_hosts RENAME COLUMN host_id TO id')
        logger.info('Renamed hyperv_hosts.host_id to id')

    # The transport used to be fixed: HTTPS, NTLM, message encryption left to pypsrp. A
    # host registered before these columns existed was therefore reached over HTTPS
    # whatever its port, so the rows that are already there get use_ssl=1 rather than the
    # column default -- otherwise every existing source would silently switch to HTTP on
    # the first restart after the upgrade and fail against a listener it never used.
    if host_columns and 'use_ssl' not in host_columns:
        cursor.execute('ALTER TABLE hyperv_hosts ADD COLUMN use_ssl INTEGER DEFAULT 0')
        cursor.execute('UPDATE hyperv_hosts SET use_ssl = 1')
        logger.info('Added use_ssl column to hyperv_hosts; existing hosts keep HTTPS')
    if host_columns and 'auth' not in host_columns:
        cursor.execute("ALTER TABLE hyperv_hosts ADD COLUMN auth TEXT DEFAULT 'negotiate'")
        # Same reasoning: those rows authenticated with NTLM, so they keep doing that.
        cursor.execute("UPDATE hyperv_hosts SET auth = 'ntlm'")
        logger.info('Added auth column to hyperv_hosts; existing hosts keep NTLM')
    if host_columns and 'encrypt_messages' not in host_columns:
        cursor.execute('ALTER TABLE hyperv_hosts ADD COLUMN encrypt_messages INTEGER DEFAULT 1')
        logger.info('Added encrypt_messages column to hyperv_hosts')
    # Empty is the honest default here, and it means "the same address as WinRM" -- which
    # is what every host registered before this column did.
    if host_columns and 'transfer_host' not in host_columns:
        cursor.execute("ALTER TABLE hyperv_hosts ADD COLUMN transfer_host TEXT DEFAULT ''")
        logger.info('Added transfer_host column to hyperv_hosts')
    # Hosts registered before this column talked to their host over one session. They get
    # the new default rather than keeping one: a single session is what queued every user
    # of a host behind every other, and nothing about an existing host asked for that.
    if host_columns and 'max_sessions' not in host_columns:
        cursor.execute(f'ALTER TABLE hyperv_hosts ADD COLUMN max_sessions INTEGER '
                       f'DEFAULT {DEFAULT_MAX_SESSIONS}')
        logger.info('Added max_sessions column to hyperv_hosts')
    if host_columns and 'transfer_check' not in host_columns:
        cursor.execute("ALTER TABLE hyperv_hosts ADD COLUMN transfer_check TEXT DEFAULT '{}'")
        logger.info('Added transfer_check column to hyperv_hosts')

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
        CREATE TABLE IF NOT EXISTS hyperv_hosts (
            -- Named `id` rather than `host_id` so the key-rotation pass in db.py finds it:
            -- that loop selects `id` and updates `WHERE id = ?` for every table holding an
            -- encrypted column. A differently named key would rotate nothing and leave the
            -- password unreadable after the next rotation.
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            host TEXT NOT NULL,
            username TEXT NOT NULL DEFAULT '',
            pass_encrypted TEXT DEFAULT '',
            winrm_port INTEGER DEFAULT 5985,
            use_ssl INTEGER DEFAULT 0,
            auth TEXT DEFAULT 'negotiate',
            encrypt_messages INTEGER DEFAULT 1,
            verify_certificate INTEGER DEFAULT 1,
            iso_library_paths TEXT DEFAULT '[]',
            smb_share_map TEXT DEFAULT '{}',
            smb_domain TEXT DEFAULT '',
            -- Where the target node reaches the disk share, when that is not where
            -- PegaProx reaches WinRM. Empty means the same address. See issue #15:
            -- management runs over an admin interface that is often 1 GbE, while the
            -- transfer should take the 10 GbE path the backups already use.
            transfer_host TEXT DEFAULT '',
            -- What a real mount from a target node last found. A host fact, not a VM fact:
            -- whether cifs-utils is installed, whether TCP 445 is reachable and whether the
            -- account may read the share is the same answer for all 159 guests. Asking it
            -- per VM, and asking somebody to confirm the answer per VM, is what made the
            -- wizard's longest warning the one it repeated most (issue #15).
            transfer_check TEXT DEFAULT '{}',
            -- How many sessions PegaProx keeps to this host at most (docs/adr/0006).
            max_sessions INTEGER DEFAULT 4,
            enabled INTEGER DEFAULT 1,
            created_at REAL NOT NULL,
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
        except IntegrityError:
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
               'target_node', 'target_storage', 'completed_at', 'source_vm_guid',
               'source_vm_name'}
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


def set_post_import(conn, migration_id: str, **fields) -> dict:
    """Merge facts about what happened to the VM after the import into its row.

    Merged rather than replaced, because the two post-import steps are independent: a
    profile switch must not erase who confirmed the drivers, and the confirmation is the
    evidence for the switch. A field set to None is removed.
    """
    row = get_migration(conn, migration_id)
    if row is None:
        raise ValueError(f'No such migration: {migration_id}')
    state = dict(row.get('post_import') or {})
    for name, value in fields.items():
        if value is None:
            state.pop(name, None)
        else:
            state[name] = value
    conn.cursor().execute(
        'UPDATE hyperv_migrations SET post_import = ?, updated_at = ? WHERE migration_id = ?',
        (json.dumps(state), time.time(), migration_id))
    conn.commit()
    return state


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


#: How much of a migration's log is kept with its record. The whole log, in practice: an
#: import writes a few lines per disk and per phase, so a run stays in the hundreds. The
#: bound is only there so that a loop logging without end cannot fill the database.
MAX_RECORDED_LOG_LINES = 10000


def save_log(conn, migration_id: str, lines, snapshot: dict | None = None) -> None:
    """Write the migration's log - and, when given, its snapshot - into its record.

    Called for every line the run logs and at every phase change, so what a record says
    is what the run had said when the process stopped, not what it had said at its last
    phase boundary. A transfer logs a handful of lines per disk and reports its progress
    through another path, so this stays a few dozen small writes per migration.
    """
    kept = [str(line) for line in (lines or [])][-MAX_RECORDED_LOG_LINES:]
    if snapshot is None:
        conn.cursor().execute(
            'UPDATE hyperv_migrations SET log_lines = ?, updated_at = ? WHERE migration_id = ?',
            (json.dumps(kept), time.time(), migration_id))
    else:
        conn.cursor().execute(
            'UPDATE hyperv_migrations SET log_lines = ?, snapshot = ?, updated_at = ? '
            'WHERE migration_id = ?',
            (json.dumps(kept), json.dumps(snapshot, default=str), time.time(), migration_id))
    conn.commit()


def clear_created_resources(conn, migration_id: str) -> None:
    """Forget what a migration created, once it has actually been removed.

    Called only by a cleanup that succeeded. The list is what makes leftovers findable, so
    emptying it while they still exist would hide them instead of removing them.
    """
    conn.cursor().execute(
        'UPDATE hyperv_migrations SET created_resources = ?, updated_at = ? '
        'WHERE migration_id = ?', (json.dumps([]), time.time(), migration_id))
    conn.commit()


def delete_migration(conn, migration_id: str) -> None:
    """Remove one finished migration's record entirely.

    Only the record goes; nothing on the target is touched.
    """
    conn.cursor().execute('DELETE FROM hyperv_migrations WHERE migration_id = ?',
                          (migration_id,))
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
    except IntegrityError:
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
    """A database row to dict, decoding the JSON columns."""
    data = dict(row)
    for column, empty in (('created_resources', []), ('disk_progress', {}),
                          ('post_import', {}), ('log_lines', []), ('snapshot', {})):
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
# The registered hosts
# ---------------------------------------------------------------------------

def save_transfer_check(conn, host_id: str, result: dict) -> None:
    """Record what a real mount from a target node found.

    Written on its own rather than through save_host: that one persists a form somebody
    filled in, and a measurement is not a setting. Keeping them apart also means saving
    the form cannot silently discard a check, and a check cannot rewrite a password.
    """
    conn.execute('UPDATE hyperv_hosts SET transfer_check = ?, updated_at = ? WHERE id = ?',
                 (json.dumps(result or {}), time.time(), host_id))
    conn.commit()


def save_host(conn, encrypt, host_id: str, data: dict) -> None:
    """Write one host. `encrypt` is the database's own encryption callable.

    The password is the only field that goes through it, and it is only rewritten when a
    new one was actually submitted — an edit that leaves the field blank keeps the stored
    credential rather than silently clearing it, which is what a form that never echoes a
    password back has to do.
    """
    existing = conn.execute(
        'SELECT pass_encrypted, created_at, transfer_check, host, transfer_host, username, '
        'smb_share_map, smb_domain FROM hyperv_hosts WHERE id = ?',
        (host_id,)).fetchone()

    password = data.get('pass') or data.get('pass_') or ''
    if password:
        stored = encrypt(password)
    elif existing:
        stored = existing['pass_encrypted']
    else:
        stored = ''

    # INSERT OR REPLACE writes the whole row, so a column left out of the statement goes
    # back to its default — and the measured transfer check would be silently erased every
    # time somebody saved the form. Carried forward here, but only while it still describes
    # what it measured: a host reached at a different address, by a different account or
    # through a different share is a host nothing has checked yet, and keeping a green
    # result over that would be worse than having none.
    transfer_check = '{}'
    if existing:
        moved = (
            (data.get('host') or '') != (existing['host'] or '')
            or (data.get('transfer_host') or '').strip() != (existing['transfer_host'] or '')
            or (data.get('user') or data.get('username') or '') != (existing['username'] or '')
            or (dict(data.get('smb_share_map') or {})
                != _decode_json(existing['smb_share_map'] or '{}', {}, host_id, 'share map'))
            or (data.get('smb_domain') or '') != (existing['smb_domain'] or '')
            # A NEW password invalidates the measurement; the stored one being carried
            # forward does not. `update_hyperv_host` fills `pass` from the existing record
            # when the form left it blank, so `password` is truthy on every edit and
            # testing it alone discarded the check on a rename. The ciphertext cannot tell
            # them apart either — Fernet is not deterministic, so re-encrypting the same
            # password produces a different string every time. The caller is the only one
            # that knows, so the caller says so.
            or bool(data.get('_password_submitted', password))
        )
        if not moved:
            transfer_check = existing['transfer_check'] or '{}'

    use_ssl = bool(data.get('use_ssl', False))
    now = time.time()
    conn.execute(
        'INSERT OR REPLACE INTO hyperv_hosts '
        '(id, name, host, username, pass_encrypted, winrm_port, use_ssl, auth, '
        ' encrypt_messages, verify_certificate, '
        ' iso_library_paths, smb_share_map, smb_domain, transfer_host, transfer_check, '
        ' max_sessions, enabled, created_at, updated_at) '
        'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
        (host_id,
         data.get('name') or data.get('host') or 'Hyper-V host',
         data.get('host') or '',
         data.get('user') or data.get('username') or '',
         stored,
         int(data.get('port') or default_winrm_port(use_ssl)),
         1 if use_ssl else 0,
         data.get('auth') or DEFAULT_AUTH_METHOD,
         1 if data.get('encrypt_messages', True) else 0,
         1 if data.get('ssl_verification', True) else 0,
         json.dumps(list(data.get('iso_library_paths') or [])),
         json.dumps(dict(data.get('smb_share_map') or {})),
         data.get('smb_domain') or '',
         (data.get('transfer_host') or '').strip(),
         transfer_check,
         parse_max_sessions(data.get('max_sessions')),
         1 if data.get('enabled', True) else 0,
         existing['created_at'] if existing else now,
         now))
    conn.commit()


def load_hosts(conn, decrypt) -> list[dict]:
    """Every registered host, ready to be handed to a manager.

    A host whose password cannot be decrypted is returned without one rather than skipped.
    It then shows up as a source that fails to authenticate, which is a problem somebody
    can see and fix; leaving it out of the list would hide it instead.
    """
    rows = conn.execute(
        'SELECT * FROM hyperv_hosts WHERE enabled = 1 ORDER BY name').fetchall()
    return [_host_row(row, decrypt) for row in rows]


def load_host(conn, decrypt, host_id: str) -> dict | None:
    """One registered host, or None."""
    row = conn.execute('SELECT * FROM hyperv_hosts WHERE id = ?', (host_id,)).fetchone()
    return _host_row(row, decrypt) if row else None


def _host_row(row, decrypt) -> dict:
    password = ''
    if row['pass_encrypted']:
        try:
            password = decrypt(row['pass_encrypted'])
        except Exception:                                    # noqa: BLE001
            logger.warning('Could not decrypt the password for Hyper-V host %s',
                           row['id'])
    return {
        'id': row['id'],
        'name': row['name'],
        'host': row['host'],
        'user': row['username'],
        'pass': password,
        'port': row['winrm_port'] or default_winrm_port(bool(row['use_ssl'])),
        'use_ssl': bool(row['use_ssl']),
        'auth': row['auth'] or DEFAULT_AUTH_METHOD,
        'encrypt_messages': bool(row['encrypt_messages']),
        'ssl_verification': bool(row['verify_certificate']),
        'iso_library_paths': _decode_json(row['iso_library_paths'], [], row['id'],
                                          'ISO library'),
        'smb_share_map': _decode_json(row['smb_share_map'], {}, row['id'], 'share map'),
        'smb_domain': row['smb_domain'] or '',
        'transfer_host': (row['transfer_host'] if 'transfer_host' in row.keys() else '') or '',
        'transfer_check': _decode_json(
            row['transfer_check'] if 'transfer_check' in row.keys() else '{}', {},
            row['id'], 'transfer check'),
        'max_sessions': _stored_max_sessions(row),
        'enabled': bool(row['enabled']),
    }


def _stored_max_sessions(row) -> int:
    """The stored session count, or the default for a value nobody could have saved."""
    raw = row['max_sessions'] if 'max_sessions' in row.keys() else None
    try:
        return parse_max_sessions(raw)
    except ValueError:
        logger.warning('Ignoring an invalid session count for Hyper-V host %s', row['id'])
        return DEFAULT_MAX_SESSIONS


def _decode_json(raw, fallback, host_id: str, label: str):
    """Decode a stored JSON setting, falling back rather than taking the host offline.

    An empty library or an unmapped drive is visible in the UI and fixable. An exception
    during start-up is neither, and it would take down every other host in the list with it.
    """
    try:
        return json.loads(raw or json.dumps(fallback))
    except (TypeError, ValueError):
        logger.warning('Unreadable %s setting for Hyper-V host %s', label, host_id)
        return fallback


def delete_host(conn, host_id: str) -> None:
    """Remove a host's registration.

    Only the registration. Its VMID mappings are dropped separately by forget_host, and
    its migration record is kept either way: those rows describe what was done to targets
    that still exist, and unregistering a source undoes none of it.
    """
    conn.execute('DELETE FROM hyperv_hosts WHERE id = ?', (host_id,))
    conn.commit()
