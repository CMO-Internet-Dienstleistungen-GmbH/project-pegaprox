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
# A peer call's body is read whole before the caller is known, so it is capped: the
# notices are a few bytes, the snapshot is a GET.
_MAX_PEER_BODY = 64 * 1024


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
# login reaches them without ever minting a ws token.
STANDBY_CONSOLE_ERROR = 'Consoles are only available on the active instance.'


def standby_console_refusal():
    """The answer a console route gives on a standby, None anywhere else."""
    if not ha.is_standby():
        return None
    return jsonify({'code': 'HA_STANDBY', 'error': STANDBY_CONSOLE_ERROR}), 409


# --- admin -----------------------------------------------------------------------

@bp.route('/api/ha/status', methods=['GET'])
@require_auth(roles=[ROLE_ADMIN])
def ha_status():
    """Role, epoch, members and last sync of this instance.

    members lists every other instance of the group, is_source marks the one a
    standby pulls from; peer is that one (or the first member) for older readers.
    suggested_url and own_fingerprint are what a pairing code made here would carry,
    so the UI can prefill the form."""
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
    """How often the standby pulls and the active looks at its peer, and the live view.

    interval is in seconds. live_view is this instance's own switch: on, a standby
    connects to the clusters read-only; off, it holds no connection at all. Either one
    or both. A standby restarts when live_view changes, because it sets its connections
    up once per process; any other role only keeps the value for when it follows."""
    denied = _refuse_confined_admin()
    if denied:
        return denied
    data = _body()
    if 'interval' not in data and 'live_view' not in data:
        return jsonify({'error': 'Nothing to change - send interval, live_view or both'}), 400
    interval = data.get('interval')
    if 'interval' in data and (isinstance(interval, bool) or not isinstance(interval, int)
                               or not _MIN_INTERVAL <= interval <= _MAX_INTERVAL):
        return jsonify({'error': f'The interval is a whole number of seconds, '
                                 f'{_MIN_INTERVAL} to {_MAX_INTERVAL}'}), 400
    live = data.get('live_view')
    if 'live_view' in data and not isinstance(live, bool):
        return jsonify({'error': 'live_view is true or false'}), 400
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
                restarting = ha.apply_config_now() is not False
        out.update(live_view=live, restarting=restarting)
    return jsonify(out)


@bp.route('/api/ha/apply-config', methods=['POST'])
@require_auth(roles=[ROLE_ADMIN])
def apply_config():
    """Restart this standby now to take up a configuration change that is waiting.

    A sync that changes how the clusters are reached leaves a restart pending, which a
    standby takes by itself once the change holds still. This skips the wait. No
    password: it only decides when this standby restarts, not what it does."""
    denied = _refuse_confined_admin()
    if denied:
        return denied
    if not ha.is_standby():
        return jsonify({'error': 'Only a standby takes its configuration from the active instance'}), 409
    pending = (ha.public_status().get('sync') or {}).get('restart_pending') or {}
    try:
        restarting = ha.apply_config_now() is not False
    except Exception as e:
        return jsonify({'error': safe_error(e, 'Could not apply the configuration')}), 500
    if restarting:
        reason = pending.get('reason') if isinstance(pending, dict) else ''
        log_audit(_user(), 'ha.config_applied', 'restarting this standby now'
                  + (f' for the waiting change: {reason}' if reason else ''))
    return jsonify({'success': True, 'restarting': restarting})


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
        if plain and (request.content_length is None or request.content_length <= _MAX_PEER_BODY):
            request.max_content_length = _MAX_PEER_BODY
            body = request.get_data(cache=True)
            if len(body) <= _MAX_PEER_BODY:
                verdict = ha.peer_verdict(request.headers, request.method, request.path, body)
    except Exception as e:
        logging.warning(f"[HA] could not check a peer call to {request.path}: {e}")
        verdict = (None, None)
    env[_PEER_VERDICT] = verdict
    return verdict


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
    release takes calls from every member, not only from one peer."""
    _p, refused = _peer_or_refuse()
    if refused:
        return refused
    return jsonify({'instance_id': ha.instance_id(), 'role': ha.role(), 'epoch': ha.epoch(),
                    'group': ha.GROUP_MARK})


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
        etag = _off_hub(lambda: ha.snapshot_etag(meta))
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
