# -*- coding: utf-8 -*-
"""
Automated installations - MK Sep 2026.

Answer-file server for the Proxmox VE auto-installer. The ISO is prepared with

    proxmox-auto-install-assistant prepare-iso pve.iso --fetch-from http \
        --url 'https://pegaprox.example.com/api/auto-install/answer' \
        --answer-auth-token 'pegaprox:<token>' [--cert-fingerprint '<sha256>']

At boot the installer POSTs its hardware inventory there and gets the answer file
back, and that POST is what the run list shows. An installer too old for
--answer-auth-token can carry the token in the URL instead (?token=...). That works,
but then it sits in every access log between the machine and us.

A profile is picked by its token, not by DMI matching. The file carries the root
password, and with matching an unauthenticated POST gets to choose which file it is
handed.

Every served file gets its own [post-installation-webhook], pointing back here with
a callback token that belongs to that one run. A finished machine can close its own
run and nothing else, and the fetch token never ends up on the installed host.
"""
import re
import json
import uuid
import hmac
import time
import hashlib
import secrets
import logging
import threading
from datetime import datetime, timezone
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode

from flask import Blueprint, jsonify, request, Response

from pegaprox.constants import SSL_CERT_FILE, SSL_DIR
from pegaprox.globals import cluster_managers
from pegaprox.core.db import get_db
from pegaprox.utils.auth import require_auth, build_authz_user
from pegaprox.utils.audit import log_audit, get_client_ip
from pegaprox.api.helpers import safe_error, load_server_settings, effective_reverse_proxy

bp = Blueprint('auto_install', __name__)

# Hostname, IPv4 or bracketed IPv6, optional port. The value ends up inside a TOML
# string in the served file, so anything else is dropped rather than escaped.
_HOST_RE = re.compile(r'^(?:[A-Za-z0-9](?:[A-Za-z0-9.\-]{0,251}[A-Za-z0-9])?|\[[0-9A-Fa-f:.]{2,45}\])(?::\d{1,5})?$')
_CLUSTER_ID_RE = re.compile(r'^[A-Za-z0-9_.\-]{0,64}$')

_MAX_SYSTEM_INFO = 64 * 1024
_MAX_ANSWER = 256 * 1024
_RUNS_KEPT_PER_PROFILE = 1000

_STATUSES = ('installing', 'installed', 'failed')

# Values a view-only reader must never get back. The installer also takes the
# snake_case spellings, so those count too. A subscription key is not a login, but
# it is somebody's paid key.
_SECRET_KEYS = ('root-password', 'root_password', 'root-password-hashed',
                'root_password_hashed', 'subscription-key', 'subscription_key')

_UNREDACTABLE = ('# This answer file cannot be shown to your role: a secret in it is written\n'
                 '# in a form that cannot be blanked line by line. Ask someone who can edit it.\n')

# DMI serials that vendors ship as placeholders. Keying runs on one of these would
# fold every machine of that model into a single row.
_JUNK_SERIALS = {'', 'none', 'unknown', 'to be filled by o.e.m.', 'system serial number',
                 'default string', '0123456789', 'not specified', 'n/a'}
_JUNK_UUIDS = {'', '00000000-0000-0000-0000-000000000000', 'ffffffff-ffff-ffff-ffff-ffffffffffff',
               '03000200-0400-0500-0006-000700080009'}


def _utcnow():
    return datetime.now(timezone.utc)


def _stamp(dt=None):
    """ISO timestamp in UTC with the offset spelled out, so the browser reads it
    as UTC and so two stamps compare correctly as plain strings."""
    return (dt or _utcnow()).astimezone(timezone.utc).isoformat(timespec='seconds')


def _parse_stamp(value):
    """Aware UTC datetime or ValueError. A value without an offset is taken as UTC,
    which is what the UI sends (toISOString) and what we store."""
    dt = datetime.fromisoformat(str(value).strip().replace('Z', '+00:00'))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _hash_token(token):
    return hashlib.sha256(token.encode('utf-8')).hexdigest()


def _new_token():
    # url-safe, no characters that need quoting on the prepare-iso command line
    return 'pgxai_' + secrets.token_urlsafe(32)


def _present_token_hint(token):
    return token[:12]


def _s(value, limit):
    """str() only for things that already are strings. The installer sends nested
    objects under names that sound like plain fields (product is a dict), and a
    repr in the run list is worse than a blank."""
    return value.strip()[:limit] if isinstance(value, str) else ''


# --- answer file ------------------------------------------------------------

def _parse_toml(text):
    """(data, error). tomllib is stdlib from Python 3.11. On an older interpreter
    every check fails with this message and nothing gets saved or served."""
    try:
        import tomllib
    except ImportError:      # pragma: no cover
        return None, 'TOML parsing needs Python 3.11 or newer'
    try:
        return tomllib.loads(text), None
    except Exception as e:
        return None, str(e)


# What the installer knows. It refuses anything else in these places outright
# (deny_unknown_fields), so a typo in an optional key only shows up at the rack.
_KNOWN_SECTIONS = ('global', 'network', 'disk-setup', 'post-installation-webhook', 'first-boot')
_KNOWN_GLOBAL = ('keyboard', 'country', 'fqdn', 'mailto', 'timezone', 'root-password',
                 'root_password', 'root-password-hashed', 'reboot-on-error', 'reboot-mode',
                 'root-ssh-keys', 'subscription-key')

_WEBHOOK_HEADER = re.compile(r'^\s*\[\s*post-installation-webhook\s*\]\s*$')
_ANY_TABLE = re.compile(r'^\s*\[')


def validate_answer(text):
    """(errors, warnings) for an answer file, checked the way the installer checks it.

    Errors are what the installer refuses. Warnings work but are usually a mistake.
    """
    errors, warnings = [], []
    if not isinstance(text, str) or not text.strip():
        return ['The answer file is empty'], []
    if len(text) > _MAX_ANSWER:
        return ['The answer file is too large (max 256 KB)'], []

    data, err = _parse_toml(text)
    if err:
        return [f'Not valid TOML: {err}'], []

    g = data.get('global')
    if not isinstance(g, dict):
        errors.append('Missing [global] section')
        g = {}
    for key in ('keyboard', 'country', 'mailto', 'timezone'):
        if not g.get(key):
            errors.append(f'[global] is missing "{key}"')

    unknown = sorted(k for k in data if k not in _KNOWN_SECTIONS)
    if unknown:
        warnings.append('The installer rejects sections it does not know: ' + ', '.join(unknown))
    unknown = sorted(k for k in g if k not in _KNOWN_GLOBAL)
    if unknown:
        warnings.append('[global] has keys the installer may not know: ' + ', '.join(unknown))

    fqdn = g.get('fqdn')
    if isinstance(fqdn, dict):
        # fqdn.source = "from-dhcp", optionally with fqdn.domain as the fallback
        if fqdn.get('source') != 'from-dhcp':
            errors.append('[global] fqdn.source must be "from-dhcp"')
    elif isinstance(fqdn, str) and fqdn:
        if '.' not in fqdn:
            errors.append('[global] "fqdn" must be fully qualified (host.domain.tld)')
    else:
        errors.append('[global] is missing "fqdn" (a name, or fqdn.source = "from-dhcp")')

    plain = g.get('root-password') or g.get('root_password')
    hashed = g.get('root-password-hashed') or g.get('root_password_hashed')
    if not plain and not hashed:
        errors.append('[global] needs either "root-password" or "root-password-hashed"')
    elif plain and hashed:
        errors.append('[global] has both "root-password" and "root-password-hashed" - pick one')

    net = data.get('network')
    if not isinstance(net, dict):
        errors.append('Missing [network] section')
    else:
        source = net.get('source')
        if source not in ('from-dhcp', 'from-answer'):
            errors.append('[network] "source" must be "from-dhcp" or "from-answer"')
        elif source == 'from-answer':
            for key in ('cidr', 'dns', 'gateway', 'filter'):
                if not net.get(key):
                    errors.append(f'[network] source=from-answer also needs "{key}"')

    disk = data.get('disk-setup')
    if not isinstance(disk, dict):
        errors.append('Missing [disk-setup] section')
    else:
        fs = disk.get('filesystem')
        if not fs:
            errors.append('[disk-setup] is missing "filesystem"')
        elif fs not in ('ext4', 'xfs', 'zfs', 'btrfs'):
            warnings.append(f'[disk-setup] filesystem "{fs}" is not one the installer normally offers')
        if not disk.get('disk-list') and not disk.get('filter'):
            errors.append('[disk-setup] needs "disk-list" or "filter" so the installer knows where to write')
        # zfs.raid = "raid1" is a dotted key, so it parses to {'zfs': {'raid': ...}}
        if fs in ('zfs', 'btrfs'):
            sub = disk.get(fs)
            if not (isinstance(sub, dict) and sub.get('raid')):
                warnings.append(f'[disk-setup] {fs} without {fs}.raid installs whatever the installer defaults to')

    if 'post-installation-webhook' in data:
        if any(_WEBHOOK_HEADER.match(line) for line in text.splitlines()):
            warnings.append('The [post-installation-webhook] section is replaced by PegaProx so it can track the install')
        else:
            # dotted or inline form: we can only replace the table form, and two
            # definitions of the same table are a parse error on the installer
            errors.append('Write post-installation-webhook as its own [post-installation-webhook] table, '
                          'or leave it out - PegaProx adds its own')

    if plain:
        warnings.append('This answer file stores the root password in clear text. '
                        '"root-password-hashed" is the better habit.')
    return errors, warnings


def _strip_webhook_section(text):
    """Drop a [post-installation-webhook] table so ours doesn't collide with it."""
    out, skipping = [], False
    for line in text.splitlines():
        if _WEBHOOK_HEADER.match(line):
            skipping = True
            continue
        if skipping:
            if _ANY_TABLE.match(line):
                skipping = False
            else:
                continue
        out.append(line)
    return '\n'.join(out)


def _toml_escape(value):
    return str(value).replace('\\', '\\\\').replace('"', '\\"')


def _with_token(url, token):
    """Add token= to a URL that may already carry a query of its own."""
    parts = urlsplit(url)
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k != 'token']
    query.append(('token', token))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


def own_cert_fingerprint():
    """SHA-256 fingerprint of config/ssl/cert.pem in the colon-separated upper-hex
    form Proxmox prints, or ''."""
    try:
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes
        with open(SSL_CERT_FILE, 'rb') as fh:
            cert = x509.load_pem_x509_certificate(fh.read())
        return ':'.join(f'{b:02X}' for b in cert.fingerprint(hashes.SHA256()))
    except Exception as e:
        logging.debug(f"[autoinstall] could not fingerprint {SSL_CERT_FILE}: {e}")
        return ''


def self_signed_fingerprint():
    """Our fingerprint if the certificate is self-signed, else ''.

    Only a self-signed certificate needs pinning, and pinning one from a CA would
    break every ISO prepared before its next renewal."""
    try:
        from pegaprox.core.acme import get_cert_info
        info = get_cert_info(SSL_DIR) or {}
    except Exception:
        info = {}
    return own_cert_fingerprint() if info.get('is_self_signed') else ''


def _public_base():
    """(scheme, host, direct) for the address the installer should call back on.

    host is '' when nothing usable came in. direct is False when a proxy sits in
    front, in which case the certificate on the wire is not ours.
    """
    from pegaprox.utils.audit import _is_trusted_proxy
    settings = load_server_settings() or {}
    behind = effective_reverse_proxy(settings)
    trusted = bool(request.remote_addr and _is_trusted_proxy(request.remote_addr))

    scheme = request.scheme
    host = request.host or ''
    forwarded = False
    if trusted:
        # same rule as the webauthn host and the CSRF origin check: forwarded
        # headers count only when a proxy we trust set them
        fwd_host = (request.headers.get('X-Forwarded-Host') or '').split(',')[0].strip()
        fwd_proto = (request.headers.get('X-Forwarded-Proto') or '').split(',')[0].strip().lower()
        if fwd_host:
            host, forwarded = fwd_host, True
        if fwd_proto in ('http', 'https'):
            scheme, forwarded = fwd_proto, True

    domain = (settings.get('domain') or '').strip()
    if domain:
        if behind or forwarded:
            host = domain
        else:
            # keep the port the installer used to reach us
            port = host.rsplit(':', 1)[1] if ':' in host and not host.endswith(']') else ''
            host = f'{domain}:{port}' if port else domain

    if not _HOST_RE.match(host):
        logging.warning(f"[autoinstall] refusing to build a callback URL from host {host!r}")
        return '', '', False
    return scheme, host, not (behind or forwarded)


def callback_target(profile):
    """(url, fingerprint) for the webhook section. An explicit per-profile URL wins."""
    url = (profile.get('callback_url') or '').strip()
    fp = (profile.get('callback_fingerprint') or '').strip()
    if url:
        return url, fp
    scheme, host, direct = _public_base()
    if not host:
        return '', ''
    url = f'{scheme}://{host}/api/auto-install/progress'
    if not fp and scheme == 'https' and direct:
        fp = self_signed_fingerprint()
    return url, fp


def render_answer(profile, callback_token):
    """The stored answer plus our webhook section. Returns (text, error)."""
    answer = profile.get('answer') or ''
    if profile.get('answer_unreadable') or not answer.strip():
        return None, 'the stored answer file could not be read'
    body = _strip_webhook_section(answer).rstrip()
    url, fp = callback_target(profile)
    if url:
        lines = [body, '', '[post-installation-webhook]',
                 f'url = "{_toml_escape(_with_token(url, callback_token))}"']
        if fp:
            lines.append(f'cert-fingerprint = "{_toml_escape(fp)}"')
        body = '\n'.join(lines)
    body += '\n'
    # check what goes out, not what came in: a file the installer refuses leaves a
    # machine sitting at the installer prompt, and a 500 here is the better outcome
    errors, _warnings = validate_answer(body)
    if errors:
        return None, errors[0]
    return body, None


def _secret_values(node):
    found = []
    if isinstance(node, dict):
        for k, v in node.items():
            if k in _SECRET_KEYS and isinstance(v, str) and v:
                found.append(v)
            else:
                found.extend(_secret_values(v))
    elif isinstance(node, list):
        for v in node:
            found.extend(_secret_values(v))
    return found


def redact_answer(text):
    """Blank secret values for a view-only reader, and fail closed.

    The line rewrite handles the usual `key = "value"`. TOML has four more ways to
    write the same key (quoted, dotted, inline table, multi-line string), so the
    result is checked against the parsed file, and if any secret survived the reader
    gets a placeholder instead of the text.
    """
    out = []
    for line in (text or '').splitlines():
        stripped = line.lstrip()
        key = stripped.split('=', 1)[0].strip() if '=' in stripped else ''
        if key in _SECRET_KEYS:
            out.append(f'{line[:len(line) - len(stripped)]}{key} = "********"')
        else:
            out.append(line)
    redacted = '\n'.join(out)
    data, err = _parse_toml(text or '')
    if err:
        return _UNREDACTABLE
    if any(value in redacted for value in _secret_values(data)):
        return _UNREDACTABLE
    return redacted


# --- storage ----------------------------------------------------------------

_PROFILE_COLS = ('id', 'name', 'description', 'answer_encrypted', 'target_cluster_id',
                 'callback_url', 'callback_fingerprint', 'token_hash', 'token_hint',
                 'enabled', 'max_uses', 'uses', 'expires_at', 'created_at', 'created_by',
                 'updated_at', 'updated_by')


def _row_to_profile(row, with_answer=False):
    keys = row.keys()
    p = {k: (row[k] if k in keys else None) for k in _PROFILE_COLS}
    answer = ''
    unreadable = False
    if p.pop('answer_encrypted', None):
        try:
            answer = get_db()._decrypt(row['answer_encrypted']) or ''
        except Exception as e:
            logging.error(f"[autoinstall] could not decrypt answer for {p.get('id')}: {e}")
            unreadable = True
    p['enabled'] = bool(p.get('enabled'))
    p['max_uses'] = int(p.get('max_uses') or 0)
    p['uses'] = int(p.get('uses') or 0)
    p['answer_unreadable'] = unreadable
    if with_answer:
        p['answer'] = answer
    return p, answer


def _load_profile(profile_id):
    c = get_db().conn.cursor()
    c.execute('SELECT * FROM auto_install_profiles WHERE id = ?', (profile_id,))
    row = c.fetchone()
    if not row:
        return None, ''
    return _row_to_profile(row, with_answer=True)


def _profile_for_token(token):
    """Lookup by the token's hash; the plaintext is never stored."""
    if not isinstance(token, str) or not token:
        return None
    th = _hash_token(token)
    c = get_db().conn.cursor()
    c.execute('SELECT * FROM auto_install_profiles WHERE token_hash = ?', (th,))
    row = c.fetchone()
    if not row or not hmac.compare_digest(str(row['token_hash']), th):
        return None
    profile, _ = _row_to_profile(row, with_answer=True)
    return profile


def _run_for_callback_token(token):
    if not isinstance(token, str) or not token:
        return None
    th = _hash_token(token)
    c = get_db().conn.cursor()
    c.execute('SELECT * FROM auto_install_runs WHERE callback_token_hash = ?', (th,))
    row = c.fetchone()
    if not row or not hmac.compare_digest(str(row['callback_token_hash']), th):
        return None
    return {k: row[k] for k in row.keys()}


def _request_token():
    """Bearer header first, then ?token=.

    --answer-auth-token sends `Authorization: Bearer <name>:<secret>`. The name is
    free text for the operator and our tokens never contain a colon, so the secret
    is whatever follows the last one.
    """
    auth = request.headers.get('Authorization', '')
    if auth[:7].lower() == 'bearer ':
        return auth[7:].strip().rsplit(':', 1)[-1].strip()
    return (request.args.get('token') or '').strip()


def _profile_usable(profile):
    """(ok, reason). Fails closed on an expiry it cannot read."""
    if not profile.get('enabled'):
        return False, 'disabled'
    expires = profile.get('expires_at') or ''
    if expires:
        try:
            if _parse_stamp(expires) <= _utcnow():
                return False, 'expired'
        except (ValueError, TypeError):
            logging.warning(f"[autoinstall] profile {profile.get('id')} has an unreadable expiry {expires!r}")
            return False, 'unreadable expiry'
    max_uses = int(profile.get('max_uses') or 0)
    if max_uses and int(profile.get('uses') or 0) >= max_uses:
        return False, 'use limit reached'
    return True, ''


# --- admin API --------------------------------------------------------------

def _refuse_confined_caller():
    """403 unless the caller sees every cluster.

    Profiles belong to no tenant. One answer file can build a host for any cluster
    and it carries that host's root password, so a tenant-confined role holding
    autoinstall.* would otherwise read and rewrite every other tenant's files.
    """
    from pegaprox.utils.rbac import get_user_clusters
    session = getattr(request, 'session', None) or {}
    try:
        scope = get_user_clusters(build_authz_user(session.get('user', ''), session),
                                  include_pools=False)
    except Exception as e:
        logging.warning(f"[autoinstall] could not resolve the caller's cluster scope: {e}")
        scope = []
    if scope is not None:
        return jsonify({'error': 'Automated installations are only available to accounts '
                                 'that are not limited to a tenant or to specific clusters'}), 403
    return None


def _may_manage():
    from pegaprox.utils.rbac import has_permission
    session = getattr(request, 'session', None) or {}
    return has_permission(build_authz_user(session.get('user'), session), 'autoinstall.manage')


def _public_profile(profile, answer=None):
    out = dict(profile)
    out.pop('answer', None)
    out.pop('token_hash', None)
    cluster = cluster_managers.get(out.get('target_cluster_id') or '')
    label = getattr(getattr(cluster, 'config', None), 'name', '') if cluster else ''
    out['target_cluster_name'] = label if isinstance(label, str) else ''
    if answer is not None:
        out['answer'] = answer
    return out


def _with_targets(out, profile):
    url, fp = callback_target(profile)
    out['callback_effective_url'] = url
    out['callback_effective_fingerprint'] = fp
    # what prepare-iso needs to trust us; '' behind a proxy or with a CA certificate
    out['fetch_fingerprint'] = '' if effective_reverse_proxy() else self_signed_fingerprint()
    return out


@bp.route('/api/auto-install/profiles', methods=['GET'])
@require_auth(perms=['autoinstall.view'])
def list_profiles():
    """Profile metadata. The answer file is a separate read since it carries the
    root password. can_manage is resolved here with token flooring applied, which
    the permission list the browser holds does not do."""
    denied = _refuse_confined_caller()
    if denied:
        return denied
    try:
        c = get_db().conn.cursor()
        c.execute('SELECT profile_id, status, COUNT(*) AS n FROM auto_install_runs '
                  'GROUP BY profile_id, status')
        counts = {}
        for r in c.fetchall():
            counts.setdefault(r['profile_id'], {})[r['status']] = r['n']
        c.execute('SELECT * FROM auto_install_profiles ORDER BY name COLLATE NOCASE')
        profiles = []
        for row in c.fetchall():
            p, _ = _row_to_profile(row)
            p['run_counts'] = counts.get(p['id'], {})
            profiles.append(_public_profile(p))
        return jsonify({'profiles': profiles, 'can_manage': _may_manage()})
    except Exception as e:
        return jsonify({'error': safe_error(e, 'Could not list installation profiles')}), 500


@bp.route('/api/auto-install/profiles/<profile_id>', methods=['GET'])
@require_auth(perms=['autoinstall.view'])
def get_profile(profile_id):
    """The answer comes back blanked unless the caller may also edit it."""
    denied = _refuse_confined_caller()
    if denied:
        return denied
    try:
        profile, answer = _load_profile(profile_id)
        if not profile:
            return jsonify({'error': 'Profile not found'}), 404
        may_edit = _may_manage()
        out = _public_profile(profile, answer if may_edit else redact_answer(answer))
        out['answer_redacted'] = not may_edit
        return jsonify(_with_targets(out, profile))
    except Exception as e:
        return jsonify({'error': safe_error(e, 'Could not read the installation profile')}), 500


def _profile_payload(data, existing=None):
    """(fields, error), shared by create and update.

    On update a key the caller did not send keeps its stored value. Falling back to
    the create defaults instead would let a PUT with only a new name re-enable a
    revoked profile and lift its use limit and expiry.
    """
    def pick(key, default):
        if key in data:
            return data.get(key)
        if existing is not None:
            return existing.get(key, default)
        return default

    name = (pick('name', '') or '').strip()
    if not name:
        return None, 'A name is required'
    if len(name) > 120:
        return None, 'The name is too long (max 120 characters)'

    answer = pick('answer', None)
    if existing is not None and 'answer' not in data and existing.get('answer_unreadable'):
        return None, 'The stored answer file cannot be read - send a new one'
    if not isinstance(answer, str) or not answer.strip():
        return None, 'The answer file is required'
    errors, _warnings = validate_answer(answer)
    if errors:
        return None, errors[0] if len(errors) == 1 else 'The answer file has %d problems' % len(errors)

    target = (pick('target_cluster_id', '') or '').strip()
    # deliberately not checked against the live cluster list: the cluster a new node
    # is meant for is often not added yet
    if not _CLUSTER_ID_RE.match(target):
        return None, 'target_cluster_id is not a valid cluster id'

    callback_url = (pick('callback_url', '') or '').strip()
    if callback_url and not callback_url.startswith(('http://', 'https://')):
        return None, 'callback_url must be an http:// or https:// URL'
    if len(callback_url) > 500:
        return None, 'callback_url is too long'
    fp = (pick('callback_fingerprint', '') or '').strip().upper()
    if fp and not re.match(r'^[0-9A-F]{2}(:[0-9A-F]{2}){31}$', fp):
        return None, 'callback_fingerprint must be a SHA-256 fingerprint (32 colon-separated hex bytes)'

    try:
        max_uses = int(pick('max_uses', 0) or 0)
    except (TypeError, ValueError):
        return None, 'max_uses must be a number'
    if max_uses < 0 or max_uses > 10000:
        return None, 'max_uses must be between 0 (unlimited) and 10000'

    expires_at = (pick('expires_at', '') or '').strip()
    if expires_at:
        try:
            expires_at = _stamp(_parse_stamp(expires_at))
        except (ValueError, TypeError):
            return None, 'expires_at must be an ISO timestamp'

    return {
        'name': name,
        'description': (pick('description', '') or '').strip()[:500],
        'answer': answer,
        'target_cluster_id': target,
        'callback_url': callback_url,
        'callback_fingerprint': fp,
        'max_uses': max_uses,
        'expires_at': expires_at,
        'enabled': 1 if pick('enabled', True) else 0,
    }, None


@bp.route('/api/auto-install/profiles', methods=['POST'])
@require_auth(perms=['autoinstall.manage'])
def create_profile():
    """The token is returned here and never again; only its hash is stored."""
    denied = _refuse_confined_caller()
    if denied:
        return denied
    try:
        fields, err = _profile_payload(request.get_json(silent=True) or {})
        if err:
            return jsonify({'error': err}), 400

        token = _new_token()
        pid = str(uuid.uuid4())
        user = request.session.get('user', 'system')
        db = get_db()
        c = db.conn.cursor()
        c.execute('''INSERT INTO auto_install_profiles
                       (id, name, description, answer_encrypted, target_cluster_id,
                        callback_url, callback_fingerprint, token_hash, token_hint,
                        enabled, max_uses, uses, expires_at, created_at, created_by,
                        updated_at, updated_by)
                     VALUES (?,?,?,?,?,?,?,?,?,?,?,0,?,?,?,?,?)''',
                  (pid, fields['name'], fields['description'], db._encrypt(fields['answer']),
                   fields['target_cluster_id'], fields['callback_url'], fields['callback_fingerprint'],
                   _hash_token(token), _present_token_hint(token), fields['enabled'],
                   fields['max_uses'], fields['expires_at'], _stamp(), user, _stamp(), user))
        db.conn.commit()
        log_audit(user, 'autoinstall.profile_created', f"Automated install profile '{fields['name']}'")

        profile, answer = _load_profile(pid)
        out = _with_targets(_public_profile(profile, answer), profile)
        out['token'] = token
        return jsonify(out), 201
    except Exception as e:
        return jsonify({'error': safe_error(e, 'Could not create the installation profile')}), 500


@bp.route('/api/auto-install/profiles/<profile_id>', methods=['PUT'])
@require_auth(perms=['autoinstall.manage'])
def update_profile(profile_id):
    denied = _refuse_confined_caller()
    if denied:
        return denied
    try:
        existing, _answer = _load_profile(profile_id)
        if not existing:
            return jsonify({'error': 'Profile not found'}), 404
        fields, err = _profile_payload(request.get_json(silent=True) or {}, existing=existing)
        if err:
            return jsonify({'error': err}), 400

        user = request.session.get('user', 'system')
        db = get_db()
        c = db.conn.cursor()
        c.execute('''UPDATE auto_install_profiles
                        SET name = ?, description = ?, answer_encrypted = ?, target_cluster_id = ?,
                            callback_url = ?, callback_fingerprint = ?, enabled = ?, max_uses = ?,
                            expires_at = ?, updated_at = ?, updated_by = ?
                      WHERE id = ?''',
                  (fields['name'], fields['description'], db._encrypt(fields['answer']),
                   fields['target_cluster_id'], fields['callback_url'], fields['callback_fingerprint'],
                   fields['enabled'], fields['max_uses'], fields['expires_at'], _stamp(), user, profile_id))
        db.conn.commit()
        log_audit(user, 'autoinstall.profile_updated', f"Automated install profile '{fields['name']}'")
        profile, answer = _load_profile(profile_id)
        return jsonify(_with_targets(_public_profile(profile, answer), profile))
    except Exception as e:
        return jsonify({'error': safe_error(e, 'Could not update the installation profile')}), 500


@bp.route('/api/auto-install/profiles/<profile_id>/token', methods=['POST'])
@require_auth(perms=['autoinstall.manage'])
def rotate_token(profile_id):
    """New token, old one dead. This is the revoke button for every ISO already
    prepared from this profile. The use counter starts over with it."""
    denied = _refuse_confined_caller()
    if denied:
        return denied
    try:
        profile, _ = _load_profile(profile_id)
        if not profile:
            return jsonify({'error': 'Profile not found'}), 404
        token = _new_token()
        user = request.session.get('user', 'system')
        db = get_db()
        c = db.conn.cursor()
        c.execute('''UPDATE auto_install_profiles
                        SET token_hash = ?, token_hint = ?, uses = 0, updated_at = ?, updated_by = ?
                      WHERE id = ?''',
                  (_hash_token(token), _present_token_hint(token), _stamp(), user, profile_id))
        db.conn.commit()
        log_audit(user, 'autoinstall.token_rotated', f"Automated install profile '{profile.get('name')}'")
        return jsonify(_with_targets({'token': token, 'token_hint': _present_token_hint(token)}, profile))
    except Exception as e:
        return jsonify({'error': safe_error(e, 'Could not rotate the token')}), 500


@bp.route('/api/auto-install/profiles/<profile_id>', methods=['DELETE'])
@require_auth(perms=['autoinstall.manage'])
def delete_profile(profile_id):
    denied = _refuse_confined_caller()
    if denied:
        return denied
    try:
        profile, _ = _load_profile(profile_id)
        if not profile:
            return jsonify({'error': 'Profile not found'}), 404
        user = request.session.get('user', 'system')
        db = get_db()
        c = db.conn.cursor()
        c.execute('DELETE FROM auto_install_runs WHERE profile_id = ?', (profile_id,))
        c.execute('DELETE FROM auto_install_profiles WHERE id = ?', (profile_id,))
        db.conn.commit()
        log_audit(user, 'autoinstall.profile_deleted', f"Automated install profile '{profile.get('name')}'")
        return jsonify({'message': 'Profile deleted'})
    except Exception as e:
        return jsonify({'error': safe_error(e, 'Could not delete the installation profile')}), 500


@bp.route('/api/auto-install/validate', methods=['POST'])
@require_auth(perms=['autoinstall.manage'])
def validate_endpoint():
    denied = _refuse_confined_caller()
    if denied:
        return denied
    data = request.get_json(silent=True) or {}
    errors, warnings = validate_answer(data.get('answer'))
    return jsonify({'valid': not errors, 'errors': errors, 'warnings': warnings})


@bp.route('/api/auto-install/runs', methods=['GET'])
@require_auth(perms=['autoinstall.view'])
def list_runs():
    """Newest 500. Each profile keeps its last 1000 runs, older ones are pruned
    when a new machine checks in."""
    denied = _refuse_confined_caller()
    if denied:
        return denied
    try:
        c = get_db().conn.cursor()
        profile_id = (request.args.get('profile_id') or '').strip()
        query = ('SELECT r.*, p.name AS profile_name FROM auto_install_runs r '
                 'LEFT JOIN auto_install_profiles p ON p.id = r.profile_id ')
        if profile_id:
            c.execute(query + 'WHERE r.profile_id = ? ORDER BY r.started_at DESC LIMIT 500', (profile_id,))
        else:
            c.execute(query + 'ORDER BY r.started_at DESC LIMIT 500')
        runs = []
        for row in c.fetchall():
            run = {k: row[k] for k in row.keys()}
            run.pop('callback_token_hash', None)
            try:
                run['system_info'] = json.loads(run.get('system_info') or '{}')
            except (ValueError, TypeError):
                run['system_info'] = {}
            runs.append(run)
        return jsonify(runs)
    except Exception as e:
        return jsonify({'error': safe_error(e, 'Could not list installation runs')}), 500


@bp.route('/api/auto-install/runs/<run_id>', methods=['DELETE'])
@require_auth(perms=['autoinstall.manage'])
def delete_run(run_id):
    denied = _refuse_confined_caller()
    if denied:
        return denied
    try:
        db = get_db()
        c = db.conn.cursor()
        c.execute('DELETE FROM auto_install_runs WHERE id = ?', (run_id,))
        db.conn.commit()
        if not c.rowcount:
            return jsonify({'error': 'Run not found'}), 404
        log_audit(request.session.get('user', 'system'), 'autoinstall.run_cleared', run_id)
        return jsonify({'message': 'Run cleared'})
    except Exception as e:
        return jsonify({'error': safe_error(e, 'Could not clear the run')}), 500


# --- installer-facing endpoints: no session, a token is the whole gate --------

def _machine_fingerprint(info):
    """A stable id for the machine, so a retried fetch updates its run instead of
    adding one. Serial, then the SMBIOS uuid (VMs usually have no serial but a
    uuid), then the lowest MAC. Nothing usable means a new row every time, which
    is the safe direction: two rows confuse, one row covering two machines lies."""
    if not isinstance(info, dict):
        return ''
    dmi = info.get('dmi') if isinstance(info.get('dmi'), dict) else {}
    system = dmi.get('system') if isinstance(dmi.get('system'), dict) else {}
    serial = _s(system.get('serial'), 128)
    if serial.lower() not in _JUNK_SERIALS:
        return f'serial:{serial}'
    smbios_uuid = _s(system.get('uuid'), 64).lower()
    if smbios_uuid not in _JUNK_UUIDS:
        return f'uuid:{smbios_uuid}'
    # the fetch payload says network_interfaces, the webhook network-interfaces
    nics = info.get('network_interfaces') or info.get('network-interfaces') or []
    macs = sorted(_s(n.get('mac'), 32).lower() for n in nics if isinstance(n, dict))
    macs = [m for m in macs if m and m != '00:00:00:00:00:00']
    return f'mac:{macs[0]}' if macs else ''


def _summarise_system(info):
    """(hostname, hardware, version) for the run list, read defensively from
    whatever the installer sent."""
    if not isinstance(info, dict):
        return '', '', ''
    dmi = info.get('dmi') if isinstance(info.get('dmi'), dict) else {}
    system = dmi.get('system') if isinstance(dmi.get('system'), dict) else {}
    hardware = ' '.join(x for x in (_s(system.get('manufacturer'), 60),
                                    _s(system.get('name') or system.get('product'), 120)) if x)
    iso = info.get('iso') if isinstance(info.get('iso'), dict) else {}
    product = info.get('product') if isinstance(info.get('product'), dict) else {}
    version = _s(product.get('version'), 40) or _s(iso.get('release'), 40)
    hostname = _s(info.get('fqdn'), 253) or _s(info.get('hostname'), 253)
    return hostname, hardware[:120], version


def _bounded_json(info):
    """The inventory, small enough to keep. Cutting the dumped string would store
    something that no longer parses, and the run list would silently show {}."""
    raw = json.dumps(info)
    if len(raw) <= _MAX_SYSTEM_INFO:
        return raw
    return json.dumps({'truncated': True, 'bytes': len(raw), 'keys': sorted(info.keys())[:40]})


_refusals = {}
_refusals_lock = threading.Lock()
_REFUSAL_WINDOW = 60
_REFUSAL_AUDIT_MAX = 10


def _reject(reason, token, status=403):
    """One answer for every refusal, so a prober cannot tell an unknown token from
    a spent one. Audited, but only the first few per address and minute - guessing
    a 256-bit token is hopeless, and past that the rows are only noise that would
    crowd real events out of the SIEM queue."""
    ip = get_client_ip()
    now = time.monotonic()
    with _refusals_lock:
        start, n = _refusals.get(ip, (now, 0))
        if now - start > _REFUSAL_WINDOW:
            start, n = now, 0
        _refusals[ip] = (start, n + 1)
        if len(_refusals) > 4096:
            _refusals.clear()
    hint = (token[:12] + '...') if token else 'missing'
    if n < _REFUSAL_AUDIT_MAX:
        log_audit('installer', 'autoinstall.fetch_refused', f'{reason} (token {hint})', ip_address=ip)
    else:
        logging.debug(f"[autoinstall] refused {ip}: {reason}")
    return jsonify({'error': 'Invalid or expired installation token'}), status


@bp.route('/api/auto-install/answer', methods=['POST'])
def serve_answer():
    """Where the prepared ISO asks for its answer file. Plain-text TOML back."""
    token = _request_token()
    if not token:
        return _reject('no token presented', '')
    try:
        profile = _profile_for_token(token)
        if not profile:
            return _reject('unknown token', token)
        ok, why = _profile_usable(profile)
        if not ok:
            return _reject(why, token)
        if profile.get('answer_unreadable'):
            logging.error(f"[autoinstall] profile {profile['id']} has an answer file that cannot be decrypted")
            return jsonify({'error': 'The stored answer file could not be read'}), 500

        # get_json can yield under gevent while a slow body trickles in, so nothing
        # decided above is still true afterwards - see the claim below
        info = request.get_json(silent=True)
        if not isinstance(info, dict):
            info = {}
        hostname, hardware, version = _summarise_system(info)
        fingerprint = _machine_fingerprint(info)

        callback_token = secrets.token_urlsafe(32)
        body, err = render_answer(profile, callback_token)
        if err:
            logging.error(f"[autoinstall] profile {profile['id']} rendered an invalid answer file: {err}")
            return jsonify({'error': 'The stored answer file could not be rendered'}), 500

        db = get_db()
        c = db.conn.cursor()
        now = _stamp()
        # the use is claimed here, in one statement, so two fetches racing for the
        # last use - or a fetch racing a revoke or a rotate - cannot both win
        c.execute('''UPDATE auto_install_profiles SET uses = uses + 1
                      WHERE id = ? AND token_hash = ? AND enabled = 1
                        AND (max_uses = 0 OR uses < max_uses)
                        AND (expires_at IS NULL OR expires_at = '' OR expires_at > ?)''',
                  (profile['id'], _hash_token(token), now))
        if c.rowcount != 1:
            return _reject('revoked, expired or used up while the request was in flight', token)

        existing = None
        if fingerprint:
            c.execute('''SELECT id FROM auto_install_runs WHERE profile_id = ? AND fingerprint = ?
                          ORDER BY started_at DESC LIMIT 1''', (profile['id'], fingerprint))
            existing = c.fetchone()
        run_values = (hostname, hardware, version, _bounded_json(info), get_client_ip(),
                      _hash_token(callback_token), now, now)
        if existing:
            c.execute('''UPDATE auto_install_runs
                            SET status = 'installing', hostname = ?, product = ?, version = ?,
                                system_info = ?, message = '', client_ip = ?,
                                callback_token_hash = ?, started_at = ?, updated_at = ?
                          WHERE id = ?''', run_values + (existing['id'],))
        else:
            c.execute('''INSERT INTO auto_install_runs
                           (hostname, product, version, system_info, client_ip,
                            callback_token_hash, started_at, updated_at,
                            id, profile_id, status, fingerprint, message)
                         VALUES (?,?,?,?,?,?,?,?,?,?,'installing',?,'')''',
                      run_values + (str(uuid.uuid4()), profile['id'], fingerprint))
            c.execute('''DELETE FROM auto_install_runs WHERE profile_id = ? AND id NOT IN
                           (SELECT id FROM auto_install_runs WHERE profile_id = ?
                             ORDER BY started_at DESC LIMIT ?)''',
                      (profile['id'], profile['id'], _RUNS_KEPT_PER_PROFILE))
        db.conn.commit()

        log_audit('installer', 'autoinstall.answer_served',
                  f"profile '{profile.get('name')}' -> {hardware or 'unknown hardware'}",
                  ip_address=get_client_ip())
        return Response(body, mimetype='text/plain')
    except Exception as e:
        return jsonify({'error': safe_error(e, 'Could not serve the answer file')}), 500


def _webhook_status(payload):
    """installing / installed / failed for a callback body.

    The installer's own webhook fires only after a successful install and has no
    status field at all: it describes the new system ($schema, fqdn, machine-id,
    ssh-public-host-keys, ...). An authenticated body of that shape is 'installed'.
    Explicit fields are for anything else that posts here, a first-boot script or
    curl, and a body we cannot read either way leaves the run where it was.
    """
    if not isinstance(payload, dict):
        return 'installing', ''
    explicit = payload.get('status') or payload.get('state') or ''
    explicit = explicit.strip().lower() if isinstance(explicit, str) else ''
    message = payload.get('message') or payload.get('error') or ''
    message = message[:2000] if isinstance(message, str) else ''
    if explicit in _STATUSES:
        return explicit, message
    if explicit in ('ok', 'success', 'succeeded', 'done', 'finished', 'complete', 'completed'):
        return 'installed', message
    if explicit in ('error', 'failure', 'aborted'):
        return 'failed', message
    if isinstance(payload.get('success'), bool):
        return ('installed' if payload['success'] else 'failed'), message
    if payload.get('error'):
        return 'failed', message
    if any(k in payload for k in ('$schema', 'machine-id', 'ssh-public-host-keys')):
        return 'installed', ''
    return 'installing', ''


@bp.route('/api/auto-install/progress', methods=['POST'])
def installer_progress():
    """[post-installation-webhook] target. Authenticated by the callback token of
    one run, which is only in the file served to that machine."""
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        payload = {}
    # ?token= is what we write into the webhook URL; a body "token" is what an
    # installer sends when the section carries auth-token
    body_token = payload.get('token') if isinstance(payload.get('token'), str) else ''
    token = _request_token() or body_token.strip()
    if not token:
        return _reject('no callback token', '')
    try:
        run = _run_for_callback_token(token)
        if not run:
            return _reject('unknown callback token', token)
        # a disabled or spent profile must still be able to close a run it started,
        # otherwise that run sits at 'installing' for good
        status, message = _webhook_status(payload)
        hostname, _hardware, version = _summarise_system(payload)
        try:
            info = json.loads(run.get('system_info') or '{}')
        except (ValueError, TypeError):
            info = {}
        if not isinstance(info, dict):
            info = {}
        report = {k: v for k, v in payload.items() if k != 'token'}
        if report:
            info['post_install'] = report
        db = get_db()
        c = db.conn.cursor()
        c.execute('''UPDATE auto_install_runs
                        SET status = ?, message = ?, hostname = ?, version = ?,
                            system_info = ?, updated_at = ?
                      WHERE id = ?''',
                  (status, message, hostname or run.get('hostname') or '',
                   version or run.get('version') or '', _bounded_json(info), _stamp(), run['id']))
        db.conn.commit()
        log_audit('installer', 'autoinstall.progress',
                  f"run {run['id'][:8]} -> {status}", ip_address=get_client_ip())
        return jsonify({'message': 'Recorded', 'status': status})
    except Exception as e:
        return jsonify({'error': safe_error(e, 'Could not record installation progress')}), 500
