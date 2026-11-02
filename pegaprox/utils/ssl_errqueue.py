"""Keep OpenSSL's per-thread error queue empty before every TLS operation.

OpenSSL records failures in an error queue that belongs to the OS thread, and
SSL_get_error() consults that queue to classify the result of the call it is asked
about. Its documentation is explicit that the queue must be empty before a TLS I/O
call, or the classification is wrong. CPython's ssl module does not empty it: a
failed write can leave an ERR_LIB_SYS entry behind, and the next read on a
different, perfectly healthy connection that would have returned "no data yet" is
then reported as that stale error - CPython raises BrokenPipeError or
ConnectionResetError for a socket nothing happened to.

Under gevent every greenlet shares one OS thread, so one browser closing a tab
while an SSE event is being written poisons whichever TLS connection reads next.
The console relay reads its pveproxy connection every 10 ms, so it is usually the
one that dies: measured on the instance, the relay's healthy socket to the node
raised BrokenPipeError in the same instant a write to another client's closed
connection failed with EPIPE, and never saw a FIN or RST of its own.

The fix is what OpenSSL asks for: clear the queue immediately before each call into
the SSL object. It has to be immediately before the call, not at method entry -
gevent's read() waits between attempts, and other greenlets run in that wait.
"""
import ctypes
import logging
import ssl

_ERR_clear_error = None
_installed = False


def _load_err_clear_error():
    """ERR_clear_error from the libcrypto the _ssl module is linked against.

    dlsym on the extension's own handle also searches its dependencies, so this is
    the same library instance CPython uses - not a second copy with its own queue.
    """
    import _ssl
    try:
        fn = ctypes.CDLL(_ssl.__file__).ERR_clear_error
    except (OSError, AttributeError):
        return None
    fn.restype = None
    fn.argtypes = []
    return fn


class _QueueClearingSSLObject:
    """Stands in for _ssl's per-connection object: clears the queue before each TLS
    call and delegates everything else, including attribute writes such as `owner`
    and `session`, to the real object."""

    __slots__ = ('_real',)

    def __init__(self, real):
        object.__setattr__(self, '_real', real)

    def __getattr__(self, name):
        return getattr(object.__getattribute__(self, '_real'), name)

    def __setattr__(self, name, value):
        setattr(object.__getattribute__(self, '_real'), name, value)

    def read(self, *args, **kwargs):
        _ERR_clear_error()
        return self._real.read(*args, **kwargs)

    def write(self, *args, **kwargs):
        _ERR_clear_error()
        return self._real.write(*args, **kwargs)

    def do_handshake(self, *args, **kwargs):
        _ERR_clear_error()
        return self._real.do_handshake(*args, **kwargs)

    def shutdown(self, *args, **kwargs):
        _ERR_clear_error()
        return self._real.shutdown(*args, **kwargs)

    def verify_client_post_handshake(self, *args, **kwargs):
        _ERR_clear_error()
        return self._real.verify_client_post_handshake(*args, **kwargs)


def _stdlib_context_class():
    """The ssl module's own SSLContext, even after gevent replaced ssl.SSLContext with
    a subclass of it - patching the base covers both."""
    for cls in ssl.SSLContext.__mro__:
        if cls.__module__ == 'ssl' and cls.__name__ == 'SSLContext':
            return cls
    return ssl.SSLContext


def install():
    """Route every TLS connection created from now on through the queue-clearing
    object. Idempotent; returns False when libcrypto's ERR_clear_error is not
    reachable, in which case nothing is changed."""
    global _ERR_clear_error, _installed
    if _installed:
        return True
    fn = _load_err_clear_error()
    if fn is None:
        logging.warning("[ssl] ERR_clear_error not found - TLS errors on one connection "
                        "can still be reported on another")
        return False
    _ERR_clear_error = fn

    ctx_cls = _stdlib_context_class()
    wrap_socket = ctx_cls._wrap_socket
    wrap_bio = ctx_cls._wrap_bio

    def _wrap_socket(self, *args, **kwargs):
        return _QueueClearingSSLObject(wrap_socket(self, *args, **kwargs))

    def _wrap_bio(self, *args, **kwargs):
        return _QueueClearingSSLObject(wrap_bio(self, *args, **kwargs))

    ctx_cls._wrap_socket = _wrap_socket
    ctx_cls._wrap_bio = _wrap_bio
    _installed = True
    return True
