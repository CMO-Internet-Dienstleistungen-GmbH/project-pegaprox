"""Write deadline for the VNC relay's connection to pveproxy.

Every console leg sets ``pve_ws.settimeout(VNC_PVE_RECV_SLICE)`` so its reader gives the
#713 lock back every 10 ms. websocket-client has one timeout per socket, so that slice
also became the deadline for every send and ping on the same connection.

A send that hits that deadline is not a clean "nothing happened". SSL_write may already
have put one TLS record of the message on the wire when the timeout fires; the caller
sees an exception and treats the message as unsent, and the next write carries on where
OpenSSL left off - with the rest of a *different* message. pveproxy then reads a
WebSocket frame spliced from two, and the console dies. Measured locally with a slow
TLS peer: 16384 bytes of one message followed by 3616 bytes of another, once per
timed-out write.

A write has no reason to share the reader's slice: the slice exists to bound how long
the reader holds the lock, and a writer already holds it. So a write gets its own,
generous deadline while it runs, and the slice is restored for the next read. On an
idle socket the write finishes in microseconds and the deadline never matters; it only
decides what happens when the socket is briefly not writable - wait, instead of
corrupting the stream.
"""
import os

# Long enough to ride out a busy hub or a stalled pveproxy worker, short enough that a
# genuinely dead peer still ends the session instead of holding the console lock.
VNC_PVE_WRITE_TIMEOUT = float(os.environ.get('PEGAPROX_VNC_WRITE_TIMEOUT', '5'))


def pve_write(pve_ws, write, *args):
    """Run one send/ping on ``pve_ws`` under the write deadline, then restore the read slice.

    The caller must hold the leg's pve_ws lock: the timeout is per socket, so changing it
    while a reader sits in recv() would change the reader's deadline too.
    """
    read_slice = pve_ws.gettimeout()
    pve_ws.settimeout(VNC_PVE_WRITE_TIMEOUT)
    try:
        return write(*args)
    finally:
        pve_ws.settimeout(read_slice)
