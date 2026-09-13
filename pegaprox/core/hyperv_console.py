# -*- coding: utf-8 -*-
"""The VMConnect console of a Hyper-V VM, reached through guacd.

Hyper-V's own console is not VNC and not SPICE. It is served by the Virtual Machine
Connection endpoint on the host — RDP on port 2179, with two differences from an ordinary
RDP session: the security mode is `vmconnect`, and the machine being connected to is named
by its GUID in the pre-connection blob rather than by an address. That is what makes it a
*machine* console: it shows the firmware, the boot loader and a guest with no network, none
of which an RDP session into the guest could ever show.

Nothing in Python speaks RDP, so the connection is made by guacd, the Guacamole proxy
daemon, which this module talks to over its text protocol. PegaProx stays in the middle of
the two: the browser never learns the host, the account or the GUID, and guacd never learns
anything about PegaProx's session.

The protocol itself is small enough to implement rather than to depend on: an instruction
is `LENGTH.VALUE,LENGTH.VALUE;` with lengths in Unicode code points, and a connection is
four instructions of handshake. What is *not* small is RDP, which is why guacd exists.

This module is pure protocol and one socket. The websocket relay that carries it to a
browser is in `hyperv_console_ws.py`, so that the part worth testing has no server in it.
"""

from __future__ import annotations

import logging
import os
import socket

logger = logging.getLogger(__name__)

# Where guacd listens. An operator points PegaProx at their own, so both are configurable
# and neither is baked in. The default is the daemon's own default port on this host.
DEFAULT_GUACD_HOST = '127.0.0.1'
DEFAULT_GUACD_PORT = 4822
GUACD_HOST_ENV = 'PEGAPROX_GUACD_HOST'
GUACD_PORT_ENV = 'PEGAPROX_GUACD_PORT'

# The VMConnect endpoint on a Hyper-V host. Not the guest's RDP port, and not configurable
# on the host side: 2179 is what the Virtual Machine Connection service listens on.
VMCONNECT_PORT = 2179

# RDP, in the mode that reaches the machine rather than the guest's desktop.
PROTOCOL = 'rdp'
SECURITY_MODE = 'vmconnect'

# guacd announces its protocol version as the first "argument" of the args instruction.
# It is a version token, not a parameter, and answering it with the version we were told
# is how a client says which dialect it will speak.
_VERSION_PREFIX = 'VERSION_'

_HANDSHAKE_TIMEOUT_SECONDS = 20

# What guacd's status codes mean for somebody looking at a console that did not open. The
# wording is ours: guacd's own messages are written for an RDP session into a desktop and
# say "server" where an operator here is thinking "Hyper-V host".
_STATUS_MEANING = {
    '256': 'guacd does not support this connection. Its RDP support is missing from the '
           'build in use.',
    '512': 'guacd failed internally while opening the console.',
    '513': 'guacd is too busy to open another console right now.',
    '514': 'The Hyper-V host did not answer in time.',
    '515': 'The Hyper-V host refused the console connection. If the host is reachable, the '
           'usual causes are that the account may not use Virtual Machine Connection, or '
           'that the VM is not on this host.',
    '516': 'The VM was not found on the Hyper-V host. Its GUID is known to PegaProx but not '
           'to the host, which happens after a VM is removed or moved.',
    # Measured against guacd 1.6.0: this one code covers "nothing is listening",
    # "refused the connection" and "the TLS handshake failed", and only guacd's own text
    # tells them apart. So the wording says what is common to all three and the detail
    # carries that text instead of this message pretending to more precision than it has.
    '519': f'The Hyper-V host did not accept a console connection on port {VMCONNECT_PORT}. '
           f'Either the Virtual Machine Connection service is not reachable from the '
           f'PegaProx host, or it refused this one. The detail below is what it said.',
    '520': 'The Hyper-V host is reachable but is not accepting console connections.',
    '769': 'The Hyper-V host rejected the account used for the console.',
    '771': 'The account may reach the host but may not open this VM\'s console.',
    '776': 'The console was idle too long and guacd closed it.',
}
_UNKNOWN_STATUS = 'The console could not be opened.'


class ConsoleError(Exception):
    """A console that did not open, with the reason in the words of this product."""

    def __init__(self, message, status='', detail=''):
        super().__init__(message)
        self.message = message
        self.status = status
        self.detail = detail

    def to_dict(self):
        return {'error': self.message, 'status': self.status, 'detail': self.detail}


# ---------------------------------------------------------------------------
# The protocol
# ---------------------------------------------------------------------------

def encode(*elements) -> bytes:
    """One Guacamole instruction.

    The length is in Unicode code points, not bytes — a VM named with an umlaut is one
    character to this protocol and two bytes on the wire, and counting the wrong one
    desynchronises the stream for everything that follows.
    """
    parts = []
    for element in elements:
        text = '' if element is None else str(element)
        parts.append(f'{len(text)}.{text}')
    return (','.join(parts) + ';').encode('utf-8')


def decode(instruction: str) -> list:
    """The elements of one instruction, without its terminating semicolon."""
    elements = []
    index = 0
    while index < len(instruction):
        dot = instruction.find('.', index)
        if dot < 0:
            raise ConsoleError('guacd sent an instruction this client cannot read.',
                               detail=instruction[:120])
        try:
            length = int(instruction[index:dot])
        except ValueError:
            raise ConsoleError('guacd sent an instruction this client cannot read.',
                               detail=instruction[:120])
        value = instruction[dot + 1:dot + 1 + length]
        elements.append(value)
        index = dot + 1 + length + 1
    return elements


class InstructionReader:
    """Splits a byte stream into instructions, keeping whatever is left over.

    A read returns whatever has arrived, which is regularly half an instruction and
    occasionally three of them. Treating each read as a message is the bug this exists to
    prevent: it drops the second instruction in a packet and truncates the first in the
    next one.
    """

    def __init__(self):
        self._buffer = ''

    def feed(self, chunk: bytes) -> list:
        """Add bytes, return every complete instruction they finished."""
        self._buffer += chunk.decode('utf-8', errors='replace')
        instructions = []
        while True:
            end = self._buffer.find(';')
            if end < 0:
                break
            instructions.append(self._buffer[:end])
            self._buffer = self._buffer[end + 1:]
        return instructions

    @property
    def pending(self) -> str:
        return self._buffer


def status_meaning(status: str) -> str:
    """What a guacd status code means here, in words an operator can act on."""
    return _STATUS_MEANING.get(str(status), _UNKNOWN_STATUS)


def connection_parameters(*, host, vm_guid, username, password, domain='',
                          width=1024, height=768, dpi=96, read_only=False) -> dict:
    """The RDP parameters that make guacd open a VM's console rather than a desktop.

    `security=vmconnect` and the GUID in the pre-connection blob are the whole difference.
    Certificate verification is off because the endpoint presents a self-signed certificate
    that is regenerated with the host and is not published anywhere a client could check it
    against; the connection is made from the PegaProx host to a host the operator
    configured, and it carries no guest credentials.
    """
    return {
        'hostname': host,
        'port': str(VMCONNECT_PORT),
        'username': username,
        'password': password,
        'domain': domain or '',
        'security': SECURITY_MODE,
        'ignore-cert': 'true',
        'preconnection-blob': str(vm_guid),
        'width': str(int(width)),
        'height': str(int(height)),
        'dpi': str(int(dpi)),
        'read-only': 'true' if read_only else '',
        # A machine console has no sound and no printer, and asking for them makes guacd
        # negotiate channels nothing here reads.
        'disable-audio': 'true',
        'enable-printing': '',
        'enable-drive': '',
    }


def build_connect(arg_names: list, parameters: dict) -> bytes:
    """The `connect` instruction: one value per name guacd asked for, in its order.

    Order is the entire contract — guacd matches values to names by position, so a missing
    value does not produce an error, it shifts every parameter after it by one. The version
    token is answered with itself, and a name this product has no opinion about is answered
    with an empty string, which is how the protocol spells "default".
    """
    values = []
    for name in arg_names:
        if name.startswith(_VERSION_PREFIX):
            values.append(name)
        else:
            values.append(parameters.get(name, ''))
    return encode('connect', *values)


def guacd_endpoint() -> tuple:
    """Where guacd is, from the environment, with the daemon's own defaults."""
    host = os.environ.get(GUACD_HOST_ENV) or DEFAULT_GUACD_HOST
    try:
        port = int(os.environ.get(GUACD_PORT_ENV) or DEFAULT_GUACD_PORT)
    except (TypeError, ValueError):
        logger.warning('%s is not a number; using %s', GUACD_PORT_ENV, DEFAULT_GUACD_PORT)
        port = DEFAULT_GUACD_PORT
    return host, port


def connect(parameters: dict, endpoint=None, timeout=_HANDSHAKE_TIMEOUT_SECONDS):
    """Open a console through guacd. Returns (socket, reader, ready).

    `ready` is the instruction guacd answered the handshake with, elements and all. It is
    handed back rather than swallowed because the browser's client library needs it: that
    instruction carries the connection id, and a tunnel that never sees one stays in
    "connecting" for ever with no error to show for it.

    The handshake is four instructions and it either ends in `ready` or in `error`. An
    `error` here is the far end refusing, and it carries a status code this module turns
    into something an operator can act on — the difference between "nothing is listening on
    that port" and "that account may not use Virtual Machine Connection" is the difference
    between calling the network team and calling the Windows team.
    """
    host, port = endpoint or guacd_endpoint()
    try:
        sock = socket.create_connection((host, port), timeout=timeout)
    except OSError as exc:
        raise ConsoleError(
            'The console service (guacd) is not reachable. The Hyper-V console needs it '
            'running and reachable from the PegaProx host; everything else in PegaProx '
            'works without it.',
            detail=f'{host}:{port}: {exc}') from exc

    reader = InstructionReader()
    try:
        sock.sendall(encode('select', PROTOCOL))
        args = _await_instruction(sock, reader, 'args', timeout)
        sock.sendall(encode('size', parameters.get('width', '1024'),
                            parameters.get('height', '768'), parameters.get('dpi', '96')))
        sock.sendall(encode('audio'))
        sock.sendall(encode('video'))
        sock.sendall(encode('image'))
        sock.sendall(build_connect(args[1:], parameters))
        ready = _await_instruction(sock, reader, 'ready', timeout)
    except ConsoleError:
        sock.close()
        raise
    except OSError as exc:
        sock.close()
        raise ConsoleError('The connection to guacd broke during the handshake.',
                           detail=str(exc)) from exc

    return sock, reader, ready


def _await_instruction(sock, reader, expected, timeout):
    """Read until the named instruction arrives, raising on `error` or on a closed stream."""
    sock.settimeout(timeout)
    while True:
        chunk = sock.recv(65536)
        if not chunk:
            raise ConsoleError('guacd closed the connection during the handshake.')
        for raw in reader.feed(chunk):
            elements = decode(raw)
            if not elements:
                continue
            if elements[0] == expected:
                return elements
            if elements[0] == 'error':
                status = elements[2] if len(elements) > 2 else ''
                raise ConsoleError(status_meaning(status), status=status,
                                   detail=elements[1] if len(elements) > 1 else '')
