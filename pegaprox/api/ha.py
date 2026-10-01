# -*- coding: utf-8 -*-
"""Warm standby routes (#625) - the admin half and the peer half.

The admin routes are a settings page: session or admin API token, admin role, and
not for an admin capped to a tenant, since pairing hands the whole deployment and
its field key to another host. The ones that pair, join, promote, unpair or remove a
member also want proof that the caller is at the keyboard: the account password, or
a fresh sign-in for an account that has none. No API token for those.

The peer routes carry no session. Every other member of the group signs its call
with its Ed25519 key: X-PegaProx-Peer (its instance id), -Ts, -Nonce and -Sig over
method, path, body, time, nonce and our instance id, checked against the public key
we keep of that member (ha.peer_verdict). A member paired before the keys still
sends "<its instance id>:<its secret>" until it has published a key. A member the
active removed gets 410 HA_REMOVED instead of 401, so it knows to let go. The one
route that runs before there is a member, /api/ha/peer/pair, is authenticated by the
pairing code in its body. Peer calls send X-Requested-With and no Origin, which the
CSRF gate in app.py already accepts, so none of this is exempted there.

A write the block in app.py would refuse on a standby goes to the active instead
(forward_to_active): the browser's request, the signed-in user and the client address
travel as the body of a signed peer call, and the active runs the request as that user
by its own accounts (peer_forward). The consoles are not among them: a standby that
serves users opens them itself, any other refuses them, see app.py.

The state machine behind all of it is pegaprox/core/ha.py; nothing here decides a
role on its own.

MK Sep 2026
"""
import base64
import binascii
import contextvars
import hmac
import io
import ipaddress
import logging
import re
import sys
import threading
import time

from flask import Blueprint, current_app, jsonify, request, Response
from werkzeug.exceptions import RequestEntityTooLarge

from pegaprox.core import ha
from pegaprox.models.permissions import ROLE_ADMIN
from pegaprox.utils.auth import require_auth, build_authz_user
from pegaprox.utils.audit import log_audit, get_client_ip
from pegaprox.utils.ratelimit import SlidingWindow
from pegaprox.utils.sanitization import sanitize_log_message
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
# A peer call's body is read whole before the caller is known, so it is capped: the
# notices are a few bytes, the snapshot is a GET.
_MAX_PEER_BODY = 64 * 1024
# A forwarded write carries its body in base64, an upload of up to FORWARD_MAX_BODY
# included. Read that far only when the header names a member that signs its calls.
_MAX_FORWARD_ENVELOPE = 4 * ((ha.FORWARD_MAX_BODY + 2) // 3) + 64 * 1024

# What a standby forwards, and what of the active's answer reaches the browser besides
# the status and the body.
_FORWARD_METHODS = ('POST', 'PUT', 'PATCH', 'DELETE')
_FORWARD_HEADERS = ('Content-Type', 'Content-Disposition', 'Retry-After')
FORWARD_UNREACHABLE_ERROR = ('The active instance cannot be reached - act again once it is '
                             'back, or promote this standby')
FORWARD_NO_ANSWER_ERROR = ('The active instance took the change but did not answer in time - '
                           'it may still be carrying it out. Check there before you try again')
_CONTROL_RE = re.compile(r'[\x00-\x1f\x7f]')
# Every write a standby forwards reaches the active from the standby's address, so they
# all share the one per-address API budget there with the standby's own sync. One user
# gets half of it.
_forward_per_user = SlidingWindow(limit=600, window=60, max_keys=4096, name='ha-forward')
# A large body is held a few times over on both instances while it travels: only a few
# at once, the rest hear 503 and try again
_FORWARD_LARGE = 1024 * 1024
_forward_large_slots = threading.BoundedSemaphore(4)


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


# A standby with the live view on holds real connections to the clusters, and reads
# through them. A console is not a read: keyboard and mouse on a guest, a root shell
# on a node, a SPICE ticket that goes to the node directly. Every console path asks
# this before it looks for a manager - the WebSocket ones too, since the old ?session=
# login reaches them without ever minting a ws token. A standby that serves users
# (ha.serving) opens them itself, as an active instance does: the console is the
# user's, and nothing that acts on its own starts there.
STANDBY_CONSOLE_ERROR = 'Consoles are only available on the active instance.'
# a plugin console on a standby that serves users, for a plugin it does not run as the
# leader does (app.py)
PLUGIN_CONSOLE_ERROR = ('This plugin is not running on this instance - open its console '
                        'on the leader.')


def by_api_token():
    """Whether this request comes with an API token rather than a browser session."""
    return request.headers.get('Authorization', '').startswith('Bearer pgx_')


def standby_console_refusal():
    """The answer a console route gives where it opens none, None where it opens: on a
    standby that does not serve users, and on one that does for an API token, which
    gets the standby answer there like for any change (scripts use the leader)."""
    if ha.consoles_here() and not (ha.is_standby() and by_api_token()):
        return None
    return jsonify({'code': 'HA_STANDBY', 'error': STANDBY_CONSOLE_ERROR}), 409


# --- forwarded writes, the standby's half ------------------------------------------

def forward_to_active(read=False):
    """The answer for a write the block in app.py would refuse on this standby, when it
    goes to the active instead. None when it does not, and the block refuses it as
    before: not a write method, no browser session behind it (an API token is nobody
    the standby can vouch for), forwarding switched off, or the member it pulls from
    not seen active.

    read: a GET of ha.FORWARDED_READS, the progress of a job the active runs or a view
    only its own tables hold, or a GET of a plugin (ha.PLUGIN_PROXY_RULE). The same way
    there, with a short timeout. When it does not come back whole: None for the progress
    of a job, and the route answers from this instance; None for a plugin as well, which
    app.py refuses; 503 HA_ACTIVE_UNREACHABLE for a view of ha.LEADER_ONLY_READS, whose
    rows here are not the active's.

    The active runs the request as the signed-in user, checked against its own
    accounts, and its status, body and content headers come back as they are. 413
    above ha.FORWARD_MAX_BODY, read no further than that; 503 HA_ACTIVE_UNREACHABLE
    when the active cannot be reached, 504 HA_FORWARD_NO_ANSWER when it took the call
    and did not answer (the change may have happened there); 429 past one user's
    share, 503 HA_FORWARD_BUSY while other large bodies are on their way. A write that
    went through (2xx) starts a pull right away. An account whose password changed on
    the active since our last sync gets 401 and a sync, which ends the session here."""
    if request.environ.get(ha.FORWARD_ENVIRON) is not None:
        # a forwarded call that met an instance which stepped down meanwhile ends here
        return None
    if request.method not in (('GET',) if read else _FORWARD_METHODS):
        return None
    if request.headers.get('Authorization', '').startswith('Bearer pgx_'):
        return None
    from pegaprox.utils.auth import validate_session
    session = validate_session(request.headers.get('X-Session-ID') or request.cookies.get('session_id'))
    if not session:
        return None
    if not ha.forwarding():
        return None
    if not _forward_per_user.allow(session['user']):
        resp = jsonify({'error': 'Too many changes through this standby at once - slow down, or '
                                 'make them on the active instance'})
        resp.headers['Retry-After'] = '60'
        return resp, 429

    if read:
        return _forward_read(session)
    large = request.content_length is None or request.content_length > _FORWARD_LARGE
    if large and not _forward_large_slots.acquire(blocking=False):
        resp = jsonify({'code': 'HA_FORWARD_BUSY',
                        'error': 'This standby is handing other large changes to the active '
                                 'instance - try again in a moment'})
        resp.headers['Retry-After'] = '10'
        return resp, 503
    try:
        return _forward(session)
    finally:
        if large:
            _forward_large_slots.release()


def _forward(session):
    too_large = (jsonify({'code': 'HA_FORWARD_TOO_LARGE',
                          'error': f'Too large to hand to the active instance - at most '
                                   f'{ha.FORWARD_MAX_BODY // (1024 * 1024)} MB. Make this '
                                   f'change on the active instance'}), 413)
    try:
        # werkzeug refuses a Content-Length above the cap before reading a byte, and cuts
        # a body without one at the cap without a word: one byte more tells a cut body
        # from a whole one. Not cached: no route runs here after this
        request.max_content_length = min(request.max_content_length or ha.FORWARD_MAX_BODY + 1,
                                         ha.FORWARD_MAX_BODY + 1)
        body = request.get_data(cache=False)
    except RequestEntityTooLarge:
        return too_large
    if len(body) > ha.FORWARD_MAX_BODY:
        return too_large

    envelope = _envelope_for(session, body)
    del body
    # the path is the browser's: no line breaks of its into our log
    what = sanitize_log_message(f'{request.method} {request.path}')
    try:
        resp = ha.forward_write(envelope)
    except ha.PeerNoAnswer as e:
        logging.warning(f"[HA] forwarded {what}, no answer from the active: {ha._error_text(e)}")
        return jsonify({'code': 'HA_FORWARD_NO_ANSWER', 'error': FORWARD_NO_ANSWER_ERROR}), 504
    except ha.HaError as e:
        logging.warning(f"[HA] could not forward {what}: {ha._error_text(e)}")
        return jsonify({'code': 'HA_ACTIVE_UNREACHABLE', 'error': FORWARD_UNREACHABLE_ERROR}), 503

    if resp.status_code == 200:
        answer = _forwarded_answer(resp)
        if answer is not None:
            status, headers, content = answer
            if 200 <= status < 300:
                ha.pull_soon()
            return Response(content, status=status, headers=headers)
        why = 'The active instance sent an answer this version does not read'
    elif resp.status_code == 403 and _answer_code(resp) == 'HA_FORWARD_STALE_SIGN_IN':
        # the password changed there: the sync ends this session, the browser signs in
        ha.pull_soon()
        return jsonify({'code': 'HA_FORWARD_STALE_SIGN_IN',
                        'error': 'Your password was changed on the active instance - sign in '
                                 'again'}), 401
    elif resp.status_code in (409, 410):
        # not active any more, or it took us out of the group: we refuse as a standby
        logging.warning(f"[HA] the active instance refused a forwarded {what} "
                        f"(HTTP {resp.status_code})")
        return None
    elif resp.status_code == 403 and _answer_code(resp) == 'HA_FORWARD_USER':
        # the account is not there, or disabled, on the active: its words
        return jsonify(resp.json()), 403
    elif resp.status_code == 413:
        # a proxy in front of the active, or its own size cap for requests
        return jsonify({'code': 'HA_FORWARD_TOO_LARGE',
                        'error': 'Too large for the active instance to take - make this '
                                 'change on the active instance'}), 413
    elif resp.status_code in (404, 405):
        why = ('The active instance runs a release that does not take changes from a '
               'standby - update it, or make the change there')
    else:
        why = ha._peer_error(resp, 'The active instance refused the change')
    logging.warning(f"[HA] forwarding {what} failed: {why}")
    return jsonify({'code': 'HA_FORWARD_REFUSED', 'error': why}), 502


def _envelope_for(session, body):
    return {
        'method': request.method,
        'path': request.path,
        'query': request.query_string.decode('latin-1'),
        'content_type': request.headers.get('Content-Type', ''),
        'body_b64': base64.b64encode(body).decode('ascii'),
        'user': session['user'],
        'sign_in': ha.sign_in_digest(session['user']),
        'client_ip': _plain_client_ip(),
    }


LEADER_READ_ERROR = ('The active instance did not answer - this list is kept there. Try '
                     'again in a moment')


def _forward_read(session):
    """The active's answer to a read of ha.FORWARDED_READS. Without one, None for our own
    copy, or the 503 a view of ha.LEADER_ONLY_READS answers instead: its rows here are
    this instance's own, and an ack picked from them would name another row there."""
    try:
        resp = ha.forward_write(_envelope_for(session, b''), timeout=ha.FORWARD_READ_TIMEOUT)
    except ha.HaError as e:
        logging.debug(f"[HA] could not fetch {sanitize_log_message(request.path)} from the "
                      f"active: {ha._error_text(e)}")
        return _no_leader_read()
    answer = _forwarded_answer(resp) if resp.status_code == 200 else None
    if answer is None:
        if resp.status_code == 403 and _answer_code(resp) == 'HA_FORWARD_STALE_SIGN_IN':
            ha.pull_soon()
        return _no_leader_read()
    status, headers, content = answer
    return Response(content, status=status, headers=headers)


def _no_leader_read():
    rule = request.url_rule.rule if request.url_rule is not None else None
    if rule not in ha.LEADER_ONLY_READS:
        return None
    return jsonify({'code': 'HA_ACTIVE_UNREACHABLE', 'error': LEADER_READ_ERROR}), 503


def _plain_client_ip():
    """The client address for the envelope, as the active checks it: a trusted proxy
    may hand on '1.2.3.4:5678', '[v6]:port' or 'unknown'. Anything that is not an
    address after taking the port off is the address the call came from here."""
    raw = (get_client_ip() or '').strip()
    candidates = [raw]
    if raw.startswith('[') and ']' in raw:
        candidates.append(raw[1:raw.index(']')])
    elif raw.count(':') == 1:
        candidates.append(raw.partition(':')[0])
    for value in candidates + [request.remote_addr or '']:
        try:
            return str(ipaddress.ip_address(value))
        except ValueError:
            continue
    return '0.0.0.0'


def _answer_code(resp):
    try:
        data = resp.json()
    except Exception:
        return ''
    return (data.get('code') or '') if isinstance(data, dict) else ''


def _forwarded_answer(resp):
    """(status, headers, body) from the active's answer to a forwarded write, None when
    it is not one."""
    try:
        data = resp.json()
        status, headers = data['status'], data['headers']
        content = base64.b64decode(data['body_b64'], validate=True)
    except Exception:
        return None
    if isinstance(status, bool) or not isinstance(status, int) or not 100 <= status <= 599:
        return None
    if not isinstance(headers, dict):
        return None
    out = [(name, value) for name, value in headers.items()
           if name in _FORWARD_HEADERS and isinstance(value, str) and not _CONTROL_RE.search(value)]
    return status, out, content


# --- admin -----------------------------------------------------------------------

@bp.route('/api/ha/status', methods=['GET'])
@require_auth(roles=[ROLE_ADMIN])
def ha_status():
    """Role, epoch, members and last sync of this instance.

    members lists every other instance of the group, is_source marks the one a
    standby pulls from; peer is that one (or the first member) for older readers.
    suggested_url and own_fingerprint are what a pairing code made here would carry,
    so the UI can prefill the form. forward_writes is this instance's switch, forwarding
    whether a standby hands its writes to the active right now."""
    denied = _refuse_confined_admin()
    if denied:
        return denied
    return jsonify(_status_body())


@bp.route('/api/ha/pairing-code', methods=['POST'])
@require_auth(roles=[ROLE_ADMIN])
def create_pairing_code():
    """A one-time code for an instance that is to follow this one.

    url is this instance as the standby will reach it, user_password the caller's
    own password (see _refuse_without_reauth). A new code replaces an open one; it is
    good for 15 minutes and for one pairing. An active that has standbys already hands
    out codes too, up to three standbys."""
    denied = _refuse_confined_admin()
    if denied:
        return denied
    url = _https_url(_body().get('url'))
    if not url:
        return jsonify({'error': 'Enter the https:// address the standby will use to reach this instance'}), 400
    if ha.is_standby():
        return jsonify({'error': 'A standby cannot hand out pairing codes - promote it first'}), 409
    if ha.group_full():
        return jsonify({'error': ha.GROUP_FULL_ERROR}), 409
    waiting = ha.group_waiting()
    if waiting:
        return jsonify({'error': ha._group_waiting_error(waiting)}), 409
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
    if ha.role() != ha.ROLE_STANDALONE or ha.members():
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

    Wants user_password. When the instance it follows still answers, one sync from it
    comes first, so the new active starts from the configuration and the member list
    of now; if that sync fails the promotion is refused (409 HA_PROMOTE_SYNC), unless
    force is true. An instance that does not answer at all is the failover this is for.
    Every member hears about the new epoch before the restart, if it answers within a
    few seconds: the instance that was active steps down, the other standbys follow
    this one from their next look at the group. A member that does not answer does the
    same as soon as it sees this instance."""
    denied = _refuse_confined_admin()
    if denied:
        return denied
    data = _body()
    if data.get('confirm') != 'PROMOTE':
        return jsonify({'error': 'Type PROMOTE to confirm'}), 400
    if not ha.is_standby():
        return jsonify({'error': 'Only a standby can be promoted'}), 409
    denied = _refuse_without_reauth('promoting this standby')
    if denied:
        return denied
    force = data.get('force') is True
    if not force:
        try:
            ok, why = ha.pull_before_promote()
        except Exception as e:
            ok, why = False, safe_error(e, 'the sync failed')
        if not ok:
            return jsonify({'code': 'HA_PROMOTE_SYNC',
                            'error': f'The instance this standby follows answers, but the sync '
                                     f'before the promotion failed: {why}. Fix that and try '
                                     f'again, or promote with force to take over anyway'}), 409
    old_active = ha.source_id()
    try:
        new_epoch = ha.promote()
    except ha.HaError as e:
        return jsonify({'error': str(e)}), 409
    except Exception as e:
        return jsonify({'error': safe_error(e, 'Promotion failed')}), 500
    # a reachable old active steps down now, not after our restart and its next watch:
    # until then both would act on the same clusters
    told = ha.tell_members('POST', '/api/ha/peer/step-down', json_body={'epoch': new_epoch},
                           timeout=5)
    for mid, err in told.items():
        if err:
            logging.warning(f"[HA] could not tell member {mid} about the promotion: {err}")
    reached = old_active in told and told[old_active] is None
    others = [mid for mid in told if mid != old_active]
    detail = (f", {sum(1 for mid in others if told[mid] is None)} of {len(others)} other "
              f"member(s) told" if others else '')
    log_audit(_user(), 'ha.promoted', f"promoted to active with epoch {new_epoch}, "
                                      f"old active {'told to step down' if reached else 'not reached'}"
                                      f"{detail}{', without the sync first (force)' if force else ''}")
    ha.restart_process('promoted to active')
    return jsonify({'success': True, 'epoch': new_epoch, 'restarting': True})


@bp.route('/api/ha/unpair', methods=['POST'])
@require_auth(roles=[ROLE_ADMIN])
def unpair_peer():
    """Leave the group. Every member is told first if it answers. Wants user_password.

    A standby leaves on its own: the active drops it, and with the next sync so does
    everybody else. It becomes standalone and restarts, because from then on it acts on
    the configuration it holds. An active leaves the group entirely: every member drops
    it and it becomes standalone; the standbys keep each other and wait for one of them
    to be promoted."""
    denied = _refuse_confined_admin()
    if denied:
        return denied
    if _body().get('confirm') != 'UNPAIR':
        return jsonify({'error': 'Type UNPAIR to confirm'}), 400
    p = ha.peer()
    group = ha.members()
    # a standby or an active without a member (the other side unpaired first, then this
    # one was promoted) must still get out; only a standalone has nothing to undo
    if not group and ha.role() == ha.ROLE_STANDALONE:
        return jsonify({'error': 'This instance is not paired'}), 409
    denied = _refuse_without_reauth('unpairing')
    if denied:
        return denied

    told = ha.tell_members('POST', '/api/ha/peer/unpaired', timeout=10) if group else {}
    for mid, err in told.items():
        if err:
            logging.warning(f"[HA] could not tell member {mid} about the unpairing: {err}")
    try:
        was = ha.unpair()
    except Exception as e:
        return jsonify({'error': safe_error(e, 'Unpairing failed')}), 500
    restarting = was == ha.ROLE_STANDBY
    peer_label = (p or {}).get('url') or (p or {}).get('instance_id') or 'no peer'
    if len(group) > 1:
        peer_label += f' and {len(group) - 1} more'
    reached = sum(1 for err in told.values() if err is None)
    if len(group) > 1:
        told_label = f'{reached} of {len(group)} members told'
    else:
        told_label = 'peer told' if reached else 'peer not told'
    log_audit(_user(), 'ha.unpaired', f"unpaired from {peer_label} (was {was}, {told_label})")
    if restarting:
        ha.restart_process('unpaired, standalone from now on')
    return jsonify({'success': True, 'restarting': restarting})


@bp.route('/api/ha/members/<instance_id>/remove', methods=['POST'])
@require_auth(roles=[ROLE_ADMIN])
def remove_member(instance_id):
    """Take a standby out of the group, on the active. Wants user_password.

    Only a member that answered as a standby under the current epoch: anything else
    may be an old active that is merely down, and would act again once it is back.
    shut_down: true is the admin confirming that the instance is shut down for good;
    without it such a member gets 409 HA_REMOVE_UNCONFIRMED. The removed instance
    stays on record as removed, and every remaining member hears so at once: from
    then on its calls get 410 everywhere, and it lets go of the group when it hears
    that. It is told right away if it answers (told), and stays a passive standby
    until an admin unpairs it there. Removing the last standby makes this instance
    standalone."""
    denied = _refuse_confined_admin()
    if denied:
        return denied
    data = _body()
    if data.get('confirm') != 'REMOVE':
        return jsonify({'error': 'Type REMOVE to confirm'}), 400
    if ha.role() != ha.ROLE_ACTIVE:
        return jsonify({'error': 'Only the active instance removes members'}), 409
    if not ha.member(instance_id):
        return jsonify({'error': 'That instance is not a member of this group'}), 404
    shut_down = data.get('shut_down') is True
    if not shut_down and not ha.member_confirmed(instance_id):
        # the last tick may predate our epoch: ask the member itself before sending the
        # admin to the shut-down confirmation
        ha.refresh_member(instance_id)
    if not shut_down and not ha.member_confirmed(instance_id):
        return jsonify({'code': 'HA_REMOVE_UNCONFIRMED', 'error': ha.REMOVE_UNCONFIRMED_ERROR}), 409
    denied = _refuse_without_reauth('removing a member')
    if denied:
        return denied
    try:
        signer = ha._signer()
        rec = ha.remove_member(instance_id, shut_down=shut_down)
    except ha.RemoveUnconfirmed as e:
        return jsonify({'code': 'HA_REMOVE_UNCONFIRMED', 'error': str(e)}), 409
    except ha.HaError as e:
        return jsonify({'error': str(e)}), 409
    except Exception as e:
        return jsonify({'error': safe_error(e, 'Removing the member failed')}), 500
    epoch = ha.epoch()
    # the others first: they drop it and keep its tombstone, so whatever it sends them
    # from now on is answered 410
    others = ha.tell_members('POST', '/api/ha/peer/member-removed',
                             json_body={'instance_id': instance_id, 'epoch': epoch},
                             timeout=10) if ha.members() else {}
    told = False
    try:
        resp = ha.call_member(rec, 'POST', '/api/ha/peer/unpaired',
                              json_body={'removed': True, 'epoch': epoch}, timeout=10, signer=signer)
        # an answer alone is not enough: it has to have let go of the group
        told = resp.status_code == 200 and (resp.json() or {}).get('left_group') is True
    except Exception as e:
        logging.warning(f"[HA] could not tell {rec.get('url') or instance_id} it was removed: {e}")
    reached = sum(1 for err in others.values() if err is None)
    log_audit(_user(), 'ha.member_removed',
              f"removed {rec.get('url') or instance_id} from the group "
              f"({'told' if told else 'not told'}"
              f"{', confirmed as shut down for good' if shut_down else ''}, "
              f"{reached} of {len(others)} other member(s) told, this instance is {ha.role()} now)")
    return jsonify({'success': True, 'told': told, 'members': ha.public_status()['members']})


@bp.route('/api/ha/settings', methods=['PUT'])
@require_auth(roles=[ROLE_ADMIN])
def update_settings():
    """How often the standby pulls and the active looks at its peer, the live view, and
    whether this instance as a standby forwards writes.

    interval is in seconds. live_view is this instance's own switch: on, a standby
    connects to the clusters read-only; off, it holds no connection at all. A standby
    restarts when live_view changes, because it sets its connections up once per
    process; any other role only keeps the value for when it follows. forward_writes,
    also this instance's own: on, a standby hands the writes of its signed-in users to
    the active; off, it refuses them. It counts from the next write. serve_users, this
    instance's own as well: on, a standby serves users like an active instance, with
    their consoles opened here and every change forwarded, as long as the live view and
    forwarding are on too (serving in the answer says whether they are). No restart,
    it counts from the next request. Any of the four, or several."""
    denied = _refuse_confined_admin()
    if denied:
        return denied
    data = _body()
    if not any(k in data for k in ('interval', 'live_view', 'forward_writes', 'serve_users')):
        return jsonify({'error': 'Nothing to change - send interval, live_view, forward_writes, '
                                 'serve_users or several of them'}), 400
    interval = data.get('interval')
    if 'interval' in data and (isinstance(interval, bool) or not isinstance(interval, int)
                               or not _MIN_INTERVAL <= interval <= _MAX_INTERVAL):
        return jsonify({'error': f'The interval is a whole number of seconds, '
                                 f'{_MIN_INTERVAL} to {_MAX_INTERVAL}'}), 400
    live = data.get('live_view')
    if 'live_view' in data and not isinstance(live, bool):
        return jsonify({'error': 'live_view is true or false'}), 400
    forward = data.get('forward_writes')
    if 'forward_writes' in data and not isinstance(forward, bool):
        return jsonify({'error': 'forward_writes is true or false'}), 400
    serve = data.get('serve_users')
    if 'serve_users' in data and not isinstance(serve, bool):
        return jsonify({'error': 'serve_users is true or false'}), 400
    status = ha.public_status()
    if status['broken']:
        # saving now would write the placeholder state over the file that could not be
        # read, and with it the instance id, the epoch and the peer secrets
        return jsonify({'error': 'The HA state file cannot be read - repair or remove '
                                 'config/ha_state.json first'}), 409

    out = {'success': True}
    if 'interval' in data:
        before = status['interval']
        try:
            # instance-local, never part of a snapshot; ha.py has no setter of its own
            ha._update(interval=interval)
        except ha.HaError as e:
            return jsonify({'error': str(e)}), 409
        except Exception as e:
            return jsonify({'error': safe_error(e, 'Could not save the interval')}), 500
        log_audit(_user(), 'ha.settings_changed', f'sync interval {before}s -> {interval}s')
        out['interval'] = interval

    # before the live view, which may restart this standby
    if 'forward_writes' in data:
        try:
            changed = ha.set_forward_writes(forward)
        except ha.HaError as e:
            return jsonify({'error': str(e)}), 409
        except Exception as e:
            return jsonify({'error': safe_error(e, 'Could not save the forwarding switch')}), 500
        if changed:
            log_audit(_user(), 'ha.settings_changed',
                      f"forwarding writes to the active instance {'on' if forward else 'off'}")
        out['forward_writes'] = forward

    if 'serve_users' in data:
        try:
            changed = ha.set_serve_users(serve)
        except ha.HaError as e:
            return jsonify({'error': str(e)}), 409
        except Exception as e:
            return jsonify({'error': safe_error(e, 'Could not save the serving switch')}), 500
        if changed:
            log_audit(_user(), 'ha.settings_changed',
                      f"serving users as a standby {'on' if serve else 'off'}")
        out['serve_users'] = serve

    if 'live_view' in data:
        was = ha.live_view()
        try:
            ha.set_live_view(live)
        except ha.HaError as e:
            return jsonify({'error': str(e)}), 409
        except Exception as e:
            return jsonify({'error': safe_error(e, 'Could not save the live view')}), 500
        restarting = False
        if live != was:
            standby = ha.is_standby()
            log_audit(_user(), 'ha.live_view_changed',
                      f"live view {'on' if live else 'off'}"
                      f"{', restarting this standby' if standby else ', takes effect as a standby'}")
            if standby:
                restarting = ha.apply_config_now() == 'restart'
        out.update(live_view=live, restarting=restarting)
    if 'serve_users' in data:
        out['serving'] = ha.serving()
    return jsonify(out)


@bp.route('/api/ha/apply-config', methods=['POST'])
@require_auth(roles=[ROLE_ADMIN])
def apply_config():
    """Take up a configuration change that is waiting on this standby, now.

    A sync that changes how the clusters are reached leaves a reload of those managers
    waiting, which a standby does by itself once the change holds still; a switched
    live view waits for a restart. This does either at once: reloaded says the
    managers were rebuilt in place, restarting that the process restarts. No password:
    it only decides when, not what."""
    denied = _refuse_confined_admin()
    if denied:
        return denied
    if not ha.is_standby():
        return jsonify({'error': 'Only a standby takes its configuration from the active instance'}), 409
    sync = ha.public_status().get('sync') or {}
    pending = sync.get('restart_pending') or sync.get('reload_pending') or {}
    try:
        done = ha.apply_config_now()
    except Exception as e:
        return jsonify({'error': safe_error(e, 'Could not apply the configuration')}), 500
    restarting, reloaded = done == 'restart', done == 'reload'
    if restarting or reloaded:
        reason = pending.get('reason') if isinstance(pending, dict) else ''
        log_audit(_user(), 'ha.config_applied',
                  ('restarting this standby now' if restarting else 'reloaded the managers now')
                  + (f' for the waiting change: {reason}' if reason else ''))
    return jsonify({'success': True, 'restarting': restarting, 'reloaded': reloaded})


# --- peer ------------------------------------------------------------------------

# where request_peer keeps its verdict: on the request itself. flask.g belongs to the
# app context, and a request served while another one of the same app is still open
# (a test client call from inside a route) shares that one.
_PEER_VERDICT = 'pegaprox.ha_peer'


def request_peer():
    """Who sent this peer call, as ha.peer_verdict says: ('member', record),
    ('removed', tombstone), ('skewed', record) or (None, None). Worked out once per
    request, because a nonce counts once: the IP allow list asks first, the route
    after it.

    No peer call carries a query string. One that does, or whose path holds a '?'
    once decoded, is nobody's: the signature covers the path alone, so the two could
    not be told apart."""
    env = request.environ
    if _PEER_VERDICT in env:
        return env[_PEER_VERDICT]
    verdict = (None, None)
    try:
        plain = not request.query_string and '?' not in request.path
        limit = _peer_body_limit()
        if plain and (request.content_length is None or request.content_length <= limit):
            request.max_content_length = limit
            body = request.get_data(cache=True)
            if len(body) <= limit:
                verdict = ha.peer_verdict(request.headers, request.method, request.path, body)
    except Exception as e:
        logging.warning(f"[HA] could not check a peer call to {request.path}: {e}")
        verdict = (None, None)
    env[_PEER_VERDICT] = verdict
    return verdict


def _peer_body_limit():
    """How much of a peer call is read before its sender is known. A forwarded write
    carries an upload, and it comes in chunks (ha._peer_call), past the size check the
    app makes on a Content-Length; its cap is set here, on the active, and only for a
    call whose headers are signed by a member we hold a key of, over the digest of the
    body to come. Everything else is a few bytes."""
    if (request.method == 'POST' and request.path == ha.FORWARD_PATH and ha.is_active()
            and signed_member_call()):
        return _MAX_FORWARD_ENVELOPE
    return _MAX_PEER_BODY


def signed_member_call():
    """Whether this request's headers carry a good signature of a member, checked
    before its body is read (ha.signed_before_body), once per request. The rate limit
    in app.py and the read limit above go by it; the route still checks the whole
    call."""
    key = 'pegaprox.ha_signed_headers'
    if key not in request.environ:
        request.environ[key] = ha.signed_before_body(request.headers, request.method, request.path)
    return request.environ[key]


@bp.after_request
def _say_we_hold_the_key(resp):
    # the caller signed with a key we hold: it can stop sending its old secret
    kind, who = request.environ.get(_PEER_VERDICT) or (None, None)
    if kind == 'member' and who.get('keyed'):
        resp.headers[ha.PEER_KEYED_HEADER] = '1'
    return resp


def _peer_body():
    """The JSON object a peer call carries, {} for anything else. Read whatever the
    Content-Type says: the signature covers the body and not that header, so a
    changed header must not turn a notice into an empty one."""
    data = request.get_json(force=True, silent=True)
    return data if isinstance(data, dict) else {}


def _peer_or_refuse():
    """(peer, None) when the call is from a member, else (None, response): 410
    HA_REMOVED for a member the group took out, 401 HA_CLOCK for a member whose
    signature is good but whose time is not, 401 for anybody else."""
    kind, who = request_peer()
    if kind == 'member':
        return who, None
    if kind == 'removed':
        return None, (jsonify({'code': 'HA_REMOVED', 'epoch': int(who.get('epoch') or 0),
                               'error': 'This instance was removed from the group - unpair it'}), 410)
    if kind == 'skewed':
        # the signature is good, so this is the member itself: no failure to count
        return None, (jsonify({'code': 'HA_CLOCK',
                               'error': f'The clocks of the two instances are more than '
                                        f'{ha.SIGNATURE_WINDOW} seconds apart - set both by NTP'}), 401)
    ip = get_client_ip()
    if not _peer_failures.allow(ip):
        logging.debug(f"[HA] peer calls from {ip} over the failure budget")
        resp = jsonify({'error': 'Too many failed peer calls'})
        resp.headers['Retry-After'] = '300'
        return None, (resp, 429)
    logging.warning(f"[HA] refused a peer call from {ip} to {request.path}")
    # who refuses: an active that every member refuses steps aside, and another
    # instance at a member's address (one set up anew there) is not that member
    return None, (jsonify({'error': 'Not the paired instance', 'instance_id': ha.instance_id()}), 401)


@bp.route('/api/ha/peer/pair', methods=['POST'])
def peer_pair():
    """The standby's half of the pairing handshake.

    No session and no peer header: the pairing code in the body authenticates the
    call and is spent by it. The standby sends its Ed25519 public key (public_key).
    The field key, our own public key, the member list and the removed members go
    back sealed with a key derived from that code."""
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
                                _str(data.get('public_key'), 128))
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
    """Role and epoch, for the watch loop of every other member. group says this
    release takes calls from every member, not only from one peer; serving that this
    standby serves users, which the others show."""
    _p, refused = _peer_or_refuse()
    if refused:
        return refused
    return jsonify({'instance_id': ha.instance_id(), 'role': ha.role(), 'epoch': ha.epoch(),
                    'group': ha.GROUP_MARK, 'serving': ha.serving()})


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


def current_etag(meta=None):
    """The etag a poll of the snapshot gets now, without the body: for the poll, and
    for the note the active sends its members after a change (ha.nudge_members).
    `meta` read on the hub, from ha.snapshot_meta(); the worker never touches the state."""
    meta = meta or ha.snapshot_meta()
    return _off_hub(lambda: ha.snapshot_etag(meta))


@bp.route('/api/ha/peer/snapshot', methods=['GET'])
def peer_snapshot():
    """The shared configuration as gzip-compressed JSON, for the standby.

    304 when If-None-Match carries the current etag. 409 unless this instance is the
    active one: a standby must never serve a snapshot another standby could take. A
    standby names the active it follows in follow {instance_id, url, fingerprint,
    public_key, epoch}, for a member that missed it; that member checks it with the
    active itself before it follows."""
    _p, refused = _peer_or_refuse()
    if refused:
        return refused
    if ha.role() != ha.ROLE_ACTIVE:
        body = {'error': 'This instance is not active'}
        hint = ha.follow_hint()
        if hint:
            body['follow'] = hint
        return jsonify(body), 409
    started = time.monotonic()
    try:
        # read on the hub, member list included: the worker never touches the state
        meta = ha.snapshot_meta()
        # the etag alone first: most polls end in a 304 and never build the body
        etag = current_etag(meta)
        headers = {'ETag': f'"{etag}"', 'Cache-Control': 'no-store'}
        if etag in _if_none_match():
            return Response(status=304, headers=headers)
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
    """A member is active under a newer epoch, or under ours with the higher instance
    id. We become its standby and restart; anything else changes nothing."""
    p, refused = _peer_or_refuse()
    if refused:
        return refused
    new_epoch = _peer_body().get('epoch')
    if ha._epoch_value(new_epoch, low=1) is None:
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
    """A member left the group, and we drop it. An active whose last member left
    becomes standalone; a standby stays passive until an admin unpairs or promotes it
    here.

    removed: true is the instance the group follows saying it took this one out of
    the group: we then let go of every member and stay passive until an admin unpairs
    us, an active included. From any other member it is a plain leave. left_group
    says which of the two it was."""
    p, refused = _peer_or_refuse()
    if refused:
        return refused
    removed = _peer_body().get('removed') is True
    was = ha.role()
    result = ha.forget_peer(p['instance_id'], whole_group=removed)
    if result:
        what = ('removed this instance from the group' if result == 'group'
                else 'unpaired on its side')
        log_audit('system', 'ha.unpaired',
                  f"member {p.get('url') or p['instance_id']} {what}, "
                  f"this instance is {ha.role()} now", ip_address=get_client_ip())
    if was == ha.ROLE_ACTIVE and ha.is_standby():
        ha.restart_process('removed from the group')
    return jsonify({'success': True, 'forgotten': bool(result), 'left_group': result == 'group',
                    'role': ha.role()})


@bp.route('/api/ha/peer/member-removed', methods=['POST'])
def peer_member_removed():
    """The active took another member out of the group: instance_id, and the epoch it
    did so under. We drop that member now rather than with the next member list, and
    keep its tombstone, so its calls get 410 here too. Taken only from the instance
    the group follows."""
    p, refused = _peer_or_refuse()
    if refused:
        return refused
    data = _peer_body()
    gone = data.get('instance_id')
    dropped = ha.note_member_removed(p['instance_id'], gone, data.get('epoch'))
    if dropped:
        log_audit('system', 'ha.member_removed',
                  f"member {p.get('url') or p['instance_id']} removed member {gone} from the group",
                  ip_address=get_client_ip())
    return jsonify({'success': True, 'dropped': dropped})


@bp.route('/api/ha/peer/tombstones', methods=['POST'])
def peer_tombstones():
    """A member holds tombstones for members this active still lists.

    The removal happened while this instance could not hear about it, and it was
    promoted since. tombstones is the list as the member list carries it. Each one is
    taken only for a member that has not answered as a standby under our epoch, asked
    once more first, and whose credentials it names; taken lists the ones that were.
    Anywhere but on the active nothing changes."""
    p, refused = _peer_or_refuse()
    if refused:
        return refused
    taken = ha.take_tombstones(p['instance_id'], _peer_body().get('tombstones'))
    for mid in taken:
        log_audit('system', 'ha.member_removed',
                  f"member {p.get('url') or p['instance_id']} holds a tombstone for member {mid}, "
                  f"which is out of the group here too", ip_address=get_client_ip())
    return jsonify({'success': True, 'taken': taken})


@bp.route('/api/ha/peer/changed', methods=['POST'])
def peer_changed():
    """The active changed its configuration (ha.nudge_members): pull now instead of at
    the next poll. Taken from the member this standby pulls from, and the pull is one
    like any other, from that member; from any other member nothing happens. etag is
    the configuration the active holds now: a standby whose last sync was that one has
    nothing to pull. A note without it (an active of an earlier release) is a pull.
    pull says whether it was taken."""
    p, refused = _peer_or_refuse()
    if refused:
        return refused
    taken = (ha.is_standby() and p['instance_id'] == ha.source_id()
             and not ha.holds_etag(_peer_body().get('etag')))
    if taken:
        # runs in the background, and asks that come in meanwhile make one more pull
        ha.pull_soon()
    return jsonify({'success': True, 'pull': taken})


# --- forwarded writes, the active's half ---------------------------------------------

@bp.route('/api/ha/peer/forward', methods=['POST'])
def peer_forward():
    """A write a standby would have refused, handed to us to run as the user it names.

    The body is {method, path, query, content_type, body_b64, user, sign_in,
    client_ip}, the browser's request as the standby took it, and the peer signature
    covers it like the body of every peer call: neither the request nor the user can
    change on the way. Only on the active (409 anywhere else, which also stops a write
    that would travel on), only from a member that signs its calls, only a write under
    /api/ and never under /api/ha/ - or a GET of ha.FORWARDED_READS, the progress of a
    job or a view only our tables hold, or of a plugin route that opens no console -
    and only for an account that exists and is enabled here. sign_in is the
    standby's digest of the account's password (ha.sign_in_digest): 403
    HA_FORWARD_STALE_SIGN_IN when ours differs, the password changed here since.
    The request then goes through our routing and every check on the way, CSRF and
    the IP list included, under a session for that user that holds for this one
    request: role, tenant and permissions are ours, whatever the standby thinks of
    them. Its audit lines name the standby and carry the client address the standby
    saw. The answer is {status, headers, body_b64}."""
    p, refused = _peer_or_refuse()
    if refused:
        return refused
    if not p.get('keyed'):
        # a member that still goes by its old secret signs nothing, the body included
        return jsonify({'code': 'HA_FORWARD_UNSIGNED',
                        'error': 'Only a member that signs its calls can hand over a change'}), 401
    if ha.role() != ha.ROLE_ACTIVE:
        return jsonify({'code': 'HA_STANDBY', 'error': 'This instance is not active'}), 409
    call, bad = _forward_envelope(_peer_body())
    if bad:
        return jsonify({'code': 'HA_FORWARD_INVALID', 'error': bad}), 400
    try:
        from pegaprox.core.db import get_db
        user = get_db().get_user(call['user'])
    except Exception as e:
        logging.warning(f"[HA] could not read the account of {call['user']} for a forwarded write: {e}")
        user = None
    if not isinstance(user, dict) or not user.get('enabled', True):
        return jsonify({'code': 'HA_FORWARD_USER',
                        'error': 'This account does not exist on the active instance, or it is '
                                 'disabled there'}), 403
    if not hmac.compare_digest(call['sign_in'], ha.sign_in_digest(call['user'])):
        # its password changed here since the standby's last sync: the session it
        # vouches for is one that sync ends
        return jsonify({'code': 'HA_FORWARD_STALE_SIGN_IN',
                        'error': 'The password of this account changed on the active instance'}), 403
    via = p.get('url') or p['instance_id']
    status, headers, body = _run_forwarded(call, user, via)
    (logging.debug if call['method'] == 'GET' else logging.info)(
        f"[HA] {call['method']} {call['path']} for {call['user']} via standby {via} "
        f"(client {call['client_ip']}): {status}")
    return jsonify({'status': status, 'headers': headers,
                    'body_b64': base64.b64encode(body).decode('ascii')})


def _text(value, limit):
    return isinstance(value, str) and len(value) <= limit and not _CONTROL_RE.search(value)


def _forward_envelope(data):
    """(the call, None) from a forward body, or (None, what is wrong with it)."""
    method, path = data.get('method'), data.get('path')
    if method not in _FORWARD_METHODS and method != 'GET':
        return None, 'method is POST, PUT, PATCH or DELETE, or GET for a job\'s progress'
    # routing goes by the path as it stands (no dot segments resolved, a double slash
    # redirects), so the prefix is what decides
    if not _text(path, 4096) or not path.startswith('/api/') or path.startswith('/api/ha/'):
        return None, 'path is under /api/, and not under /api/ha/'
    if method == 'GET' and not _forwarded_read(path):
        return None, 'a read is the progress of a job, a view only the active holds or a plugin\'s'
    if method != 'GET' and _opens_a_console(method, path):
        # a standby never hands one on: the browser connects where the console opened
        return None, 'a console opens on the instance the browser is on'
    sign_in = data.get('sign_in')
    if not isinstance(sign_in, str) or not (sign_in == '' or re.fullmatch(r'[0-9a-f]{64}', sign_in)):
        return None, 'sign_in is the digest of the account\'s sign-in'
    query, content_type = data.get('query', ''), data.get('content_type', '')
    if not _text(query, 8192) or not _text(content_type, 1024):
        return None, 'query and content_type are short strings'
    user = data.get('user')
    if not _text(user, 255) or not user:
        return None, 'user names the account'
    try:
        client_ip = str(ipaddress.ip_address(data.get('client_ip')))
    except (TypeError, ValueError):
        return None, 'client_ip is an IP address'
    raw = data.get('body_b64', '')
    if not isinstance(raw, str) or len(raw) > _MAX_FORWARD_ENVELOPE:
        return None, 'body_b64 is the body in base64'
    try:
        body = base64.b64decode(raw, validate=True)
    except (binascii.Error, ValueError):
        return None, 'body_b64 is the body in base64'
    if len(body) > ha.FORWARD_MAX_BODY:
        return None, f'the body is larger than {ha.FORWARD_MAX_BODY} bytes'
    if method == 'GET' and body:
        return None, 'a read has no body'
    return {'method': method, 'path': path, 'query': query, 'content_type': content_type,
            'body': body, 'user': user, 'sign_in': sign_in, 'client_ip': client_ip}, None


def _opens_a_console(method, path):
    try:
        rule, args = current_app.url_map.bind('localhost').match(path, method=method, return_rule=True)
    except Exception:
        return False
    if rule.rule == ha.PLUGIN_PROXY_RULE:
        return args.get('subpath') in ha.PLUGIN_CONSOLE_PATHS
    return (method, rule.rule) in ha.CONSOLE_WRITES


def _forwarded_read(path):
    """Whether a standby may hand us a GET of `path`: one of ha.FORWARDED_READS, or a
    plugin route that opens no console. A standby opens a plugin console itself or not
    at all, and one opened here would be no use to the browser there."""
    try:
        rule, args = current_app.url_map.bind('localhost').match(path, method='GET', return_rule=True)
    except Exception:
        return False
    if rule.rule == ha.PLUGIN_PROXY_RULE:
        return args.get('subpath') not in ha.PLUGIN_CONSOLE_PATHS
    return rule.rule in ha.FORWARDED_READS


def _run_forwarded(call, user, via):
    """Run the call through this app as its user: (status, headers, body)."""
    from pegaprox.utils.auth import open_forwarded_session, end_forwarded_session
    outer = request.environ
    sid = open_forwarded_session(call['user'], user.get('role') or 'viewer', call['client_ip'], via)
    environ = {
        'REQUEST_METHOD': call['method'],
        'SCRIPT_NAME': '',
        # WSGI carries the path as the latin-1 text of its UTF-8 bytes
        'PATH_INFO': call['path'].encode('utf-8').decode('latin-1'),
        'QUERY_STRING': call['query'],
        'SERVER_NAME': outer.get('SERVER_NAME') or 'localhost',
        'SERVER_PORT': outer.get('SERVER_PORT') or '443',
        'SERVER_PROTOCOL': 'HTTP/1.1',
        'REMOTE_ADDR': call['client_ip'],
        'HTTP_HOST': request.host,
        # the marker of the UI's own calls, and no foreign Origin: the CSRF gate takes it
        # like any same-origin call. No Origin of ours either, the gate compares an IPv6
        # host with its brackets against one without
        'HTTP_X_REQUESTED_WITH': 'XMLHttpRequest',
        'HTTP_X_SESSION_ID': sid,
        'HTTP_USER_AGENT': f'PegaProx standby {via}'[:200],
        'CONTENT_LENGTH': str(len(call['body'])),
        'wsgi.version': (1, 0),
        'wsgi.url_scheme': outer.get('wsgi.url_scheme') or 'https',
        'wsgi.input': io.BytesIO(call['body']),
        'wsgi.errors': outer.get('wsgi.errors') or sys.stderr,
        'wsgi.multithread': bool(outer.get('wsgi.multithread')),
        'wsgi.multiprocess': False,
        'wsgi.run_once': False,
        ha.FORWARD_ENVIRON: {'session': sid, 'via': via, 'client_ip': call['client_ip']},
    }
    if call['content_type']:
        environ['CONTENT_TYPE'] = call['content_type']
    try:
        # a request of its own: flask.g and the contexts of this peer call stay out of it
        return contextvars.Context().run(_dispatch, current_app._get_current_object(), environ)
    finally:
        end_forwarded_session(sid)


def _dispatch(app, environ):
    started, chunks = {}, []

    def start_response(status, headers, exc_info=None):
        started['status'], started['headers'] = status, headers
        return chunks.append

    result = app(environ, start_response)
    size, too_large = 0, False
    try:
        for chunk in result:
            size += len(chunk)
            if size > ha.FORWARD_MAX_BODY:
                too_large = True
                break
            chunks.append(chunk)
    finally:
        close = getattr(result, 'close', None)
        if close:
            close()
    if too_large:
        # done here, but more than the standby takes back
        return 502, {'Content-Type': 'application/json'}, (
            b'{"error":"The change was made, but its answer is too large to pass on - '
            b'fetch it on the active instance"}')
    status = int(str(started.get('status') or '500').split(' ', 1)[0])
    wanted = {name.lower(): name for name in _FORWARD_HEADERS}
    headers = {wanted[k.lower()]: v for k, v in started.get('headers') or () if k.lower() in wanted}
    return status, headers, b''.join(chunks)
