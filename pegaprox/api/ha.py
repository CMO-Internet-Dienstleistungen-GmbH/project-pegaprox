# -*- coding: utf-8 -*-
"""Warm standby routes (#625) - the admin half and the peer half.

The admin routes are a settings page: session or admin API token, admin role, and
not for an admin capped to a tenant, since pairing hands the whole deployment and
its field key to another host. The four that pair, join, promote or unpair also want
proof that the caller is at the keyboard: the account password, or a fresh sign-in
for an account that has none. No API token for those.

The peer routes carry no session. The other instance sends X-PegaProx-Peer
("<its instance id>:<secret>"), checked against the hash we keep. The one route that
runs before there is a peer, /api/ha/peer/pair, is authenticated by the pairing code
in its body. Peer calls send X-Requested-With and no Origin, which the CSRF gate in
app.py already accepts, so none of this is exempted there.

The state machine behind all of it is pegaprox/core/ha.py; nothing here decides a
role on its own.

MK Sep 2026
"""
import logging
import time

from flask import Blueprint, jsonify, request, Response

from pegaprox.core import ha
from pegaprox.models.permissions import ROLE_ADMIN
from pegaprox.utils.auth import require_auth, build_authz_user
from pegaprox.utils.audit import log_audit, get_client_ip
from pegaprox.utils.ratelimit import SlidingWindow
from pegaprox.api.helpers import safe_error, effective_reverse_proxy

bp = Blueprint('ha', __name__)

_MIN_INTERVAL, _MAX_INTERVAL = 5, 3600

# A code is 256 bits and lives 15 minutes, so this is about noise, not guessing.
_pair_attempts = SlidingWindow(limit=5, window=300, max_keys=2048, name='ha-pair')
# Failed peer headers per address. Only failures are counted: the real peer never
# lands here, and someone sharing its NAT cannot lock it out by guessing.
_peer_failures = SlidingWindow(limit=10, window=300, max_keys=2048, name='ha-peer-auth')
# Password re-checks per account. Every attempt counts, not only the failed ones: a
# budget that only counted failures would still wave a right guess through.
_reauth_attempts = SlidingWindow(limit=5, window=300, max_keys=2048, name='ha-reauth')
# How old a session may be when an account without a password stands in for one.
_REAUTH_MAX_AGE = 600


def _body():
    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else {}


def _str(value, limit=512):
    return value.strip()[:limit] if isinstance(value, str) else ''


def _https_url(value):
    """The address as ha.valid_https_url takes it, '' when it does not. The same rule
    the other side applies to the code and to the pair call, so what passes here is
    what the peer will accept. Not cut to length first: a cut URL is another URL."""
    if not isinstance(value, str):
        return ''
    return (ha.valid_https_url(value.strip()) or '').rstrip('/')


def _user():
    return (getattr(request, 'session', None) or {}).get('user', 'system')


def _refuse_confined_admin():
    """403 unless the caller sees every cluster - the automated-installations rule,
    reused so the two cannot drift apart - and no tenant override lowers them where
    they live. Fails closed.

    The second half is not implied by the first: an admin mapped down to viewer in the
    default tenant still "sees every cluster", because an empty cluster list there
    means all of them."""
    from pegaprox.api.auto_install import _sees_every_cluster
    from pegaprox.utils.rbac import _admin_is_capped_in_own_tenant
    try:
        session = getattr(request, 'session', None) or {}
        user = build_authz_user(session.get('user', ''), session)
        unconfined = not _admin_is_capped_in_own_tenant(user) and _sees_every_cluster(user)
    except Exception as e:
        logging.warning(f"[HA] could not resolve the caller's cluster scope: {e}")
        unconfined = False
    if not unconfined:
        return jsonify({'error': 'Instance pairing is only available to administrators '
                                 'who are not limited to a tenant or to specific clusters'}), 403
    return None


def _refuse_without_reauth(what):
    """403 unless the caller proved just now who they are. None when they did.

    Pairing hands the field key and every stored credential to another host; promote
    and unpair decide which instance acts on the clusters. A stolen session must not
    be enough for that, so this is the bar the config backup sets: the account password
    in user_password, checked the way the backup checks it, audited and rate limited
    when wrong. An OIDC account has no password to type, so its session has to be
    younger than ten minutes instead. API tokens are refused, they cannot do either.
    `what` names the action in the audit trail."""
    session = getattr(request, 'session', None) or {}
    if session.get('api_token'):
        return jsonify({'error': 'This needs an interactive sign-in - an API token cannot '
                                 'confirm it with a password',
                        'code': 'HA_REAUTH'}), 403
    username = session.get('user', '')
    try:
        from pegaprox.core.db import get_db
        user = get_db().get_user(username)
    except Exception as e:
        logging.warning(f"[HA] could not read the account of {username} for the re-check: {e}")
        user = None
    if not isinstance(user, dict):
        return jsonify({'error': 'Confirm with your password', 'code': 'HA_REAUTH'}), 403

    from pegaprox.utils.oidc import OIDC_AUTH_SOURCES
    if user.get('auth_source') in OIDC_AUTH_SOURCES and not user.get('password_hash'):
        created = session.get('created_at')
        fresh = (isinstance(created, (int, float)) and not isinstance(created, bool)
                 and 0 <= time.time() - created <= _REAUTH_MAX_AGE)
        if not fresh:
            return jsonify({'error': 'Sign in again, then retry within 10 minutes',
                            'code': 'HA_REAUTH_RECENT'}), 403
        return None

    password = _body().get('user_password')
    if not isinstance(password, str) or not password or len(password) > 1024:
        return jsonify({'error': 'Confirm with your password', 'code': 'HA_REAUTH'}), 403
    if not _reauth_attempts.allow(username):
        resp = jsonify({'error': 'Too many password attempts - wait a few minutes',
                        'code': 'HA_REAUTH'})
        resp.headers['Retry-After'] = '300'
        return resp, 429
    from pegaprox.utils.auth import recheck_account_password
    ok, source = recheck_account_password(username, password, user,
                                          audit_action='ha.reauth_failed', context=what)
    if not ok:
        logging.warning(f"[HA] password re-check for {what} failed for {username} (auth_source={source})")
        return jsonify({'error': 'The password is not correct', 'code': 'HA_REAUTH'}), 403
    return None


def _own_fingerprint():
    """What a peer has to pin to reach us: our self-signed certificate, or nothing
    when a proxy terminates TLS or the certificate comes from a CA."""
    if effective_reverse_proxy():
        return ''
    from pegaprox.api.auto_install import self_signed_fingerprint
    return self_signed_fingerprint()


def _suggested_url():
    """https://host:port as the browser reached us, trusted-proxy aware."""
    from pegaprox.api.auto_install import _public_base
    try:
        scheme, host, _direct = _public_base()
    except Exception as e:
        logging.debug(f"[HA] no suggested address: {e}")
        return ''
    return f'{scheme}://{host}' if host else ''


def _status_body():
    out = ha.public_status()
    out['suggested_url'] = _suggested_url()
    out['own_fingerprint'] = _own_fingerprint()
    return out


# --- admin -----------------------------------------------------------------------

@bp.route('/api/ha/status', methods=['GET'])
@require_auth(roles=[ROLE_ADMIN])
def ha_status():
    """Role, epoch, peer and last sync of this instance.

    suggested_url and own_fingerprint are what a pairing code made here would carry,
    so the UI can prefill the form."""
    denied = _refuse_confined_admin()
    if denied:
        return denied
    return jsonify(_status_body())


@bp.route('/api/ha/pairing-code', methods=['POST'])
@require_auth(roles=[ROLE_ADMIN])
def create_pairing_code():
    """A one-time code for the instance that is to follow this one.

    url is this instance as the standby will reach it, user_password the caller's
    own password (see _refuse_without_reauth). A new code replaces an open one; it is
    good for 15 minutes and for one pairing."""
    denied = _refuse_confined_admin()
    if denied:
        return denied
    url = _https_url(_body().get('url'))
    if not url:
        return jsonify({'error': 'Enter the https:// address the standby will use to reach this instance'}), 400
    if ha.is_standby():
        return jsonify({'error': 'A standby cannot hand out pairing codes - promote it first'}), 409
    if ha.peer():
        return jsonify({'error': 'This instance is already paired - unpair it first'}), 409
    denied = _refuse_without_reauth('a pairing code')
    if denied:
        return denied
    try:
        code, expires = ha.create_pairing_code(url, _own_fingerprint())
    except ha.HaError as e:
        return jsonify({'error': str(e)}), 409
    except Exception as e:
        return jsonify({'error': safe_error(e, 'Could not create a pairing code')}), 500
    log_audit(_user(), 'ha.pairing_code_created', f'pairing code for {url}, valid for 15 minutes')
    return jsonify({'code': code, 'expires_at': expires})


@bp.route('/api/ha/join', methods=['POST'])
@require_auth(roles=[ROLE_ADMIN])
def join_active():
    """Become the standby of the instance that made the code, then restart.

    This instance takes over the active's field key and, with the first sync, its
    configuration, so it wants user_password like the pairing code does. 502 means the
    conversation with the active failed: unreachable, wrong certificate, or it refused
    the code."""
    denied = _refuse_confined_admin()
    if denied:
        return denied
    data = _body()
    if data.get('confirm') is not True:
        return jsonify({'error': 'Joining replaces the configuration of this instance - confirm it to go ahead'}), 400
    code = _str(data.get('code'), 4096)
    if not code:
        return jsonify({'error': 'Paste the pairing code from the active instance'}), 400
    own_url = _https_url(data.get('own_url'))
    if not own_url:
        return jsonify({'error': 'Enter the https:// address the active instance will use to reach this one'}), 400
    if ha.role() != ha.ROLE_STANDALONE or ha.peer():
        return jsonify({'error': 'Only a standalone, unpaired instance can become a standby'}), 409
    try:
        info = ha.decode_code(code)
    except ha.HaError as e:
        return jsonify({'error': str(e)}), 400
    if info['instance_id'] == ha.instance_id():
        return jsonify({'error': 'That code was made on this instance'}), 400
    denied = _refuse_without_reauth('joining an active instance')
    if denied:
        return denied

    try:
        p = ha.join(code, own_url, _own_fingerprint())
    except ha.HaError as e:
        logging.warning(f"[HA] joining {info['url']} failed: {e}")
        return jsonify({'error': str(e)}), 502
    except Exception as e:
        return jsonify({'error': safe_error(e, 'Joining the active instance failed')}), 500
    log_audit(_user(), 'ha.joined', f"standby of {p.get('url')} from now on (epoch {ha.epoch()})")
    ha.restart_process('joined as standby')
    return jsonify({'success': True, 'restarting': True})


@bp.route('/api/ha/sync-now', methods=['POST'])
@require_auth(roles=[ROLE_ADMIN])
def sync_now():
    """Pull from the active now instead of at the next interval."""
    denied = _refuse_confined_admin()
    if denied:
        return denied
    result = ha.pull_once()
    return jsonify({'result': result, 'status': _status_body()})


@bp.route('/api/ha/promote', methods=['POST'])
@require_auth(roles=[ROLE_ADMIN])
def promote_standby():
    """Make this standby the active instance under a new epoch, then restart.

    Wants user_password. The instance that was active is told to step down before the
    restart, if it answers within a few seconds; otherwise it steps down as soon as
    either side sees the other."""
    denied = _refuse_confined_admin()
    if denied:
        return denied
    if _body().get('confirm') != 'PROMOTE':
        return jsonify({'error': 'Type PROMOTE to confirm'}), 400
    if not ha.is_standby():
        return jsonify({'error': 'Only a standby can be promoted'}), 409
    denied = _refuse_without_reauth('promoting this standby')
    if denied:
        return denied
    try:
        new_epoch = ha.promote()
    except ha.HaError as e:
        return jsonify({'error': str(e)}), 409
    except Exception as e:
        return jsonify({'error': safe_error(e, 'Promotion failed')}), 500
    # a reachable old active steps down now, not after our restart and its next watch:
    # until then both would act on the same clusters
    told = False
    if ha.peer():
        try:
            told = ha.call_peer('POST', '/api/ha/peer/step-down', json_body={'epoch': new_epoch},
                                timeout=5).status_code == 200
        except Exception as e:
            logging.warning(f"[HA] could not tell the old active to step down: {e}")
    log_audit(_user(), 'ha.promoted', f"promoted to active with epoch {new_epoch}, "
                                      f"old active {'told to step down' if told else 'not reached'}")
    ha.restart_process('promoted to active')
    return jsonify({'success': True, 'epoch': new_epoch, 'restarting': True})


@bp.route('/api/ha/unpair', methods=['POST'])
@require_auth(roles=[ROLE_ADMIN])
def unpair_peer():
    """Forget the peer. The peer is told first if it answers. Wants user_password.

    A standby becomes standalone and restarts, because from then on it acts on the
    configuration it holds."""
    denied = _refuse_confined_admin()
    if denied:
        return denied
    if _body().get('confirm') != 'UNPAIR':
        return jsonify({'error': 'Type UNPAIR to confirm'}), 400
    p = ha.peer()
    # a standby or an active without a peer (the other side unpaired first, then this
    # one was promoted) must still get out; only a standalone has nothing to undo
    if not p and ha.role() == ha.ROLE_STANDALONE:
        return jsonify({'error': 'This instance is not paired'}), 409
    denied = _refuse_without_reauth('unpairing')
    if denied:
        return denied

    told = False
    if p:
        try:
            told = ha.call_peer('POST', '/api/ha/peer/unpaired', timeout=10).status_code == 200
        except Exception as e:
            logging.warning(f"[HA] could not tell the peer about the unpairing: {e}")
    try:
        was = ha.unpair()
    except Exception as e:
        return jsonify({'error': safe_error(e, 'Unpairing failed')}), 500
    restarting = was == ha.ROLE_STANDBY
    peer_label = (p or {}).get('url') or (p or {}).get('instance_id') or 'no peer'
    log_audit(_user(), 'ha.unpaired',
              f"unpaired from {peer_label} (was {was}, peer {'told' if told else 'not told'})")
    if restarting:
        ha.restart_process('unpaired, standalone from now on')
    return jsonify({'success': True, 'restarting': restarting})


@bp.route('/api/ha/settings', methods=['PUT'])
@require_auth(roles=[ROLE_ADMIN])
def update_settings():
    """How often the standby pulls and the active looks at its peer, in seconds."""
    denied = _refuse_confined_admin()
    if denied:
        return denied
    interval = _body().get('interval')
    if isinstance(interval, bool) or not isinstance(interval, int) \
            or not _MIN_INTERVAL <= interval <= _MAX_INTERVAL:
        return jsonify({'error': f'The interval is a whole number of seconds, '
                                 f'{_MIN_INTERVAL} to {_MAX_INTERVAL}'}), 400
    status = ha.public_status()
    if status['broken']:
        # saving now would write the placeholder state over the file that could not be
        # read, and with it the instance id, the epoch and the peer secrets
        return jsonify({'error': 'The HA state file cannot be read - repair or remove '
                                 'config/ha_state.json first'}), 409
    before = status['interval']
    try:
        # instance-local, never part of a snapshot; ha.py has no setter of its own
        ha._update(interval=interval)
    except ha.HaError as e:
        return jsonify({'error': str(e)}), 409
    except Exception as e:
        return jsonify({'error': safe_error(e, 'Could not save the interval')}), 500
    log_audit(_user(), 'ha.settings_changed', f'sync interval {before}s -> {interval}s')
    return jsonify({'success': True, 'interval': interval})


# --- peer ------------------------------------------------------------------------

def _peer_or_refuse():
    """(peer, None) when X-PegaProx-Peer is right, else (None, response)."""
    p = ha.verify_peer(request.headers.get(ha.PEER_HEADER, ''))
    if p:
        return p, None
    ip = get_client_ip()
    if not _peer_failures.allow(ip):
        logging.debug(f"[HA] peer calls from {ip} over the failure budget")
        resp = jsonify({'error': 'Too many failed peer calls'})
        resp.headers['Retry-After'] = '300'
        return None, (resp, 429)
    logging.warning(f"[HA] refused a peer call from {ip} to {request.path}")
    return None, (jsonify({'error': 'Not the paired instance'}), 401)


@bp.route('/api/ha/peer/pair', methods=['POST'])
def peer_pair():
    """The standby's half of the pairing handshake.

    No session and no peer header: the pairing code in the body authenticates the
    call and is spent by it. The field key goes back sealed with a key derived from
    that code."""
    ip = get_client_ip()
    if not _pair_attempts.allow(ip):
        resp = jsonify({'error': 'Too many pairing attempts - wait a few minutes'})
        resp.headers['Retry-After'] = '300'
        return resp, 429
    data = _body()
    # not cut here: core refuses an over-long address instead of pairing a shortened one
    standby_url = _str(data.get('url'), 4096)
    try:
        out = ha.accept_pairing(_str(data.get('code')), _str(data.get('instance_id'), 64),
                                standby_url, _str(data.get('fingerprint'), 128),
                                _str(data.get('secret')))
    except ha.HaError as e:
        logging.warning(f"[HA] pairing attempt from {ip} refused: {e}")
        return jsonify({'error': str(e)}), 403
    except Exception as e:
        return jsonify({'error': safe_error(e, 'Pairing failed')}), 500
    log_audit('system', 'ha.paired',
              f"standby {standby_url or data.get('instance_id')} paired, epoch {out['epoch']}",
              ip_address=ip)
    return jsonify(out)


@bp.route('/api/ha/peer/status', methods=['GET'])
def peer_status():
    """Role and epoch, for the peer's watch loop."""
    _p, refused = _peer_or_refuse()
    if refused:
        return refused
    return jsonify({'instance_id': ha.instance_id(), 'role': ha.role(), 'epoch': ha.epoch()})


def _if_none_match():
    """The tags in If-None-Match, unquoted. The standby sends the bare etag it read
    from the last body; a proxy in between may have quoted it."""
    raw = request.headers.get('If-None-Match', '')
    tags = set()
    for part in raw.split(','):
        part = part.strip()
        if part.startswith('W/'):
            part = part[2:]
        tags.add(part.strip('"'))
    tags.discard('')
    return tags


_SNAPSHOT_SEM = None


def _build_and_pack(meta):
    # runs in the threadpool: no state lock and no logging in here, both are gevent
    # locks a native thread cannot hand back to a waiting greenlet
    stuck = []
    snap = ha.build_snapshot(meta, stuck=stuck)
    return snap, ha.snapshot_bytes(snap), stuck


def _off_hub(fn):
    """Hashing and packing every shared table is CPU work. In gevent's threadpool the
    hub keeps serving the UI and the consoles meanwhile; one snapshot at a time."""
    global _SNAPSHOT_SEM
    try:
        from gevent import get_hub
        from gevent.lock import BoundedSemaphore
    except Exception:
        return fn()
    if _SNAPSHOT_SEM is None:
        _SNAPSHOT_SEM = BoundedSemaphore(1)
    with _SNAPSHOT_SEM:
        return get_hub().threadpool.apply(fn)


@bp.route('/api/ha/peer/snapshot', methods=['GET'])
def peer_snapshot():
    """The shared configuration as gzip-compressed JSON, for the standby.

    304 when If-None-Match carries the current etag. 409 unless this instance is the
    active one: a standby must never serve a snapshot another standby could take."""
    _p, refused = _peer_or_refuse()
    if refused:
        return refused
    if ha.role() != ha.ROLE_ACTIVE:
        return jsonify({'error': 'This instance is not active'}), 409
    started = time.monotonic()
    try:
        # the etag alone first: most polls end in a 304 and never build the body
        etag = _off_hub(ha.snapshot_etag)
        headers = {'ETag': f'"{etag}"', 'Cache-Control': 'no-store'}
        if etag in _if_none_match():
            return Response(status=304, headers=headers)
        meta = ha.snapshot_meta()
        snap, body, stuck = _off_hub(lambda: _build_and_pack(meta))
        ha.warn_stuck(stuck)
    except Exception as e:
        return jsonify({'error': safe_error(e, 'Could not build the snapshot')}), 500
    headers['ETag'] = f'"{snap["etag"]}"'
    logging.debug(f"[HA] snapshot {snap['etag']}: {len(body)} bytes in {time.monotonic() - started:.2f}s")
    # Content-Encoding set here keeps flask-compress from compressing it again
    headers['Content-Encoding'] = 'gzip'
    return Response(body, status=200, mimetype='application/json', headers=headers)


@bp.route('/api/ha/peer/step-down', methods=['POST'])
def peer_step_down():
    """The peer is active under a newer epoch. We become its standby and restart;
    an older or equal epoch changes nothing."""
    p, refused = _peer_or_refuse()
    if refused:
        return refused
    new_epoch = _body().get('epoch')
    if isinstance(new_epoch, bool) or not isinstance(new_epoch, int) or not 0 < new_epoch < 2 ** 31:
        return jsonify({'error': 'epoch must be a positive whole number'}), 400
    changed = ha.step_down(new_epoch, p['instance_id'])
    if changed:
        log_audit('system', 'ha.stepped_down',
                  f"peer {p.get('url') or p['instance_id']} is active with epoch {new_epoch}",
                  ip_address=get_client_ip())
        ha.restart_process('stepped down to standby')
    return jsonify({'stepped_down': changed, 'role': ha.role(), 'epoch': ha.epoch()})


@bp.route('/api/ha/peer/unpaired', methods=['POST'])
def peer_unpaired():
    """The peer unpaired on its side. An active instance becomes standalone; a
    standby stays passive until an admin unpairs or promotes it here."""
    p, refused = _peer_or_refuse()
    if refused:
        return refused
    forgotten = ha.forget_peer(p['instance_id'])
    if forgotten:
        log_audit('system', 'ha.unpaired',
                  f"peer {p.get('url') or p['instance_id']} unpaired on its side, "
                  f"this instance is {ha.role()} now", ip_address=get_client_ip())
    return jsonify({'success': True, 'forgotten': forgotten, 'role': ha.role()})
