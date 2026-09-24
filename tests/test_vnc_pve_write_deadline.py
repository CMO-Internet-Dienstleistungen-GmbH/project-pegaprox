"""A write to pveproxy must not inherit the reader's 10ms slice.

The relay sets one socket timeout for the whole pve_ws, so the recv slice that keeps the
#713 lock moving was also every send's deadline. A send that times out has not necessarily
sent nothing: SSL_write can already have put a full TLS record of it on the wire, and the
next write then completes that record with bytes from a different message. pveproxy reads
a WebSocket frame spliced from two and drops the console.

The integration test below reproduces the splice against a slow TLS peer and shows that
pve_write removes it. The unit tests pin the contract the fix relies on.
"""
import os
import socket
import ssl
import subprocess
import sys
import threading
import time

import pytest

from pegaprox.utils.vnc_pve_io import VNC_PVE_WRITE_TIMEOUT, pve_write

SLICE = 0.01


class RecordingWs:
    """Stands in for websocket-client's WebSocket: remembers the timeout each write ran under."""

    def __init__(self):
        self.timeout = SLICE
        self.seen = []

    def gettimeout(self):
        return self.timeout

    def settimeout(self, t):
        self.timeout = t

    def send(self, data):
        self.seen.append(('send', self.timeout))
        return len(data)

    def ping(self):
        self.seen.append(('ping', self.timeout))

    def boom(self):
        self.seen.append(('boom', self.timeout))
        raise ConnectionResetError('peer went away')


def test_a_write_runs_under_the_write_deadline_not_the_read_slice():
    ws = RecordingWs()

    pve_write(ws, ws.send, b'x')
    pve_write(ws, ws.ping)

    assert ws.seen == [('send', VNC_PVE_WRITE_TIMEOUT), ('ping', VNC_PVE_WRITE_TIMEOUT)]
    assert VNC_PVE_WRITE_TIMEOUT > SLICE * 100, 'the deadline must outlast a busy hub by far'


def test_the_read_slice_is_restored_after_a_write():
    ws = RecordingWs()

    pve_write(ws, ws.send, b'x')

    assert ws.timeout == SLICE, 'the next recv would block the lock for the whole write deadline'


def test_the_read_slice_is_restored_when_the_write_fails():
    ws = RecordingWs()

    with pytest.raises(ConnectionResetError):
        pve_write(ws, ws.boom)

    assert ws.timeout == SLICE


def test_the_write_deadline_is_tunable(monkeypatch):
    import importlib
    import pegaprox.utils.vnc_pve_io as m

    monkeypatch.setenv('PEGAPROX_VNC_WRITE_TIMEOUT', '12')
    importlib.reload(m)
    try:
        assert m.VNC_PVE_WRITE_TIMEOUT == 12
    finally:
        monkeypatch.delenv('PEGAPROX_VNC_WRITE_TIMEOUT', raising=False)
        importlib.reload(m)


def test_every_write_on_a_vnc_leg_goes_through_the_deadline():
    """Four legs share pve_ws code by copy; a raw send left behind brings the splice back."""
    vms = open('pegaprox/api/vms.py').read()
    poll = open('pegaprox/utils/vnc_polling.py').read()

    for raw in ('pve_ws.send(data)', 'pve_ws.send(msg)', 'pve_ws.send_binary(msg)',
                'pve_ws.ping()', 'self.pve_ws.send_binary(raw)'):
        assert raw not in vms and raw not in poll, f'unguarded write: {raw}'
    assert vms.count('pve_write(pve_ws,') == 5
    assert 'pve_write(self.pve_ws,' in poll


# ── the splice itself, against a real TLS peer ───────────────────────────────

_WRITER = r'''
import os, socket, ssl, sys
sys.path.insert(0, os.environ["REPO"])
from gevent import monkey; monkey.patch_all()
import websocket
from pegaprox.utils.vnc_pve_io import pve_write

ctx = ssl.create_default_context(); ctx.check_hostname = False; ctx.verify_mode = ssl.CERT_NONE
raw = socket.create_connection(("127.0.0.1", int(sys.argv[1])))
raw.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4096)

class Ws:
    """The two websocket-client calls the relay makes, on a raw TLS socket."""
    def __init__(self, s): self.s = s
    def gettimeout(self): return self.s.gettimeout()
    def settimeout(self, t): self.s.settimeout(t)
    def send(self, data): return websocket._socket.send(self.s, data)

ws = Ws(ctx.wrap_socket(raw)); ws.settimeout(float(sys.argv[2]))
guarded = sys.argv[3] == "1"
for i in range(int(sys.argv[4])):
    payload = bytes([i % 256]) * 20000
    try:
        if guarded: pve_write(ws, ws.send, payload)
        else: ws.send(payload)
    except websocket.WebSocketTimeoutException:
        pass          # the relay's view: nothing was sent, carry on
ws.s.close()
'''


def _run_splice(tmp_path, guarded, messages):
    """Return the lengths of each same-byte run the slow peer received."""
    cert, key = tmp_path / 'c.pem', tmp_path / 'k.pem'
    subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-keyout', str(key),
                    '-out', str(cert), '-days', '1', '-subj', '/CN=localhost'],
                   check=True, capture_output=True)
    sctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    sctx.load_cert_chain(str(cert), str(key))
    ls = socket.socket()
    ls.bind(('127.0.0.1', 0))
    ls.listen(1)
    port = ls.getsockname()[1]
    received = bytearray()

    def peer():
        c, _ = ls.accept()
        c.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
        s = sctx.wrap_socket(c, server_side=True)
        try:
            while True:
                d = s.recv(65536)
                if not d:
                    break
                received.extend(d)
                time.sleep(0.02)   # a busy pveproxy worker, so the sender's buffer fills
        except (OSError, ssl.SSLError):
            pass

    t = threading.Thread(target=peer, daemon=True)
    t.start()
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    r = subprocess.run([sys.executable, '-c', _WRITER, str(port), str(SLICE),
                        '1' if guarded else '0', str(messages)],
                       env=dict(os.environ, REPO=repo), capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stderr[-800:]
    t.join(timeout=60)
    ls.close()

    runs = []
    for b in received:
        if runs and runs[-1][0] == b:
            runs[-1][1] += 1
        else:
            runs.append([b, 1])
    return [n for _, n in runs]


@pytest.mark.skipif(sys.platform == 'win32', reason='needs openssl and SO_SNDBUF semantics')
def test_a_timed_out_write_splices_two_messages(tmp_path):
    """Guards the guard: without the deadline the stream really does get spliced."""
    lengths = _run_splice(tmp_path, guarded=False, messages=120)

    assert any(n % 20000 for n in lengths), 'no splice observed - the reproduction lost its teeth'


@pytest.mark.skipif(sys.platform == 'win32', reason='needs openssl and SO_SNDBUF semantics')
def test_pve_write_delivers_every_message_whole(tmp_path):
    lengths = _run_splice(tmp_path, guarded=True, messages=60)

    assert lengths and all(n == 20000 for n in lengths), f'spliced runs: {sorted(set(lengths))}'
    assert len(lengths) == 60
