"""Warm standby for PegaProx itself (#625).

Two instances are paired. The ACTIVE one runs as always. The STANDBY one keeps the
UI up, pulls the shared configuration from the active every few seconds, refuses
writes, starts no cluster managers and lets none of the background loops act. An
admin promotes the standby by hand; the instance that was active steps down the
moment it learns about a newer epoch, from either side.

What travels:
  * pairing - the standby POSTs the one-time code to the active and gets back the
    field key (.pegaprox_aes256.key) and a secret, both sealed with a key derived
    from the code. Encrypted columns then copy 1:1, including ones added later;
    a value still in the old Fernet format is resealed on its way out.
  * sync - GET /api/ha/peer/snapshot returns every table in SYNC_TABLES plus the host
    key pins, the login background and the plugins' config.json files.
    Instance-local tables and settings stay put.

Both sides hold a secret to present to the other and the hash of the one they
expect, so the roles can swap without pairing again.

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
PEER_HEADER = 'X-PegaProx-Peer'

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

_lock = threading.RLock()
_state = None
_loop_started = False
# the stored etag is dropped once per process start: an upgrade or a restored
# database changes what we hold without the active knowing
_etag_checked = False


class HaError(Exception):
    """Something the admin (or the peer) should be told as is."""


# --- state ---------------------------------------------------------------------

def _now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _default_state():
    return {
        'role': ROLE_STANDALONE,
        'epoch': 0,
        'instance_id': uuid.uuid4().hex,
        'interval': DEFAULT_INTERVAL,
        'peer': None,
        'pairing': None,
        'sync': {},
    }


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
        base.update(st)
        if base.get('role') not in (ROLE_STANDALONE, ROLE_ACTIVE, ROLE_STANDBY):
            base['role'] = ROLE_STANDBY
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


def _update_peer(**changes):
    with _lock:
        _load()
        if not _state.get('peer'):
            return
        peer = dict(_state['peer'])
        peer.update(changes)
        _commit_locked(dict(_state, peer=peer))


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
    """True when this instance may act: start managers, fire schedules, send mail.

    A standalone instance is active too. Only a standby holds back.
    """
    return role() != ROLE_STANDBY


def instance_id():
    return _load()['instance_id']


def epoch():
    return int(_load().get('epoch') or 0)


def peer():
    p = _load().get('peer')
    return dict(p) if p else None


# --- secrets and codes ---------------------------------------------------------

def _hash_secret(value):
    return hashlib.sha256(('pegaprox-ha:' + (value or '')).encode()).hexdigest()


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
    """On the instance that will be active. Returns (code, expires_at).

    One code at a time; a new one replaces the old. It is good for PAIRING_TTL.
    """
    st = _load()
    if st['role'] == ROLE_STANDBY:
        raise HaError('A standby cannot hand out pairing codes - promote it first')
    if st.get('peer'):
        raise HaError('This instance is already paired - unpair it first')
    secret = secrets.token_urlsafe(32)
    expires = int(time.time()) + PAIRING_TTL
    _update(pairing={'code_hash': _hash_secret(secret), 'expires': expires})
    return encode_code(own_url, fingerprint, secret, st['instance_id']), expires


def verify_peer(header_value):
    """The peer dict when an incoming X-PegaProx-Peer header is right, else None."""
    if not header_value or ':' not in header_value:
        return None
    claimed_id, _, secret = header_value.partition(':')
    p = peer()
    if not p or not secret:
        return None
    ok_id = hmac.compare_digest(claimed_id.encode(), (p.get('instance_id') or '').encode())
    ok_secret = hmac.compare_digest(_hash_secret(secret), p.get('secret_in_hash') or '')
    return p if (ok_id and ok_secret) else None


# --- pairing -------------------------------------------------------------------

def accept_pairing(code_secret, standby_id, standby_url, standby_fp, standby_secret):
    """Active side of the handshake. Returns the response body for the standby."""
    with _lock:
        st = _load()
        pairing = st.get('pairing') or {}
        valid = (pairing.get('code_hash')
                 and int(pairing.get('expires') or 0) >= int(time.time())
                 and hmac.compare_digest(_hash_secret(code_secret), pairing['code_hash']))
        if not valid:
            raise HaError('The pairing code is wrong or has expired')
        if st['role'] == ROLE_STANDBY or st.get('peer'):
            raise HaError('This instance cannot take a standby right now')
        if not re.match(r'^[0-9a-f]{32}$', standby_id or '') or standby_id == st['instance_id']:
            raise HaError('The standby did not identify itself')
        if not isinstance(standby_secret, str) or len(standby_secret) < 32:
            raise HaError('The standby did not send a usable secret')
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
        if standby_fp and not re.match(r'^[0-9A-F]{2}(:[0-9A-F]{2}){31}$', standby_fp):
            raise HaError('The standby sent a malformed certificate fingerprint')

        from pegaprox.core.db import get_db
        field_key = get_db().aes_key
        if not field_key or len(field_key) != 32:
            raise HaError('This instance has no field key to share')

        for_standby = secrets.token_urlsafe(32)
        new_epoch = max(1, int(st.get('epoch') or 0))
        _commit_locked(dict(st, role=ROLE_ACTIVE, epoch=new_epoch, pairing=None, peer={
            'instance_id': standby_id,
            'url': standby_url,
            'fingerprint': standby_fp,
            'secret_out': standby_secret,
            'secret_in_hash': _hash_secret(for_standby),
            'paired_at': _now(),
            'role_seen': ROLE_STANDBY,
            'epoch_seen': new_epoch,
        }))
        sealed = _seal(code_secret, {'field_key': base64.b64encode(field_key).decode(),
                                     'secret': for_standby}, aad=standby_id)
        return {'instance_id': st['instance_id'], 'epoch': new_epoch, 'sealed': sealed,
                'key_fp': key_fingerprint(field_key)}


def _check_can_join(st, info):
    if st['role'] != ROLE_STANDALONE or st.get('peer'):
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
    my_secret = secrets.token_urlsafe(32)
    body = {'code': info['secret'], 'instance_id': me, 'url': own_url,
            'fingerprint': own_fingerprint or '', 'secret': my_secret}
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
        secret_to_active = opened['secret']
    except Exception:
        raise HaError('The answer from the active instance could not be opened')
    if len(field_key) != 32 or key_fingerprint(field_key) != data.get('key_fp'):
        raise HaError('The field key from the active instance is not intact')
    new_epoch = data.get('epoch')
    if (isinstance(new_epoch, bool) or not isinstance(new_epoch, int)
            or not 0 < new_epoch < 2 ** 31
            or not isinstance(secret_to_active, str) or len(secret_to_active) < 32):
        raise HaError('The answer from the active instance is incomplete')

    with _lock:
        st = _load()
        if st['role'] == ROLE_STANDALONE and not st.get('peer') and st['instance_id'] == me:
            # standby first, key second: if the key write fails we are a passive
            # standby whose sync refuses a key mismatch, not a standalone that acts
            # on a foreign key. Both under the lock, so accept_pairing cannot seal
            # the adopted key to anybody in between.
            _commit_locked(dict(st, role=ROLE_STANDBY, epoch=new_epoch, pairing=None, sync={}, peer={
                'instance_id': info['instance_id'],
                'url': info['url'],
                'fingerprint': info['fingerprint'],
                'secret_out': secret_to_active,
                'secret_in_hash': _hash_secret(my_secret),
                'paired_at': _now(),
                'role_seen': ROLE_ACTIVE,
                'epoch_seen': new_epoch,
            }))
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
                   auth=None, headers={PEER_HEADER: f'{me}:{secret_to_active}'}, timeout=10)
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
    """Forget the peer. A standby becomes standalone, which means it starts acting
    after the restart the caller schedules.

    The one write allowed on a state file _load could not read: it is the way out,
    and the note about the broken file goes with it.
    """
    with _lock:
        st = _load()
        was = st['role']
        new = {k: v for k, v in st.items() if k != 'broken'}
        new.update(peer=None, pairing=None, sync={}, role=ROLE_STANDALONE)
        _commit_locked(new)
        return was


def forget_peer(peer_id):
    """The peer told us it unpaired. Only the peer itself may do that."""
    with _lock:
        st = _load()
        p = st.get('peer') or {}
        if p.get('instance_id') != peer_id:
            return False
        was = st['role']
        _commit_locked(dict(st, peer=None,
                            role=ROLE_STANDALONE if was == ROLE_ACTIVE else was))
        return True


# --- promotion and stepping down -------------------------------------------------

def promote():
    """Standby to active under a new epoch. The caller restarts the process."""
    with _lock:
        st = _load()
        if st.get('broken'):
            # the stand-in for an unreadable file has no peer and a fresh identity:
            # an active made from it could never tell the real active to step down
            raise HaError('The HA state file cannot be read - restore config/ha_state.json '
                          'and restart before promoting')
        if st['role'] != ROLE_STANDBY:
            raise HaError('Only a standby can be promoted')
        seen = int((st.get('peer') or {}).get('epoch_seen') or 0)
        new_epoch = max(int(st.get('epoch') or 0), seen) + 1
        new = dict(st, epoch=new_epoch, role=ROLE_ACTIVE)
        if st.get('peer'):
            new['peer'] = dict(st['peer'], role_seen=None)
        _commit_locked(new)
        return new_epoch


def step_down(new_epoch, by_peer_id):
    """Active to standby because the peer holds a newer epoch. Returns True when
    this call changed the role; the caller restarts the process then."""
    with _lock:
        st = _load()
        p = st.get('peer') or {}
        if p.get('instance_id') != by_peer_id:
            return False
        if st['role'] != ROLE_ACTIVE or int(new_epoch) <= int(st.get('epoch') or 0):
            return False
        _commit_locked(dict(st, role=ROLE_STANDBY, epoch=int(new_epoch), sync={},
                            peer=dict(p, role_seen=ROLE_ACTIVE, epoch_seen=int(new_epoch))))
    logging.warning(f"[HA] stepped down: peer {by_peer_id} is active with epoch {new_epoch}")
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
    build_snapshot, so the threadpool worker never touches a gevent lock."""
    st = _load()
    return dict(instance_id=st['instance_id'], role=st['role'],
                epoch=int(st.get('epoch') or 0), key_fp=key_fingerprint())


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
                etag=etag, **meta)


def snapshot_etag():
    """The etag build_snapshot() would put on a snapshot now, without building the
    body: a poll that ends in 304 reads and hashes, nothing more."""
    return _walk_snapshot(body=False)[0]


def snapshot_bytes(snap):
    return gzip.compress(json.dumps(snap, default=str).encode(), compresslevel=6)


def apply_snapshot(snap):
    """Replace every SYNC table with the snapshot's rows, in one transaction.

    Refuses a snapshot that is not from our peer, not from an active instance, from
    an older epoch, or sealed under a different field key. Returns a summary.
    """
    p = peer()
    if not p:
        raise HaError('Not paired')
    if snap.get('format') != SNAPSHOT_FORMAT:
        raise HaError('The active instance sends a snapshot format this version does not read - update both to the same release')
    if snap.get('instance_id') != p.get('instance_id'):
        raise HaError('The snapshot is not from the paired instance')
    if snap.get('role') != ROLE_ACTIVE:
        raise HaError('The paired instance is not active')
    if int(snap.get('epoch') or 0) < epoch():
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

    after = _sign_in_rows(conn.cursor())
    if before is not None and after is not None:
        _end_sessions(sorted(u for u, row in before.items() if after.get(u) != row))
    _apply_files(snap.get('files') or {})
    _after_apply()
    return summary


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
    kh = files.get('ssh_known_hosts')
    if isinstance(kh, str):
        _write_private(KNOWN_HOSTS_FILE, kh.encode())
    branding = files.get('branding')
    if isinstance(branding, dict):
        os.makedirs(BRANDING_DIR, exist_ok=True)
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


# --- talking to the peer -------------------------------------------------------

def _peer_call(method, base_url, fingerprint, path, json_body=None, auth='peer',
               headers=None, timeout=15):
    import requests
    from pegaprox.utils.url_security import is_safe_outbound_url
    url = base_url.rstrip('/') + path
    ok, why = is_safe_outbound_url(url, allowed_schemes=('https',), allow_private=True)
    if not ok:
        raise HaError(f'The peer address is not allowed: {why}')
    sess = requests.Session()
    if fingerprint:
        from pegaprox.core.pbs import _PinnedFingerprintAdapter
        sess.mount('https://', _PinnedFingerprintAdapter(fingerprint))
        verify = False
    else:
        verify = True
    h = {'X-Requested-With': 'XMLHttpRequest', 'Accept': 'application/json'}
    if auth == 'peer':
        p = peer()
        if not p:
            raise HaError('Not paired')
        h[PEER_HEADER] = f"{instance_id()}:{p['secret_out']}"
    if headers:
        h.update(headers)
    try:
        import urllib3
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    except Exception:
        pass
    try:
        return sess.request(method, url, json=json_body, headers=h, verify=verify,
                            timeout=timeout, allow_redirects=False)
    except requests.exceptions.SSLError as e:
        if fingerprint:
            raise HaError(f'The peer certificate does not match the pinned fingerprint ({type(e).__name__})')
        raise HaError('The peer certificate is not trusted by a CA, and no fingerprint is pinned '
                      f'for it ({type(e).__name__})')
    except requests.exceptions.RequestException as e:
        raise HaError(f'Cannot reach the peer: {type(e).__name__}')
    finally:
        sess.close()


def _peer_error(resp, fallback):
    try:
        return (resp.json() or {}).get('error') or f'{fallback} (HTTP {resp.status_code})'
    except Exception:
        return f'{fallback} (HTTP {resp.status_code})'


def call_peer(method, path, json_body=None, headers=None, timeout=15):
    p = peer()
    if not p or not p.get('url'):
        raise HaError('No peer address known')
    return _peer_call(method, p['url'], p.get('fingerprint') or '', path,
                      json_body=json_body, headers=headers, timeout=timeout)


def pull_once():
    """Standby: fetch and apply one snapshot. Returns a short status string."""
    global _etag_checked
    if not is_standby():
        return 'not a standby'
    if not peer():
        # nothing to pull from, and writing here would replace a state file that
        # _load could not read with a fresh one
        return 'not paired'
    with _lock:
        first, _etag_checked = not _etag_checked, True
    if first:
        # the first pull after a start is a full one: an upgrade may have added
        # columns, a restored database may hold other rows, and the active's etag
        # knows about neither
        _update_sync(last_attempt_at=_now(), etag=None)
    else:
        _update_sync(last_attempt_at=_now())
    try:
        etag = (_load().get('sync') or {}).get('etag')
        resp = call_peer('GET', '/api/ha/peer/snapshot',
                         headers={'If-None-Match': etag} if etag else None, timeout=60)
        if resp.status_code == 304:
            _update_sync(last_ok_at=_now(), last_error='')
            _update_peer(last_contact=_now(), last_error='')
            return 'unchanged'
        if resp.status_code != 200:
            raise HaError(_peer_error(resp, 'The active instance refused the snapshot'))
        snap = resp.json()
        _update_peer(last_contact=_now(), role_seen=snap.get('role'),
                     epoch_seen=int(snap.get('epoch') or 0), last_error='')
        summary = apply_snapshot(snap)
        # an etag stands for all of the content; with columns left out we hold
        # less than that, and a 304 would keep it so after we are upgraded
        etag = None if summary['skipped_columns'] else snap.get('etag')
        _update_sync(last_ok_at=_now(), last_error='', etag=etag,
                     source_epoch=int(snap.get('epoch') or 0), rows=summary['rows'],
                     tables=summary['tables'], skipped_columns=summary['skipped_columns'])
        return 'applied'
    except Exception as e:
        msg = str(e) if isinstance(e, HaError) else f'{type(e).__name__}: {e}'
        _update_sync(last_error=msg[:300])
        logging.warning(f"[HA] sync failed: {msg}")
        return 'failed'


def _peer_status(timeout):
    """(role, epoch) as the peer reports them. Raises when it does not answer."""
    resp = call_peer('GET', '/api/ha/peer/status', timeout=timeout)
    if resp.status_code != 200:
        raise HaError(_peer_error(resp, 'The peer refused the status call'))
    data = resp.json()
    if not isinstance(data, dict):
        raise HaError('The peer sent a status this version does not read')
    return data.get('role'), int(data.get('epoch') or 0)


def check_peer_at_boot(timeout=5):
    """Ask the peer once, before managers and loops start.

    An instance that comes back as active may have been replaced while it was
    down. When the peer is active under a newer epoch we step down right here,
    without a restart, so the caller comes up as a standby and nothing acts on
    the old configuration. Never raises; returns a short status string.
    """
    try:
        if role() != ROLE_ACTIVE or not peer():
            return 'idle'
        mine, p = epoch(), peer()
        try:
            their_role, their_epoch = _peer_status(timeout)
        except Exception as e:
            _update_peer(last_error=str(e)[:300])
            return 'unreachable'
        _update_peer(last_contact=_now(), role_seen=their_role, epoch_seen=their_epoch, last_error='')
        if their_role == ROLE_ACTIVE and their_epoch > mine and step_down(their_epoch, p['instance_id']):
            _audit('ha.stepped_down', f"at start: peer {p.get('url') or p['instance_id']} "
                                      f"is active with epoch {their_epoch}")
            return 'stepped down'
        return 'ok'
    except Exception as e:
        logging.warning(f"[HA] peer check at start failed: {e}")
        return 'error'


def watch_once():
    """Active: look at the peer. Step down to a newer active, tell an older one
    to step down. Never promotes anything."""
    if role() != ROLE_ACTIVE or not peer():
        return 'idle'
    # our epoch as it was before the call; the peer may step us down meanwhile
    mine = epoch()
    try:
        their_role, their_epoch = _peer_status(10)
    except Exception as e:
        _update_peer(last_error=str(e)[:300])
        return 'unreachable'
    if role() != ROLE_ACTIVE:
        # stepped down while the call was out; that path restarts us already
        return 'idle'
    _update_peer(last_contact=_now(), role_seen=their_role, epoch_seen=their_epoch, last_error='')
    if their_role != ROLE_ACTIVE:
        return 'ok'
    if their_epoch > mine:
        if step_down(their_epoch, peer()['instance_id']):
            _audit('ha.stepped_down', f'peer {peer()["url"]} is active with epoch {their_epoch}')
            restart_process('stepped down to standby')
            return 'stepped down'
        return 'ok'
    if their_epoch < mine:
        try:
            r = call_peer('POST', '/api/ha/peer/step-down', json_body={'epoch': mine}, timeout=10)
            return 'told peer to step down' if r.status_code == 200 else 'peer refused to step down'
        except Exception as e:
            _update_peer(last_error=str(e)[:300])
            return 'unreachable'
    # promote() takes only a standby, so the way out is a fresh pairing
    _update_peer(last_error='Both instances are active with the same epoch - unpair one of them '
                            'and pair it again as the standby of the other')
    logging.error('[HA] both instances are active with the same epoch')
    return 'conflict'


def _audit(action, details):
    try:
        from pegaprox.utils.audit import log_audit
        log_audit('system', action, details)
    except Exception:
        pass


def _loop():
    # give the app a moment to come up before the first call
    time.sleep(5)
    while True:
        try:
            r = role()
            if r == ROLE_STANDBY and peer():
                pull_once()
            elif r == ROLE_ACTIVE:
                watch_once()
        except Exception as e:
            logging.error(f"[HA] loop: {e}")
        interval = int(_load().get('interval') or DEFAULT_INTERVAL)
        time.sleep(max(5, min(interval, 3600)))


def start_loop():
    """Idempotent. Runs in every role; it only works when paired."""
    global _loop_started
    with _lock:
        if _loop_started:
            return
        _loop_started = True
    threading.Thread(target=_loop, daemon=True, name='ha-peer').start()


def public_status():
    st = _load()
    p = st.get('peer') or {}
    pairing = st.get('pairing') or {}
    return {
        'role': st['role'],
        'epoch': int(st.get('epoch') or 0),
        'instance_id': st['instance_id'],
        'interval': int(st.get('interval') or DEFAULT_INTERVAL),
        'broken': st.get('broken') or '',
        'pairing_open_until': pairing.get('expires') if pairing.get('code_hash') and
                              int(pairing.get('expires') or 0) >= int(time.time()) else None,
        'peer': {
            'instance_id': p.get('instance_id'),
            'url': p.get('url'),
            'fingerprint': p.get('fingerprint'),
            'paired_at': p.get('paired_at'),
            'role_seen': p.get('role_seen'),
            'epoch_seen': p.get('epoch_seen'),
            'last_contact': p.get('last_contact'),
            'last_error': p.get('last_error') or '',
        } if p else None,
        'sync': dict(st.get('sync') or {}, etag=None),
    }


def banner():
    """What every logged-in user sees about this instance, nothing more."""
    st = _load()
    if st['role'] != ROLE_STANDBY:
        return {'role': st['role']}
    p = st.get('peer') or {}
    return {'role': st['role'], 'peer_url': p.get('url') or '',
            'last_sync_at': (st.get('sync') or {}).get('last_ok_at') or ''}
