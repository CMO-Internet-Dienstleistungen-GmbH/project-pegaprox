# -*- coding: utf-8 -*-
"""
Automated installations — MK Sep 2026.

PegaProx as the answer-file server for Proxmox VE's automated installer.

An operator writes the answer file here, PegaProx hands out a fetch token, and
the ISO is prepared with

    proxmox-auto-install-assistant prepare-iso pve.iso \
        --fetch-from http --url 'https://pegaprox.example.com/api/auto-install/answer?token=...'

The installer POSTs its hardware inventory to that URL at boot and gets the
answer file back. We record the POST as a run, so "which machines are currently
installing" is answerable from the UI instead of from a KVM console.

Why a token and not DMI matching (which is how the upstream assistant tends to be
demoed): with matching, an unauthenticated POST decides which configuration it is
handed, and the answer file carries the root password of the machine being built.
With a token, the operator decides — and a leaked token is revocable, a matching
rule is not.

The webhook leg is best-effort by design: the installer reports back to
/api/auto-install/progress if the answer file carries a [post-installation-webhook]
section, which we inject on the way out. If a given installer build does not send
one, the run simply stays in 'installing' until somebody clears it — the fetch leg
is what the feature stands on.
"""
import os
import re
import json
import uuid
import hmac
import hashlib
import secrets
import logging
from datetime import datetime, timedelta

from flask import Blueprint, jsonify, request, Response

from pegaprox.constants import SSL_CERT_FILE, SSL_CERT_FILE_LEGACY
from pegaprox.globals import cluster_managers
from pegaprox.core.db import get_db
from pegaprox.utils.auth import require_auth
from pegaprox.utils.audit import log_audit, get_client_ip
from pegaprox.api.helpers import safe_error

bp = Blueprint('auto_install', __name__)

# The installer only ever sends us its own Host header, so a hostile value can
# at worst poison the answer file that same machine is about to consume. That
# said, the value lands inside a TOML string, and "can only hurt itself" is not
# a reason to concatenate unchecked input into a config format. Hostname, IPv4,
# or bracketed IPv6, optional port — nothing else gets through.
_HOST_RE = re.compile(r'^(?:[A-Za-z0-9](?:[A-Za-z0-9.\-]{0,251}[A-Za-z0-9])?|\[[0-9A-Fa-f:.]{2,45}\])(?::\d{1,5})?$')

# A run's system_info is whatever the installer felt like sending. Cap it before
# it reaches the DB; the interesting part (dmi, nics, disks) is a few KB.
_MAX_SYSTEM_INFO = 64 * 1024
_MAX_ANSWER = 256 * 1024

_STATUSES = ('installing', 'installed', 'failed')

# Keys in the answer file that must never come back out over the API. The install
# root password is in here, and a read-only role having 'autoinstall.view' should
# not be a way to read it.
_SECRET_KEYS = ('root-password', 'root-password-hashed')


def _now():
    return datetime.now().isoformat()


def _hash_token(token):
    return hashlib.sha256(token.encode('utf-8')).hexdigest()


def _new_token():
    # 32 bytes url-safe; the whole thing ends up in a shell command line on the
    # machine that prepares the ISO, so no characters that need quoting.
    return 'pgxai_' + secrets.token_urlsafe(32)


def _present_token_hint(token):
    return token[:12]


# ──────────────────────────────────────────────────────────────────────────
# answer file handling
# ──────────────────────────────────────────────────────────────────────────

def _parse_toml(text):
    """Return (data, error). tomllib is stdlib from 3.11; PegaProx already needs
    newer than that for the gevent/py3.13 handling, so no vendored fallback."""
    try:
        import tomllib
    except ImportError:      # pragma: no cover - 3.10 and older
        return None, 'TOML parsing needs Python 3.11 or newer'
    try:
        return tomllib.loads(text), None
    except Exception as e:
        return None, str(e)


def validate_answer(text):
    """Check an answer file the way the installer will, but four hours earlier.

    Returns (errors, warnings). Errors are things the installer refuses outright;
    warnings are things that work but are usually a mistake.
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
    for key in ('keyboard', 'country', 'fqdn', 'mailto', 'timezone'):
        if not g.get(key):
            errors.append(f'[global] is missing "{key}"')
    if not g.get('root-password') and not g.get('root-password-hashed'):
        errors.append('[global] needs either "root-password" or "root-password-hashed"')
    if g.get('root-password') and g.get('root-password-hashed'):
        errors.append('[global] has both "root-password" and "root-password-hashed" - pick one')
    fqdn = g.get('fqdn') or ''
    if fqdn and '.' not in fqdn:
        errors.append('[global] "fqdn" must be fully qualified (host.domain.tld)')

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
        if isinstance(fs, str) and fs.startswith(('zfs', 'btrfs')) and not disk.get('zfs.raid') and not disk.get('btrfs.raid'):
            # not fatal, the installer defaults, but people expect a mirror and get a stripe
            warnings.append(f'[disk-setup] {fs} without an explicit raid level installs a stripe')

    if 'post-installation-webhook' in data:
        warnings.append('The [post-installation-webhook] section is replaced by PegaProx so it can track the install')

    if isinstance(g.get('root-password'), str) and g.get('root-password'):
        warnings.append('This answer file stores the root password in clear text. '
                        '"root-password-hashed" is the better habit.')
    return errors, warnings


_WEBHOOK_HEADER = re.compile(r'^\s*\[\s*post-installation-webhook\s*\]\s*$')
_ANY_TABLE = re.compile(r'^\s*\[')


def _strip_webhook_section(text):
    """Drop an existing [post-installation-webhook] table so ours doesn't collide
    (a duplicate table is a TOML error, not a last-one-wins)."""
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


def own_cert_fingerprint():
    """SHA-256 fingerprint of the certificate we serve, colon-separated upper hex —
    the shape Proxmox prints everywhere. Returns '' when we can't read one."""
    for path in (SSL_CERT_FILE, SSL_CERT_FILE_LEGACY):
        try:
            if not path or not os.path.exists(path):
                continue
            from cryptography import x509
            from cryptography.hazmat.primitives import hashes
            with open(path, 'rb') as fh:
                cert = x509.load_pem_x509_certificate(fh.read())
            digest = cert.fingerprint(hashes.SHA256())
            return ':'.join(f'{b:02X}' for b in digest)
        except Exception as e:
            logging.debug(f"[autoinstall] could not fingerprint {path}: {e}")
    return ''


def _behind_proxy():
    return any(request.headers.get(h) for h in
               ('X-Forwarded-Proto', 'X-Forwarded-Host', 'X-Forwarded-For'))


def callback_target(profile):
    """Where the installer should report back to, and which certificate it should
    expect there. An explicit per-profile override always wins; otherwise we use
    the address the installer just reached us on, which is by construction an
    address it can reach.

    The fingerprint is only auto-filled when nothing indicates a reverse proxy in
    front of us — behind one, the certificate on the wire is the proxy's and ours
    would make the installer hang up. Blank means "no pinning", which is what the
    installer wants for a publicly trusted certificate anyway.
    """
    url = (profile.get('callback_url') or '').strip()
    fp = (profile.get('callback_fingerprint') or '').strip()
    if url:
        return url, fp

    host = request.host or ''
    if not _HOST_RE.match(host):
        logging.warning(f"[autoinstall] refusing to build a callback URL from host {host!r}")
        return '', ''
    scheme = request.scheme
    fwd_proto = request.headers.get('X-Forwarded-Proto', '')
    if fwd_proto in ('http', 'https'):
        scheme = fwd_proto
    url = f'{scheme}://{host}/api/auto-install/progress'
    if not fp and scheme == 'https' and not _behind_proxy():
        fp = own_cert_fingerprint()
    return url, fp


def render_answer(profile, token):
    """The stored answer file plus the callback section that makes the run
    trackable. Returns (text, error)."""
    body = _strip_webhook_section(profile.get('answer') or '').rstrip()
    url, fp = callback_target(profile)
    if url:
        lines = [body, '', '[post-installation-webhook]',
                 f'url = "{_toml_escape(url)}?token={_toml_escape(token)}"']
        if fp:
            lines.append(f'cert-fingerprint = "{_toml_escape(fp)}"')
        body = '\n'.join(lines)
    body += '\n'

    # Re-check what we actually produced. Serving a half-valid answer file means a
    # machine that boots into an installer prompt in a datacenter at 3am, so a 500
    # here is strictly the kinder outcome.
    _, err = _parse_toml(body)
    if err:
        return None, err
    return body, None


def redact_answer(text):
    """Blank out password values, keeping the structure so the UI can still show
    the shape of the file to someone with only the view permission."""
    out = []
    for line in (text or '').splitlines():
        stripped = line.lstrip()
        key = stripped.split('=', 1)[0].strip() if '=' in stripped else ''
        if key in _SECRET_KEYS:
            indent = line[:len(line) - len(stripped)]
            out.append(f'{indent}{key} = "********"')
        else:
            out.append(line)
    return '\n'.join(out)


# ──────────────────────────────────────────────────────────────────────────
# storage
# ──────────────────────────────────────────────────────────────────────────

_PROFILE_COLS = ('id', 'name', 'description', 'answer_encrypted', 'target_cluster_id',
                 'callback_url', 'callback_fingerprint', 'token_hash', 'token_hint',
                 'enabled', 'max_uses', 'uses', 'expires_at', 'created_at', 'created_by',
                 'updated_at', 'updated_by')


def _row_to_profile(row, with_answer=False):
    keys = row.keys()
    p = {k: (row[k] if k in keys else None) for k in _PROFILE_COLS}
    answer = ''
    if p.pop('answer_encrypted', None):
        try:
            answer = get_db()._decrypt(row['answer_encrypted']) or ''
        except Exception as e:
            logging.error(f"[autoinstall] could not decrypt answer for {p.get('id')}: {e}")
    p['enabled'] = bool(p.get('enabled'))
    p['max_uses'] = int(p.get('max_uses') or 0)
    p['uses'] = int(p.get('uses') or 0)
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
    """Look the profile up by the token's hash — the plaintext never hits the DB.
    compare_digest on top of the indexed lookup, because an index lookup is a
    byte-wise comparison and this is the one place where that is measurable."""
    if not isinstance(token, str) or not token:
        return None
    th = _hash_token(token)
    c = get_db().conn.cursor()
    c.execute('SELECT * FROM auto_install_profiles WHERE token_hash = ?', (th,))
    row = c.fetchone()
    if not row:
        return None
    if not hmac.compare_digest(str(row['token_hash']), th):
        return None
    profile, _ = _row_to_profile(row, with_answer=True)
    return profile


def _request_token():
    auth = request.headers.get('Authorization', '')
    if auth.startswith('Bearer '):
        return auth[7:].strip()
    return (request.args.get('token') or '').strip()


def _profile_usable(profile):
    """(ok, reason). Kept separate from the lookup so the audit line can say which
    of the three ways a token stops working actually fired."""
    if not profile.get('enabled'):
        return False, 'disabled'
    expires = profile.get('expires_at') or ''
    if expires:
        try:
            if datetime.fromisoformat(expires) < datetime.now():
                return False, 'expired'
        except ValueError:
            logging.warning(f"[autoinstall] profile {profile.get('id')} has an unparsable expires_at {expires!r}")
    max_uses = int(profile.get('max_uses') or 0)
    if max_uses and int(profile.get('uses') or 0) >= max_uses:
        return False, 'use limit reached'
    return True, ''


# ──────────────────────────────────────────────────────────────────────────
# admin API
# ──────────────────────────────────────────────────────────────────────────

def _public_profile(profile, answer=None):
    out = dict(profile)
    out.pop('answer', None)
    cluster = cluster_managers.get(out.get('target_cluster_id') or '')
    out['target_cluster_name'] = getattr(getattr(cluster, 'config', None), 'name', '') if cluster else ''
    if answer is not None:
        out['answer'] = answer
    return out


@bp.route('/api/auto-install/profiles', methods=['GET'])
@require_auth(perms=['autoinstall.view'])
def list_profiles():
    """Metadata only. The answer file is a separate read because it carries the
    root password of every machine built from it.

    can_manage rides along so the UI can grey out the buttons it would only get a
    403 from — the frontend has the session's role, not its permission list."""
    try:
        c = get_db().conn.cursor()
        c.execute('SELECT * FROM auto_install_profiles ORDER BY name COLLATE NOCASE')
        rows = c.fetchall()
        profiles = []
        for row in rows:
            p, _ = _row_to_profile(row)
            c2 = get_db().conn.cursor()
            c2.execute('SELECT status, COUNT(*) AS n FROM auto_install_runs WHERE profile_id = ? GROUP BY status',
                       (p['id'],))
            p['run_counts'] = {r['status']: r['n'] for r in c2.fetchall()}
            profiles.append(_public_profile(p))
        from pegaprox.utils.rbac import has_permission
        from pegaprox.utils.auth import build_authz_user
        session = getattr(request, 'session', {}) or {}
        return jsonify({
            'profiles': profiles,
            'can_manage': has_permission(build_authz_user(session.get('user'), session),
                                         'autoinstall.manage'),
        })
    except Exception as e:
        return jsonify({'error': safe_error(e, 'Could not list installation profiles')}), 500


@bp.route('/api/auto-install/profiles/<profile_id>', methods=['GET'])
@require_auth(perms=['autoinstall.view'])
def get_profile(profile_id):
    """The answer file comes back redacted unless the caller may also edit it."""
    try:
        profile, answer = _load_profile(profile_id)
        if not profile:
            return jsonify({'error': 'Profile not found'}), 404
        from pegaprox.utils.rbac import has_permission
        from pegaprox.utils.auth import build_authz_user
        session = getattr(request, 'session', {}) or {}
        may_edit = has_permission(build_authz_user(session.get('user'), session),
                                  'autoinstall.manage')
        out = _public_profile(profile, answer if may_edit else redact_answer(answer))
        out['answer_redacted'] = not may_edit
        url, fp = callback_target(profile)
        out['callback_effective_url'] = url
        out['callback_effective_fingerprint'] = fp
        return jsonify(out)
    except Exception as e:
        return jsonify({'error': safe_error(e, 'Could not read the installation profile')}), 500


def _profile_payload(data, existing=None):
    """(fields, error). Shared by create and update so the two can't drift."""
    name = (data.get('name') or '').strip()
    if not name:
        return None, 'A name is required'
    if len(name) > 120:
        return None, 'The name is too long (max 120 characters)'

    answer = data.get('answer')
    if answer is None and existing is not None:
        answer = existing
    if not isinstance(answer, str) or not answer.strip():
        return None, 'The answer file is required'
    errors, _warnings = validate_answer(answer)
    if errors:
        return None, errors[0] if len(errors) == 1 else 'The answer file has %d problems' % len(errors)

    target = (data.get('target_cluster_id') or '').strip()
    # Deliberately not checked against cluster_managers: the cluster this node is
    # meant to join is frequently the one that does not exist yet.
    if len(target) > 64:
        return None, 'target_cluster_id is too long'

    callback_url = (data.get('callback_url') or '').strip()
    if callback_url and not callback_url.startswith(('http://', 'https://')):
        return None, 'callback_url must be an http:// or https:// URL'
    if len(callback_url) > 500:
        return None, 'callback_url is too long'
    fp = (data.get('callback_fingerprint') or '').strip().upper()
    if fp and not re.match(r'^[0-9A-F]{2}(:[0-9A-F]{2}){31}$', fp):
        return None, 'callback_fingerprint must be a SHA-256 fingerprint (32 colon-separated hex bytes)'

    try:
        max_uses = int(data.get('max_uses') or 0)
    except (TypeError, ValueError):
        return None, 'max_uses must be a number'
    if max_uses < 0 or max_uses > 10000:
        return None, 'max_uses must be between 0 (unlimited) and 10000'

    expires_at = (data.get('expires_at') or '').strip()
    if expires_at:
        try:
            datetime.fromisoformat(expires_at)
        except ValueError:
            return None, 'expires_at must be an ISO timestamp'

    return {
        'name': name,
        'description': (data.get('description') or '').strip()[:500],
        'answer': answer,
        'target_cluster_id': target,
        'callback_url': callback_url,
        'callback_fingerprint': fp,
        'max_uses': max_uses,
        'expires_at': expires_at,
        'enabled': 1 if data.get('enabled', True) else 0,
    }, None


@bp.route('/api/auto-install/profiles', methods=['POST'])
@require_auth(perms=['autoinstall.manage'])
def create_profile():
    """The token is returned exactly here and never again — it is stored hashed."""
    try:
        data = request.get_json(silent=True) or {}
        fields, err = _profile_payload(data)
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
                   fields['max_uses'], fields['expires_at'], _now(), user, _now(), user))
        db.conn.commit()
        log_audit(user, 'autoinstall.profile_created', f"Automated install profile '{fields['name']}'")

        profile, answer = _load_profile(pid)
        out = _public_profile(profile, answer)
        out['token'] = token
        url, fp = callback_target(profile)
        out['callback_effective_url'] = url
        out['callback_effective_fingerprint'] = fp
        return jsonify(out), 201
    except Exception as e:
        return jsonify({'error': safe_error(e, 'Could not create the installation profile')}), 500


@bp.route('/api/auto-install/profiles/<profile_id>', methods=['PUT'])
@require_auth(perms=['autoinstall.manage'])
def update_profile(profile_id):
    try:
        existing, current_answer = _load_profile(profile_id)
        if not existing:
            return jsonify({'error': 'Profile not found'}), 404
        data = request.get_json(silent=True) or {}
        fields, err = _profile_payload(data, existing=current_answer)
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
                   fields['enabled'], fields['max_uses'], fields['expires_at'], _now(), user, profile_id))
        db.conn.commit()
        log_audit(user, 'autoinstall.profile_updated', f"Automated install profile '{fields['name']}'")
        profile, answer = _load_profile(profile_id)
        return jsonify(_public_profile(profile, answer))
    except Exception as e:
        return jsonify({'error': safe_error(e, 'Could not update the installation profile')}), 500


@bp.route('/api/auto-install/profiles/<profile_id>/token', methods=['POST'])
@require_auth(perms=['autoinstall.manage'])
def rotate_token(profile_id):
    """Rotating invalidates every ISO already prepared from this profile. That is
    the point — it is the revoke button."""
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
                  (_hash_token(token), _present_token_hint(token), _now(), user, profile_id))
        db.conn.commit()
        log_audit(user, 'autoinstall.token_rotated', f"Automated install profile '{profile.get('name')}'")
        return jsonify({'token': token, 'token_hint': _present_token_hint(token)})
    except Exception as e:
        return jsonify({'error': safe_error(e, 'Could not rotate the token')}), 500


@bp.route('/api/auto-install/profiles/<profile_id>', methods=['DELETE'])
@require_auth(perms=['autoinstall.manage'])
def delete_profile(profile_id):
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
    data = request.get_json(silent=True) or {}
    errors, warnings = validate_answer(data.get('answer'))
    return jsonify({'valid': not errors, 'errors': errors, 'warnings': warnings})


@bp.route('/api/auto-install/runs', methods=['GET'])
@require_auth(perms=['autoinstall.view'])
def list_runs():
    try:
        c = get_db().conn.cursor()
        profile_id = (request.args.get('profile_id') or '').strip()
        if profile_id:
            c.execute('''SELECT r.*, p.name AS profile_name FROM auto_install_runs r
                           LEFT JOIN auto_install_profiles p ON p.id = r.profile_id
                          WHERE r.profile_id = ? ORDER BY r.started_at DESC LIMIT 500''', (profile_id,))
        else:
            c.execute('''SELECT r.*, p.name AS profile_name FROM auto_install_runs r
                           LEFT JOIN auto_install_profiles p ON p.id = r.profile_id
                          ORDER BY r.started_at DESC LIMIT 500''')
        runs = []
        for row in c.fetchall():
            run = {k: row[k] for k in row.keys()}
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


# ──────────────────────────────────────────────────────────────────────────
# installer-facing endpoints — no session, the token is the whole gate
# ──────────────────────────────────────────────────────────────────────────

def _machine_fingerprint(info):
    """A stable id for the machine being installed, so a retried fetch updates the
    same run instead of adding a row. Serial first, MAC second, and if the
    installer told us neither we give up and treat every fetch as a new machine —
    which is the safe direction: two rows are confusing, one row silently covering
    two machines is wrong."""
    if not isinstance(info, dict):
        return ''
    dmi = info.get('dmi') if isinstance(info.get('dmi'), dict) else {}
    system = dmi.get('system') if isinstance(dmi.get('system'), dict) else {}
    serial = str(system.get('serial') or dmi.get('serial') or '').strip()
    if serial and serial.lower() not in ('', 'none', 'unknown', 'to be filled by o.e.m.',
                                         'system serial number', 'default string', '0123456789'):
        return f'serial:{serial}'
    nics = info.get('network_interfaces') or info.get('nics') or []
    macs = sorted(str(n.get('mac') or '').lower() for n in nics
                  if isinstance(n, dict) and n.get('mac'))
    macs = [m for m in macs if m and m != '00:00:00:00:00:00']
    if macs:
        return 'mac:' + macs[0]
    return ''


def _summarise_system(info):
    """(hostname, product, version) for the run list, pulled defensively — this is
    whatever a stranger's installer decided to send."""
    if not isinstance(info, dict):
        return '', '', ''
    dmi = info.get('dmi') if isinstance(info.get('dmi'), dict) else {}
    system = dmi.get('system') if isinstance(dmi.get('system'), dict) else {}
    vendor = str(system.get('manufacturer') or '').strip()
    model = str(system.get('product') or system.get('name') or '').strip()
    product = ' '.join(x for x in (vendor, model) if x)[:120]
    return (str(info.get('hostname') or '').strip()[:253],
            product or str(info.get('product') or '').strip()[:120],
            str(info.get('version') or '').strip()[:60])


def _reject(reason, token, status=403):
    """One shape for every refusal so a prober cannot tell 'no such token' from
    'that token is spent'."""
    log_audit('installer', 'autoinstall.fetch_denied',
              f"{reason} (token {token[:12] + '…' if token else 'missing'})",
              ip_address=get_client_ip())
    return jsonify({'error': 'Invalid or expired installation token'}), status


@bp.route('/api/auto-install/answer', methods=['POST'])
def serve_answer():
    """The endpoint the prepared ISO talks to. Returns the answer file as plain
    text, which is what proxmox-auto-install-assistant expects on the wire."""
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

        info = request.get_json(silent=True)
        if not isinstance(info, dict):
            info = {}
        raw = json.dumps(info)[:_MAX_SYSTEM_INFO]
        hostname, product, version = _summarise_system(info)
        fingerprint = _machine_fingerprint(info)

        body, err = render_answer(profile, token)
        if err:
            logging.error(f"[autoinstall] profile {profile['id']} rendered invalid TOML: {err}")
            return jsonify({'error': 'The stored answer file could not be rendered'}), 500

        db = get_db()
        c = db.conn.cursor()
        existing = None
        if fingerprint:
            c.execute('''SELECT id FROM auto_install_runs
                          WHERE profile_id = ? AND fingerprint = ?
                          ORDER BY started_at DESC LIMIT 1''', (profile['id'], fingerprint))
            existing = c.fetchone()
        if existing:
            c.execute('''UPDATE auto_install_runs
                            SET status = 'installing', hostname = ?, product = ?, version = ?,
                                system_info = ?, message = '', client_ip = ?, started_at = ?, updated_at = ?
                          WHERE id = ?''',
                      (hostname, product, version, raw, get_client_ip(), _now(), _now(), existing['id']))
        else:
            c.execute('''INSERT INTO auto_install_runs
                           (id, profile_id, status, fingerprint, hostname, product, version,
                            system_info, message, client_ip, started_at, updated_at)
                         VALUES (?,?,'installing',?,?,?,?,?,'',?,?,?)''',
                      (str(uuid.uuid4()), profile['id'], fingerprint, hostname, product, version,
                       raw, get_client_ip(), _now(), _now()))
        c.execute('UPDATE auto_install_profiles SET uses = uses + 1 WHERE id = ?', (profile['id'],))
        db.conn.commit()

        log_audit('installer', 'autoinstall.answer_served',
                  f"profile '{profile.get('name')}' -> {product or 'unknown hardware'}",
                  ip_address=get_client_ip())
        return Response(body, mimetype='text/plain; charset=utf-8')
    except Exception as e:
        return jsonify({'error': safe_error(e, 'Could not serve the answer file')}), 500


def _webhook_status(payload):
    """Map whatever the installer sent onto installing/installed/failed.

    The post-installation webhook body is not something we can pin down across
    installer versions, so this reads the fields that have shown up rather than
    insisting on one schema. Anything unrecognised counts as still installing —
    an install wrongly shown as finished is the failure mode that costs somebody
    a night, the other direction only costs a stale row.
    """
    if not isinstance(payload, dict):
        return 'installing', ''
    explicit = str(payload.get('status') or payload.get('state') or '').strip().lower()
    if explicit in _STATUSES:
        return explicit, str(payload.get('message') or payload.get('error') or '')[:2000]
    if explicit in ('ok', 'success', 'succeeded', 'done', 'finished', 'complete', 'completed'):
        return 'installed', str(payload.get('message') or '')[:2000]
    if explicit in ('error', 'failure', 'failed', 'aborted'):
        return 'failed', str(payload.get('message') or payload.get('error') or '')[:2000]

    if isinstance(payload.get('success'), bool):
        return ('installed' if payload['success'] else 'failed'), str(payload.get('message') or '')[:2000]
    if payload.get('error'):
        return 'failed', str(payload['error'])[:2000]
    return 'installing', ''


@bp.route('/api/auto-install/progress', methods=['POST'])
def installer_progress():
    """[post-installation-webhook] target. Same token as the answer fetch."""
    token = _request_token()
    if not token:
        return _reject('no token presented', '')
    try:
        profile = _profile_for_token(token)
        if not profile:
            return _reject('unknown token', token)
        # A spent or disabled token must still be able to close out a run it
        # already started; refusing here would leave that install stuck at
        # 'installing' forever. Only an outright unknown token is turned away.

        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            payload = {}
        status, message = _webhook_status(payload)
        fingerprint = _machine_fingerprint(payload)

        db = get_db()
        c = db.conn.cursor()
        row = None
        if fingerprint:
            c.execute('''SELECT id FROM auto_install_runs WHERE profile_id = ? AND fingerprint = ?
                          ORDER BY started_at DESC LIMIT 1''', (profile['id'], fingerprint))
            row = c.fetchone()
        if not row:
            c.execute('''SELECT id FROM auto_install_runs WHERE profile_id = ? AND status = 'installing'
                          ORDER BY started_at DESC LIMIT 1''', (profile['id'],))
            row = c.fetchone()
        if not row:
            logging.info(f"[autoinstall] progress for profile {profile['id']} with no matching run")
            return jsonify({'message': 'No matching installation run'}), 404

        c.execute('UPDATE auto_install_runs SET status = ?, message = ?, updated_at = ? WHERE id = ?',
                  (status, message, _now(), row['id']))
        db.conn.commit()
        log_audit('installer', 'autoinstall.progress',
                  f"profile '{profile.get('name')}' -> {status}", ip_address=get_client_ip())
        return jsonify({'message': 'Recorded', 'status': status})
    except Exception as e:
        return jsonify({'error': safe_error(e, 'Could not record installation progress')}), 500
