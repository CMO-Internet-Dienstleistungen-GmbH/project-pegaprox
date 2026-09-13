# -*- coding: utf-8 -*-
"""The websocket relay that carries a Hyper-V VM console to a browser.

It sits between two things that must never meet. On one side is a browser holding a
single-use token and nothing else: no host name, no account, no VM GUID. On the other is
guacd, which is given all three and knows nothing about PegaProx's sessions. Everything
that decides whether this connection may exist happens here, in the middle, before a
single byte is forwarded.

Three checks, in this order, because each one makes the next meaningful:

1. The token is valid and single-use. It is spent by the first connection that presents it.
2. The account it names may open *this* VM's console — asked of the VM access-control
   layer, not of a role, so a per-VM grant works and a neighbouring VM does not.
3. The GUID is resolved from the durable identity map by VMID. It is never read from the
   request, so a caller cannot name a VM the access check was not asked about.

A console that is open when the account is disabled is closed on the next heartbeat rather
than living out its natural length.
"""

from __future__ import annotations

import asyncio
import logging
import re
import threading
from urllib.parse import urlparse, parse_qs

from pegaprox.core import hyperv_console

logger = logging.getLogger(__name__)

# /api/hyperv/<cluster_id>/vms/<vmid>/console
_PATH = re.compile(r'^/api/hyperv/([^/]+)/vms/(\d+)/console$')

# A Hyper-V VM is a full machine, so it is a 'qemu' guest to the access-control layer.
_GUEST_TYPE = 'qemu'
_CONSOLE_PERMISSION = 'vm.console'

# How often an open console re-asks whether the account behind it still exists and is still
# enabled. Short enough that a disabled account loses its console in a minute, long enough
# that it is not a user lookup per frame.
_REVOCATION_CHECK_SECONDS = 30

# The display the browser asks for, bounded. These are numbers from a query string and they
# end up in a connection parameter guacd allocates buffers from.
_MIN_DIMENSION = 320
_MAX_DIMENSION = 4096
_MIN_DPI = 32
_MAX_DPI = 480

_CLOSE_POLICY_VIOLATION = 1008


def _bounded(value, fallback, low, high):
    try:
        number = int(value)
    except (TypeError, ValueError):
        return fallback
    return max(low, min(high, number))


def authorize(path: str):
    """Decide whether this request may open a console, and what it would connect to.

    Returns (job, None) or (None, reason). A reason is a sentence for the operator; it
    never says which of the checks failed in a way that would let somebody map out what
    exists, but it does distinguish "your token is spent" from "you may not open this VM",
    because those need different actions from the person reading them.
    """
    from pegaprox.globals import cluster_managers
    from pegaprox.utils.auth import load_users, build_authz_user
    from pegaprox.utils.rbac import user_can_access_vm
    from pegaprox.utils.realtime import validate_ws_token

    parsed = urlparse(path)
    match = _PATH.match(parsed.path)
    if not match:
        return None, 'That is not a console address.'
    cluster_id, vmid = match.group(1), int(match.group(2))

    query = parse_qs(parsed.query)
    token = (query.get('token') or [''])[0]
    token_data = validate_ws_token(token) if token else None
    if not token_data:
        return None, 'This console link has expired or has already been used.'

    username = token_data.get('user') or ''
    users = load_users()
    account = users.get(username)
    if not account or account.get('enabled') is False:
        return None, 'This account can no longer open a console.'

    user = build_authz_user(username, {'user': username, 'role': token_data.get('role', '')})
    if not user_can_access_vm(user, cluster_id, vmid, _CONSOLE_PERMISSION, _GUEST_TYPE):
        return None, 'This account may not open this VM\'s console.'

    manager = cluster_managers.get(cluster_id)
    if manager is None or getattr(manager, 'cluster_type', '') != 'hyperv':
        return None, 'That host is not a Hyper-V source.'

    guid = manager.guid_for(vmid)
    if not guid:
        return None, 'That VM is not known on this host.'

    parameters = hyperv_console.connection_parameters(
        host=manager.config.host,
        vm_guid=guid,
        username=manager.config.user,
        password=manager.config.pass_,
        domain=getattr(manager.config, 'smb_domain', '') or '',
        width=_bounded((query.get('width') or [None])[0], 1024, _MIN_DIMENSION, _MAX_DIMENSION),
        height=_bounded((query.get('height') or [None])[0], 768, _MIN_DIMENSION, _MAX_DIMENSION),
        dpi=_bounded((query.get('dpi') or [None])[0], 96, _MIN_DPI, _MAX_DPI),
    )
    return {'cluster_id': cluster_id, 'vmid': vmid, 'username': username,
            'parameters': parameters}, None


def start_hyperv_console_server(port, ssl_cert=None, ssl_key=None, host=''):
    """Run the relay in a daemon thread, the way the other console servers are run.

    Returns True once it is listening, False if it could not start. A failure here costs
    the Hyper-V console and nothing else: every other part of PegaProx runs without it,
    which is the point of it being a separate server on a port of its own.
    """
    try:
        import websockets  # noqa: F401
    except ImportError:
        logger.warning('The websockets library is missing; the Hyper-V console is off.')
        return False

    ready = threading.Event()
    failure = {}

    def run():
        try:
            asyncio.run(_serve(port, host, ssl_cert, ssl_key, ready))
        except Exception as exc:  # noqa: BLE001 — reported through `failure`
            failure['error'] = exc
            logger.error('The Hyper-V console server stopped: %s', exc, exc_info=True)
            ready.set()

    threading.Thread(target=run, name='hyperv-console-ws', daemon=True).start()
    ready.wait(timeout=10)
    return not failure


async def _serve(port, host, ssl_cert, ssl_key, ready):
    import websockets

    context = None
    if ssl_cert and ssl_key:
        import ssl as ssl_module
        context = ssl_module.SSLContext(ssl_module.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(ssl_cert, ssl_key)

    async with websockets.serve(_handler, host or None, port, ssl=context,
                                subprotocols=['guacamole'], max_size=None):
        logger.info('Hyper-V console relay listening on port %s', port)
        ready.set()
        await asyncio.Future()


async def _handler(websocket):
    path = websocket.request.path if hasattr(websocket, 'request') else websocket.path
    loop = asyncio.get_running_loop()

    job, reason = await loop.run_in_executor(None, authorize, path)
    if job is None:
        await _refuse(websocket, reason)
        return

    try:
        sock, reader, ready = await loop.run_in_executor(
            None, hyperv_console.connect, job['parameters'])
    except hyperv_console.ConsoleError as exc:
        await _refuse(websocket, exc.message, exc.status, exc.detail)
        return

    connection_id = ready[1] if len(ready) > 1 else ''
    logger.info('Hyper-V console %s open for %s on %s/%s', connection_id[:12],
                job['username'], job['cluster_id'], job['vmid'])
    # The handshake's answer belongs to the browser too: its client library reads the
    # connection id out of this instruction and stays in "connecting" without it.
    await websocket.send(hyperv_console.encode(*ready).decode())
    sock.setblocking(False)
    try:
        await asyncio.gather(
            _guacd_to_browser(loop, sock, reader, websocket),
            _browser_to_guacd(loop, sock, websocket),
            _close_when_revoked(job, websocket),
        )
    except (asyncio.CancelledError, ConnectionError):
        pass
    except Exception:
        logger.debug('Hyper-V console relay ended', exc_info=True)
    finally:
        try:
            sock.close()
        except OSError:
            pass
        logger.info('Hyper-V console closed for %s on %s/%s', job['username'],
                    job['cluster_id'], job['vmid'])


async def _refuse(websocket, reason, status='', detail=''):
    """Tell the browser why, in its own protocol, then close.

    The refusal is sent as a Guacamole `error` instruction rather than only as a close
    code, because the client library renders that and shows nothing for a bare close. A
    console that fails silently is the one thing worse than one that fails.
    """
    try:
        message = reason if not detail else f'{reason} ({detail})'
        await websocket.send(hyperv_console.encode('error', message, status or '519').decode())
    except Exception:
        logger.debug('Could not deliver the console refusal', exc_info=True)
    try:
        await websocket.close(_CLOSE_POLICY_VIOLATION, 'console refused')
    except Exception:
        pass


async def _guacd_to_browser(loop, sock, reader, websocket):
    while True:
        chunk = await loop.sock_recv(sock, 65536)
        if not chunk:
            await websocket.close(1000, 'guacd closed the console')
            return
        for instruction in reader.feed(chunk):
            await websocket.send(instruction + ';')


async def _browser_to_guacd(loop, sock, websocket):
    async for message in websocket:
        if isinstance(message, bytes):
            message = message.decode('utf-8', errors='replace')
        await loop.sock_sendall(sock, message.encode('utf-8'))


async def _close_when_revoked(job, websocket):
    """End an open console when the account behind it is disabled or deleted.

    The token check happens once, at the start. Without this, a console opened a minute
    before an account was disabled would keep working for as long as somebody left the tab
    open, which is the gap between "access revoked" and "access ended".
    """
    from pegaprox.utils.auth import load_users

    while True:
        await asyncio.sleep(_REVOCATION_CHECK_SECONDS)
        account = load_users().get(job['username'])
        if not account or account.get('enabled') is False:
            logger.info('Closing the Hyper-V console of %s: the account is gone or disabled',
                        job['username'])
            await websocket.close(_CLOSE_POLICY_VIOLATION, 'account disabled')
            return
