"""A failed TLS write on one connection must not be reported on another.

OpenSSL's error queue is per OS thread and CPython does not empty it before a TLS
call, so a stale entry from connection X is read back as the result of a harmless
"no data yet" read on connection Y. Under gevent every connection shares the thread;
on the instance this is what killed healthy console sessions with BrokenPipeError.

Each scenario runs in a subprocess: install() patches ssl.SSLContext process-wide and
gevent's monkey-patch must not leak into the rest of the suite.
"""
import os
import subprocess
import sys
import textwrap

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_SCENARIO = textwrap.dedent(r'''
    import os, sys
    MODE, FIX = sys.argv[1], sys.argv[2] == 'fix'
    if MODE == 'gevent':
        from gevent import monkey; monkey.patch_all()
    sys.path.insert(0, os.environ['REPO'])
    import socket, ssl, struct, subprocess, tempfile, threading, time
    if FIX:
        from pegaprox.utils.ssl_errqueue import install
        assert install(), 'ERR_clear_error not reachable'

    d = tempfile.mkdtemp()
    cert, key = os.path.join(d, 'c.pem'), os.path.join(d, 'k.pem')
    subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-keyout', key,
                    '-out', cert, '-days', '1', '-subj', '/CN=localhost'], check=True, capture_output=True)
    sctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER); sctx.load_cert_chain(cert, key)
    cctx = ssl.create_default_context(); cctx.check_hostname = False; cctx.verify_mode = ssl.CERT_NONE

    def pair():
        ls = socket.socket(); ls.bind(('127.0.0.1', 0)); ls.listen(1)
        out = {}
        t = threading.Thread(target=lambda: out.setdefault(
            'c', cctx.wrap_socket(socket.create_connection(ls.getsockname()))))
        t.start(); raw, _ = ls.accept(); srv = sctx.wrap_socket(raw, server_side=True); t.join()
        return srv, out['c']

    _keep, healthy = pair(); healthy.settimeout(0.05)
    blamed = 0
    for _ in range(6):
        server_side, client = pair()
        # the browser goes away without a goodbye, the way a closed tab does
        client.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack('ii', 1, 0)); client.close()
        time.sleep(0.1)
        for _ in range(10):
            try:
                server_side.send(b'data: ping\n\n')
            except OSError:
                break
            time.sleep(0.02)
        try:
            healthy.recv(1024)
        except (socket.timeout, TimeoutError):
            pass
        except OSError:
            blamed += 1
    print('BLAMED', blamed)
''')


def _run(mode, fix):
    r = subprocess.run([sys.executable, '-c', _SCENARIO, mode, 'fix' if fix else 'nofix'],
                       env=dict(os.environ, REPO=REPO), capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stderr[-1500:]
    return int(r.stdout.split('BLAMED')[-1].strip())


@pytest.mark.parametrize('mode', ['threads', 'gevent'])
def test_without_the_fix_a_healthy_socket_is_blamed(mode):
    """Guards the guard: the reproduction must still show the bug, or the test below
    proves nothing."""
    assert _run(mode, fix=False) > 0, 'this CPython/OpenSSL no longer leaks - revisit the patch'


@pytest.mark.parametrize('mode', ['threads', 'gevent'])
def test_with_the_fix_a_healthy_socket_is_never_blamed(mode):
    assert _run(mode, fix=True) == 0


_ROUNDTRIP = textwrap.dedent(r'''
    import os, sys
    if sys.argv[1] == 'gevent':
        from gevent import monkey; monkey.patch_all()
    sys.path.insert(0, os.environ['REPO'])
    import asyncio, socket, ssl, subprocess, tempfile, threading
    from pegaprox.utils.ssl_errqueue import install, _QueueClearingSSLObject
    assert install() and install(), 'install must be idempotent'

    d = tempfile.mkdtemp()
    cert, key = os.path.join(d, 'c.pem'), os.path.join(d, 'k.pem')
    subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-keyout', key,
                    '-out', cert, '-days', '1', '-subj', '/CN=localhost'], check=True, capture_output=True)
    sctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER); sctx.load_cert_chain(cert, key)
    cctx = ssl.create_default_context(); cctx.check_hostname = False; cctx.verify_mode = ssl.CERT_NONE

    # socket path (gevent SSLSocket / stdlib SSLSocket): handshake, both directions, peer info, unwrap
    ls = socket.socket(); ls.bind(('127.0.0.1', 0)); ls.listen(1)
    def serve():
        raw, _ = ls.accept(); s = sctx.wrap_socket(raw, server_side=True)
        s.sendall(s.recv(64).upper()); s.unwrap(); raw.close()
    t = threading.Thread(target=serve); t.start()
    c = cctx.wrap_socket(socket.create_connection(ls.getsockname()))
    assert isinstance(c._sslobj, _QueueClearingSSLObject)
    assert c.version() and c.cipher() and c.getpeercert(binary_form=True)
    c.sendall(b'hello'); assert c.recv(64) == b'HELLO'
    c.unwrap(); t.join()

    # memory-BIO path (asyncio, and so the console's browser leg)
    async def bio():
        srv = await asyncio.start_server(lambda r, w: (w.write(b'pong'), w.close()), '127.0.0.1', 0, ssl=sctx)
        port = srv.sockets[0].getsockname()[1]
        r, w = await asyncio.open_connection('127.0.0.1', port, ssl=cctx)
        assert isinstance(w.get_extra_info('ssl_object')._sslobj, _QueueClearingSSLObject)
        data = await r.read(); w.close(); srv.close()
        return data
    assert asyncio.run(bio()) == b'pong'
    print('OK')
''')


@pytest.mark.parametrize('mode', ['threads', 'gevent'])
def test_tls_still_works_through_the_wrapper(mode):
    r = subprocess.run([sys.executable, '-c', _ROUNDTRIP, mode],
                       env=dict(os.environ, REPO=REPO), capture_output=True, text=True, timeout=120)
    assert r.returncode == 0 and r.stdout.strip().endswith('OK'), r.stderr[-1500:]


def test_the_entrypoint_installs_it_right_after_gevent():
    """Before anything opens a TLS connection - existing objects are not rewrapped."""
    src = open(os.path.join(REPO, 'pegaprox_multi_cluster.py')).read()
    hook = src.index('_install_ssl_errqueue()')
    assert src.index('monkey.patch_all()') < hook < src.index('from pegaprox.app import')


def test_every_tls_io_call_clears_first(monkeypatch):
    from pegaprox.utils import ssl_errqueue as m

    calls = []
    monkeypatch.setattr(m, '_ERR_clear_error', lambda: calls.append('clear'))

    class Real:
        owner = None
        def read(self, *a): calls.append('read'); return b'x'
        def write(self, d): calls.append('write'); return len(d)
        def do_handshake(self): calls.append('handshake')
        def shutdown(self): calls.append('shutdown')
        def version(self): return 'TLSv1.3'

    real = Real()
    o = m._QueueClearingSSLObject(real)
    o.do_handshake(); o.read(10); o.write(b'ab'); o.shutdown()
    assert calls == ['clear', 'handshake', 'clear', 'read', 'clear', 'write', 'clear', 'shutdown']
    assert o.version() == 'TLSv1.3', 'non-I/O calls are passed through untouched'
    o.owner = 'me'
    assert real.owner == 'me', 'attribute writes such as owner/session reach the real object'
