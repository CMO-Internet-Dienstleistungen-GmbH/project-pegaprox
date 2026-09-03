# Request-pool starvation — the server stopped accepting while the process sat idle.
#
# Two halves. (1) gevent.pywsgi never bounds the wait for the next request line on a
# keep-alive connection, so every idle browser socket holds one greenlet of the
# request pool; full pool = no accept for anybody. (2) run_concurrent returned after
# its joinall timeout but left the stragglers running in the shared pool, so enough
# slow guest-agent calls blocked every later spawn in Pool.add.
#
# conftest applies gevent.monkey.patch_all() first, so the pool exists and the client
# sockets below yield to the server running in the same hub.

import datetime
import socket
import ssl
import time

import gevent
from gevent.pywsgi import WSGIServer, WSGIHandler

import pegaprox.utils.concurrent as c


def _blocker():
    gevent.sleep(30)
    return 'late'


def _settle(pool, expected, budget=2.0):
    """kill(block=False) lands on a later loop iteration, and the pool's discard link one after that."""
    deadline = time.monotonic() + budget
    while pool.free_count() != expected and time.monotonic() < deadline:
        gevent.sleep(0.01)
    return pool.free_count()


def test_run_concurrent_kills_tasks_that_outlive_the_timeout():
    pool = c.GEVENT_POOL
    assert pool is not None
    free_before = pool.free_count()

    t0 = time.monotonic()
    results = c.run_concurrent([_blocker, lambda: 'fast'], timeout=0.2)
    assert time.monotonic() - t0 < 5
    assert results == [None, 'fast']

    assert _settle(pool, free_before) == free_before


def test_manager_run_concurrent_kills_stragglers_too():
    # core/manager.py carries its own copy of the helper with its own pool
    import pegaprox.core.manager as m
    pool = m.GEVENT_POOL
    assert pool is not None
    free_before = pool.free_count()

    results = m.run_concurrent([_blocker, lambda: 'fast'], timeout=0.2)
    assert results == [None, 'fast']

    assert _settle(pool, free_before) == free_before


class _Handler(c.KeepAliveTimeoutMixin, WSGIHandler):
    keepalive_timeout = 0.5


def _serve(app):
    server = WSGIServer(('127.0.0.1', 0), app, handler_class=_Handler, log=None)
    server.start()
    return server


def _ok(environ, start_response):
    start_response('200 OK', [('Content-Type', 'text/plain'), ('Content-Length', '2')])
    return [b'ok']


def _slow(environ, start_response):
    gevent.sleep(1.0)  # longer than the idle timeout, but a request is in flight
    return _ok(environ, start_response)


def _recv_until(sock, marker):
    data = b''
    while marker not in data:
        chunk = sock.recv(4096)
        assert chunk, f'connection closed before {marker!r} arrived: {data!r}'
        data += chunk
    return data


def test_idle_keepalive_connection_is_closed_after_the_timeout():
    server = _serve(_ok)
    cli = socket.create_connection(('127.0.0.1', server.server_port), timeout=5)
    try:
        cli.sendall(b'GET / HTTP/1.1\r\nHost: x\r\n\r\n')
        _recv_until(cli, b'ok')

        t0 = time.monotonic()
        assert cli.recv(4096) == b''  # the server hangs up on the idle connection
        elapsed = time.monotonic() - t0
        assert 0.3 < elapsed < 3, elapsed
    finally:
        cli.close()
        server.stop()


def test_a_fresh_connection_that_never_sends_a_request_is_closed_too():
    server = _serve(_ok)
    cli = socket.create_connection(('127.0.0.1', server.server_port), timeout=5)
    try:
        assert cli.recv(4096) == b''
    finally:
        cli.close()
        server.stop()


def test_headers_that_never_arrive_are_bounded_too():
    server = _serve(_ok)
    cli = socket.create_connection(('127.0.0.1', server.server_port), timeout=5)
    try:
        cli.sendall(b'GET / HTTP/1.1\r\n')  # request line, then silence instead of headers
        t0 = time.monotonic()
        data = b''
        while True:  # pywsgi answers a timed-out header read with a 400 and hangs up
            chunk = cli.recv(4096)
            if not chunk:
                break
            data += chunk
        assert time.monotonic() - t0 < 3
        assert data == b'' or data.startswith(b'HTTP/1.1 400'), data
    finally:
        cli.close()
        server.stop()


def test_a_second_request_within_the_timeout_is_served_on_the_same_connection():
    server = _serve(_ok)
    cli = socket.create_connection(('127.0.0.1', server.server_port), timeout=5)
    try:
        for _ in range(2):
            cli.sendall(b'GET / HTTP/1.1\r\nHost: x\r\n\r\n')
            _recv_until(cli, b'ok')
            gevent.sleep(0.2)  # idle, but under the 0.5 s bound
    finally:
        cli.close()
        server.stop()


def _self_signed(tmp_path):
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'localhost')])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
            .public_key(key.public_key()).serial_number(1)
            .not_valid_before(now - datetime.timedelta(minutes=1))
            .not_valid_after(now + datetime.timedelta(days=1))
            .sign(key, hashes.SHA256()))
    cert_path, key_path = tmp_path / 'cert.pem', tmp_path / 'key.pem'
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM,
                                           serialization.PrivateFormat.TraditionalOpenSSL,
                                           serialization.NoEncryption()))
    return str(cert_path), str(key_path)


class _TlsServer(c.KeepAliveTimeoutServerMixin, WSGIServer):
    pass


def test_a_tls_client_that_never_starts_the_handshake_is_closed(tmp_path, monkeypatch):
    # The pool slot is taken before the handler exists: a half-open TCP client that
    # never sends a ClientHello has to be bounded by the server, not the handler.
    monkeypatch.setattr(c, 'KEEPALIVE_TIMEOUT', 0.5)
    cert, key = _self_signed(tmp_path)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(cert, key)
    server = _TlsServer(('127.0.0.1', 0), _ok, handler_class=_Handler, ssl_context=ctx, log=None)
    server.start()
    cli = socket.create_connection(('127.0.0.1', server.server_port), timeout=5)
    try:
        t0 = time.monotonic()
        assert cli.recv(4096) == b''
        assert time.monotonic() - t0 < 3
    finally:
        cli.close()
        server.stop()


def test_a_request_in_flight_is_not_subject_to_the_idle_timeout():
    server = _serve(_slow)
    cli = socket.create_connection(('127.0.0.1', server.server_port), timeout=5)
    try:
        cli.sendall(b'GET / HTTP/1.1\r\nHost: x\r\n\r\n')
        assert b'ok' in _recv_until(cli, b'ok')  # 1.0 s of application time > 0.5 s idle timeout
    finally:
        cli.close()
        server.stop()


def test_zero_disables_the_timeout():
    class Unbounded(c.KeepAliveTimeoutMixin, WSGIHandler):
        keepalive_timeout = 0

    server = WSGIServer(('127.0.0.1', 0), _ok, handler_class=Unbounded, log=None)
    server.start()
    cli = socket.create_connection(('127.0.0.1', server.server_port), timeout=5)
    try:
        cli.sendall(b'GET / HTTP/1.1\r\nHost: x\r\n\r\n')
        _recv_until(cli, b'ok')
        cli.settimeout(1.0)
        try:
            cli.recv(4096)
        except TimeoutError:
            pass  # still open after a second: nothing closed it
        else:
            raise AssertionError('server closed an idle connection although the timeout is disabled')
    finally:
        cli.close()
        server.stop()
