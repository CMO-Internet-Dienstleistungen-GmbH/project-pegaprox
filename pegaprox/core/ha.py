"""Warm standby for PegaProx itself (#625).

Up to four instances form a group: one ACTIVE and up to three STANDBYs. The active
runs as always. A standby keeps the UI up, pulls the shared configuration from the
active every few seconds, refuses writes and lets none of the background loops act.
An admin promotes a standby by hand. Every member follows the active with the
highest epoch, and an active that learns about a newer one steps down and becomes
its standby. Two actives under the same epoch (two admins promoting two standbys at
once) are settled by the instance id: the higher one stays, the other steps down.

The group is a star with the active in the middle. A standby pairs with the active,
never with another standby; the active hands the member list to every standby with
each snapshot, so they know each other by the time one of them is promoted.

Every instance has an Ed25519 key pair and signs each call to another member over
method, path, a digest of the body, the time, a nonce and the instance id of the
receiver; the member list carries the public keys. A captured call is worth nothing
at another member, and nothing at the same one after two minutes, a second time, or
once the receiver has restarted: the nonces it saw are memory only, so a call older
than its process is refused like one from outside the window. The members' clocks
have to agree within SIGNATURE_WINDOW. A pair from before the groups presented a
secret instead ('<id>:<secret>', the other side holding its hash): such a member is
still taken with that header until it has published a key, and an instance on this
release sends its key along with every such call until the other side says it holds
it (_auth_for). The key is taken that way only from the partner of such a pair, the
one other instance that knows the secret. The group format with hashes that came
between the two was never released and has no such path: there every member had
seen every other member's secret, so a secret proves nothing about a key sent with
it. Its members keep going by their secrets until they pair again.

A member the active removes stays on record as removed (a tombstone, handed out
with the member list): its calls get 410 HA_REMOVED everywhere, and an instance
that hears so from a member lets go of the group and stays passive. A standby that
holds a tombstone for a member its active still lists (the active was promoted while
it could not hear about the removal) hands it back (take_tombstones).

The live view (on unless an admin switches it off, per instance): a standby starts
its cluster, PBS and ESXi managers as well and lets them read, so the UI shows the
clusters as they are. They act on nothing - every path that would is gated on
is_active(). Settings the managers read when they use them are handed over in
place after each sync; once a change to how they connect has settled, the managers
it concerns are stopped and built again in this process (reload_managers), so the
users signed in here stay signed in. Only a switched live view or a new role
restarts the process. With the live view off a standby starts no managers at all.

The active tells its members after each change it takes (nudge_members, POST
/api/ha/peer/changed), and they pull within seconds instead of at their next poll.

Forwarded writes (on unless an admin switches them off, per instance): a write the
standby would refuse goes to the active it follows instead, when a user signed in on
the standby sends it from the browser. The standby vouches for that user: a signed
peer call carries the request and the username, and the active checks the account
against its own users table and runs the request through its own routing, as that
user and with that user's rights there (api/ha.py peer_forward). An API token is
nobody the standby can vouch for, its writes are refused as before. A write that went
through is followed by a pull right away (pull_soon), so it shows here at once.

What travels:
  * pairing - the standby POSTs the one-time code and its public key to the active
    and gets back the field key (.pegaprox_aes256.key), the active's public key and
    the member list, sealed with a key derived from the code.
    Encrypted columns then copy 1:1, including ones added later; a value still in
    the old Fernet format is resealed on its way out.
  * sync - GET /api/ha/peer/snapshot returns every table in SYNC_TABLES plus the host
    key pins, the login background, the plugins' config.json files and the member
    list. Instance-local tables and settings stay put.

A state file from before the groups holds a single peer. It reads as a group of two
(_from_pair_format), and both sides keep talking without pairing again.

State lives in config/ha_state.json, next to the key files and just as private.
It is never part of a snapshot.

MK Sep 2026
"""
import base64
import gzip
import hashlib
import hmac
import importlib
import ipaddress
import json
import logging
import os
import re
import secrets
import stat
import subprocess
import sys
import threading
import time
import urllib.parse
import uuid
from datetime import datetime, timezone

from pegaprox.constants import CONFIG_DIR, BRANDING_DIR, PLUGINS_DIR

ROLE_STANDALONE = 'standalone'
ROLE_ACTIVE = 'active'
ROLE_STANDBY = 'standby'

STATE_FILE = os.path.join(CONFIG_DIR, 'ha_state.json')
AES_KEY_FILE = os.path.join(CONFIG_DIR, '.pegaprox_aes256.key')
KNOWN_HOSTS_FILE = os.path.join(CONFIG_DIR, '.ssh_known_hosts')

SNAPSHOT_FORMAT = 1
CODE_PREFIX = 'pgxha1_'
PAIRING_TTL = 15 * 60
DEFAULT_INTERVAL = 30
# the sender's instance id; '<id>:<secret>' from a member paired before the keys
PEER_HEADER = 'X-PegaProx-Peer'
PEER_TS_HEADER = 'X-PegaProx-Peer-Ts'
PEER_NONCE_HEADER = 'X-PegaProx-Peer-Nonce'
PEER_SIG_HEADER = 'X-PegaProx-Peer-Sig'
# our public key, along with a call that still carries the old secret
PEER_KEY_HEADER = 'X-PegaProx-Peer-Key'
# the answer to it: the receiver holds the key this call was signed with
PEER_KEYED_HEADER = 'X-PegaProx-Peer-Keyed'
# the sha256 of the body, as signed: a receiver can check who sent a large call before
# it reads the body (signed_before_body), and the body against it afterwards
PEER_BODY_HEADER = 'X-PegaProx-Peer-Body'
# how far a signed call's time may be off, either way
SIGNATURE_WINDOW = 120
_NONCES_PER_SENDER = 4096
# /peer/status and the snapshot say they come from this release: members, keys, tombstones
GROUP_MARK = 1
# the highest epoch any member reads, holds or hands on. A promotion that would go past
# it is refused: an epoch nobody can read would leave two actives that never settle
EPOCH_MAX = 2 ** 31 - 1
# one active and up to three standbys
MAX_MEMBERS = 4
MAX_TOMBSTONES = 16
GROUP_FULL_ERROR = f'This group already has {MAX_MEMBERS - 1} standbys - remove one first'
REMOVED_ERROR = 'This instance was removed from the group'
# the pull of a pass: at most this long, at least the floor whatever the interval,
# and short when the watch of the same pass could not reach the source
PULL_TIMEOUT = 60
PULL_TIMEOUT_FLOOR = 20
PULL_TIMEOUT_UNREACHABLE = 10

# A write a standby forwards: the peer route that takes it on the active, and the key
# the active marks the call it runs for the standby with in the WSGI environ (the
# session made for it, the standby, the client address). A client cannot set an
# environ key of that name; headers arrive as HTTP_*.
FORWARD_PATH = '/api/ha/peer/forward'
FORWARD_ENVIRON = 'pegaprox.ha_forward'
FORWARD_MAX_BODY = 50 * 1024 * 1024
# The browser waits for it, and some writes run a while before they answer (a clone
# with cloud-init waits up to 10 minutes for its task). Connecting has its own, short
# limit: an active that is gone costs a click seconds, not minutes.
FORWARD_TIMEOUT = 900
FORWARD_CONNECT_TIMEOUT = 15
FORWARD_READ_TIMEOUT = 30
_FORWARD_CHUNK = 1024 * 1024
# The reads a forwarding standby hands on as well: the progress of jobs that live in the
# process, or in the tables of its own, of the instance that started them. A job
# started through a standby runs on the active, and without these the standby would
# show no progress at all (nor the cutover of a migration that waits for one). And the
# views only the active's own tables hold, because only its loops fill them
# (LEADER_ONLY_READS). A GET rule, as app.url_map writes it; the active serves nothing
# else as a forwarded read.
#
# Leader-only: the alerts that fire, drift, the push inbox and the migration history.
# A standby has rows of its own in those tables, under ids of its own (from when it
# acted, or from before it joined), and an ack picked from such a list would name
# another row on the active. When the active does not answer, a forwarding standby
# says so (api/ha.py _forward_read) instead of showing its own; the progress of a job
# falls back to the standby's copy.
LEADER_ONLY_READS = frozenset((
    '/api/clusters/<cluster_id>/active-alerts',
    '/api/clusters/<cluster_id>/drift/status',
    '/api/clusters/<cluster_id>/drift/events',
    '/api/push/inbox',
    '/api/migration-history',
    '/api/clusters/<cluster_id>/vms/<int:vmid>/migration-history',
))
FORWARDED_READS = LEADER_ONLY_READS | frozenset((
    '/api/vmware/migrations',
    '/api/vmware/migrations/<mid>',
    '/api/xhm/migrations',
    '/api/xhm/migrations/<mid>',
    '/api/clusters/<cluster_id>/updates/status',
    '/api/clusters/<cluster_id>/nodes/<node_name>/update',
    '/api/clusters/<cluster_id>/nodes/<node_name>/maintenance',
    '/api/clusters/<cluster_id>/datastores/<storage_name>/download-status/<task_id>',
    '/api/pbs/<pbs_id>/update',
    '/api/clusters/<cluster_id>/backup-verify/<task_id>',
    '/api/clusters/<cluster_id>/backup-verify/active',
    '/api/clusters/<cluster_id>/backup-verify/history',
    '/api/clusters/<cluster_id>/iso-sync/last-result',
    '/api/clusters/<cluster_id>/migrations',
    '/api/cluster-groups/<group_id>/lb-history',
    '/api/clusters/<cluster_id>/templates/deployments',
    '/api/templates/deployments/<dep_id>',
    '/api/dr-drills/<drill_id>',
    '/api/site-recovery/plans/<plan_id>/drills',
    '/api/site-recovery/plans/<plan_id>/events',
    '/api/clusters/<cluster_id>/snapshot-policies/<pid>/runs',
))

# Shared configuration. Everything else in the database is per host: sessions,
# audit trail, metrics, run and event history, runtime alerts. A table that is in
# neither list is not synced; tests/test_ha_core.py fails until it is placed.
SYNC_TABLES = (
    'server_settings', 'users', 'user_folders', 'user_favorites', 'api_tokens',
    'webauthn_credentials', 'custom_roles', 'tenants', 'vm_acls', 'pool_permissions',
    'clusters', 'cluster_groups', 'node_maintenance', 'balancing_excluded_vms',
    'balancing_excluded_pools', 'affinity_rules', 'vm_tags', 'alerts', 'cluster_alerts',
    'scheduled_tasks', 'scheduled_actions', 'update_schedules', 'custom_scripts',
    'node_bmc_endpoints', 'esxi_storages', 'storage_clusters', 'pbs_servers',
    'vmware_servers', 'xcpng_pools', 'xcpng_pool_members', 'xcpng_vmid_map',
    'cross_cluster_replications', 'efficient_snapshots', 'site_recovery_plans',
    'site_recovery_vms', 'snapshot_policies', 'drift_baselines', 'multi_cluster_vnets',
    'siem_targets', 'push_subscriptions', 'plugin_state', 'status_incidents',
    'custom_cloud_templates', 'power_rates', 'cost_rates', 'auto_install_profiles',
    'pegaprox_kv',
)
LOCAL_TABLES = (
    'sessions', 'audit_log', 'task_users', 'migration_history', 'metrics_history',
    'active_alerts', 'site_recovery_events', 'cve_history', 'backup_verifications',
    'status_uptime', 'cloud_init_deployments', 'dr_drills', 'dr_drill_checks',
    'snapshot_runs', 'drift_events', 'auto_install_runs', 'push_inbox',
    'balance_recommendations', 'logs', 'logs_fts',
)

# server_settings keys that describe this host, not the deployment
LOCAL_SETTING_KEYS = frozenset((
    'domain', 'port', 'ssl_enabled', 'http_redirect_port', 'reverse_proxy_enabled',
    'trusted_proxies', 'proxy_bind_address', 'oidc_redirect_uri', 'syslog_enabled',
    'syslog_retention_days', 'alert_last_notified_version', 'hardware_monitoring',
    'hardware_monitoring_redfish',
))
LOCAL_SETTING_PREFIXES = ('acme_',)

# Columns every login, token use or push delivery writes. They still travel in
# the body, but leaving them out of the etag keeps a busy active from forcing a
# full transfer on nearly every poll.
VOLATILE_COLUMNS = {
    'users': ('last_login', 'last_ldap_sync', 'last_oidc_sync'),
    'api_tokens': ('last_used_at', 'last_used_ip'),
    'webauthn_credentials': ('last_used_at', 'last_used_ip'),
    'push_subscriptions': ('last_used_at', 'failures'),
    'plugin_state': ('loaded_at',),
    'siem_targets': ('last_status', 'last_ok_at', 'last_error_at', 'last_error',
                     'sent_count', 'error_count'),
}

# Encrypted columns, the same inventory db.rotate_encryption_key walks. A value
# still in the pre-2026 Fernet format is resealed under the field key on its way
# into a snapshot, because the standby holds our field key but not our Fernet key.
ENCRYPTED_COLUMNS = {
    'users': ('totp_secret_encrypted', 'totp_pending_secret_encrypted'),
    'clusters': ('pass_encrypted', 'ssh_key_encrypted', 'api_token_secret_encrypted',
                 'ha_settings'),
    'esxi_storages': ('password_encrypted',),
    'node_bmc_endpoints': ('bmc_password_encrypted',),
    'pbs_servers': ('pass_encrypted', 'api_token_secret_encrypted', 'ssh_key_encrypted'),
    'vmware_servers': ('pass_encrypted',),
    'auto_install_profiles': ('answer_encrypted',),
}
SECRET_SETTING_KEYS = ('smtp_password', 'ldap_bind_password', 'oidc_client_secret')

_MAX_BRANDING_BYTES = 8 * 1024 * 1024
_MAX_PLUGIN_CONFIG_BYTES = 256 * 1024
_MAX_PLUGIN_CONFIG_TOTAL = 2 * 1024 * 1024
_PLUGIN_ID_RE = re.compile(r'^[a-z0-9][a-z0-9_-]{0,63}$')
_TABLE_NAME_RE = re.compile(r'^[a-z_][a-z0-9_]*$')
_COLUMN_NAME_RE = re.compile(r'[A-Za-z_][A-Za-z0-9_]{0,63}')

_MAX_URL_LEN = 512
_DNS_LABEL_RE = re.compile(r'(?!-)[a-z0-9-]{1,63}(?<!-)')
_URL_CHARS_RE = re.compile(r'[A-Za-z0-9.\-_~:/\[\]]+')
_URL_PATH_RE = re.compile(r'/[A-Za-z0-9._~\-/]*')
_URL_PORT_RE = re.compile(r':[0-9]{1,5}')
_ID_RE = re.compile(r'[0-9a-f]{32}')
_SECRET_HASH_RE = re.compile(r'[0-9a-f]{64}')
_FP_RE = re.compile(r'[0-9A-F]{2}(:[0-9A-F]{2}){31}')
# an Ed25519 public key or signature, base64 of the raw bytes
_PUBLIC_KEY_RE = re.compile(r'[A-Za-z0-9+/]{43}=')
_SIGNATURE_RE = re.compile(r'[A-Za-z0-9+/]{86}==')
_NONCE_RE = re.compile(r'[A-Za-z0-9_-]{16,64}')
_TS_RE = re.compile(r'[0-9]{1,12}')
_DIGEST_RE = re.compile(r'[0-9a-f]{64}')

# A standby reloads the managers a change to how they connect is about once the new
# settings have held for RELOAD_SETTLE seconds, so a burst of edits is one reload.
# The active tells its members about NUDGE_DELAY seconds after a change, so edits made
# in one go (a dialog that saves twice, the API token the active makes on the first
# connect and saves right after) reach a member with the same note, or with the next
# one NUDGE_SPACING later. A later edit costs one more reconnect of that one manager,
# nobody's session: the minute this was before was for a restart.
RELOAD_SETTLE = 10
BOOT_PULL_TIMEOUT = 10

# The active's note to its members after a change: NUDGE_DELAY seconds after the first
# write, and while writes keep coming one every NUDGE_SPACING seconds, the last one
# after the last write. Each note carries the etag of the configuration, worked out
# once for every member, and a member that holds it pulls nothing. That walk over the
# shared tables is what a note costs the active (about 0.4 s at 40k synced rows): at
# most one per NUDGE_SPACING, under 5% of one core however busy it is. Ten seconds is
# also the RELOAD_SETTLE a member waits before a new connection takes effect, and a
# third of the default poll.
NUDGE_PATH = '/api/ha/peer/changed'
NUDGE_DELAY = 2
NUDGE_SPACING = 10
NUDGE_TIMEOUT = 5

_lock = threading.RLock()
_state = None
_loop_started = False
# the stored etag is dropped once per process start: an upgrade or a restored
# database changes what we hold without the active knowing
_etag_checked = False


def _fresh_run():
    return {
        'started': time.monotonic(),
        'managers': False,          # main() started the cluster, PBS and ESXi managers
        'signature': None,          # manager_signature() the running managers stand for
        'items': None,              # the per-connection digests behind it, to name a change
        'live_view': None,          # the live view this process runs with, once known
        'live_view_changed': None,  # when set_live_view last changed it
        'reload': None,             # a change to how they connect, waiting to settle
        'last_reload': None,        # when the last reload ran, why, and what it could not build
        'restarting': False,
    }


# What the running managers were built from. Memory only: the signature is taken
# from the decrypted passwords and keys, so it never goes near the state file.
_run = _fresh_run()
_pull_lock = threading.Lock()
# one reload at a time, whoever asks: its timer, a pull, the admin's "apply now"
_reload_lock = threading.Lock()
# the nonces of signed calls taken within the window, per (receiver, sender). Memory
# only, so a call signed before this process started is not taken at all
_nonce_lock = threading.Lock()
_seen_nonces = {}
_PROCESS_STARTED = int(time.time())
# what the last look at the group could not reach, for the pull of the same pass
_last_watch = {'at': None, 'unreachable': frozenset()}
# the member this standby pulls from, when the last try to reach it (watch, pull or a
# forwarded write) got no answer at all; the next answer clears it. Forwarding waits
# for that answer instead of holding every write until it times out
_silent_source = {'id': None}


class HaError(Exception):
    """Something the admin (or the peer) should be told as is."""


class PeerUnreachable(HaError):
    """The member did not answer at all: network, timeout or a certificate we cannot trust."""


class PeerNoAnswer(PeerUnreachable):
    """The member took the call and then sent no answer, in time or at all: whatever
    the call asked for may have happened there."""


class PeerRefused(HaError):
    """The member answered and turned the call away (401, or 410 once it removed us)."""

    def __init__(self, message, status, code=''):
        super().__init__(message)
        self.status, self.code = status, code


class RemoveUnconfirmed(HaError):
    """remove_member: the member was not seen as a standby under the current epoch."""


# --- state ---------------------------------------------------------------------

def _now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _default_state():
    return {
        'role': ROLE_STANDALONE,
        'epoch': 0,
        'instance_id': uuid.uuid4().hex,
        'interval': DEFAULT_INTERVAL,
        # what this instance signs every call to another member with (base64 of the raw
        # Ed25519 private key), made when it first pairs
        'signing_key': None,
        # the secret a group from before the keys presented; gone once every member
        # holds our public key
        'member_secret': None,
        # every OTHER member: {instance id: {url, fingerprint, public_key, secret_hash,
        # pair_secret, role_seen, epoch_seen, serving_seen, last_contact, last_error,
        # joined_at, group_seen, key_acked}}
        'members': {},
        # members the active took out: {instance id: {epoch, at, by, public_key,
        # secret_hash}}, so their calls get 410 and no stale list takes them back
        'tombstones': {},
        # set when a member told us we were removed: {epoch, at, by}
        'removed': None,
        # on a standby, the member it pulls from
        'source': None,
        'pairing': None,
        'sync': {},
    }


def _from_pair_format(st):
    """A state file from before the groups: one 'peer' with the secret we present to it
    (secret_out) and the hash of the one it presents to us (secret_in_hash). That is a
    group of two, and it reads as one. Our secret_out becomes the secret we present to
    every member, and the other side holds its hash already, so neither side pairs
    again - whichever of the two is upgraded first. Only in memory; the file takes
    the new form with the next write.

    The record is marked pair_secret: its secret was made for this pair and nobody
    else has seen it, so a key the member sends along with it is its own
    (peer_verdict)."""
    p = st.pop('peer', None)
    if 'members' in st or not isinstance(p, dict):
        return st
    pid = p.get('instance_id')
    st['members'] = {}
    if isinstance(pid, str) and pid:
        st['member_secret'] = p.get('secret_out') or None
        st['members'][pid] = {
            'url': p.get('url') or '',
            'fingerprint': p.get('fingerprint') or '',
            'secret_hash': p.get('secret_in_hash') or '',
            'pair_secret': True,
            'role_seen': p.get('role_seen'),
            'epoch_seen': p.get('epoch_seen') or 0,
            'last_contact': p.get('last_contact'),
            'last_error': p.get('last_error') or '',
            'joined_at': p.get('paired_at'),
        }
        if st.get('role') == ROLE_STANDBY:
            st['source'] = pid
    return st


def _key_backups():
    """The .pre-ha copies of our own field key. Only join() writes them, so one
    being there means this instance was a standby at some point."""
    folder = os.path.dirname(AES_KEY_FILE) or '.'
    prefix = os.path.basename(AES_KEY_FILE) + '.pre-ha.'
    try:
        return sorted(fn for fn in os.listdir(folder) if fn.startswith(prefix))
    except OSError:
        return []


def _load():
    global _state
    with _lock:
        if _state is not None:
            return _state
        st = None
        try:
            with open(STATE_FILE, 'r', encoding='utf-8') as fh:
                st = json.load(fh)
            if not isinstance(st, dict):
                raise ValueError('not a JSON object')
            # a note an older build saved; this file reads fine
            st.pop('broken', None)
        except FileNotFoundError:
            st = None
            if _key_backups():
                # joined once and the file is gone (a restore from before the
                # pairing, a hand-made "reset"): an acting standalone here would
                # run next to the active on its key and its configuration
                logging.error(f"[HA] {STATE_FILE} is missing but this instance joined a pair "
                              "before - staying passive until it is restored or unpaired")
                st = dict(_default_state(), role=ROLE_STANDBY,
                          broken='The HA state file is missing, but this instance joined a pair '
                                 'before - restore config/ha_state.json and restart, or unpair it')
        except Exception as e:
            # a state file we cannot read must not turn a standby into an acting
            # instance: stay standby until someone looks
            logging.error(f"[HA] {STATE_FILE} is unreadable ({e}) - staying passive until it is fixed")
            st = dict(_default_state(), role=ROLE_STANDBY, broken=str(e)[:200])
        if not isinstance(st, dict):
            st = _default_state()
        base = _default_state()
        base.update(_from_pair_format(st))
        if base.get('role') not in (ROLE_STANDALONE, ROLE_ACTIVE, ROLE_STANDBY):
            base['role'] = ROLE_STANDBY
        for field in ('members', 'tombstones'):
            ms = base.get(field) if isinstance(base.get(field), dict) else {}
            base[field] = {k: v for k, v in ms.items() if isinstance(k, str) and isinstance(v, dict)}
        if not isinstance(base.get('removed'), dict):
            base['removed'] = None
        # nothing is written until something changes: an instance that never pairs
        # never grows a state file
        _state = base
        return _state


def _write_locked(st):
    """Write `st` as the state file.

    Refuses the stand-in _load built for a file it could not read: saving it would
    replace the only copy of the peer record and the secrets with a fresh
    identity. unpair() is the one way out, and it drops the note first.
    """
    if st.get('broken'):
        raise HaError('The HA state file cannot be read - restore config/ha_state.json and '
                      'restart, or unpair this instance')
    tmp = STATE_FILE + '.tmp'
    data = json.dumps({k: v for k, v in st.items() if k != 'broken'}, indent=2, sort_keys=True)
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    os.replace(tmp, STATE_FILE)
    try:
        os.chmod(STATE_FILE, 0o600)
    except OSError:
        pass


def _commit_locked(new):
    """Write `new`, then make it the state. Memory never runs ahead of the file:
    a failed write leaves both as they were."""
    _write_locked(new)
    _state.clear()
    _state.update(new)


def _update(**changes):
    with _lock:
        _load()
        _commit_locked(dict(_state, **changes))
        return dict(_state)


def _update_sync(**changes):
    with _lock:
        _load()
        sync = dict(_state.get('sync') or {})
        sync.update(changes)
        _commit_locked(dict(_state, sync=sync))


def _note_members(notes):
    """{member id: changes} into the member records, one write for all of them, and
    none when nothing changes (a member that stays down keeps the same error). A
    member that left meanwhile stays gone."""
    with _lock:
        _load()
        ms = dict(_state.get('members') or {})
        hit = False
        for mid, changes in notes.items():
            if mid in ms:
                merged = dict(ms[mid], **changes)
                if merged != ms[mid]:
                    ms[mid] = merged
                    hit = True
        if hit:
            _commit_locked(dict(_state, members=ms))


def reset_for_tests():
    """Forget the cached state so the next call rereads STATE_FILE."""
    global _state
    with _lock:
        _state = None


def role():
    return _load().get('role', ROLE_STANDALONE)


def is_standby():
    return role() == ROLE_STANDBY


def is_active():
    """True when this instance may act: change a cluster, fire schedules, send mail.

    A standalone instance is active too. Only a standby holds back; with the live
    view its managers still read (managers_wanted), and nothing more.
    """
    return role() != ROLE_STANDBY


def instance_id():
    return _load()['instance_id']


def epoch():
    return int(_load().get('epoch') or 0)


def _epoch_value(value, low=0):
    """`value` when it is an epoch, None for anything else: a whole number from `low`
    to EPOCH_MAX. The one check for every epoch another member sends or reports, and
    for every one this instance takes over from it."""
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= EPOCH_MAX:
        return None
    return value


def _joined_order(ms):
    """Member ids in the order they joined, the id settling a tie."""
    return sorted(ms, key=lambda mid: (str(ms[mid].get('joined_at') or ''), mid))


def members():
    """Every other member of the group in the order they joined, as copies that
    carry their instance_id."""
    ms = _load().get('members') or {}
    return [dict(ms[mid], instance_id=mid) for mid in _joined_order(ms)]


def member(member_id):
    """The record of one member with its instance_id, None for anybody else."""
    rec = (_load().get('members') or {}).get(member_id) if isinstance(member_id, str) else None
    return dict(rec, instance_id=member_id) if rec else None


def source_id():
    """The member a standby pulls from, None on any other instance or when it has
    lost it (the active unpaired while this one could not be told)."""
    st = _load()
    sid = st.get('source')
    if st['role'] != ROLE_STANDBY or sid not in (st.get('members') or {}):
        return None
    return sid


def peer():
    """The one member, for code that needs only one: on a standby the member it pulls
    from, anywhere else the first member. None when there is none."""
    if role() == ROLE_STANDBY:
        return member(source_id())
    ms = members()
    return ms[0] if ms else None


def standby_count():
    """Standbys in the group as this instance knows it: on the active its members, on
    a standby everybody but the member it pulls from, itself included."""
    st = _load()
    n = len(st.get('members') or {})
    if st['role'] == ROLE_ACTIVE:
        return n
    if st['role'] == ROLE_STANDBY and n:
        return n + 1 - (1 if source_id() else 0)
    return 0


def group_full():
    """True when this instance has MAX_MEMBERS - 1 other members and takes no more."""
    return len(_load().get('members') or {}) >= MAX_MEMBERS - 1


def _seen_in_group(rec):
    """A member known to run this release: it answered with the group mark, or it has
    a public key, which only this release makes."""
    return bool(rec.get('group_seen') or rec.get('public_key'))


def group_waiting():
    """The first member not yet seen running this release, None when there is none.
    A release from before the groups takes calls from its one peer only, so a
    further standby would be refused there and stranded once it is promoted."""
    for rec in members():
        if not _seen_in_group(rec):
            return rec
    return None


def _group_waiting_error(rec):
    return (f"{rec.get('url') or rec['instance_id']} has not answered as a member of a group "
            "yet - update it to this release and let it answer once before adding a standby")


def _confirmed_standby(rec, st):
    """The member answered as a standby under our current epoch."""
    return (rec.get('role_seen') == ROLE_STANDBY
            and int(rec.get('epoch_seen') or 0) == int(st.get('epoch') or 0))


# --- the live view -------------------------------------------------------------

def live_view():
    """Whether a standby runs its managers read-only, so its UI shows live data.

    Per instance and never part of a snapshot; on until an admin switches it off.
    Means nothing on an active instance, whose managers always run."""
    value = _load().get('live_view', True)
    return value if isinstance(value, bool) else True


def set_live_view(value):
    """Switch the live view of this instance. Returns True when it changed.

    Only saved: a standby runs with the new value after its next start, which
    apply_config_now() brings about."""
    if not isinstance(value, bool):
        raise HaError('live_view is true or false')
    with _lock:
        before = live_view()
        if _run['live_view'] is None:
            # nothing has changed it in this process yet, so this is what it runs with
            _run['live_view'] = before
        if value == before:
            return False
        _update(live_view=value)
        _run['live_view_changed'] = _now()
    return True


def managers_wanted():
    """True when this process starts its cluster, PBS and ESXi managers: always on an
    instance that acts, on a standby only with the live view on.

    A state file that cannot be read keeps them down. Whether the admin switched
    the live view off is in the part we cannot read."""
    st = _load()
    if st['role'] != ROLE_STANDBY:
        return True
    return not st.get('broken') and live_view()


# --- forwarded writes ------------------------------------------------------------

def forward_writes():
    """Whether this instance, as a standby, hands the writes it refuses to the active.

    Per instance and never part of a snapshot; on until an admin switches it off."""
    value = _load().get('forward_writes', True)
    return value if isinstance(value, bool) else True


def set_forward_writes(value):
    """Switch forward_writes. Returns True when it changed. Nothing holds on to the
    value, the next write goes by it."""
    if not isinstance(value, bool):
        raise HaError('forward_writes is true or false')
    with _lock:
        if value == forward_writes():
            return False
        _update(forward_writes=value)
    return True


def leader_reachable():
    """On a standby: the member it pulls from last answered as active, and did answer
    the last time this instance tried. False on every other instance. A standby that
    was removed, or cannot read its state file, has no such member."""
    st = _load()
    if st['role'] != ROLE_STANDBY:
        return False
    rec = (st.get('members') or {}).get(st.get('source'))
    return (bool(rec) and rec.get('role_seen') == ROLE_ACTIVE
            and _silent_source['id'] != st.get('source'))


def forwarding():
    """True when a write this standby refuses goes to the active right now: forwarding
    is on and the leader is reachable. False on every other instance."""
    return leader_reachable() and forward_writes()


# --- serving users ---------------------------------------------------------------

def serve_users():
    """Whether this instance, as a standby, serves users the way an active instance
    does: they sign in here, see the clusters live and open their consoles here, and
    every change goes to the leader. The role stays standby, so nothing that acts on
    its own starts here (is_active).

    Per instance and never part of a snapshot; off until an admin switches it on."""
    value = _load().get('serve_users', False)
    return value if isinstance(value, bool) else False


def set_serve_users(value):
    """Switch serve_users. Returns True when it changed. Nothing holds on to the value,
    the next request goes by it."""
    if not isinstance(value, bool):
        raise HaError('serve_users is true or false')
    with _lock:
        if value == serve_users():
            return False
        _update(serve_users=value)
    return True


def serving():
    """True when this standby serves users now: the switch is on, and so are the two it
    needs, the live view (clusters to show and consoles to open) and forwarding (the
    changes go to the leader). False on every other instance, and on a standby that was
    removed from the group or has lost the member it pulls from: its accounts and rights
    are those of its last sync, and nothing the leader changes reaches it any more. A
    leader that is only out of reach keeps it serving."""
    st = _load()
    if st['role'] != ROLE_STANDBY or st.get('removed') or source_id() is None:
        return False
    return serve_users() and live_view() and forward_writes()


def consoles_here():
    """Whether consoles, shells and SPICE open on this instance: everywhere but on a
    standby that does not serve users."""
    return not is_standby() or serving()


def sign_in_digest(username):
    """A digest of this instance's copy of the account's password hash and salt, ''
    when there is no such account. A standby sends it with a forwarded write: an
    active whose copy differs has changed the password since, and the session the
    standby vouches for is one the next sync would end (_end_sessions)."""
    from pegaprox.core.db import get_db
    try:
        row = get_db().conn.cursor().execute(
            'SELECT password_hash, password_salt FROM users WHERE username = ?', (username,)).fetchone()
    except Exception as e:
        logging.warning(f"[HA] could not read the sign-in of {username!r}: {e}")
        return ''
    if row is None:
        return ''
    return hashlib.sha256(b'pegaprox-ha-sign-in:' + json.dumps([row[0], row[1]]).encode()).hexdigest()


def _note_source_heard(member_id, heard):
    """Whether the member this standby pulls from answered the last call to it."""
    if heard:
        if _silent_source['id'] == member_id:
            _silent_source['id'] = None
    elif member_id:
        _silent_source['id'] = member_id


# --- what the managers connect with -----------------------------------------------

# Everything a manager is built from and holds on to once connected (the current
# host, the auth mode, the SSH pool). A change here needs a new manager, which a
# standby builds in place of the old one. Fallback hosts, the HA settings, updated_at
# and the display and balancing fields stay out: the active rewrites some of them on
# its own, and the managers read the rest each time they use them.
# (kind, table, only enabled rows, columns)
_IDENTITY = (
    ('cluster', 'clusters', False,
     ('cluster_type', 'host', 'user', 'pass_encrypted', 'api_port', 'ssl_verification',
      'api_token_user', 'api_token_secret_encrypted', 'ssh_user', 'ssh_key_encrypted',
      'ssh_port', 'ssh_disabled')),
    ('pbs', 'pbs_servers', True,
     ('host', 'port', 'user', 'pass_encrypted', 'api_token_id', 'api_token_secret_encrypted',
      'fingerprint', 'ssl_verify', 'ssh_user', 'ssh_port', 'ssh_key_encrypted')),
    ('vmware', 'vmware_servers', True,
     ('host', 'port', 'username', 'pass_encrypted', 'server_type', 'ssl_verify')),
)
_IDENTITY_LABELS = {'cluster': ('cluster', 'clusters'), 'pbs': ('PBS server', 'PBS servers'),
                    'vmware': ('ESXi server', 'ESXi servers')}
_UNREADABLE = '\x00unreadable'

# Read by the managers at the moment they use them, so a sync hands them over in
# place: what the active's cluster routes set on mgr.config (PUT /api/clusters/<id>,
# location, backup SLA) less the connection fields above, plus the fallback hosts
# and SMBIOS settings the active changes without an admin. ha_enabled and
# ha_settings are the exception: a PVE manager copies them when it is built, so
# _hand_over_ha_view refreshes those copies as well.
_REFRESH_CLUSTER_FIELDS = (
    'name', 'enabled', 'check_interval', 'migration_threshold', 'migration_tolerance',
    'auto_migrate', 'balance_containers', 'balance_local_disks', 'dry_run', 'ha_enabled',
    'ha_settings', 'excluded_nodes', 'predictive_balancing', 'predictive_threshold',
    'balance_cpu_weight', 'balance_mem_weight', 'balance_io_weight', 'cpu_baseline',
    'vnc_tunnel', 'proxlb_tags_enabled', 'node_ui_suffix', 'backup_sla_max_age_hours',
    'latitude', 'longitude', 'location_label', 'fallback_hosts', 'smbios_autoconfig',
)
_REFRESH_SERVER_FIELDS = ('name', 'notes', 'linked_clusters')


def _plain(db, value):
    """A sealed column as the manager sees it. One we cannot open gets a fixed marker:
    a fresh nonce on every save must not read as a change."""
    if not value:
        return ''
    try:
        return db._decrypt(value)
    except Exception:
        return _UNREADABLE


def _identity_items():
    """{'<kind>:<id>': digest} over every cluster and every enabled PBS and ESXi server.

    Taken from the decrypted values: each save on the active seals the secrets
    again under a new nonce, and that alone must not reload anything."""
    from pegaprox.core.db import get_db
    db = get_db()
    cur = db.conn.cursor()
    present = _existing_tables(cur)
    items = {}
    for kind, table, enabled_only, columns in _IDENTITY:
        if table not in present:
            continue
        cur.execute(f'SELECT * FROM "{table}"' + (' WHERE enabled = 1' if enabled_only else ''))
        for row in cur.fetchall():
            row = dict(row)
            h = hashlib.sha256()
            for col in columns:
                v = row.get(col)
                _hash_value(h, _plain(db, v) if col.endswith('_encrypted') else v)
            items[f"{kind}:{row['id']}"] = h.hexdigest()
    return items


def _signature_of(items):
    h = hashlib.sha256(b'pegaprox-ha-managers')
    for key in sorted(items):
        _hash_value(h, key)
        _hash_value(h, items[key])
    return h.hexdigest()


def manager_signature():
    """A digest of how the cluster, PBS and ESXi managers connect: which ones there are,
    host, user, credentials, ports, TLS and SSH settings, cluster and server type.

    Held in memory only, never written anywhere."""
    return _signature_of(_identity_items())


def _describe_change(before, after):
    """'1 cluster added, 2 PBS servers changed' - counts and kinds, no names or values."""
    parts = []
    if before is not None:
        for kind, (one, many) in _IDENTITY_LABELS.items():
            b = {k: v for k, v in before.items() if k.startswith(kind + ':')}
            a = {k: v for k, v in after.items() if k.startswith(kind + ':')}
            for word, n in (('added', len(a.keys() - b.keys())),
                            ('removed', len(b.keys() - a.keys())),
                            ('changed', sum(1 for k in a.keys() & b.keys() if a[k] != b[k]))):
                if n:
                    parts.append(f'{n} {one if n == 1 else many} {word}')
    return ', '.join(parts) or 'the connection settings changed'


def note_managers_started(signature):
    """main(), right after the managers came up: the manager_signature() they started
    from. A sync that changes it reloads the managers it concerns on a standby
    (reload_managers)."""
    try:
        items = _identity_items()
    except Exception as e:
        logging.warning(f"[HA] could not read what the managers were started from: {e}")
        items = None
    with _lock:
        _run.update(managers=True, signature=signature, items=items, live_view=live_view(),
                    reload=None)


def _refresh_managers():
    """Hand what the running managers read at the moment of use over to them, the way
    the active's PUT /api/clusters/<id> does: setattr on mgr.config, no stop and no
    start. On a standby also the HA view and the PegaProx node maintenance, which a
    PVE manager otherwise only reads at its start. Returns how many values changed."""
    from pegaprox import globals as g
    from pegaprox.core.db import get_db
    db = get_db()
    changed = 0
    for cid, data in (db.get_all_clusters() or {}).items():
        mgr = g.cluster_managers.get(cid)
        cfg = getattr(mgr, 'config', None)
        if cfg is None or getattr(mgr, 'cluster_type', None) == 'esxi':
            continue
        ha_changed = False
        for key in _REFRESH_CLUSTER_FIELDS:
            if key in data and hasattr(cfg, key) and getattr(cfg, key) != data[key]:
                setattr(cfg, key, data[key])
                changed += 1
                ha_changed = ha_changed or key in ('ha_enabled', 'ha_settings')
        if is_active():
            continue
        try:
            if ha_changed:
                _hand_over_ha_view(mgr, cfg)
            follow = getattr(mgr, '_follow_persisted_maintenance', None)
            if follow is not None:
                changed += int(follow() or 0)
        except Exception as e:
            logging.warning(f"[HA] could not refresh the HA or maintenance view of cluster {cid}: {e}")
    cur = db.conn.cursor()
    for registry, table in ((g.pbs_managers, 'pbs_servers'), (g.vmware_managers, 'vmware_servers')):
        if not registry:
            continue
        cur.execute(f'SELECT id, name, notes, linked_clusters FROM "{table}"')
        for row in cur.fetchall():
            mgr = registry.get(row['id'])
            if mgr is None:
                continue
            try:
                linked = json.loads(row['linked_clusters'] or '[]')
            except (TypeError, ValueError):
                linked = []
            fresh = {'name': row['name'], 'notes': row['notes'], 'linked_clusters': linked}
            for key in _REFRESH_SERVER_FIELDS:
                value = fresh[key]
                if getattr(mgr, key, None) != value:
                    setattr(mgr, key, value)
                    changed += 1
    return changed


def _hand_over_ha_view(mgr, cfg):
    """A PVE manager copies ha_enabled and ha_settings into its own fields when it is
    built, and the HA page (get_ha_status) reads those copies, not mgr.config. On a
    standby the HA monitor never runs, so the copies are only a view: rebuild them
    from the synced row. Wherever a monitor runs they are its own and stay as they are."""
    apply = getattr(mgr, '_apply_ha_settings', None)
    if apply is None or getattr(mgr, 'ha_thread', None) is not None:
        return
    settings = getattr(cfg, 'ha_settings', None)
    mgr.ha_enabled = bool(getattr(cfg, 'ha_enabled', False))
    apply(settings if isinstance(settings, dict) else {})


def _after_sync_applied():
    """A sync changed our database: refresh the running managers in place, and when
    their connection settings changed, note a reload. Never raises."""
    if not _run['managers']:
        return
    try:
        n = _refresh_managers()
        if n:
            logging.info(f"[HA] sync: handed {n} changed setting(s) to the running managers")
    except Exception as e:
        logging.warning(f"[HA] could not refresh the running managers after a sync: {e}")
    try:
        items = _identity_items()
    except Exception as e:
        logging.warning(f"[HA] could not read the connection settings after a sync: {e}")
        return
    _note_signature(_signature_of(items), items)


def _note_signature(sig, items):
    with _lock:
        pending = _run['reload']
        if sig == _run['signature']:
            if pending:
                # changed and changed back before it settled
                _run['reload'] = None
                logging.warning("[HA] the connection settings are back to what the managers "
                                "run with - nothing to reload")
            return
        if pending and pending['signature'] == sig:
            return
        reason = _describe_change(_run['items'], items)
        _run['reload'] = {'signature': sig, 'since': time.monotonic(), 'since_iso': _now(),
                          'reason': reason}
    logging.warning(f"[HA] connection settings changed on the active instance ({reason}) - "
                    f"reloading those managers once they have held for {RELOAD_SETTLE}s")
    # a second past it: the timer's clock and time.monotonic() need not agree to the
    # millisecond, and a timer that comes early finds nothing due
    _reload_later(RELOAD_SETTLE + 1)


def _later(delay, fn, name):
    """fn in a thread of its own, `delay` seconds from now (a greenlet under gevent)."""
    t = threading.Timer(delay, fn)
    t.daemon = True
    t.name = name
    t.start()


def _reload_later(delay):
    try:
        _later(delay, _reload_if_due, 'ha-reload')
    except Exception as e:
        # the next pull looks again
        logging.warning(f"[HA] could not schedule the reload of the managers: {e}")


def _live_view_switch():
    """'on' or 'off' when a standby's live view is not the one this process runs with."""
    running = _run['live_view']
    if running is None or not is_standby():
        return ''
    now = live_view()
    return '' if now == running else ('on' if now else 'off')


def _reload_wait():
    """Seconds until the waiting reload may run, 0 once it may, None when none waits."""
    # read once: a sync that brings the old settings back clears it meanwhile
    pending = _run['reload']
    if not pending or _run['restarting'] or not is_standby():
        return None
    return max(0.0, pending['since'] + RELOAD_SETTLE - time.monotonic())


def _reload_if_due():
    """The reload that waits, once it has settled: from its timer, and after every pull
    in case the timer never came. Returns True when it reloaded something."""
    if _reload_wait() != 0:
        return False
    return reload_managers()


def reload_managers(wait=False):
    """A standby with the live view: bring the running managers in line with how the
    configuration says they connect, in this process. A cluster, PBS or ESXi server
    that is new is built and started the way main() starts it (app._start_managers),
    read-only like every manager on a standby; one that is gone is stopped and dropped;
    one whose connection changed is stopped and built again. The others keep running,
    and so does every session and console of this instance.

    One at a time. Another caller gets False at once, or with `wait` (the admin's
    "apply now") waits for the one that runs. The configuration is read under the pull
    lock, never halfway through a sync; a sync that lands while the managers are being
    rebuilt notes its change, which is reloaded after this one. Returns True when a
    manager was added, dropped or rebuilt."""
    got = _reload_lock.acquire(timeout=PULL_TIMEOUT + 5) if wait else _reload_lock.acquire(False)
    if not got:
        return False
    started_with = _run['reload']
    try:
        return _reload_locked()
    finally:
        _reload_lock.release()
        pending = _run['reload']
        if pending and pending is not started_with:
            # came in meanwhile, and its timer may have found the lock taken
            _reload_later(max(0.0, pending['since'] + RELOAD_SETTLE - time.monotonic()) + 1)


def _reload_locked():
    # a restart is on its way, or about to be asked for: it takes the managers along
    if not is_standby() or not _run['managers'] or _run['restarting'] or _live_view_switch():
        return False
    if not _pull_lock.acquire(timeout=PULL_TIMEOUT + 5):
        logging.warning("[HA] a sync is taking long - the managers are reloaded after it")
        return False
    try:
        from pegaprox.core.db import get_db
        items = _identity_items()
        clusters = get_db().get_all_clusters()
    except Exception as e:
        logging.warning(f"[HA] could not read the connection settings to reload the managers: {e}")
        return False
    finally:
        _pull_lock.release()
    before = _run['items']
    if before is None:
        # what they started from could not be read at the start: every running one
        # counts as changed
        before = {key: None for key in _running_keys()}
    added = sorted(items.keys() - before.keys())
    removed = sorted(before.keys() - items.keys())
    changed = sorted(k for k in items.keys() & before.keys() if items[k] != before[k])
    sig = _signature_of(items)
    reason = _describe_change(before, items)
    failed = _rebuild(added, removed, changed, clusters) if added or removed or changed else None
    with _lock:
        _run.update(signature=sig, items=dict(items))
        pending = _run['reload']
        if pending and pending['signature'] == sig:
            _run['reload'] = None
        if failed is not None:
            _run['last_reload'] = {'at': _now(), 'reason': reason, 'failed': failed}
    if failed is None:
        return False
    _after_rebuild(items)
    note = f" - could not build {', '.join(failed)}" if failed else ''
    _audit('ha.managers_reloaded', reason + note)
    logging.warning(f"[HA] reloaded the managers for the configuration of the active "
                    f"instance: {reason}{note}")
    return True


def _after_rebuild(built):
    """A sync that landed while the managers were being rebuilt handed its rows to the
    old, stopped ones and was compared with what ran before, so one that undid the
    change cleared the reload it is owed now. Once more under the pull lock, against the
    managers that run now: hand the rows over in place, and note a reload when how they
    connect is no longer `built`. A sync that holds the lock meanwhile does both itself."""
    if not _pull_lock.acquire(timeout=PULL_TIMEOUT + 5):
        return
    try:
        try:
            _refresh_managers()
        except Exception as e:
            logging.warning(f"[HA] could not refresh the reloaded managers: {e}")
        items = _identity_items()
    except Exception as e:
        logging.warning(f"[HA] could not read the connection settings after the reload: {e}")
        return
    finally:
        _pull_lock.release()
    if items != built:
        _note_signature(_signature_of(items), items)


def _registries():
    from pegaprox import globals as g
    return {'cluster': g.cluster_managers, 'pbs': g.pbs_managers, 'vmware': g.vmware_managers}


def _running_keys():
    regs = _registries()
    keys = {f'pbs:{mid}' for mid in list(regs['pbs'])} | {f'vmware:{mid}' for mid in list(regs['vmware'])}
    # an ESXi host is listed among the clusters too (XHM), and counts as its vmware entry
    return keys | {f'cluster:{mid}' for mid, mgr in list(regs['cluster'].items())
                   if getattr(mgr, 'cluster_type', None) != 'esxi'}


def _drop(regs, kind, mid, mgr):
    """Take `mgr` out of its registry, and an ESXi host's cluster entry with it. Only
    that object: a newer one under the same id stays."""
    if mgr is None:
        return
    if regs[kind].get(mid) is mgr:
        regs[kind].pop(mid, None)
    if kind == 'vmware':
        entry = regs['cluster'].get(mid)
        if getattr(entry, 'cluster_type', None) == 'esxi' and getattr(entry, '_vmware', None) is mgr:
            regs['cluster'].pop(mid, None)


def _rebuild(added, removed, changed, clusters):
    """Stop what is gone or changed, build what is new or changed, drop what is gone.
    Returns the keys that could not be built; their managers are gone as well, like
    the ones a start cannot build.

    A changed manager stays in its registry, stopped, until the new one takes its
    place, so a request in between still finds the cluster. stop() acts on nothing
    here: it ends the manager's own threads, and the self-fence agents are stopped only
    where the HA monitor ran and this instance acts (manager.stop_ha_monitor)."""
    from pegaprox.app import _start_managers
    regs = _registries()
    old = {}
    for key in removed + changed:
        kind, _, mid = key.partition(':')
        mgr = old[key] = regs[kind].get(mid)
        # PBS and ESXi servers have no loop of their own to stop
        if kind == 'cluster' and mgr is not None:
            try:
                mgr.stop()
            except Exception as e:
                logging.warning(f"[HA] could not stop the manager of cluster {mid}: {e}")
    fresh = added + changed
    for key in fresh:
        kind, _, mid = key.partition(':')
        if kind == 'cluster' and mid in clusters:
            # one at a time: a cluster that cannot be built keeps none of the others back
            try:
                _start_managers({mid: clusters[mid]}, only=())
            except Exception as e:
                logging.warning(f"[HA] could not start the manager of cluster {mid}: {e}")
    servers = [key for key in fresh if not key.startswith('cluster:')]
    if servers:
        try:
            _start_managers({}, only=servers)
        except Exception as e:
            logging.warning(f"[HA] could not start the PBS and ESXi managers: {e}")
    for key in removed:
        kind, _, mid = key.partition(':')
        _drop(regs, kind, mid, old[key])
    failed = []
    for key in fresh:
        kind, _, mid = key.partition(':')
        mgr = regs[kind].get(mid)
        if mgr is None or mgr is old.get(key):
            failed.append(key)
            _drop(regs, kind, mid, old.get(key))
        elif kind == 'vmware':
            # rebuilt, and no ESXi host any more: its old cluster entry goes
            _drop(regs, 'vmware', mid, old.get(key))
    return failed


def _restart_for_config(reason):
    with _lock:
        if _run['restarting']:
            return False
        _run['restarting'] = True
    _audit('ha.restart_for_config', reason)
    logging.warning(f"[HA] restarting to pick up the configuration of the active instance: {reason}")
    restart_process(f'configuration changed on the active instance: {reason}')
    return True


def apply_config_now():
    """The admin's "apply now" on a standby. A live view switched since this process
    started restarts it at once. A change to how the managers connect, waiting or not
    looked at yet, is reloaded now, past the settle time.

    Returns 'restart' when a restart is on its way, 'reload' when managers were
    reloaded, False when there is nothing to apply. Raises HaError anywhere but on a
    standby."""
    if not is_standby():
        raise HaError('Only a standby takes its configuration from the active instance')
    switch = _live_view_switch()
    if switch:
        return 'restart' if _restart_for_config(f'the live view was switched {switch}') else False
    if _run['managers'] and reload_managers(wait=True):
        return 'reload'
    return False


def _restart_pending():
    """public_status: None, or since and reason of the restart this standby waits for:
    a live view switched since it started."""
    if not is_standby():
        return None
    switch = _live_view_switch()
    if switch:
        return {'since': _run['live_view_changed'] or _now(),
                'reason': f'the live view was switched {switch}'}
    return None


def _reload_pending():
    """public_status: None, or since and reason of the reload that waits to settle."""
    pending = _run['reload'] if is_standby() else None
    return {'since': pending['since_iso'], 'reason': pending['reason']} if pending else None


# --- secrets and codes ---------------------------------------------------------

def _hash_secret(value):
    return hashlib.sha256(('pegaprox-ha:' + (value or '')).encode()).hexdigest()


def _new_signing_key():
    """A fresh Ed25519 private key, as the state file keeps it."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    raw = Ed25519PrivateKey.generate().private_bytes(
        serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption())
    return base64.b64encode(raw).decode()


def _private_key(value):
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    return Ed25519PrivateKey.from_private_bytes(base64.b64decode(value))


def _public_of(private):
    from cryptography.hazmat.primitives import serialization
    return base64.b64encode(private.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw)).decode()


# The y coordinates of the eight Ed25519 points of small order (libsodium keeps the
# same list). OpenSSL takes them as public keys, and under the identity point one
# fixed signature holds for every message: such a key would be an identity anybody
# can sign as.
_ED25519_P = 2 ** 255 - 19
_ORDER_8_Y = 2707385501144840649318225287225658788936804267575313519463743609750303402022
_SMALL_ORDER_Y = frozenset((0, 1, _ED25519_P - 1, _ORDER_8_Y, _ED25519_P - _ORDER_8_Y))


def _public_key(value):
    """The Ed25519 public key in `value` (base64 of the raw 32 bytes), None for anything
    else, a point of small order included."""
    if not isinstance(value, str) or not _PUBLIC_KEY_RE.fullmatch(value):
        return None
    raw = base64.b64decode(value)
    # the sign bit of x left out, and y taken mod p: the encodings above p are the
    # same points
    if (int.from_bytes(raw, 'little') & ((1 << 255) - 1)) % _ED25519_P in _SMALL_ORDER_Y:
        return None
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    try:
        return Ed25519PublicKey.from_public_bytes(raw)
    except Exception:
        return None


def peer_key_fingerprint(public_key):
    """A short digest of a member's public key for the status page, '' without one."""
    if not _public_key(public_key):
        return ''
    return hashlib.sha256(b'pegaprox-ha-peer-key:' + base64.b64decode(public_key)).hexdigest()[:16]


def own_public_key():
    """The public key of this instance, '' until it has one."""
    value = _load().get('signing_key')
    try:
        return _public_of(_private_key(value)) if value else ''
    except Exception:
        return ''


def _wire_body(json_body):
    """The bytes a peer call carries, the same ones its signature covers."""
    if json_body is None:
        return b''
    return json.dumps(json_body, separators=(',', ':'), sort_keys=True).encode()


def _body_digest(body):
    return hashlib.sha256(body or b'').hexdigest()


def _to_sign(method, path, body, ts, nonce, receiver, sender, digest=None):
    return '\n'.join(('pegaprox-ha-peer-1', method.upper(), path,
                      digest if digest is not None else _body_digest(body), ts, nonce,
                      receiver, sender)).encode()


def _signed_headers(private, sender, receiver, method, path, body):
    ts, nonce, digest = str(int(time.time())), secrets.token_urlsafe(18), _body_digest(body)
    sig = private.sign(_to_sign(method, path, body, ts, nonce, receiver, sender, digest))
    return {PEER_HEADER: sender, PEER_TS_HEADER: ts, PEER_NONCE_HEADER: nonce,
            PEER_SIG_HEADER: base64.b64encode(sig).decode(), PEER_BODY_HEADER: digest}


class _Signer:
    """Who this instance is to the other members, read once: a caller that fans out
    hands it to every call instead of reading the state in each of them."""

    def __init__(self, instance_id, private=None, secret=None):
        self.instance_id, self.private, self.secret = instance_id, private, secret
        self.public_key = _public_of(private) if private is not None else ''


def _signer():
    """This instance's _Signer. Makes the key pair on first use, once it is paired: a
    group from before the keys has none yet. A key is written before it is used, or a
    member would take one that a restart forgets. When it cannot be written, the old
    secret alone still reaches the members that hold its hash."""
    with _lock:
        st = _load()
        if not st.get('signing_key') and (st.get('members') or st.get('member_secret')):
            try:
                _commit_locked(dict(st, signing_key=_new_signing_key()))
            except Exception as e:
                logging.warning(f"[HA] could not save a key pair for the peer calls: {e}")
            st = _load()
        value, secret = st.get('signing_key'), st.get('member_secret') or None
        if not value and not secret:
            raise HaError('Not paired')
        private = None
        if value:
            try:
                private = _private_key(value)
            except Exception as e:
                if not secret:
                    raise HaError(f'The key pair in the HA state file cannot be read ({type(e).__name__})')
        return _Signer(st['instance_id'], private, secret)


def _auth_for(signer, receiver, legacy=False):
    """The headers of one call to `receiver`, as auth for _peer_call: signed, and with
    `legacy` also the old '<id>:<secret>' and our public key, for a member that may
    not hold that key yet. It records the key from such a call and says so in the
    answer (PEER_KEYED_HEADER); a member from before the keys goes by the secret."""
    def auth(method, path, body):
        h = {}
        if signer.private is not None:
            h.update(_signed_headers(signer.private, signer.instance_id, receiver, method, path, body))
        if legacy and signer.secret:
            h[PEER_HEADER] = f'{signer.instance_id}:{signer.secret}'
            if signer.public_key:
                h[PEER_KEY_HEADER] = signer.public_key
        if PEER_HEADER not in h:
            raise HaError('Not paired')
        return h
    return auth


def forget_seen_nonces():
    """For tests: start the replay cache over."""
    with _nonce_lock:
        _seen_nonces.clear()


def _fresh_nonce(receiver, sender, nonce, ts):
    """True the first time `nonce` comes from `sender` within the window. Only called
    once the signature is good, so nobody else can fill a member's share."""
    now = time.time()
    with _nonce_lock:
        seen = _seen_nonces.setdefault((receiver, sender), {})
        for n in [n for n, until in seen.items() if until < now]:
            del seen[n]
        if nonce in seen:
            return False
        if len(seen) >= _NONCES_PER_SENDER:
            logging.warning(f"[HA] member {sender} sent more signed calls than the replay "
                            "cache holds - refusing until they age out")
            return False
        seen[nonce] = ts + SIGNATURE_WINDOW + 1
        return True


def _signature_check(headers, method, path, body, sender, public_key, receiver):
    """'ok' when the signature headers hold for this call, from `sender` under
    `public_key` and for `receiver`, inside the window and with a nonce not seen
    before. 'skewed' for a good signature from outside the window: the member's clock
    is off, or the call is an old one. A call signed before this process started is
    one of those too, since the nonces seen until then are gone. '' for anything else."""
    ts, nonce, sig = (headers.get(PEER_TS_HEADER), headers.get(PEER_NONCE_HEADER),
                      headers.get(PEER_SIG_HEADER))
    if not all(isinstance(v, str) for v in (ts, nonce, sig)):
        return ''
    if not (_TS_RE.fullmatch(ts) and _NONCE_RE.fullmatch(nonce) and _SIGNATURE_RE.fullmatch(sig)):
        return ''
    key = _public_key(public_key)
    if key is None:
        return ''
    from cryptography.exceptions import InvalidSignature
    try:
        key.verify(base64.b64decode(sig), _to_sign(method, path, body, ts, nonce, receiver, sender))
    except (InvalidSignature, ValueError):
        return ''
    if abs(time.time() - int(ts)) > SIGNATURE_WINDOW:
        logging.warning(f"[HA] a signed call from member {sender} is {int(time.time()) - int(ts)}s "
                        "off our clock - are the clocks of the members in sync?")
        return 'skewed'
    if int(ts) < _PROCESS_STARTED:
        # whether we took it before the restart is not known any more: a member whose
        # clock is behind hears HA_CLOCK for that long, a replay nothing better
        logging.info(f"[HA] a signed call from member {sender} is older than this process")
        return 'skewed'
    return 'ok' if _fresh_nonce(receiver, sender, nonce, int(ts)) else ''


def _signature_ok(headers, method, path, body, sender, public_key, receiver):
    return _signature_check(headers, method, path, body, sender, public_key, receiver) == 'ok'


def signed_before_body(headers, method, path):
    """Before the body of a large peer call is read: True when its headers carry a
    good signature, inside the window, from a member we hold a key of, over the body
    digest they name. Nothing more - the nonce is not spent here and the body is not
    known yet; peer_verdict checks both once it is read, and a body that is not the
    one named fails there."""
    try:
        claimed = (headers.get(PEER_HEADER) or '').partition(':')[0]
        digest = headers.get(PEER_BODY_HEADER)
        ts, nonce, sig = (headers.get(PEER_TS_HEADER), headers.get(PEER_NONCE_HEADER),
                          headers.get(PEER_SIG_HEADER))
        if not (isinstance(digest, str) and _DIGEST_RE.fullmatch(digest)
                and all(isinstance(v, str) for v in (ts, nonce, sig))
                and _ID_RE.fullmatch(claimed) and _TS_RE.fullmatch(ts)
                and _NONCE_RE.fullmatch(nonce) and _SIGNATURE_RE.fullmatch(sig)):
            return False
        if abs(time.time() - int(ts)) > SIGNATURE_WINDOW or int(ts) < _PROCESS_STARTED:
            return False
        st = _load()
        rec = (st.get('members') or {}).get(claimed) or {}
        key = _public_key(rec.get('public_key')) if rec.get('public_key') else None
        if key is None:
            return False
        from cryptography.exceptions import InvalidSignature
        try:
            key.verify(base64.b64decode(sig), _to_sign(method, path, None, ts, nonce,
                                                       st['instance_id'], claimed, digest))
        except (InvalidSignature, ValueError):
            return False
        return True
    except Exception as e:
        logging.debug(f"[HA] could not check the headers of a peer call: {e}")
        return False


def _legacy_ok(secret, digest):
    return (bool(secret) and isinstance(digest, str) and bool(digest)
            and hmac.compare_digest(_hash_secret(secret), digest))


def _record_key(member_id, public_key):
    """A member from before the keys has shown it holds `public_key`: from now on only
    its signed calls count. True when we hold the key afterwards."""
    with _lock:
        st = _load()
        ms = dict(st.get('members') or {})
        rec = ms.get(member_id)
        if rec is None:
            return False
        if rec.get('public_key'):
            return rec['public_key'] == public_key
        ms[member_id] = dict(rec, public_key=public_key)
        try:
            _commit_locked(dict(st, members=ms))
        except Exception as e:
            logging.warning(f"[HA] could not record the key of member {member_id}: {e}")
            return False
    logging.info(f"[HA] member {member_id} signs its calls from now on")
    return True


def peer_verdict(headers, method, path, body):
    """Who sent a peer call: ('member', record), ('removed', tombstone), ('skewed',
    record) or (None, None).

    `headers` is the request's, `path` its path (a peer call carries no query
    string), `body` is every byte of it. A member with a public key on record needs a
    good signature, made for us. One with only the hash of a secret (paired before
    the keys) needs that secret. A key it sends along with a signature over this very
    call is its key from then on, but only when the secret was made for our pair
    (pair_secret): a member list from the active can carry the hash of a secret as
    well, and more instances than the member itself may know that one. The record
    carries keyed=True when we hold the key the call was signed with. 'skewed' is a
    member whose signature is good but whose time is outside the window (or before
    our start): refused, but told why, since that is a clock to fix and no sign
    that it was removed. A removed member is known by the same credentials, so
    the answer it gets tells it and nobody else that it is out."""
    raw = headers.get(PEER_HEADER) if headers is not None else None
    if not isinstance(raw, str) or not raw:
        return None, None
    claimed, _, secret = raw.partition(':')
    if not _ID_RE.fullmatch(claimed):
        return None, None
    st = _load()
    me = st['instance_id']
    rec = (st.get('members') or {}).get(claimed)
    if rec is not None:
        rec = dict(rec, instance_id=claimed)
        if rec.get('public_key'):
            check = _signature_check(headers, method, path, body, claimed, rec['public_key'], me)
            if check == 'ok':
                return 'member', dict(rec, keyed=True)
            if check == 'skewed':
                return 'skewed', rec
            return None, None
        if not _legacy_ok(secret, rec.get('secret_hash')):
            return None, None
        offered, keyed = headers.get(PEER_KEY_HEADER), False
        if (rec.get('pair_secret') is True and isinstance(offered, str) and _public_key(offered)
                and _signature_ok(headers, method, path, body, claimed, offered, me)):
            keyed = _record_key(claimed, offered)
        return 'member', dict(rec, keyed=keyed)
    tomb = (st.get('tombstones') or {}).get(claimed)
    if tomb:
        # an old call of a removed member hears the same, whatever its time
        if tomb.get('public_key') and _signature_check(headers, method, path, body, claimed,
                                                       tomb['public_key'], me):
            return 'removed', dict(tomb, instance_id=claimed)
        if tomb.get('secret_hash') and _legacy_ok(secret, tomb['secret_hash']):
            return 'removed', dict(tomb, instance_id=claimed)
    return None, None


def verify_peer(headers, method='GET', path='', body=b''):
    """The member record (with its instance_id) when a peer call is from a member, else
    None. See peer_verdict."""
    kind, who = peer_verdict(headers, method, path, body)
    return who if kind == 'member' else None


def key_fingerprint(key=None):
    """A short, domain-separated digest of the field key, never the key itself."""
    if key is None:
        from pegaprox.core.db import get_db
        key = get_db().aes_key or b''
    return hashlib.sha256(b'pegaprox-ha-key-fp:' + key).hexdigest()[:16]


def _seal_key(code_secret, salt):
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=salt,
                info=b'pegaprox-ha-pairing').derive(code_secret.encode())


def _seal(code_secret, payload, aad):
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    salt, nonce = os.urandom(16), os.urandom(12)
    ct = AESGCM(_seal_key(code_secret, salt)).encrypt(
        nonce, json.dumps(payload).encode(), aad.encode())
    return base64.b64encode(salt + nonce + ct).decode()


def _unseal(code_secret, blob, aad):
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    raw = base64.b64decode(blob)
    salt, nonce, ct = raw[:16], raw[16:28], raw[28:]
    return json.loads(AESGCM(_seal_key(code_secret, salt)).decrypt(nonce, ct, aad.encode()))


def _valid_host(host):
    if len(host) > 253:
        return False
    labels = host.split('.')
    if not all(_DNS_LABEL_RE.fullmatch(label) for label in labels):
        return False
    if all(label.isdigit() for label in labels):
        # all digits is an IPv4 address or nothing
        try:
            ipaddress.IPv4Address(host)
        except ValueError:
            return False
    return True


def valid_https_url(url):
    """`url` as https://host[:port][/path] with the trailing slash gone, or ''.

    One shape for every address that travels: the admin routes, the address a
    pairing code carries and the one a standby sends. The host is a DNS name, an
    IPv4 address or a bracketed IPv6 address. No user info, query, fragment,
    percent escapes, whitespace or control characters. Too long is refused, not cut.
    """
    if not isinstance(url, str):
        return ''
    url = url.strip()
    if not url or len(url) > _MAX_URL_LEN or not url.startswith('https://'):
        return ''
    url = url.rstrip('/')
    if not _URL_CHARS_RE.fullmatch(url):
        return ''
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        return ''
    if parts.scheme != 'https' or parts.query or parts.fragment or '@' in parts.netloc:
        return ''
    netloc = parts.netloc
    if netloc.startswith('['):
        host, bracket, port = netloc[1:].partition(']')
        if not bracket:
            return ''
        try:
            ipaddress.IPv6Address(host)
        except ValueError:
            return ''
        host = f'[{host.lower()}]'
    else:
        host, colon, port = netloc.partition(':')
        port = colon + port
        host = host.lower()
        if not _valid_host(host):
            return ''
    if port:
        if not _URL_PORT_RE.fullmatch(port) or not 0 < int(port[1:]) < 65536:
            return ''
        port = f':{int(port[1:])}'
    if parts.path and not _URL_PATH_RE.fullmatch(parts.path):
        return ''
    return f'https://{host}{port}{parts.path}'


def encode_code(url, fingerprint, secret, active_id):
    body = json.dumps({'u': url, 'f': fingerprint or '', 'c': secret, 'i': active_id},
                      separators=(',', ':')).encode()
    return CODE_PREFIX + base64.urlsafe_b64encode(body).decode().rstrip('=')


def decode_code(code):
    code = (code or '').strip()
    if not code.startswith(CODE_PREFIX):
        raise HaError('This is not a PegaProx pairing code')
    raw = code[len(CODE_PREFIX):]
    try:
        body = json.loads(base64.urlsafe_b64decode(raw + '=' * (-len(raw) % 4)))
    except Exception:
        raise HaError('The pairing code is damaged - copy it again')
    if not isinstance(body, dict) or not all(isinstance(body.get(k) or '', str) for k in 'ufci'):
        raise HaError('The pairing code is damaged - copy it again')
    url = valid_https_url(body.get('u') or '')
    if not url:
        raise HaError('The pairing code does not carry a usable https:// address')
    fp = (body.get('f') or '').strip().upper()
    if fp and not re.match(r'^[0-9A-F]{2}(:[0-9A-F]{2}){31}$', fp):
        raise HaError('The pairing code carries a malformed certificate fingerprint')
    secret, active_id = body.get('c') or '', body.get('i') or ''
    if len(secret) < 32 or not re.match(r'^[0-9a-f]{32}$', active_id):
        raise HaError('The pairing code is incomplete')
    return {'url': url, 'fingerprint': fp, 'secret': secret, 'instance_id': active_id}


def create_pairing_code(own_url, fingerprint):
    """On the instance that will be active, or already is. Returns (code, expires_at).

    One code at a time; a new one replaces the old. It is good for PAIRING_TTL. The
    address and pin it carries are also what the member list says about this instance.
    """
    with _lock:
        st = _load()
        if st['role'] == ROLE_STANDBY:
            raise HaError('A standby cannot hand out pairing codes - promote it first')
        if len(st.get('members') or {}) >= MAX_MEMBERS - 1:
            raise HaError(GROUP_FULL_ERROR)
        waiting = group_waiting()
        if waiting:
            raise HaError(_group_waiting_error(waiting))
        secret = secrets.token_urlsafe(32)
        expires = int(time.time()) + PAIRING_TTL
        changes = dict(pairing={'code_hash': _hash_secret(secret), 'expires': expires},
                       own_url=own_url, own_fingerprint=fingerprint or '')
        if not st.get('signing_key'):
            changes['signing_key'] = _new_signing_key()
        _update(**changes)
    return encode_code(own_url, fingerprint, secret, st['instance_id']), expires


def _credentials(rec):
    return {'public_key': rec.get('public_key') or '', 'secret_hash': rec.get('secret_hash') or ''}


def _member_list(st):
    """The group as the active hands it out: the active itself and every member, each
    with address, pin, public key and, for a member paired before the keys, the hash
    of its secret. Sorted, so the etag stays put."""
    out = []
    own = {'public_key': '', 'secret_hash': ''}
    if st.get('signing_key'):
        try:
            own['public_key'] = _public_of(_private_key(st['signing_key']))
        except Exception as e:
            logging.warning(f"[HA] the key pair in the state file cannot be read: {e}")
    if st.get('member_secret'):
        own['secret_hash'] = _hash_secret(st['member_secret'])
    if own['public_key'] or own['secret_hash']:
        out.append(dict(own, instance_id=st['instance_id'], url=st.get('own_url') or '',
                        fingerprint=st.get('own_fingerprint') or ''))
    for mid, rec in (st.get('members') or {}).items():
        out.append(dict(_credentials(rec), instance_id=mid, url=rec.get('url') or '',
                        fingerprint=rec.get('fingerprint') or ''))
    return sorted(out, key=lambda e: e['instance_id'])


def _clean_entries(entries):
    """{instance id: {url, fingerprint, public_key, secret_hash}} from a member list as
    it arrives, one entry per id. Each needs a public key or the hash of a secret.
    Whatever is not well formed is left out; an address that is not plain https
    counts as unknown, and a pin without an address as none."""
    out = {}
    if not isinstance(entries, list):
        return out
    for e in entries[:MAX_MEMBERS * 2]:
        if not isinstance(e, dict):
            continue
        mid, digest, key = e.get('instance_id'), e.get('secret_hash'), e.get('public_key')
        if not (isinstance(mid, str) and _ID_RE.fullmatch(mid)):
            continue
        digest = digest if isinstance(digest, str) and _SECRET_HASH_RE.fullmatch(digest) else ''
        key = key if _public_key(key) else ''
        if not digest and not key:
            continue
        url = valid_https_url(e.get('url')) if e.get('url') else ''
        fp = e.get('fingerprint')
        fp = fp.strip().upper() if isinstance(fp, str) and url else ''
        if fp and not _FP_RE.fullmatch(fp):
            fp = ''
        out.setdefault(mid, {'url': url, 'fingerprint': fp, 'public_key': key, 'secret_hash': digest})
    return out


def _clean_tombstones(entries):
    """{instance id: {epoch, at, by, public_key, secret_hash}} from a tombstone list as
    it arrives. The credentials say which instance is out: one that pairs again comes
    with a new key and is not taken for it."""
    out = {}
    if not isinstance(entries, list):
        return out
    for e in entries[:MAX_TOMBSTONES * 2]:
        if not isinstance(e, dict):
            continue
        mid, ep, at, by = e.get('instance_id'), e.get('epoch'), e.get('at'), e.get('by')
        if not (isinstance(mid, str) and _ID_RE.fullmatch(mid)):
            continue
        if _epoch_value(ep) is None:
            continue
        creds = _clean_entries([dict(e, url='', fingerprint='')]).get(mid)
        if not creds:
            continue
        out[mid] = {'epoch': ep, 'at': at if isinstance(at, str) and len(at) <= 40 else '',
                    'by': by if isinstance(by, str) and _ID_RE.fullmatch(by) else '',
                    'public_key': creds['public_key'], 'secret_hash': creds['secret_hash']}
    return out


def _tombstone_list(st):
    return sorted((dict(t, instance_id=mid) for mid, t in (st.get('tombstones') or {}).items()),
                  key=lambda e: e['instance_id'])


def _bounded_tombstones(tombs):
    keep = sorted(tombs, key=lambda mid: (int(tombs[mid].get('epoch') or 0),
                                          str(tombs[mid].get('at') or ''), mid))[-MAX_TOMBSTONES:]
    return {mid: tombs[mid] for mid in keep}


def _merged_tombstones(local, incoming, me):
    """Ours and the ones that came with a member list, the later of two for one id."""
    out = dict(local or {})
    for mid, t in (incoming or {}).items():
        old = out.get(mid)
        if old is None or (int(t['epoch']), t['at']) > (int(old.get('epoch') or 0), str(old.get('at') or '')):
            out[mid] = t
    out.pop(me, None)
    return _bounded_tombstones(out)


def _matches_tombstone(rec, tomb):
    """The member record is the instance the tombstone is about."""
    if not tomb:
        return False
    key, digest = tomb.get('public_key'), tomb.get('secret_hash')
    return bool((key and rec.get('public_key') == key) or (digest and rec.get('secret_hash') == digest))


# --- pairing -------------------------------------------------------------------

def accept_pairing(code_secret, standby_id, standby_url, standby_fp, standby_public_key):
    """Active side of the handshake. Returns the response body for the standby.

    The standby sends its public key. Up to MAX_MEMBERS - 1 standbys; an instance
    that is a member already takes its old place again (it unpaired while we could
    not be told), and one we removed comes back with the new key it pairs with."""
    with _lock:
        st = _load()
        pairing = st.get('pairing') or {}
        valid = (pairing.get('code_hash')
                 and int(pairing.get('expires') or 0) >= int(time.time())
                 and hmac.compare_digest(_hash_secret(code_secret), pairing['code_hash']))
        if not valid:
            raise HaError('The pairing code is wrong or has expired')
        if st['role'] == ROLE_STANDBY:
            raise HaError('This instance cannot take a standby right now')
        if not re.match(r'^[0-9a-f]{32}$', standby_id or '') or standby_id == st['instance_id']:
            raise HaError('The standby did not identify itself')
        ms = dict(st.get('members') or {})
        if len([mid for mid in ms if mid != standby_id]) >= MAX_MEMBERS - 1:
            raise HaError(GROUP_FULL_ERROR)
        for mid, rec in ms.items():
            if mid != standby_id and not _seen_in_group(rec):
                raise HaError(_group_waiting_error(dict(rec, instance_id=mid)))
        # a public key: a standby from before the keys sends the hash of a secret (or
        # the secret itself) and is refused here
        if not _public_key(standby_public_key):
            raise HaError('The standby did not send a usable public key - update it to '
                          'this release and pair again')
        if standby_url and not isinstance(standby_url, str):
            raise HaError('The standby address must be https://host[:port][/path]')
        if standby_url and standby_url.strip():
            # stored, shown and used as the base of every call to the standby
            standby_url = valid_https_url(standby_url)
            if not standby_url:
                raise HaError('The standby address must be https://host[:port][/path]')
        else:
            standby_url = ''
        standby_fp = (standby_fp or '').strip().upper()
        if standby_fp and not _FP_RE.fullmatch(standby_fp):
            raise HaError('The standby sent a malformed certificate fingerprint')

        from pegaprox.core.db import get_db
        field_key = get_db().aes_key
        if not field_key or len(field_key) != 32:
            raise HaError('This instance has no field key to share')

        signing_key = st.get('signing_key') or _new_signing_key()
        new_epoch = max(1, int(st.get('epoch') or 0))
        if _epoch_value(new_epoch) is None:
            # the standby would refuse the answer and still stand in our member list
            raise HaError('This instance holds an epoch no member can read - unpair it here, '
                          'then pair again')
        ms[standby_id] = {
            'url': standby_url,
            'fingerprint': standby_fp,
            'public_key': standby_public_key,
            'role_seen': ROLE_STANDBY,
            'epoch_seen': new_epoch,
            'last_contact': None,
            'last_error': '',
            'joined_at': _now(),
            'group_seen': True,
        }
        tombs = dict(st.get('tombstones') or {})
        tombs.pop(standby_id, None)
        new = dict(st, role=ROLE_ACTIVE, epoch=new_epoch, pairing=None, signing_key=signing_key,
                   members=ms, source=None, tombstones=tombs, removed=None)
        _commit_locked(new)
        # the member list too, so the new standby can verify every other member the
        # day one of them is promoted, and who is out
        sealed = _seal(code_secret, {'field_key': base64.b64encode(field_key).decode(),
                                     'public_key': _public_of(_private_key(signing_key)),
                                     'members': _member_list(new),
                                     'tombstones': _tombstone_list(new)}, aad=standby_id)
        return {'instance_id': st['instance_id'], 'epoch': new_epoch, 'sealed': sealed,
                'key_fp': key_fingerprint(field_key)}


def _check_can_join(st, info):
    if st['role'] != ROLE_STANDALONE or st.get('members'):
        raise HaError('Only a standalone, unpaired instance can become a standby')
    if info['instance_id'] == st['instance_id']:
        raise HaError('That code was made on this instance')


def join(code, own_url, own_fingerprint):
    """Standby side: pair with the active behind `code`, adopt its field key.

    The caller restarts the process afterwards. Until the active has answered, the
    sealed payload has opened and every field of the answer checks out, the only
    write is the withdrawal of this instance's own open pairing code.
    """
    info = decode_code(code)
    raw_url = own_url.strip() if isinstance(own_url, str) else ''
    own_url = valid_https_url(raw_url) if raw_url else ''
    if raw_url and not own_url:
        raise HaError("This instance's address must be https://host[:port][/path]")
    with _lock:
        st = _load()
        _check_can_join(st, info)
        me = st['instance_id']
        if st.get('pairing'):
            # a code of our own, redeemed while we wait for the other active, would
            # make us its active and then be overwritten below: one pairing at a time
            _commit_locked(dict(st, pairing=None))
    # a fresh key pair for every group this instance joins; only the public half leaves
    my_key = _new_signing_key()
    body = {'code': info['secret'], 'instance_id': me, 'url': own_url,
            'fingerprint': own_fingerprint or '', 'public_key': _public_of(_private_key(my_key))}
    resp = _peer_call('POST', info['url'], info['fingerprint'], '/api/ha/peer/pair',
                      json_body=body, auth=None)
    if resp.status_code != 200:
        raise HaError(_peer_error(resp, 'The active instance refused the pairing'))
    try:
        data = resp.json()
    except Exception:
        data = None
    if not isinstance(data, dict):
        raise HaError('The answer from the active instance could not be read')
    if data.get('instance_id') != info['instance_id']:
        raise HaError('The instance that answered is not the one that made the code')
    try:
        opened = _unseal(info['secret'], data.get('sealed') or '', aad=me)
        field_key = base64.b64decode(opened['field_key'])
        active_key = opened.get('public_key')
        group = opened.get('members', [])
        tombs = opened.get('tombstones', [])
    except Exception:
        raise HaError('The answer from the active instance could not be opened')
    if len(field_key) != 32 or key_fingerprint(field_key) != data.get('key_fp'):
        raise HaError('The field key from the active instance is not intact')
    new_epoch = data.get('epoch')
    if (_epoch_value(new_epoch, low=1) is None
            or not _public_key(active_key)
            or not isinstance(group, list) or not isinstance(tombs, list)):
        raise HaError('The answer from the active instance is incomplete')
    others = _clean_entries(group)
    others.pop(me, None)
    others.pop(info['instance_id'], None)
    tombs = _clean_tombstones(tombs)
    tombs.pop(me, None)

    with _lock:
        st = _load()
        if st['role'] == ROLE_STANDALONE and not st.get('members') and st['instance_id'] == me:
            now = _now()
            ms = {info['instance_id']: {
                'url': info['url'], 'fingerprint': info['fingerprint'], 'public_key': active_key,
                'role_seen': ROLE_ACTIVE, 'epoch_seen': new_epoch, 'last_contact': now,
                'last_error': '', 'joined_at': now, 'group_seen': True}}
            for mid in sorted(others)[:MAX_MEMBERS - 2]:
                if _matches_tombstone(others[mid], tombs.get(mid)):
                    continue
                ms[mid] = dict(others[mid], role_seen=None, epoch_seen=0, last_contact=None,
                               last_error='', joined_at=now)
            # standby first, key second: if the key write fails we are a passive
            # standby whose sync refuses a key mismatch, not a standalone that acts
            # on a foreign key. Both under the lock, so accept_pairing cannot seal
            # the adopted key to anybody in between.
            _commit_locked(dict(st, role=ROLE_STANDBY, epoch=new_epoch, pairing=None, sync={},
                                signing_key=my_key, member_secret=None, members=ms,
                                tombstones=_bounded_tombstones(tombs), removed=None,
                                source=info['instance_id']))
            try:
                _install_field_key(field_key)
            except Exception as e:
                try:
                    _update_sync(last_error=f'The field key from the active instance could not be written: {e}'[:300])
                except Exception as e2:
                    # the same full disk, usually - the HaError below still says what happened
                    logging.warning(f"[HA] could not note the failed key write either: {e2}")
                raise HaError('Paired, but the field key could not be written - check the config '
                              'directory and pair again')
            return peer()

    # something paired with us while the call was out. The active has taken us as
    # its standby by now; tell it, so it does not wait for one that never comes.
    try:
        _peer_call('POST', info['url'], info['fingerprint'], '/api/ha/peer/unpaired',
                   auth=_auth_for(_Signer(me, _private_key(my_key)), info['instance_id']),
                   timeout=10)
    except Exception as e:
        logging.warning(f"[HA] could not tell {info['url']} that the join was dropped: {e}")
    raise HaError('This instance changed its pairing while joining - try again')


def _install_field_key(new_key):
    """Adopt the active's field key, keeping ours as a dated .pre-ha backup (which
    also marks this instance as one that joined a pair)."""
    from pegaprox.core.db import get_db
    if os.path.exists(AES_KEY_FILE):
        with open(AES_KEY_FILE, 'rb') as fh:
            old_key = fh.read()
        base = f"{AES_KEY_FILE}.pre-ha.{datetime.now().strftime('%Y%m%d-%H%M%S')}"
        backup, n = base, 0
        while True:
            try:
                fd = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                break
            except FileExistsError:
                n += 1
                backup = f'{base}.{n}'
        with os.fdopen(fd, 'wb') as fh:
            os.fchmod(fh.fileno(), 0o600)
            fh.write(old_key)
            fh.flush()
            os.fsync(fh.fileno())
    # the swap itself is a key rotation onto the given key: what stays local and was
    # sealed or signed with our own key (acme_* secrets, the VAPID key until the first
    # sync, every audit signature) is re-sealed under the new one in the same
    # transaction that writes the key file, instead of being stranded
    stats = get_db().rotate_encryption_key(new_key=new_key)
    if not stats.get('success'):
        raise HaError('Could not adopt the field key: '
                      + (stats.get('error') or '; '.join(stats.get('errors') or []) or 'unknown error'))


def unpair():
    """Leave the group. A standby becomes standalone, which means it starts acting
    after the restart the caller schedules; an active becomes standalone and the
    members go on without it. Telling the members is the caller's part.

    The one write allowed on a state file _load could not read: it is the way out,
    and the note about the broken file goes with it.
    """
    with _lock:
        st = _load()
        was = st['role']
        new = {k: v for k, v in st.items() if k != 'broken'}
        # the key pair goes too: the next group gets a fresh one. So does the epoch: a
        # standalone has nobody to be ordered against, and the next group counts anew
        # (one at the ceiling would otherwise refuse every promotion there as well)
        new.update(members={}, source=None, member_secret=None, signing_key=None, pairing=None,
                   sync={}, tombstones={}, removed=None, role=ROLE_STANDALONE, epoch=0)
        _commit_locked(new)
        return was


def _mark_removed(by, their_epoch):
    """A member says this instance is out of the group: let go of every member and
    stay passive, an active included, until an admin unpairs it. Returns the role it
    had, None when nothing changed. Never raises."""
    try:
        with _lock:
            st = _load()
            if st.get('broken') or st['role'] == ROLE_STANDALONE:
                return None
            if st.get('removed') and not st.get('members'):
                # heard it already, from another member
                return None
            if _epoch_value(their_epoch) is None:
                their_epoch = int(st.get('epoch') or 0)
            was = st['role']
            _commit_locked(dict(st, role=ROLE_STANDBY, members={}, source=None, sync={}, pairing=None,
                                epoch=max(int(st.get('epoch') or 0), their_epoch),
                                removed={'epoch': their_epoch, 'at': _now(), 'by': by}))
    except Exception as e:
        logging.error(f"[HA] member {by} says this instance was removed, and that could not be "
                      f"saved: {e}")
        return None
    logging.warning(f"[HA] member {by} says this instance was removed from the group (epoch "
                    f"{their_epoch}) - passive until it is unpaired")
    _audit('ha.removed', f"removed from the group, as member {by} says (epoch {their_epoch}); "
                         f"this instance was {was} and stays passive until it is unpaired")
    return was


def _removed_answer(rec, resp):
    """A member answered 410 HA_REMOVED: it holds a tombstone for us. Taken from any
    member, since the answer came from its address under its pin."""
    try:
        data = resp.json()
    except Exception:
        data = None
    if not isinstance(data, dict) or data.get('code') != 'HA_REMOVED':
        return None
    their = _epoch_value(data.get('epoch'))
    if their is None:
        their = int(rec.get('epoch_seen') or 0)
    return _mark_removed(rec['instance_id'], their)


def _speaks_for_group(peer_id, timeout=3):
    """The epoch under which `peer_id` speaks for the group to us, None when it does
    not: on a standby the member it pulls from or one it has seen active; otherwise
    the member has to answer as active right now, under at least our epoch (an active
    wants it newer, or the same and the tie won)."""
    st = _load()
    rec = (st.get('members') or {}).get(peer_id)
    if rec is None:
        return None
    mine = int(st.get('epoch') or 0)
    if st['role'] == ROLE_STANDBY and (peer_id == st.get('source') or rec.get('role_seen') == ROLE_ACTIVE):
        return max(mine, int(rec.get('epoch_seen') or 0))
    try:
        their_role, their_epoch = _ask(dict(rec, instance_id=peer_id), _signer(), timeout)[:2]
    except Exception as e:
        logging.warning(f"[HA] could not ask member {peer_id} whether it is active: {e}")
        return None
    _note_members({peer_id: {'role_seen': their_role, 'epoch_seen': their_epoch}})
    if their_role != ROLE_ACTIVE:
        return None
    if st['role'] == ROLE_ACTIVE:
        wins = their_epoch > mine or (their_epoch == mine and _wins_tie(peer_id, st['instance_id']))
    else:
        wins = their_epoch >= mine
    return their_epoch if wins else None


def forget_peer(peer_id, whole_group=False):
    """A member told us it left the group. Only that member itself may say so, and
    only about itself: we drop it and keep the others. Returns 'group' when this
    instance let go of the whole group, 'member' when it dropped the caller, '' when
    nothing changed.

    whole_group is the active telling us that it removed us: we are out of the group
    then, let go of every member and stay passive (_mark_removed). Taken only from the
    instance the group follows (_speaks_for_group); from anybody else it is a plain
    leave. When we have to ask the member first, it already holds the tombstone and
    answers 410, and we let go right there (_removed_answer): that is the same
    'group'. An active whose last member left is standalone again."""
    st = _load()
    if not isinstance(peer_id, str) or peer_id not in (st.get('members') or {}):
        return ''
    if whole_group:
        their = _speaks_for_group(peer_id)
        if their is not None and _mark_removed(peer_id, their) is not None:
            return 'group'
        after = _load()
        if (after.get('removed') or {}).get('by') == peer_id and not after.get('members'):
            return 'group'
    with _lock:
        st = _load()
        ms = dict(st.get('members') or {})
        if peer_id not in ms:
            return ''
        was = st['role']
        ms.pop(peer_id)
        new = dict(st, members=ms)
        if new.get('source') not in ms:
            new['source'] = None
        if not ms and was == ROLE_ACTIVE:
            new.update(role=ROLE_STANDALONE, member_secret=None)
        _commit_locked(new)
        return 'member'


REMOVE_UNCONFIRMED_ERROR = ('This instance has not answered as a standby under the current '
                            'epoch - it may still be active. Let it come back and follow this '
                            'one first, or confirm that it is shut down for good')


def member_confirmed(member_id):
    """The member answered as a standby under our current epoch (see remove_member)."""
    st = _load()
    rec = (st.get('members') or {}).get(member_id)
    return bool(rec) and _confirmed_standby(rec, st)


def refresh_member(member_id, timeout=5):
    """Ask one member for its role and epoch now and note the answer. Never raises:
    a member that does not answer simply stays as the last tick left it."""
    rec = member(member_id)
    if not rec:
        return
    try:
        their_role, their_epoch, mark, serving_seen = _ask(rec, _signer(), timeout)
    except Exception as e:
        logging.info(f"[HA] member {rec.get('url') or member_id} did not answer the check: {e}")
        return
    note = {'last_contact': _now(), 'role_seen': their_role, 'epoch_seen': their_epoch,
            'serving_seen': serving_seen, 'last_error': ''}
    if mark == GROUP_MARK:
        note['group_seen'] = True
    try:
        _note_members({member_id: note})
    except Exception as e:
        logging.warning(f"[HA] could not note the member's answer: {e}")


def remove_member(member_id, shut_down=False):
    """Active: take a standby out of the group. Returns its record, for the caller to
    tell it and the others.

    Only a member seen as a standby under our epoch, unless the admin says it is shut
    down for good (shut_down): an old active that is simply down would otherwise come
    back to a group that refuses it, and act. It stays on record as removed (a
    tombstone), handed out with the member list, so its calls get 410 everywhere and
    a stale member list does not take it back. Removing the last one makes this
    instance standalone, as that standby's own unpairing would."""
    if not shut_down and not member_confirmed(member_id):
        # the last tick may predate our epoch (a promotion minutes ago): ask it now
        # rather than send the admin to the shut-down confirmation for nothing
        refresh_member(member_id)
    with _lock:
        st = _load()
        if st['role'] != ROLE_ACTIVE:
            raise HaError('Only the active instance removes members')
        ms = dict(st.get('members') or {})
        if not isinstance(member_id, str) or member_id not in ms:
            raise HaError('That instance is not a member of this group')
        if not shut_down and not _confirmed_standby(ms[member_id], st):
            raise RemoveUnconfirmed(REMOVE_UNCONFIRMED_ERROR)
        rec = dict(ms.pop(member_id), instance_id=member_id)
        tombs = dict(st.get('tombstones') or {})
        tombs[member_id] = dict(_credentials(rec), epoch=int(st.get('epoch') or 0), at=_now(),
                                by=st['instance_id'])
        new = dict(st, members=ms, tombstones=_bounded_tombstones(tombs))
        if not ms:
            new.update(role=ROLE_STANDALONE, member_secret=None)
        _commit_locked(new)
    return rec


def tombstone(member_id):
    """The tombstone of a removed member, None for anybody else."""
    t = (_load().get('tombstones') or {}).get(member_id)
    return dict(t, instance_id=member_id) if t else None


def note_member_removed(by_id, member_id, their_epoch):
    """The active took `member_id` out of the group and tells us right away, not only
    with the next member list: drop it and keep its tombstone. Taken only from the
    instance the group follows (_speaks_for_group). Returns True when it was dropped."""
    st = _load()
    if (not isinstance(member_id, str) or member_id == st['instance_id']
            or member_id == by_id or member_id not in (st.get('members') or {})):
        return False
    if _epoch_value(their_epoch) is None:
        return False
    if _speaks_for_group(by_id) is None:
        return False
    with _lock:
        st = _load()
        ms = dict(st.get('members') or {})
        rec = ms.pop(member_id, None)
        if rec is None:
            return False
        tombs = dict(st.get('tombstones') or {})
        tombs[member_id] = dict(_credentials(rec), epoch=their_epoch, at=_now(), by=by_id)
        new = dict(st, members=ms, tombstones=_bounded_tombstones(tombs))
        if new.get('source') == member_id:
            new['source'] = None
        _commit_locked(new)
    logging.warning(f"[HA] member {by_id} removed member {member_id} from the group")
    return True


def take_tombstones(sender_id, entries):
    """Active: the member `sender_id` holds tombstones (`entries`, as a member list
    carries them) for members we still list. The removal happened while we could not
    hear about it, and we were promoted since. Taken for a member only when the
    tombstone names the credentials we hold for it, and when that member has not
    answered as a standby under our epoch, asked once more now: a removed instance
    never does, so no member takes a live standby out this way. Returns the ids
    taken out."""
    offered = _clean_tombstones(entries)
    st = _load()
    ms = st.get('members') or {}
    if st['role'] != ROLE_ACTIVE or sender_id not in ms:
        return []
    maybe = [mid for mid in sorted(offered) if mid in ms and mid not in (sender_id, st['instance_id'])
             and _matches_tombstone(ms[mid], offered[mid])]
    for mid in maybe:
        if not member_confirmed(mid):
            refresh_member(mid)
    taken = []
    with _lock:
        st = _load()
        ms = dict(st.get('members') or {})
        if st['role'] != ROLE_ACTIVE or sender_id not in ms:
            return []
        tombs = dict(st.get('tombstones') or {})
        for mid in maybe:
            rec = ms.get(mid)
            if rec is None or not _matches_tombstone(rec, offered[mid]) or _confirmed_standby(rec, st):
                continue
            ms.pop(mid)
            tombs[mid] = offered[mid]
            taken.append(mid)
        if taken:
            _commit_locked(dict(st, members=ms, tombstones=_bounded_tombstones(tombs)))
    for mid in taken:
        logging.warning(f"[HA] member {sender_id} holds a tombstone for member {mid}: out of the "
                        "group here too")
    return taken


# --- promotion and stepping down -------------------------------------------------

def promote():
    """Standby to active under a new epoch, one above every epoch this instance has
    seen in the group. The caller restarts the process."""
    with _lock:
        st = _load()
        if st.get('broken'):
            # the stand-in for an unreadable file has no members and a fresh identity:
            # an active made from it could never tell the real active to step down
            raise HaError('The HA state file cannot be read - restore config/ha_state.json '
                          'and restart before promoting')
        if st['role'] != ROLE_STANDBY:
            raise HaError('Only a standby can be promoted')
        if st.get('removed'):
            # an active of its own would act next to the group that took it out
            raise HaError(f'{REMOVED_ERROR} - unpair it here first')
        ms = st.get('members') or {}
        seen = max([int(rec.get('epoch_seen') or 0) for rec in ms.values()] + [0])
        new_epoch = max(int(st.get('epoch') or 0), seen) + 1
        if new_epoch > EPOCH_MAX:
            # no member could read it: the old active would never step down to us
            raise HaError('The group has reached the highest epoch there is - unpair every '
                          'instance and pair them again')
        # whoever was active may not be once it hears about us
        ms = {mid: dict(rec, role_seen=None) if rec.get('role_seen') == ROLE_ACTIVE else dict(rec)
              for mid, rec in ms.items()}
        _commit_locked(dict(st, epoch=new_epoch, role=ROLE_ACTIVE, members=ms, source=None))
        return new_epoch


def _wins_tie(one, other):
    """Two actives under the same epoch: the higher instance id stays active."""
    return str(one) > str(other)


def step_down(new_epoch, by_peer_id):
    """Active to standby of `by_peer_id`, because that member is active under a newer
    epoch, or under ours and wins the tie. Returns True when this call changed the
    role; the caller restarts the process then."""
    with _lock:
        st = _load()
        ms = st.get('members') or {}
        if not isinstance(by_peer_id, str) or by_peer_id not in ms or st['role'] != ROLE_ACTIVE:
            return False
        if _epoch_value(new_epoch) is None:
            return False
        mine = int(st.get('epoch') or 0)
        if new_epoch < mine or (new_epoch == mine and not _wins_tie(by_peer_id, st['instance_id'])):
            return False
        ms = dict(ms)
        ms[by_peer_id] = dict(ms[by_peer_id], role_seen=ROLE_ACTIVE, epoch_seen=new_epoch)
        _commit_locked(dict(st, role=ROLE_STANDBY, epoch=new_epoch, sync={}, members=ms,
                            source=by_peer_id))
    logging.warning(f"[HA] stepped down: member {by_peer_id} is active with epoch {new_epoch}")
    return True


def step_aside(new_epoch, reason):
    """Active to a passive standby that follows nobody yet: the group has moved on
    without us (a member reports a newer epoch and no active under it answers), or
    every member refuses us. The next look at the group follows the active once one
    answers under at least that epoch. Returns True when this call changed the role;
    the caller restarts the process then."""
    with _lock:
        st = _load()
        if st['role'] != ROLE_ACTIVE:
            return False
        mine = int(st.get('epoch') or 0)
        new_epoch = mine if _epoch_value(new_epoch) is None else new_epoch
        _commit_locked(dict(st, role=ROLE_STANDBY, epoch=max(mine, new_epoch),
                            source=None, sync={'last_error': f'Stepped aside: {reason}'[:300]}))
    logging.warning(f"[HA] stepped aside to a passive standby: {reason}")
    return True


def restart_process(reason):
    """Restart so managers and loops come up in the new role. Same order as the
    other restart paths: systemd if it runs us, else exec ourselves."""
    def _go():
        time.sleep(1.5)
        logging.warning(f"[HA] restarting: {reason}")
        try:
            r = subprocess.run(['systemctl', 'is-active', 'pegaprox'],
                               capture_output=True, text=True, timeout=5)
            if r.returncode == 0:
                cmd = ['systemctl', 'restart', 'pegaprox']
                if hasattr(os, 'geteuid') and os.geteuid() != 0:
                    cmd = ['sudo', '-n'] + cmd
                if subprocess.run(cmd, capture_output=True, timeout=30).returncode == 0:
                    return
        except Exception:
            pass
        try:
            os.execv(sys.executable, [sys.executable] + sys.argv)
        except Exception:
            os._exit(0)
    threading.Thread(target=_go, daemon=True, name='ha-restart').start()


# --- snapshot ------------------------------------------------------------------

def _is_local_setting(key):
    return key in LOCAL_SETTING_KEYS or key.startswith(LOCAL_SETTING_PREFIXES)


def _enc(value):
    if isinstance(value, (bytes, bytearray, memoryview)):
        return {'$b64': base64.b64encode(bytes(value)).decode()}
    return value


def _dec(value):
    if isinstance(value, dict) and '$b64' in value:
        return base64.b64decode(value['$b64'])
    return value


def _existing_tables(cur):
    cur.execute("SELECT name, sql FROM sqlite_master WHERE type = 'table'")
    return {r[0]: r[1] for r in cur.fetchall()}


def _hash_value(h, v):
    # typed and length-prefixed, so no two different rows feed the same bytes
    if v is None:
        h.update(b'n;')
    elif isinstance(v, (bytes, bytearray, memoryview)):
        v = bytes(v)
        h.update(b'b%d:' % len(v))
        h.update(v)
    elif isinstance(v, int):
        h.update(b'i%d;' % v)
    elif isinstance(v, float):
        h.update(b'f' + repr(v).encode() + b';')
    else:
        v = str(v).encode('utf-8', 'surrogatepass')
        h.update(b's%d:' % len(v))
        h.update(v)


def _row_digest(values, masked):
    h = hashlib.sha256()
    for i, v in enumerate(values):
        if i in masked:
            h.update(b'm;')
        else:
            _hash_value(h, v)
    return h.digest()


def _reseal_legacy(db, value, label, stuck):
    """A legacy Fernet token as AES-256-GCM under the field key, anything else as is.

    Only a token our own Fernet key opens is touched; its HMAC rules out a string
    that merely looks like one. Nothing is written back here.
    """
    if not isinstance(value, str) or not value.startswith('gAAAA'):
        return value
    fernet, aesgcm = getattr(db, 'fernet', None), getattr(db, 'aesgcm', None)
    if fernet is None or aesgcm is None:
        return value
    try:
        plain = fernet.decrypt(value.encode()).decode('utf-8')
    except Exception:
        stuck.append(label)
        return value
    return db._encrypt_with_key(plain, aesgcm)


def _reseal_setting(db, key, raw, stuck):
    # server_settings values are JSON text; a very old row may be a bare string
    try:
        value, encoded = json.loads(raw), True
    except (TypeError, ValueError):
        value, encoded = raw, False
    if key == 'webpush_vapid_keypair':
        if not isinstance(value, dict):
            return raw
        pem = _reseal_legacy(db, value.get('private_pem'), f'server_settings.{key}', stuck)
        if pem == value.get('private_pem'):
            return raw
        value = dict(value, private_pem=pem)
    else:
        new = _reseal_legacy(db, value, f'server_settings.{key}', stuck)
        if new == value:
            return raw
        value = new
    return json.dumps(value) if encoded else value


def _row_converter(db, name, cols, stuck):
    """What a row of `name` looks like on the wire, or None when it goes as stored."""
    if name == 'server_settings':
        if 'key' not in cols or 'value' not in cols:
            return None
        ki, vi = cols.index('key'), cols.index('value')

        def convert_setting(vals):
            key = str(vals[ki])
            if key not in SECRET_SETTING_KEYS and key != 'webpush_vapid_keypair':
                return vals
            out = list(vals)
            out[vi] = _reseal_setting(db, key, vals[vi], stuck)
            return out
        return convert_setting
    enc = [i for i, c in enumerate(cols) if c in ENCRYPTED_COLUMNS.get(name, ())]
    if not enc:
        return None

    def convert(vals):
        if not any(isinstance(vals[i], str) and vals[i].startswith('gAAAA') for i in enc):
            return vals
        out = list(vals)
        for i in enc:
            out[i] = _reseal_legacy(db, vals[i], f'{name}.{cols[i]}', stuck)
        return out
    return convert


def _plugin_configs():
    """(plugin id, bytes, text) for each plugins/<id>/config.json worth sending: a
    regular file, valid JSON, within the per-file and total caps."""
    try:
        names = sorted(os.listdir(PLUGINS_DIR))
    except OSError:
        return []
    out, total = [], 0
    for pid in names:
        if not _PLUGIN_ID_RE.fullmatch(pid):
            continue
        path = os.path.join(PLUGINS_DIR, pid, 'config.json')
        try:
            st = os.lstat(path)
            if (not stat.S_ISREG(st.st_mode) or st.st_size > _MAX_PLUGIN_CONFIG_BYTES
                    or total + st.st_size > _MAX_PLUGIN_CONFIG_TOTAL):
                continue
            with open(path, 'rb') as fh:
                raw = fh.read(_MAX_PLUGIN_CONFIG_BYTES + 1)
            if len(raw) > _MAX_PLUGIN_CONFIG_BYTES:
                continue
            text = raw.decode('utf-8')
            json.loads(text)
        except (OSError, ValueError):
            continue
        total += len(raw)
        out.append((pid, raw, text))
    return out


def _walk_files(h, body):
    files = {}
    try:
        with open(KNOWN_HOSTS_FILE, 'rb') as fh:
            raw = fh.read()
        text = raw.decode('utf-8')
    except (OSError, ValueError):
        pass
    else:
        h.update(b'F:ssh_known_hosts;')
        _hash_value(h, raw)
        if body:
            files['ssh_known_hosts'] = text

    branding, total = {}, 0
    try:
        names = sorted(os.listdir(BRANDING_DIR))
    except OSError:
        names = []
    for fn in names:
        path = os.path.join(BRANDING_DIR, fn)
        try:
            if fn.startswith('.') or not os.path.isfile(path):
                continue
            size = os.path.getsize(path)
            if total + size > _MAX_BRANDING_BYTES:
                continue
            with open(path, 'rb') as fh:
                data = fh.read()
        except OSError:
            continue
        total += size
        h.update(b'F:branding;')
        _hash_value(h, fn)
        _hash_value(h, data)
        if body:
            branding[fn] = base64.b64encode(data).decode()
    if body:
        files['branding'] = branding

    configs = {}
    for pid, raw, text in _plugin_configs():
        h.update(b'F:plugin_config;')
        _hash_value(h, pid)
        _hash_value(h, raw)
        if body:
            configs[pid] = text
    if body:
        files['plugin_config'] = configs
    return files


def _walk_snapshot(body):
    """The etag of what a snapshot carries and, with body=True, its tables and files.

    Each row is hashed on its own with VOLATILE_COLUMNS masked, and the row digests
    are sorted, so neither a login nor an INSERT OR REPLACE that moves a row changes
    the etag. It is taken from the values as stored: a resealed legacy value is
    randomized and would change it on every build.
    """
    from pegaprox.core.db import get_db
    db = get_db()
    cur = db.conn.cursor()
    present = _existing_tables(cur)
    h = hashlib.sha256(b'pegaprox-ha-snapshot')
    tables, stuck = {}, []
    for name in SYNC_TABLES:
        if name not in present:
            continue
        cur.execute(f'SELECT * FROM "{name}"')
        cols = [d[0] for d in cur.description]
        masked = {i for i, c in enumerate(cols) if c in VOLATILE_COLUMNS.get(name, ())}
        convert = _row_converter(db, name, cols, stuck) if body else None
        digests, rows = [], []
        for r in cur:
            vals = tuple(r)
            if name == 'server_settings' and _is_local_setting(str(vals[0])):
                continue
            digests.append(_row_digest(vals, masked))
            if body:
                rows.append([_enc(v) for v in (convert(vals) if convert else vals)])
        h.update(b'T')
        _hash_value(h, name)
        _hash_value(h, present[name] or '')
        _hash_value(h, '\x00'.join(cols))
        h.update(b'%d;' % len(digests))
        for d in sorted(digests):
            h.update(d)
        if body:
            cur.execute(f'PRAGMA table_info("{name}")')
            coldefs = {r[1]: [r[2] or '', r[4]] for r in cur.fetchall()}
            tables[name] = {'sql': present[name], 'columns': cols, 'rows': rows,
                            'coldefs': coldefs}
    files = _walk_files(h, body)
    return h.hexdigest()[:32], tables, files, sorted(set(stuck))


def warn_stuck(stuck):
    if stuck:
        logging.warning("[HA] values in the legacy Fernet format that this instance cannot "
                        f"open go to the standby as they are: {', '.join(stuck)} - "
                        "save them again here")


def snapshot_meta():
    """Who we are, read under the state lock. Taken on the hub and handed to
    build_snapshot and snapshot_etag, so the threadpool worker never touches a gevent
    lock. On the active it carries the member list for the standbys."""
    st = _load()
    meta = dict(instance_id=st['instance_id'], role=st['role'],
                epoch=int(st.get('epoch') or 0), key_fp=key_fingerprint(), group=GROUP_MARK)
    if st['role'] == ROLE_ACTIVE:
        meta['members'] = _member_list(st)
        meta['tombstones'] = _tombstone_list(st)
    return meta


def _group_etag(etag, meta):
    """The etag of tables and files, with the member list and the tombstones folded in
    when there are any: a standby added or removed reaches every standby with its next
    poll, not only once the configuration changes as well."""
    group, tombs = meta.get('members'), meta.get('tombstones')
    if not group and not tombs:
        return etag
    h = hashlib.sha256(b'pegaprox-ha-group')
    _hash_value(h, etag)
    _hash_value(h, meta.get('instance_id'))
    _hash_value(h, int(meta.get('epoch') or 0))
    for e in group or []:
        for key in ('instance_id', 'url', 'fingerprint', 'public_key', 'secret_hash'):
            _hash_value(h, e.get(key))
    for t in tombs or []:
        h.update(b'R')
        for key in ('instance_id', 'epoch', 'at', 'by', 'public_key', 'secret_hash'):
            _hash_value(h, t.get(key))
    return h.hexdigest()[:32]


def build_snapshot(meta=None, stuck=None):
    """Everything a standby needs, as a JSON-ready dict.

    From the threadpool, pass `meta` from snapshot_meta() and a `stuck` list to
    collect the legacy values that could not be resealed; the caller logs them on
    the hub. Called without them it does both itself."""
    meta = meta or snapshot_meta()
    etag, tables, files, found = _walk_snapshot(body=True)
    if stuck is None:
        warn_stuck(found)
    else:
        stuck.extend(found)
    return dict(tables=tables, files=files, format=SNAPSHOT_FORMAT, generated_at=_now(),
                etag=_group_etag(etag, meta), **meta)


def snapshot_etag(meta=None):
    """The etag build_snapshot(meta) would put on a snapshot now, without building the
    body: a poll that ends in 304 reads and hashes, nothing more. From the threadpool,
    pass `meta` from snapshot_meta()."""
    meta = meta or snapshot_meta()
    return _group_etag(_walk_snapshot(body=False)[0], meta)


def snapshot_bytes(snap):
    return gzip.compress(json.dumps(snap, default=str).encode(), compresslevel=6)


def apply_snapshot(snap):
    """Replace every SYNC table with the snapshot's rows, in one transaction.

    Refuses a snapshot that is not from the member we pull from, not from an active
    instance, from an older epoch, or sealed under a different field key. Takes the
    member list and the epoch that come with it. Returns a summary.
    """
    p = peer() if is_standby() else None
    if not p:
        raise HaError('Not paired')
    if snap.get('format') != SNAPSHOT_FORMAT:
        raise HaError('The active instance sends a snapshot format this version does not read - update both to the same release')
    if snap.get('instance_id') != p.get('instance_id'):
        raise HaError('The snapshot is not from the paired instance')
    if snap.get('role') != ROLE_ACTIVE:
        raise HaError('The paired instance is not active')
    their_epoch = _epoch_value(snap.get('epoch') or 0)
    if their_epoch is None:
        raise HaError('The active instance sent an epoch this version does not read')
    if their_epoch < epoch():
        raise HaError('The paired instance runs an older epoch than this one')
    if snap.get('key_fp') != key_fingerprint():
        raise HaError('The field key changed on the active instance (key rotation?) - pair again')

    from pegaprox.core.db import get_db
    db = get_db()
    conn = db.conn
    cur = conn.cursor()
    tables = snap.get('tables') or {}
    summary = {'tables': 0, 'rows': 0, 'skipped_columns': {}, 'created': []}
    try:
        if conn.in_transaction:
            conn.commit()
        cur.execute('BEGIN IMMEDIATE')
        cur.execute('PRAGMA defer_foreign_keys = ON')
        before = _sign_in_rows(cur)
        present = _existing_tables(cur)
        for name in SYNC_TABLES:
            t = tables.get(name)
            if name not in present:
                if not t:
                    continue
                sql = t.get('sql') or ''
                if not re.match(r'^\s*CREATE TABLE\s+(IF NOT EXISTS\s+)?"?' + re.escape(name) + r'"?\s*\(', sql):
                    raise HaError(f'Refusing the table definition sent for {name}')
                cur.execute(sql)
                summary['created'].append(name)
            cur.execute(f'PRAGMA table_info("{name}")')
            local_cols = [r[1] for r in cur.fetchall()]
            if name == 'server_settings':
                cur.execute('SELECT key FROM server_settings')
                for (key,) in cur.fetchall():
                    if not _is_local_setting(str(key)):
                        cur.execute('DELETE FROM server_settings WHERE key = ?', (key,))
            else:
                cur.execute(f'DELETE FROM "{name}"')
            if not t:
                continue
            # A column the active has and we do not yet: columns that code adds on first
            # use (custom_scripts.deleted_at and friends) or a newer release. Leaving it
            # out lost data (a soft-deleted script came back as live), so add it without
            # a type; every ALTER in our own migrations checks or tolerates an existing
            # column. Only a name that is not a plain identifier is left out.
            missing = []
            have = {c.lower() for c in local_cols}
            for c in t.get('columns') or []:
                if isinstance(c, str) and c.lower() in have:
                    continue                # SQLite column names ignore case
                if isinstance(c, str) and _COLUMN_NAME_RE.fullmatch(c):
                    _add_column(cur, name, c, (t.get('coldefs') or {}).get(c))
                    local_cols.append(c)
                    have.add(c.lower())
                    summary.setdefault('added_columns', {}).setdefault(name, []).append(c)
                else:
                    missing.append(c)
            if missing:
                summary['skipped_columns'][name] = missing
            cols = [c for c in t.get('columns') or [] if isinstance(c, str) and c.lower() in have]
            idx = [t['columns'].index(c) for c in cols]
            if not cols:
                continue
            placeholders = ','.join('?' * len(cols))
            collist = ','.join(f'"{c}"' for c in cols)
            stmt = f'INSERT OR REPLACE INTO "{name}" ({collist}) VALUES ({placeholders})'
            n = 0
            for row in t.get('rows') or []:
                values = [_dec(row[i]) for i in idx]
                if name == 'server_settings' and _is_local_setting(str(values[cols.index('key')])):
                    continue
                cur.execute(stmt, values)
                n += 1
            summary['tables'] += 1
            summary['rows'] += n
        conn.commit()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        raise

    # the rows are in; nothing below raises, so a caller that got here can count them
    after = _sign_in_rows(conn.cursor())
    if before is not None and after is not None:
        _end_sessions(sorted(u for u, row in before.items() if after.get(u) != row))
    summary['file_errors'] = _apply_files(snap.get('files') or {})
    problem = _adopt_group(snap)
    if problem:
        summary['file_errors'].append(problem)
    summary['tombstones_owed'] = _tombstones_owed(snap)
    _after_apply()
    return summary


def _merged_members(st, sender, entries, tombstones=None):
    """Our member records after the member list `entries` from `sender`, the active we
    pull from. Whoever the active no longer lists is gone, whoever a tombstone names
    stays gone (a stale list from a promoted standby must not take a removed member
    back), and we never list ourselves. The sender stays whatever its list says, and
    keeps the address we reached it on; for the others the list is the word on
    address, pin and keys, except that a key we hold is not given up for the hash of a
    secret. What we noted about each member ourselves (roles seen, contact, errors)
    stays."""
    me, local = st['instance_id'], st.get('members') or {}
    tombs = (st.get('tombstones') or {}) if tombstones is None else tombstones
    listed = _clean_entries(entries)
    listed.pop(me, None)
    out = {}
    for mid in [sender] + sorted(m for m in listed if m != sender):
        entry, old = listed.get(mid), local.get(mid)
        if entry is None:
            if mid == sender and old:
                out[mid] = dict(old)
            continue
        rec = dict(old or {'role_seen': None, 'epoch_seen': 0, 'last_contact': None,
                           'last_error': '', 'joined_at': _now()})
        if entry['public_key']:
            if rec.get('public_key') and rec['public_key'] != entry['public_key']:
                # paired again: it holds our key only once it says so
                rec['key_acked'] = False
            rec['public_key'], rec['secret_hash'] = entry['public_key'], entry['secret_hash']
        else:
            rec['secret_hash'] = entry['secret_hash']
        if not (mid == sender and old and old.get('url')) and entry['url']:
            rec['url'], rec['fingerprint'] = entry['url'], entry['fingerprint']
        rec.setdefault('url', '')
        rec.setdefault('fingerprint', '')
        if mid != sender and _matches_tombstone(rec, tombs.get(mid)):
            continue
        out[mid] = rec
        if len(out) >= MAX_MEMBERS - 1:
            break
    return out


def _adopt_group(snap):
    """A standby takes the member list, the tombstones and the epoch of the active it
    pulled from, once the rows are in. Tombstones add up: an active that stepped down
    keeps the ones it made. An active from before the groups sends no list: ours
    stays. Never raises; returns what the sync status should say when the state could
    not be saved."""
    try:
        with _lock:
            st = _load()
            sender = snap.get('instance_id')
            if st['role'] != ROLE_STANDBY or st.get('source') != sender:
                return ''
            new = dict(st)
            tombs = st.get('tombstones') or {}
            if isinstance(snap.get('tombstones'), list):
                tombs = _merged_tombstones(tombs, _clean_tombstones(snap['tombstones']),
                                           st['instance_id'])
                new['tombstones'] = tombs
            if isinstance(snap.get('members'), list):
                new['members'] = _merged_members(st, sender, snap['members'], tombs)
            their_epoch = _epoch_value(snap.get('epoch') or 0)
            if their_epoch is not None and their_epoch > int(st.get('epoch') or 0):
                # never back to an active from before this one
                new['epoch'] = their_epoch
            if new != st:
                _commit_locked(new)
        return ''
    except Exception as e:
        logging.warning(f"[HA] could not take the member list from the active instance: {e}")
        return f'the member list was not saved ({type(e).__name__}: {e})'


def _tombstones_owed(snap):
    """The tombstones we hold for members that the member list in `snap` still names:
    the removal reached us and not the active we pull from, which was promoted while it
    could not hear about it. As a tombstone list, for take_tombstones on that active;
    [] when there are none."""
    if not isinstance(snap.get('members'), list):
        return []
    st = _load()
    sender = snap.get('instance_id')
    if st['role'] != ROLE_STANDBY or st.get('source') != sender:
        return []
    tombs = st.get('tombstones') or {}
    listed = _clean_entries(snap['members'])
    return [dict(tombs[mid], instance_id=mid) for mid in sorted(listed)
            if mid not in (sender, st['instance_id']) and _matches_tombstone(listed[mid], tombs.get(mid))]


def _sign_in_rows(cur):
    """username -> (password hash, salt, enabled), or None when it cannot be read."""
    try:
        cur.execute('SELECT username, password_hash, password_salt, enabled FROM users')
        return {r[0]: (r[1], r[2], r[3]) for r in cur.fetchall()}
    except Exception as e:
        logging.warning(f"[HA] could not read the users around a sync: {e}")
        return None


def _end_sessions(usernames):
    """Sessions live per instance. A password reset, a disable or a delete on the
    active ends the user's sessions there, but only the new row reaches us: end
    them here as well. Best effort; a failure is logged and the sync stands."""
    steps = (
        ('pegaprox.utils.auth', 'invalidate_all_user_sessions'),
        ('pegaprox.utils.realtime', 'invalidate_user_ws_tokens'),
        ('pegaprox.utils.realtime', 'invalidate_user_sse_tokens'),
    )
    for username in usernames:
        for mod, fn in steps:
            try:
                getattr(importlib.import_module(mod), fn)(username)
            except Exception as e:
                logging.warning(f"[HA] {mod}.{fn}({username!r}) after sync: {e}")
    if usernames:
        logging.info(f"[HA] sync: ended the sessions of {len(usernames)} user(s) whose "
                     "sign-in changed on the active instance")


_COLUMN_TYPE_RE = re.compile(r'[A-Za-z][A-Za-z0-9_ ]{0,31}(\(\d{1,5}(,\s*\d{1,5})?\))?')
_COLUMN_DEFAULT_RE = re.compile(r"-?\d{1,18}(\.\d{1,18})?|'(?:[^']|''){0,256}'|NULL", re.IGNORECASE)


def _add_column(cur, table, column, coldef):
    """ALTER TABLE ... ADD COLUMN with the active's declared type and default when
    both are plain, so a later migration that finds the column already there still
    gets the default it would have set. Anything else: a column without a type."""
    ctype, dflt = (list(coldef) + ['', None])[:2] if isinstance(coldef, (list, tuple)) else ('', None)
    parts = [f'ALTER TABLE "{table}" ADD COLUMN "{column}"']
    if isinstance(ctype, str) and _COLUMN_TYPE_RE.fullmatch(ctype.strip()):
        parts.append(ctype.strip())
    if isinstance(dflt, str) and _COLUMN_DEFAULT_RE.fullmatch(dflt.strip()):
        parts.append('DEFAULT ' + dflt.strip())
    try:
        cur.execute(' '.join(parts))
    except Exception:
        if len(parts) == 1:
            raise
        cur.execute(parts[0])


def _apply_files(files):
    """The host key pins, the login background and the plugins' config.json files.

    Runs once the rows are committed, so it never raises: a file that cannot be
    written must not make a sync whose rows are in look failed. Returns what the
    sync status should say about it, [] when all went well."""
    problems = []
    kh = files.get('ssh_known_hosts')
    if isinstance(kh, str):
        try:
            _write_private(KNOWN_HOSTS_FILE, kh.encode())
        except Exception as e:
            logging.warning(f"[HA] could not write the SSH host key pins: {e}")
            problems.append(f'the SSH host key pins were not written ({type(e).__name__}: {e})')
    branding = files.get('branding')
    if isinstance(branding, dict):
        try:
            os.makedirs(BRANDING_DIR, exist_ok=True)
        except Exception as e:
            logging.warning(f"[HA] could not create the branding folder: {e}")
            problems.append(f'the login background was not written ({type(e).__name__}: {e})')
            branding = {}
        for fn, b64 in branding.items():
            if not re.match(r'^[A-Za-z0-9_.\-]{1,64}$', fn) or fn.startswith('.'):
                continue
            try:
                _write_private(os.path.join(BRANDING_DIR, fn), base64.b64decode(b64), mode=0o644)
            except Exception as e:
                logging.warning(f"[HA] could not write branding file {fn}: {e}")
    configs = files.get('plugin_config')
    if isinstance(configs, dict):
        total = 0
        for pid, text in sorted(configs.items()):
            if not isinstance(pid, str) or not _PLUGIN_ID_RE.fullmatch(pid) or not isinstance(text, str):
                continue
            total += len(text.encode('utf-8'))
            if total > _MAX_PLUGIN_CONFIG_TOTAL:
                logging.warning("[HA] the plugin configurations in the snapshot exceed the size cap")
                break
            try:
                _apply_plugin_config(pid, text)
            except Exception as e:
                logging.warning(f"[HA] could not write the configuration of plugin {pid}: {e}")
    return problems


def _apply_plugin_config(pid, text):
    """Write plugins/<pid>/config.json, only into a plugin this instance has.
    Returns True when the file changed."""
    data = text.encode('utf-8')
    if len(data) > _MAX_PLUGIN_CONFIG_BYTES:
        return False
    json.loads(text)
    folder = os.path.join(PLUGINS_DIR, pid)
    if not os.path.isdir(folder):
        # a snapshot never creates a plugin directory
        return False
    path = os.path.join(folder, 'config.json')
    mode = 0o600
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        st = None
    if st is not None:
        if not stat.S_ISREG(st.st_mode):
            return False
        with open(path, 'rb') as fh:
            if fh.read() == data:
                return False
        mode = stat.S_IMODE(st.st_mode)
    _write_private(path, data, mode=mode)
    return True


def _write_private(path, data, mode=0o600):
    tmp = path + '.ha-tmp'
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(fd, 'wb') as fh:
        # O_CREAT's mode is cut by the umask and ignored for a leftover tmp file
        os.fchmod(fh.fileno(), mode)
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def _after_apply():
    """Caches that do not reread the database on their own."""
    steps = (
        ('pegaprox.utils.rbac', 'invalidate_roles_cache'),
        ('pegaprox.utils.rbac', 'invalidate_tenants_cache'),
        ('pegaprox.utils.rbac', 'invalidate_vm_acls_cache'),
        ('pegaprox.utils.rbac', 'invalidate_pool_cache'),
        ('pegaprox.api.settings', 'load_ip_whitelist'),
        ('pegaprox.api.storage', 'load_esxi_config'),
        ('pegaprox.api.storage', 'load_storage_clusters'),
    )
    for mod, fn in steps:
        try:
            getattr(importlib.import_module(mod), fn)()
        except Exception as e:
            logging.debug(f"[HA] {mod}.{fn} after sync: {e}")


# --- talking to the other members ----------------------------------------------

def _peer_call(method, base_url, fingerprint, path, json_body=None, auth=None,
               headers=None, timeout=15):
    """One HTTPS call to another instance. `auth` is None (the pairing call) or a
    callable (method, path, body) -> headers, from _auth_for, that signs exactly the
    bytes sent here."""
    import requests
    from pegaprox.utils.url_security import is_safe_outbound_url
    url = base_url.rstrip('/') + path
    ok, why = is_safe_outbound_url(url, allowed_schemes=('https',), allow_private=True)
    if not ok:
        raise HaError(f'The peer address is not allowed: {why}')
    body = _wire_body(json_body)
    h = {'X-Requested-With': 'XMLHttpRequest', 'Accept': 'application/json'}
    if body:
        h['Content-Type'] = 'application/json'
    if auth is not None:
        h.update(auth(method, path, body))
    if headers:
        h.update(headers)
    sess = requests.Session()
    if fingerprint:
        from pegaprox.core.pbs import _PinnedFingerprintAdapter
        sess.mount('https://', _PinnedFingerprintAdapter(fingerprint))
        verify = False
    else:
        verify = True
    try:
        import urllib3
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    except Exception:
        pass
    data = body or None
    if body and path == FORWARD_PATH:
        # The envelope of an upload is larger than what the receiving app takes with a
        # Content-Length before any route has seen the call (PEGAPROX_MAX_REQUEST_SIZE).
        # Sent in chunks, it meets the cap the forward route sets for itself instead.
        data = (body[i:i + _FORWARD_CHUNK] for i in range(0, len(body), _FORWARD_CHUNK))
    try:
        return sess.request(method, url, data=data, headers=h, verify=verify,
                            timeout=timeout, allow_redirects=False)
    except requests.exceptions.SSLError as e:
        if fingerprint:
            raise PeerUnreachable(f'The peer certificate does not match the pinned fingerprint ({type(e).__name__})')
        raise PeerUnreachable('The peer certificate is not trusted by a CA, and no fingerprint is '
                              f'pinned for it ({type(e).__name__})')
    except (requests.exceptions.ReadTimeout, requests.exceptions.ChunkedEncodingError) as e:
        raise PeerNoAnswer(f'The peer took the call but sent no answer: {type(e).__name__}')
    except requests.exceptions.ConnectionError as e:
        # connected, then dropped before an answer came: the call may have run there.
        # Refused, unresolvable or timed out while connecting: it never got there
        from urllib3.exceptions import ProtocolError
        if (not isinstance(e, requests.exceptions.ConnectTimeout) and e.args
                and isinstance(e.args[0], ProtocolError)):
            raise PeerNoAnswer(f'The peer took the call but sent no answer: {type(e).__name__}')
        raise PeerUnreachable(f'Cannot reach the peer: {type(e).__name__}')
    except requests.exceptions.RequestException as e:
        raise PeerUnreachable(f'Cannot reach the peer: {type(e).__name__}')
    finally:
        sess.close()


def _peer_error(resp, fallback):
    try:
        return (resp.json() or {}).get('error') or f'{fallback} (HTTP {resp.status_code})'
    except Exception:
        return f'{fallback} (HTTP {resp.status_code})'


def _error_text(e):
    return (str(e) if isinstance(e, HaError) else f'{type(e).__name__}: {e}')[:300]


def _answer_header(resp, name):
    headers = getattr(resp, 'headers', None)
    try:
        return headers.get(name) if headers is not None else None
    except Exception:
        return None


def _note_key_acked(member_id):
    """The member holds our public key now: no more old secret towards it. Once every
    member does, the secret is dropped altogether."""
    try:
        with _lock:
            st = _load()
            ms = dict(st.get('members') or {})
            if member_id not in ms or ms[member_id].get('key_acked'):
                return
            ms[member_id] = dict(ms[member_id], key_acked=True)
            new = dict(st, members=ms)
            if st.get('member_secret') and all(r.get('key_acked') for r in ms.values()):
                new['member_secret'] = None
                logging.info("[HA] every member holds our key - the old secret is dropped")
            _commit_locked(new)
    except Exception as e:
        logging.warning(f"[HA] could not note that member {member_id} holds our key: {e}")


def call_member(rec, method, path, json_body=None, headers=None, timeout=15, signer=None):
    """One signed call to the member `rec` (a record from members()). `signer` is ours,
    read beforehand by a caller that fans out (_signer).

    A member that is not known to hold our key yet also gets the old secret and the
    key, if this instance still has a secret from before the keys. An answer of
    410 HA_REMOVED means that member removed us: we let go of the group here."""
    if not rec or not rec.get('url'):
        raise HaError('No address known for this member')
    signer = signer or _signer()
    legacy = bool(signer.secret) and not rec.get('key_acked')
    resp = _peer_call(method, rec['url'], rec.get('fingerprint') or '', path,
                      json_body=json_body, auth=_auth_for(signer, rec['instance_id'], legacy),
                      headers=headers, timeout=timeout)
    if legacy and signer.private is not None and _answer_header(resp, PEER_KEYED_HEADER) == '1':
        _note_key_acked(rec['instance_id'])
    if resp.status_code == 410:
        _removed_answer(rec, resp)
    return resp


def _fan_out(jobs, timeout):
    """Run the callables in `jobs` side by side and wait at most `timeout` seconds for
    all of them. Returns [(result, None) or (None, exception)] in the order given; a
    job still out when the time is up counts as failed, a single one as well.

    Under gevent these threads are greenlets: a call waiting on the network costs a
    socket, and a member that does not answer holds up none of the others."""
    results = [None] * len(jobs)

    def run(i, job):
        try:
            results[i] = (job(), None)
        except Exception as e:
            results[i] = (None, e)
    threads = [threading.Thread(target=run, args=(i, job), daemon=True, name='ha-member-call')
               for i, job in enumerate(jobs)]
    for t in threads:
        t.start()
    deadline = time.monotonic() + timeout
    for t in threads:
        t.join(max(0.0, deadline - time.monotonic()))
    late = HaError('The member did not answer in time')
    return [r if r is not None else (None, late) for r in list(results)]


def tell_members(method, path, json_body=None, timeout=10, only=None, note=True):
    """The same call to every member, or to the ids in `only`, side by side.

    Returns {member id: None when it answered 200, else what went wrong}, and notes
    the failures on the member records unless `note` is False. Never raises."""
    try:
        signer = _signer()
        targets = [m for m in members() if only is None or m['instance_id'] in only]
    except Exception as e:
        logging.warning(f"[HA] cannot call the members: {e}")
        return {mid: _error_text(e) for mid in (only or [])}
    results = _fan_out([lambda rec=rec: call_member(rec, method, path, json_body=json_body,
                                                    timeout=timeout, signer=signer)
                        for rec in targets], timeout + 5)
    out, notes = {}, {}
    for rec, (resp, err) in zip(targets, results):
        mid = rec['instance_id']
        if err is None and resp.status_code != 200:
            err = HaError(_peer_error(resp, f'The member refused {path}'))
        out[mid] = None if err is None else _error_text(err)
        if err is not None and note:
            notes[mid] = {'last_error': out[mid]}
    try:
        _note_members(notes)
    except Exception as e:
        logging.warning(f"[HA] could not note the member errors: {e}")
    return out


def pull_once(timeout=PULL_TIMEOUT):
    """Standby: fetch and apply one snapshot from the member it pulls from. Returns a
    short status string.

    Also where a reload of the managers that has settled goes out, should its timer
    not have come (see reload_managers): after a sync, and on the polls that find
    nothing new."""
    # one pull at a time: "sync now" and the loop would otherwise apply side by side,
    # and the file writes share their temporary names
    if not _pull_lock.acquire(timeout=timeout + 5):
        return 'busy'
    try:
        result = _pull(timeout)
    finally:
        _pull_lock.release()
    if result in ('applied', 'unchanged', 'failed'):
        _reload_if_due()
    return result


def forward_write(envelope, timeout=FORWARD_TIMEOUT):
    """Standby: hand one write to the member it pulls from, as a signed call whose body
    is `envelope` (api/ha.py forward_to_active builds it). Returns the answer; raises
    PeerUnreachable when the active cannot be reached, and PeerNoAnswer (one of those)
    when it took the call and sent nothing back: the write may have happened there.
    Until the active answers again, forwarding() is False."""
    src = peer() if is_standby() else None
    if not src:
        raise HaError('Not paired')
    try:
        resp = call_member(src, 'POST', FORWARD_PATH, json_body=envelope,
                           timeout=(FORWARD_CONNECT_TIMEOUT, timeout))
    except PeerNoAnswer:
        # it got there: the active is busy with it, or went down while at it
        raise
    except PeerUnreachable:
        _note_source_heard(src['instance_id'], False)
        raise
    _note_source_heard(src['instance_id'], True)
    return resp


_soon_lock = threading.Lock()
_soon = {'wanted': False, 'running': False}


def _in_background(fn, name):
    threading.Thread(target=fn, daemon=True, name=name).start()


def pull_soon():
    """Standby: one pull right away, in the background, after a write it forwarded went
    through or once the active said its configuration changed (/api/ha/peer/changed),
    so the change shows here without waiting for the interval. Asks that come in
    while that pull runs get one more pull after it, not one each. Returns True when
    it started a run."""
    with _soon_lock:
        _soon['wanted'] = True
        if _soon['running']:
            return False
        _soon['running'] = True
    try:
        _in_background(_pull_soon_run, 'ha-pull-soon')
    except Exception as e:
        with _soon_lock:
            _soon['running'] = False
        logging.warning(f"[HA] could not start a sync out of turn: {e}")
        return False
    return True


def _pull_soon_run():
    finished = False
    try:
        while True:
            with _soon_lock:
                if not _soon['wanted']:
                    _soon['running'] = False
                    finished = True
                    return
                _soon['wanted'] = False
            try:
                if is_standby() and peer():
                    pull_once(timeout=PULL_TIMEOUT_FLOOR)
            except Exception as e:
                logging.warning(f"[HA] a sync out of turn failed: {_error_text(e)}")
    finally:
        if not finished:
            # killed halfway: the next ask starts a run of its own
            with _soon_lock:
                _soon['running'] = False


_nudge_lock = threading.Lock()
# last: when the last note went out (monotonic), for the spacing under a run of writes
_nudge = {'due': False, 'last': None}


def nudge_members():
    """The active, after a write went through (app.py): tell every member that the
    configuration changed, and one that does not hold it yet pulls it now rather than
    at its next poll. The note goes NUDGE_DELAY seconds after the write, and no sooner
    than NUDGE_SPACING after the one before; writes until then go with the same note.
    Returns True when it set one up. Never raises: the write is done either way."""
    try:
        st = _load()
        if st['role'] != ROLE_ACTIVE or not st.get('members'):
            return False
        with _nudge_lock:
            if _nudge['due']:
                return False
            _nudge['due'] = True
            last = _nudge.get('last')
        delay = NUDGE_DELAY
        if last is not None:
            delay = max(delay, last + NUDGE_SPACING - time.monotonic())
        _later(delay, _nudge_run, 'ha-nudge')
    except Exception as e:
        with _nudge_lock:
            _nudge['due'] = False
        logging.warning(f"[HA] could not set up the note to the members about a change: {e}")
        return False
    return True


def _nudge_run():
    # cleared first: a write that comes in while the calls are out gets a call of its
    # own, its change may be newer than what the members fetch now
    with _nudge_lock:
        _nudge['due'] = False
        _nudge['last'] = time.monotonic()
    try:
        if role() != ROLE_ACTIVE:
            return
        body = None
        try:
            # once for all of them, off the hub like the etag of a poll: a write that
            # changed nothing they hold (a VM started, a test mail) costs no member a pull
            from pegaprox.api.ha import current_etag
            body = {'etag': current_etag()}
        except Exception as e:
            # without one every member pulls, as from a release before the etag
            logging.warning(f"[HA] could not work out the etag for the note to the members: {e}")
        # not noted on the member records: a member that misses one (down, or on a
        # release without the route) is not wrong, it pulls at its next poll
        missed = {mid: err for mid, err in tell_members(
            'POST', NUDGE_PATH, json_body=body, timeout=NUDGE_TIMEOUT, note=False).items() if err}
    except Exception as e:
        logging.warning(f"[HA] could not tell the members about a change: {e}")
        return
    for mid, err in missed.items():
        logging.info(f"[HA] member {mid} did not take the note about a change ({err}) - "
                     "it pulls at its next poll")


def holds_etag(etag):
    """Standby: whether the last sync it applied is the configuration `etag` stands for,
    as the active's note about a change names it (nudge_members). Never before the first
    pull of this process, which is a full one whatever the etag says."""
    if not _etag_checked or not isinstance(etag, str) or not etag:
        return False
    return etag == (_load().get('sync') or {}).get('etag')


def pull_before_promote(timeout=15):
    """The promote route, before this standby becomes active: one pull from the member
    it follows, so a planned failover starts from the configuration, the member list
    and the tombstones of now. Returns (True, '') when it is fine to go on: the pull
    worked, there is nothing to pull from, or the source did not answer at all (the
    failover this is for). (False, why) when the source answered and the pull failed,
    or this instance was removed meanwhile."""
    if not is_standby() or not peer():
        return True, ''
    if not _pull_lock.acquire(timeout=timeout + 5):
        return False, 'a sync is running right now - try again in a moment'
    try:
        result, answered, error = _pull_detail(timeout)
    finally:
        _pull_lock.release()
    if result in ('applied', 'unchanged', 'not paired', 'not a standby'):
        return True, ''
    if result == 'removed':
        return False, REMOVED_ERROR
    if result == 'source switched':
        rec = peer() or {}
        return False, (f"the group has an active instance, {rec.get('url') or rec.get('instance_id')}, "
                       "and this standby follows it from now on")
    if not answered:
        return True, ''
    return False, error or result


def boot_pull(timeout=BOOT_PULL_TIMEOUT):
    """main(), on a standby with the live view on, before the managers start: one
    short pull, so they start from the active's configuration of now and not from
    the one this instance stopped with. Never raises."""
    try:
        if not is_standby() or not peer():
            return 'idle'
        return pull_once(timeout=timeout)
    except Exception as e:
        logging.warning(f"[HA] pull at start failed: {e}")
        return 'error'


def _finish_pull(sid, sync, member=None):
    """The outcome of a pull in one write: the sync status and what it says about the
    source. Nothing is written when nothing changed."""
    with _lock:
        st = _load()
        new = dict(st, sync=dict(st.get('sync') or {}, **sync))
        ms = st.get('members') or {}
        if member and sid in ms:
            new['members'] = dict(ms, **{sid: dict(ms[sid], **member)})
        if new != st:
            _commit_locked(new)


def _pull(timeout):
    return _pull_detail(timeout)[0]


def _pull_detail(timeout):
    """(result, answered, error): answered is True once the source sent an HTTP answer."""
    global _etag_checked
    if not is_standby():
        return 'not a standby', False, ''
    src = peer()
    if not src:
        # nothing to pull from, and writing here would replace a state file that
        # _load could not read with a fresh one
        return 'not paired', False, ''
    sid = src['instance_id']
    with _lock:
        first, _etag_checked = not _etag_checked, True
    started = _now()
    committed = answered = False
    try:
        # the first pull after a start is a full one: an upgrade may have added
        # columns, a restored database may hold other rows, and the active's etag
        # knows about neither
        etag = None if first else (_load().get('sync') or {}).get('etag')
        resp = call_member(src, 'GET', '/api/ha/peer/snapshot',
                           headers={'If-None-Match': etag} if etag else None, timeout=timeout)
        answered = True
        _note_source_heard(sid, True)
        if resp.status_code == 304:
            now = _now()
            _finish_pull(sid, {'last_attempt_at': started, 'last_ok_at': now, 'last_error': ''},
                         {'last_contact': now, 'last_error': ''})
            return 'unchanged', True, ''
        if resp.status_code == 410 and _load().get('removed'):
            return 'removed', True, REMOVED_ERROR
        if resp.status_code == 409:
            switched = _take_follow_hint(src, resp, timeout)
            if switched:
                return switched, True, ''
        if resp.status_code != 200:
            raise HaError(_peer_error(resp, 'The active instance refused the snapshot'))
        snap = resp.json()
        seen = {'last_contact': _now(), 'role_seen': snap.get('role'),
                'epoch_seen': int(snap.get('epoch') or 0), 'last_error': ''}
        if snap.get('group') == GROUP_MARK:
            seen['group_seen'] = True
        summary = apply_snapshot(snap)
        committed = True
        problems = summary.get('file_errors') or []
        # an etag stands for all of the content; with columns left out or a file not
        # written we hold less than that, and a 304 would keep it so (after an
        # upgrade, or once the file can be written again)
        etag = None if summary['skipped_columns'] or problems else snap.get('etag')
        owed = summary.get('tombstones_owed') or []
        if owed and not _hand_back_tombstones(src, owed):
            # the next pull is a full one and finds them again
            etag = None
        note = ('The configuration was applied, but ' + '; '.join(problems)) if problems else ''
        _finish_pull(sid, {'last_attempt_at': started, 'last_ok_at': _now(), 'last_error': note[:300],
                           'etag': etag, 'source_epoch': int(snap.get('epoch') or 0),
                           'rows': summary['rows'], 'tables': summary['tables'],
                           'skipped_columns': summary['skipped_columns']}, seen)
        return 'applied', True, ''
    except Exception as e:
        msg = _error_text(e)
        if not answered and isinstance(e, PeerUnreachable):
            _note_source_heard(sid, False)
        try:
            _finish_pull(sid, dict({'last_attempt_at': started, 'last_error': msg},
                                   **({'etag': None} if first else {})))
        except Exception as e2:
            logging.warning(f"[HA] could not note the failed sync: {e2}")
        logging.warning(f"[HA] sync failed: {msg}")
        return 'failed', answered, msg
    finally:
        # the database holds the new rows now, whatever failed after the commit: the
        # managers are compared against them, or a new connection setting would never
        # restart anything while that failure lasts
        if committed:
            _after_sync_applied()


def _hand_back_tombstones(src, owed):
    """Tell the active we pull from about the members it lists and we hold tombstones
    for (_tombstones_owed). True once it has heard, whether it took them or not."""
    try:
        resp = call_member(src, 'POST', '/api/ha/peer/tombstones', json_body={'tombstones': owed},
                           timeout=10)
    except Exception as e:
        logging.warning(f"[HA] could not hand the tombstones back to {src.get('url')}: {_error_text(e)}")
        return False
    if resp.status_code != 200:
        logging.warning(f"[HA] {src.get('url')} did not take the tombstones: "
                        f"{_peer_error(resp, 'refused')}")
        return False
    return True


def follow_hint():
    """What a standby tells a member that asks it for a snapshot: the member it follows,
    as that member is to be reached and checked. None when it follows nobody it has
    seen active."""
    st = _load()
    if st['role'] != ROLE_STANDBY or st.get('removed'):
        return None
    sid = st.get('source')
    rec = (st.get('members') or {}).get(sid)
    if not rec or rec.get('role_seen') != ROLE_ACTIVE or not rec.get('public_key') or not rec.get('url'):
        return None
    return {'instance_id': sid, 'url': rec['url'], 'fingerprint': rec.get('fingerprint') or '',
            'public_key': rec['public_key'],
            'epoch': max(int(st.get('epoch') or 0), int(rec.get('epoch_seen') or 0))}


def _take_follow_hint(src, resp, timeout):
    """The member we pull from is not active and names the one it follows. It is a
    member we trust, so we take the name, but only once that instance answers a signed
    status call as active under that very epoch, at least ours. Returns 'source
    switched', or None when the hint is not taken."""
    try:
        hint = (resp.json() or {}).get('follow')
    except Exception:
        return None
    if not isinstance(hint, dict):
        return None
    entry = _clean_entries([hint]).get(hint.get('instance_id'))
    their = hint.get('epoch')
    st = _load()
    me = st['instance_id']
    if (not entry or not entry['public_key'] or not entry['url']
            or _epoch_value(their, low=int(st.get('epoch') or 0)) is None):
        return None
    hid = hint['instance_id']
    if hid in (me, src['instance_id']) or _matches_tombstone(entry, (st.get('tombstones') or {}).get(hid)):
        return None
    known = (st.get('members') or {}).get(hid)
    if known is None and len(st.get('members') or {}) >= MAX_MEMBERS - 1:
        logging.warning(f"[HA] {src.get('url')} follows {entry['url']}, and there is no room "
                        "for another member here")
        return None
    rec = dict(known or entry, instance_id=hid)
    try:
        their_role, their_epoch, group = _ask(rec, _signer(), timeout=min(10, timeout))[:3]
    except Exception as e:
        logging.warning(f"[HA] {src.get('url')} follows {entry['url']}, which did not confirm it: {e}")
        return None
    if their_role != ROLE_ACTIVE or their_epoch != their:
        return None
    with _lock:
        st = _load()
        ms = dict(st.get('members') or {})
        if st['role'] != ROLE_STANDBY or st.get('source') != src['instance_id']:
            return None
        if hid not in ms and len(ms) >= MAX_MEMBERS - 1:
            return None
        base = ms.get(hid) or dict(entry, joined_at=_now(), last_error='')
        ms[hid] = dict(base, role_seen=ROLE_ACTIVE, epoch_seen=their_epoch, last_contact=_now(),
                       group_seen=bool(base.get('group_seen') or group == GROUP_MARK))
        _commit_locked(dict(st, members=ms, source=hid,
                            sync=dict(st.get('sync') or {}, etag=None, last_error='')))
    logging.warning(f"[HA] {src.get('url')} is not active and follows {entry['url']}, which "
                    f"answers as active with epoch {their_epoch} - following it from now on")
    _audit('ha.follow_hint', f"following {entry['url']} (epoch {their_epoch}), as "
                             f"{src.get('url') or src['instance_id']} does")
    return 'source switched'


def _ask(rec, signer, timeout):
    """(role, epoch, group mark, serving) as the member `rec` reports them, serving False
    from a release that does not say. Raises PeerRefused when it turns us away (401,
    410), HaError when it does not answer usably."""
    resp = call_member(rec, 'GET', '/api/ha/peer/status', timeout=timeout, signer=signer)
    if resp.status_code in (401, 410):
        try:
            data = resp.json()
        except Exception:
            data = None
        data = data if isinstance(data, dict) else {}
        said = data.get('instance_id')
        if said is not None and said != rec['instance_id']:
            # set up anew at that address, say: its refusal is not the member's
            raise HaError('Another instance answers at the address of this member')
        raise PeerRefused(_peer_error(resp, 'The member refused the status call'),
                          resp.status_code, data.get('code') or '')
    if resp.status_code != 200:
        raise HaError(_peer_error(resp, 'The member refused the status call'))
    data = resp.json()
    if not isinstance(data, dict):
        raise HaError('The member sent a status this version does not read')
    said = data.get('instance_id')
    if said is not None and said != rec['instance_id']:
        raise HaError('Another instance answers at the address of this member')
    their_role, their_epoch = data.get('role'), _epoch_value(data.get('epoch') or 0)
    if their_role not in (ROLE_STANDALONE, ROLE_ACTIVE, ROLE_STANDBY):
        their_role = None
    if their_epoch is None:
        raise HaError('The member sent an epoch this version does not read')
    return their_role, their_epoch, data.get('group'), data.get('serving') is True


def _ask_members(timeout, refused=None):
    """Ask every member for its role and epoch, side by side and each within `timeout`.
    Notes contact or error on every record (one write, none when nothing changed) and
    returns {member id: (role, epoch)} of the members that answered. `refused`, a dict,
    gets {member id: HTTP status} of the ones that turned us away."""
    ms = members()
    if not ms:
        return {}
    signer = _signer()
    results = _fan_out([lambda rec=rec: _ask(rec, signer, timeout) for rec in ms], timeout + 5)
    answers, notes, unreachable, now = {}, {}, set(), _now()
    for rec, (value, err) in zip(ms, results):
        mid = rec['instance_id']
        if err is not None:
            notes[mid] = {'last_error': _error_text(err)}
            if isinstance(err, PeerRefused) and err.code != 'HA_CLOCK':
                if refused is not None:
                    refused[mid] = err.status
            elif not isinstance(err, PeerRefused):
                unreachable.add(mid)
            else:
                # it knows us and says our clocks differ: no sign that we are out
                logging.warning(f"[HA] member {rec.get('url') or mid} refuses our calls: {err}")
            continue
        answers[mid] = value[:2]
        notes[mid] = {'last_contact': now, 'role_seen': value[0], 'epoch_seen': value[1],
                      'serving_seen': value[3], 'last_error': ''}
        if value[2] == GROUP_MARK:
            notes[mid]['group_seen'] = True
    _last_watch.update(at=time.monotonic(), unreachable=frozenset(unreachable))
    src = source_id()
    if src in answers or src in unreachable:
        _note_source_heard(src, src in answers)
    try:
        _note_members(notes)
    except Exception as e:
        # the answers stand: a full disk must not keep the leader from telling another
        # active to step down, which needs no write of ours
        logging.warning(f"[HA] could not note the member answers: {e}")
    return answers


def _leader(answers, own=None):
    """(epoch, instance id) of the instance the group follows: the active with the
    highest epoch, a tie going to the higher instance id. `own` is this instance when
    it is active. None when nobody is."""
    actives = [(e, mid) for mid, (r, e) in answers.items() if r == ROLE_ACTIVE]
    if own:
        actives.append(own)
    return max(actives) if actives else None


def check_peer_at_boot(timeout=5):
    """Ask every member once, before managers and loops start.

    An instance that comes back as active may have been replaced while it was
    down. When a member is active under a newer epoch (or under ours, and wins the
    tie) we step down to it right here, without a restart, so the caller comes up as
    a standby and nothing acts on the old configuration. The same when a member says
    we were removed ('removed'), when every member refuses us or one reports a newer
    epoch that no active answers under ('stepped aside'). Never raises; returns a
    short status string.
    """
    try:
        if role() != ROLE_ACTIVE or not members():
            return 'idle'
        mine, me, n = epoch(), instance_id(), len(members())
        refused = {}
        answers = _ask_members(timeout, refused)
        if _load().get('removed'):
            # a member holds a tombstone for us: we come up passive, nothing restarts
            return 'removed'
        if not answers and not refused:
            return 'unreachable'
        top = _leader(answers, (mine, me))
        if top[1] != me and step_down(top[0], top[1]):
            rec = member(top[1]) or {}
            _audit('ha.stepped_down', f"at start: member {rec.get('url') or top[1]} "
                                      f"is active with epoch {top[0]}")
            return 'stepped down'
        aside = _moved_on(answers, refused, mine, n)
        if top[1] == me and aside and step_aside(*aside):
            _audit('ha.stepped_aside', f'at start: {aside[1]}')
            return 'stepped aside'
        return 'ok'
    except Exception as e:
        logging.warning(f"[HA] member check at start failed: {e}")
        return 'error'


def _moved_on(answers, refused, mine, n):
    """(epoch, reason) when an active that leads among the answers has still been left
    behind: every one of its `n` members refused it, or a member reports an epoch
    above ours while no active under that epoch answered (it would lead otherwise).
    Unreachable members prove nothing and count for neither. None when neither holds."""
    if n and len(refused) == n:
        return mine, f'every member ({n}) refused this instance'
    if answers:
        seen, mid = max((e, m) for m, (_r, e) in answers.items())
        if seen > mine:
            rec = member(mid) or {}
            return seen, (f"member {rec.get('url') or mid} reports epoch {seen}, above this "
                          f"instance's {mine}, and no active under it answers")
    return None


def watch_once(timeout=10):
    """One look at the group, every tick on every paired instance. Never promotes
    anything.

    Every member is asked for its role and epoch. The group follows the active with
    the highest epoch, a tie going to the higher instance id. An active that is not
    that one steps down and becomes its standby; the one that is tells every other
    active it sees to step down. An active the group has moved on from, or that every
    member refuses, steps aside to a passive standby (_moved_on). A standby pulls from
    the leader from then on (_follow). A member that removed us says so (410), and we
    let go of the group."""
    st = _load()
    was = st['role']
    if was == ROLE_STANDALONE or not st.get('members'):
        return 'idle'
    # our epoch as it was before the calls; a member may step us down meanwhile
    mine, me, n = int(st.get('epoch') or 0), st['instance_id'], len(st['members'])
    refused = {}
    answers = _ask_members(timeout, refused)
    if _load().get('removed'):
        if was == ROLE_ACTIVE:
            restart_process('removed from the group')
        return 'removed'
    if role() != was:
        # stepped down while the calls were out; that path restarts us already
        return 'idle'
    if was == ROLE_STANDBY:
        return _follow(answers, mine)
    top = _leader(answers, (mine, me))
    if top[1] != me:
        if step_down(top[0], top[1]):
            rec = member(top[1]) or {}
            _audit('ha.stepped_down', f"member {rec.get('url') or top[1]} is active "
                                      f"with epoch {top[0]}")
            restart_process('stepped down to standby')
            return 'stepped down'
        return 'ok'
    aside = _moved_on(answers, refused, mine, n)
    if aside:
        if step_aside(*aside):
            _audit('ha.stepped_aside', aside[1])
            restart_process('stepped aside to standby')
            return 'stepped aside'
        return 'ok'
    others = [mid for mid, (r, _e) in answers.items() if r == ROLE_ACTIVE]
    if not others:
        return 'ok' if answers else 'unreachable'
    told = tell_members('POST', '/api/ha/peer/step-down', json_body={'epoch': mine},
                        timeout=timeout, only=others)
    if all(told.get(mid) is None for mid in others):
        return 'told peer to step down'
    return 'peer refused to step down'


def _follow(answers, mine):
    """A standby's half of watch_once: pull from the leader among the members that
    answered as active under at least our epoch. When none did, the member we pull
    from stays what it is and the pull says what is wrong; a standby never promotes
    itself."""
    top = _leader({mid: a for mid, a in answers.items() if a[1] >= mine})
    if top is None:
        return 'no active member' if answers else 'unreachable'
    if top[1] == source_id():
        return 'ok'
    with _lock:
        st = _load()
        if st['role'] != ROLE_STANDBY or top[1] not in (st.get('members') or {}):
            return 'idle'
        before = st.get('source')
        # a full pull from the new one: an etag of the old one says nothing here
        _commit_locked(dict(st, source=top[1], sync=dict(st.get('sync') or {}, etag=None)))
    logging.warning(f"[HA] following {top[1]} from now on, active with epoch {top[0]} "
                    f"(was following {before or 'nobody'})")
    return 'source switched'


def _audit(action, details):
    try:
        from pegaprox.utils.audit import log_audit
        log_audit('system', action, details)
    except Exception:
        pass


def _pull_timeout(started, interval):
    """The timeout of the pull in the pass that began at `started` (monotonic): short
    when the watch of this pass could not reach the source, else what is left of the
    interval, within PULL_TIMEOUT_FLOOR and PULL_TIMEOUT. A source that is gone then
    costs a pass seconds, not a minute and more."""
    at, unreachable = _last_watch['at'], _last_watch['unreachable']
    if at is not None and at >= started and source_id() in unreachable:
        return PULL_TIMEOUT_UNREACHABLE
    left = interval - (time.monotonic() - started)
    return int(max(PULL_TIMEOUT_FLOOR, min(PULL_TIMEOUT, left)))


def _loop():
    # give the app a moment to come up before the first call
    time.sleep(5)
    while True:
        r = None
        started = time.monotonic()
        try:
            r = role()
            if r != ROLE_STANDALONE and _load().get('members'):
                watch_once()
        except Exception as e:
            logging.error(f"[HA] loop: {e}")
        try:
            # only when this pass started as a standby: one that just stepped down
            # restarts first
            if r == ROLE_STANDBY and is_standby() and peer():
                interval = int(_load().get('interval') or DEFAULT_INTERVAL)
                pull_once(timeout=_pull_timeout(started, interval))
        except Exception as e:
            logging.error(f"[HA] loop: {e}")
        interval = int(_load().get('interval') or DEFAULT_INTERVAL)
        # a reload of the managers goes out on its timer; should that not come, still
        # when it has settled and not up to an hour later
        wait = _reload_wait()
        if wait is not None:
            interval = min(interval, int(wait) + 1)
        time.sleep(max(5, min(interval, 3600)))


def start_loop():
    """Idempotent. Runs in every role; it only works when paired."""
    global _loop_started
    with _lock:
        if _loop_started:
            return
        _loop_started = True
    threading.Thread(target=_loop, daemon=True, name='ha-peer').start()


def _member_view(rec, src, st):
    """A member record as the status page shows it: no secret, no hash, the public key
    only as a short fingerprint ('' while the member still goes by its old secret)."""
    return {
        'instance_id': rec['instance_id'],
        'url': rec.get('url') or '',
        'fingerprint': rec.get('fingerprint') or '',
        'role_seen': rec.get('role_seen'),
        'epoch_seen': rec.get('epoch_seen'),
        'confirmed_standby': _confirmed_standby(rec, st),
        # the member said it serves users (a standby of theirs the UI calls active)
        'serving_seen': rec.get('serving_seen') is True,
        'key_fingerprint': peer_key_fingerprint(rec.get('public_key')),
        'last_contact': rec.get('last_contact'),
        'last_error': rec.get('last_error') or '',
        'joined_at': rec.get('joined_at'),
        'is_source': rec['instance_id'] == src,
    }


def public_status():
    st = _load()
    p = peer() or {}
    src = source_id()
    pairing = st.get('pairing') or {}
    removed = st.get('removed')
    return {
        'role': st['role'],
        'epoch': int(st.get('epoch') or 0),
        'instance_id': st['instance_id'],
        'interval': int(st.get('interval') or DEFAULT_INTERVAL),
        'live_view': live_view(),
        # the switch, and whether a refused write goes to the active right now
        'forward_writes': forward_writes(),
        'forwarding': forwarding(),
        # the switch, and whether this standby serves users right now
        'serve_users': serve_users(),
        'serving': serving(),
        'managers_running': bool(_run['managers']),
        'broken': st.get('broken') or '',
        # set once a member told this instance it was removed; it is passive then
        'removed': {'epoch': removed.get('epoch'), 'at': removed.get('at'), 'by': removed.get('by')}
                   if removed else None,
        'pairing_open_until': pairing.get('expires') if pairing.get('code_hash') and
                              int(pairing.get('expires') or 0) >= int(time.time()) else None,
        # the member a standby pulls from, or the first one; kept for what reads one peer
        'peer': {
            'instance_id': p.get('instance_id'),
            'url': p.get('url'),
            'fingerprint': p.get('fingerprint'),
            'paired_at': p.get('joined_at'),
            'role_seen': p.get('role_seen'),
            'epoch_seen': p.get('epoch_seen'),
            'last_contact': p.get('last_contact'),
            'last_error': p.get('last_error') or '',
        } if p else None,
        'members': [_member_view(rec, src, st) for rec in members()],
        'max_members': MAX_MEMBERS,
        'standby_count': standby_count(),
        'sync': dict(st.get('sync') or {}, etag=None, restart_pending=_restart_pending(),
                     reload_pending=_reload_pending(), last_reload=_run['last_reload']),
    }


def banner():
    """What every logged-in user sees about this instance, nothing more."""
    st = _load()
    if st['role'] != ROLE_STANDBY:
        return {'role': st['role']}
    p = peer() or {}
    # removed: the group took it out, and it stays passive until an admin unpairs it
    return {'role': st['role'], 'peer_url': p.get('url') or '',
            'last_sync_at': (st.get('sync') or {}).get('last_ok_at') or '',
            'removed': bool(st.get('removed'))}
