"""The witness of an automatic group (#625 stage 2): a third vote, and nothing else.

Two data members alone tolerate no failure. A witness is a small process at a third
site that votes, keeps its promise and never leads. It holds no configuration of the
deployment: no database, no field key, no cluster credentials, no users. What it keeps
in its own state directory is its instance id, its Ed25519 key (only to say who it is
when it pairs or leaves), the TLS pair it serves with, and the vote: epoch, voted_for,
the promise, the floor, and the voter config with up to 16 configs before it.

It answers the few signed peer calls a voter needs, on one port:
  GET  /api/ha/peer/status    its own small status, for the skew and the checks
  POST /api/ha/peer/vote      a vote or a pre-vote of a candidate
  POST /api/ha/peer/renew     the leader's renewal, or a round of the switch
  POST /api/ha/peer/unpaired  the leader took it out of the group
The rules are those of pegaprox/core/ha_vote.py, run by the same Node class every
member runs; the signature and the sealed pairing answer are those of ha_wire.py.

    pegaprox-witness join <code> --url https://witness.example:5005
    pegaprox-witness run
    pegaprox-witness fingerprint | status | leave [--force] | health

or `python -m pegaprox.witness ...`, and in the Docker image the command `witness`.
The state directory is PEGAPROX_WITNESS_DIR, else the one systemd made for the unit
(STATE_DIRECTORY), else /var/lib/pegaprox-witness where that exists, else ./witness.

MK Oct 2026 (#625)
"""
import argparse
import errno
import ipaddress
import json
import logging
import os
import random
import re
import socket
import sys
import threading
import time
import uuid
from collections import Counter, OrderedDict
from datetime import datetime, timezone

from pegaprox.core import ha_vote, ha_wire

DEFAULT_PORT = 5005
STATE_NAME = ha_wire.WITNESS_STATE_NAME
LOCK_NAME = '.witness.lock'
SSL_DIRNAME = 'ssl'
STATUS_PATH = '/api/ha/peer/status'
VOTE_PATH = '/api/ha/peer/vote'
RENEW_PATH = '/api/ha/peer/renew'
UNPAIRED_PATH = '/api/ha/peer/unpaired'
PAIR_PATH = '/api/ha/peer/pair-witness'
LEAVE_PATH = '/api/ha/peer/witness-leave'
ROUTES = {(STATUS_PATH, 'GET'): 'status', (VOTE_PATH, 'POST'): 'vote',
          (RENEW_PATH, 'POST'): 'renew', (UNPAIRED_PATH, 'POST'): 'unpaired'}
MAX_BODY = 64 * 1024
CALL_TIMEOUT = 15
EXIT_CONFIG = 78
# what lies in a PegaProx config directory, or next to the state of a member: a witness
# never keeps its vote there
DATA_FILES = ('pegaprox.db', 'syslog.db', 'ha_state.json', '.ha-member',
              '.pegaprox_aes256.key', '.pegaprox.key', '.pegaprox.lock')
# what the node writes down, next to who this witness is
_LEASE_KEYS = ('epoch', 'voted_for', 'gen', 'cfg', 'cfg_chain', 'floor_cv', 'led', 'released',
               'campaign_after', 'promised')
# how often the state file is written again as it is, to know it still can be (lease time)
WRITE_CHECK = 10
# the server (see _Server): deadlines in seconds and how many connections it holds
_ANON_S = 5             # the TLS handshake and the head of the first call, from the accept
_BODY_S = 5             # the body of a call that carried a good signature, from its head
_IDLE_S = 30            # a kept connection of a member, from an answer to the next head
_PER_SOURCE = 32        # connections of one source address that showed no signature yet
_ANON_MAX = 128         # all such connections
_SHARE_V6 = 48          # the prefix an IPv6 source counts by in the share of all (_group)
_MEMBER_MAX = 32        # connections whose calls carried a good signature
_HEAD_MAX = 16 * 1024
_PEER_HEADERS = (ha_wire.PEER_HEADER, ha_wire.PEER_TS_HEADER, ha_wire.PEER_NONCE_HEADER,
                 ha_wire.PEER_SIG_HEADER, ha_wire.PEER_BODY_HEADER)
_REASONS = {200: 'OK', 400: 'Bad Request', 401: 'Unauthorized', 404: 'Not Found',
            405: 'Method Not Allowed', 413: 'Payload Too Large', 429: 'Too Many Requests',
            500: 'Internal Server Error'}


class WitnessError(Exception):
    """Something the admin is told as it is."""


def _now_text():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def release():
    """The release this witness runs, read from the source rather than imported:
    pegaprox.constants makes the directories of a PegaProx instance when it loads."""
    root = os.path.dirname(os.path.abspath(__file__))
    try:
        with open(os.path.join(root, 'constants.py'), encoding='utf-8') as fh:
            found = re.search(r'^PEGAPROX_VERSION\s*=\s*["\']([^"\']{1,32})["\']', fh.read(), re.M)
        if found:
            return found.group(1)
    except OSError:
        pass
    try:
        with open(os.path.join(os.path.dirname(root), 'version.json'), encoding='utf-8') as fh:
            return str(json.load(fh).get('version') or '')[:32]
    except Exception:
        return ''


RELEASE = release()


# --- the state directory -------------------------------------------------------------

def default_dir():
    for var in ('PEGAPROX_WITNESS_DIR', 'STATE_DIRECTORY'):
        value = (os.environ.get(var) or '').strip()
        if value:
            # systemd may hand over several, separated by colons: ours is the first
            return value.split(':')[0]
    if os.path.isdir('/var/lib/pegaprox-witness'):
        return '/var/lib/pegaprox-witness'
    return os.path.abspath('witness')


def foreign_data(path):
    """What in `path` says it belongs to a PegaProx instance, '' when nothing does."""
    for name in DATA_FILES:
        if os.path.lexists(os.path.join(path, name)):
            return name
    return ''


def check_dir(path, create=True):
    """The state directory, made with 0700 when it is missing. Raises WitnessError for a
    directory a PegaProx instance keeps its data in."""
    path = os.path.abspath(path)
    found = foreign_data(path) if os.path.isdir(path) else ''
    if found:
        raise WitnessError(f'{path} holds {found} - that is the data of a PegaProx instance, and '
                           'a witness never keeps its vote next to it. Give it a directory of its own')
    if not os.path.isdir(path):
        if not create:
            raise WitnessError(f'{path} does not exist')
        os.makedirs(path, mode=0o700, exist_ok=True)
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass
    return path


def _fsync_dir(path):
    """fsync the directory `path` is in. True when it was synced, False on a filesystem
    that does not sync directories at all; raises for anything else."""
    fd = os.open(os.path.dirname(os.path.abspath(path)), os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
    try:
        os.fsync(fd)
        return True
    except OSError as e:
        if e.errno in (errno.EINVAL, getattr(errno, 'ENOTSUP', errno.EINVAL)):
            return False
        raise
    finally:
        os.close(fd)


def _write_json(path, data, strict=True):
    """`data` as the file `path`, 0600: on disk, file and directory, before it returns.
    Returns whether the directory could be synced; raises when the write failed. Not
    `strict`: a directory that cannot be synced is let through."""
    tmp = path + '.tmp'
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as fh:
            os.fchmod(fh.fileno(), 0o600)
            fh.write(json.dumps(data, indent=2, sort_keys=True))
            fh.flush()
            os.fsync(fh.fileno())
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    os.replace(tmp, path)
    try:
        return _fsync_dir(path)
    except OSError:
        if strict:
            raise
        return False


def write_check(path):
    """'' when a file can be written into the state directory `path` and synced, file
    and directory, as a vote is; what went wrong when not. For the commands, which run
    next to the witness process: they never write its state file, only one of their own."""
    probe = os.path.join(path, f'.write-check-{os.getpid()}')
    try:
        _write_json(probe, {'at': _now_text()})
        return ''
    except Exception as e:
        return f'{type(e).__name__}: {e}'[:200]
    finally:
        for name in (probe, probe + '.tmp'):
            try:
                os.unlink(name)
            except OSError:
                pass


def lock_dir(path):
    """An exclusive flock on the state directory, for as long as the descriptor lives:
    one process votes from it. Raises WitnessError when another one holds it."""
    try:
        import fcntl
    except ImportError:
        return None
    fd = os.open(os.path.join(path, LOCK_NAME), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as e:
        os.close(fd)
        if e.errno in (errno.EWOULDBLOCK, errno.EAGAIN):
            raise WitnessError(f'Another witness process runs on {path} - stop the service first')
        raise
    try:
        os.ftruncate(fd, 0)
        os.pwrite(fd, f'{os.getpid()}\n'.encode(), 0)
    except OSError:
        pass
    return fd


def unlock_dir(fd):
    if fd is not None:
        try:
            os.close(fd)
        except OSError:
            pass


# --- TLS -----------------------------------------------------------------------------

def tls_paths(path):
    folder = os.path.join(path, SSL_DIRNAME)
    return os.path.join(folder, 'cert.pem'), os.path.join(folder, 'key.pem')


def _generate_self_signed(cert_file, key_file):
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.ORGANIZATION_NAME, 'PegaProx'),
                      x509.NameAttribute(NameOID.COMMON_NAME, 'PegaProx witness')])
    now = datetime.now(timezone.utc)
    # pinned by its fingerprint, not checked by a chain: the dates are not what makes it
    # trusted, and a witness should not stop answering one day because a year went by
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now)
            .not_valid_after(now.replace(year=now.year + 10)).sign(key, hashes.SHA256()))
    os.makedirs(os.path.dirname(cert_file), mode=0o700, exist_ok=True)
    key_pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL,
                                serialization.NoEncryption())
    fd = os.open(key_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'wb') as fh:
        fh.write(key_pem)
    with open(cert_file, 'wb') as fh:
        fh.write(cert.public_bytes(serialization.Encoding.PEM))


def tls_pair(path, create=True):
    """(cert file, key file, pin) of the state directory `path`. The pin is the SHA-256
    fingerprint of a self-signed certificate, '' for one a CA signed, which members
    check by its chain. Made when neither file is there, as the main app does; one half
    alone, or a pair that does not load, raises WitnessError rather than serve without."""
    import ssl
    cert_file, key_file = tls_paths(path)
    have = [os.path.exists(cert_file), os.path.exists(key_file)]
    if not any(have):
        if not create:
            raise WitnessError(f'There is no certificate in {os.path.dirname(cert_file)} yet - '
                               'it is made by join, or by the first run')
        _generate_self_signed(cert_file, key_file)
    elif not all(have):
        missing = key_file if have[0] else cert_file
        raise WitnessError(f'{missing} is missing next to its counterpart - refusing to make a '
                           'new pair over the half that is there')
    try:
        ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER).load_cert_chain(cert_file, key_file)
    except (ssl.SSLError, OSError, ValueError) as e:
        raise WitnessError(f'{cert_file} and {key_file} do not load as a pair: {e}')
    with open(cert_file, 'rb') as fh:
        pem = fh.read()
    return cert_file, key_file, (ha_wire.cert_fingerprint(pem) if _self_signed(pem) else '')


def _self_signed(pem):
    try:
        from cryptography import x509
        cert = x509.load_pem_x509_certificate(pem)
        return cert.issuer == cert.subject
    except Exception:
        return False


# --- talking to a member ---------------------------------------------------------------

def https_call(method, base_url, fingerprint, path, body, headers, timeout=CALL_TIMEOUT):
    """One HTTPS call to a member: (status, the JSON object it answered, or None).
    Pinned to `fingerprint` where one is given, checked by the CA chain where not.
    Raises WitnessError when the member does not answer."""
    import requests
    from pegaprox.utils.url_security import is_safe_outbound_url
    url = base_url.rstrip('/') + path
    ok, why = is_safe_outbound_url(url, allowed_schemes=('https',), allow_private=True)
    if not ok:
        raise WitnessError(f'The address is not allowed: {why}')
    sess = requests.Session()
    if fingerprint:
        class _Pinned(requests.adapters.HTTPAdapter):
            def init_poolmanager(self, *a, **kw):
                kw['assert_fingerprint'] = fingerprint
                return super().init_poolmanager(*a, **kw)
        sess.mount('https://', _Pinned())
    h = {'X-Requested-With': 'XMLHttpRequest', 'Accept': 'application/json'}
    if body:
        h['Content-Type'] = 'application/json'
    h.update(headers or {})
    try:
        resp = sess.request(method, url, data=body or None, headers=h, verify=not fingerprint,
                            timeout=timeout, allow_redirects=False)
    except requests.exceptions.SSLError:
        raise WitnessError('The certificate of the member does not match the pin in the code')
    except requests.exceptions.RequestException as e:
        raise WitnessError(f'Cannot reach {base_url}: {type(e).__name__}')
    finally:
        sess.close()
    try:
        data = resp.json()
    except Exception:
        data = None
    return resp.status_code, data if isinstance(data, dict) else None


# --- the witness ---------------------------------------------------------------------

class _Store:
    """The node's state in the witness's state file. save() is on disk, file and
    directory, before it returns, and raises when it is not: a vote or a promise that
    is not written is not given."""

    def __init__(self, w):
        self.w = w

    def load(self):
        st = self.w.st
        if not st or not isinstance(st.get('cfg'), dict):
            return None
        out = {k: st.get(k) for k in _LEASE_KEYS}
        out.update(role=ha_vote.ROLE_WITNESS, cv=None, base_cv=None,
                   cfg_chain=list(st.get('cfg_chain') or []),
                   epoch=st['epoch'] if type(st.get('epoch')) is int else 0,
                   gen=st['gen'] if type(st.get('gen')) is int else 0)
        return out

    def save(self, new):
        out = dict(self.w.st)
        for k in _LEASE_KEYS:
            out[k] = new.get(k)
        out['floor_cv'] = list(new.get('floor_cv') or ha_vote.ZERO)
        out['role'] = ha_vote.ROLE_WITNESS
        self.w.write(out)


class _Hooks:
    """What the node reports, kept until it let go: nothing is logged or written from
    inside it (F10)."""

    def __init__(self, w):
        self.w = w

    def restart(self, why):
        self.w.events.append(('restart', {'why': why}))

    def event(self, name, info):
        if name in ('granted', 'write_failed', 'cfg', 'restart'):
            self.w.events.append((name, info))

    def snapshot(self):
        return None

    def apply_snapshot(self, frm, ans):
        return None


class Witness:
    """The witness of one state directory: its state file, its node, and the answers
    to the peer calls. handle() takes one call as it arrives and returns (HTTP status,
    JSON object); the server and the tests both go through it."""

    def __init__(self, path, *, clock=None, wall=None, started=None, boot_id=None, call=None):
        self.dir = path
        self.path = os.path.join(path, STATE_NAME)
        self.clock = clock or ha_vote.ha_clock
        self.wall = wall or time.time
        # a call signed before this process started is refused: the nonces seen before
        # are gone with the process. The start is kept in lease time (`started` pins a
        # wall clock value instead, for the tests)
        self.pinned_start = started
        self.started_clock = self.clock()
        self.boot_id = ha_vote.read_boot_id() if boot_id is None else boot_id
        self.call = call or https_call
        self.lock = threading.RLock()
        self.nonces = {}
        # sender -> its streams of lease calls, (sender, 'calls') -> those of its other
        # calls (ha_wire.take_stream)
        self.streams = {}
        self.skews = {}
        self.events = []
        self.said = set()
        self.dir_sync = None
        # why the last write of the state file failed, None while writes go through, and
        # when one was last tried (lease time)
        self.write_failed = None
        self.write_checked = None
        from pegaprox.utils.ratelimit import SlidingWindow
        self.failures = SlidingWindow(limit=10, window=300, max_keys=2048, name='witness-auth')
        self.st = self.read()
        self.node = None

    @property
    def started(self):
        """When this process started, by the wall clock as it reads now: a clock that was
        ahead at the start and that NTP set back since moves the start back with it, and
        the members are not refused for as long as it was off."""
        if self.pinned_start is not None:
            return self.pinned_start
        return int(self.wall() - (self.clock() - self.started_clock))

    # --- the state file ---

    def read(self):
        try:
            with open(self.path, encoding='utf-8') as fh:
                st = json.load(fh)
        except FileNotFoundError:
            return {}
        except Exception as e:
            raise WitnessError(f'{self.path} cannot be read ({e}) - fix or remove it')
        if not isinstance(st, dict) or st.get('role') != ha_vote.ROLE_WITNESS:
            raise WitnessError(f'{self.path} is no witness state file')
        return st

    def write(self, st):
        self.write_checked = self.clock()
        try:
            synced = _write_json(self.path, st)
        except Exception as e:
            if self.write_failed is None:
                self.events.append(('unwritable', {'error': f'{type(e).__name__}: {e}'}))
            self.write_failed = f'{type(e).__name__}: {e}'[:200]
            self._put_back()
            raise
        if self.write_failed is not None:
            self.events.append(('writable', {}))
        self.write_failed = None
        self.dir_sync = synced
        self.st = st

    def _put_back(self):
        """After a failed write: a write that failed at the directory sync has its rename
        in place already. The file goes back to what this process holds, so a vote it
        refused is not on disk for the next start either (ha.py does the same)."""
        if not self.st:
            return
        try:
            with open(self.path, encoding='utf-8') as fh:
                if json.load(fh) == self.st:
                    return
            _write_json(self.path, self.st, strict=False)
        except Exception:
            pass

    def check_write(self):
        """The state file written again as it is, at most every WRITE_CHECK seconds. A
        renewal at the term held writes nothing: without this a witness on a full disk or
        in a directory it cannot write any more acks those and looks healthy, until the
        vote of the next failover, which it cannot give. Every answer says what it finds."""
        if not self.paired():
            return
        now = self.clock()
        if self.write_checked is not None and 0 <= now - self.write_checked < WRITE_CHECK:
            return
        try:
            self.write(dict(self.st))
        except Exception:
            pass

    def instance_id(self):
        iid = self.st.get('instance_id')
        return iid if isinstance(iid, str) and ha_wire.ID_RE.fullmatch(iid) else ''

    def paired(self):
        return isinstance(self.st.get('cfg'), dict) and bool(self.instance_id())

    def _node(self):
        if self.node is None and self.paired():
            def refuse(_message):
                raise WitnessError('A witness makes no voter config')
            self.node = ha_vote.Node(
                self.instance_id(), ha_vote.KIND_WITNESS, store=_Store(self), clock=self.clock,
                wall=self.wall, send=lambda *_a: None, hooks=_Hooks(self),
                rng=random.Random(), sign=refuse, verify=ha_wire.cfg_signed, boot_id=self.boot_id)
        return self.node

    def start(self):
        """Build the node now, so the hold after a start counts from the process start."""
        with self.lock:
            return self._node()

    # --- the peer calls ---

    def handle(self, method, path, headers, body=b'', remote=''):
        kind = ROUTES.get((path, method))
        if kind is None:
            if any(p == path for p, _m in ROUTES):
                return 405, {'error': 'Method not allowed'}
            return 404, {'error': 'Not found - this is a PegaProx witness'}
        if len(body or b'') > MAX_BODY:
            return 413, {'error': 'Too large'}
        try:
            with self.lock:
                out = self._answer(kind, path, method, headers, body or b'', remote)
        finally:
            self._say()
        return out

    def _answer(self, kind, path, method, headers, body, remote):
        if not self.paired():
            return 401, {'error': 'This witness is not paired', 'instance_id': self.instance_id()}
        data = {}
        if body:
            try:
                data = json.loads(body)
            except ValueError:
                data = None
            if not isinstance(data, dict):
                return 400, {'error': 'The body is no JSON object'}
        sender = headers.get(ha_wire.PEER_HEADER)
        node = self._node()
        key = self._key_of(sender, kind, data, node) if isinstance(sender, str) else None
        verdict = ''
        if key:
            verdict = ha_wire.signature_verdict(headers, method, path, body, sender, key,
                                                self.instance_id(), self.wall(), self.started)
        if verdict in ('window', 'early'):
            # the signature is good: the member itself, with a clock that is off
            self._note_skew(sender, headers)
            return 401, {'code': 'HA_CLOCK', 'error': f'The clocks of the two instances are more '
                         f'than {ha_wire.SIGNATURE_WINDOW} seconds apart - set both by NTP'}
        if verdict == 'ok' and self._spend(sender, kind, headers) != 'ok':
            verdict = ''
        if verdict != 'ok':
            if not self.failures.allow(remote or '-'):
                return 429, {'error': 'Too many failed peer calls'}
            self.events.append(('refused', {'from': remote, 'path': path}))
            return 401, {'error': 'Not a member of the group of this witness',
                         'instance_id': self.instance_id()}
        self._note_skew(sender, headers, data.get('wall') if kind == 'renew' else None)
        if kind in ('status', 'renew'):
            self.check_write()
        if kind == 'status':
            return 200, self.status(sender)
        if kind == 'unpaired':
            return 200, self._unpaired(sender, data)
        if kind == 'renew' and not ha_vote.takes_switch_back(data, node.view.mode):
            # the same rule a data member goes by (ha.py lease_request)
            ans = {'ok': False, 'granted': False, 'reason': 'NOT_PENDING', 'epoch': node.epoch}
        else:
            ans = node.on_request(sender, kind, data)
        if node.view.witness != self.instance_id():
            self.events.append(('not_named', {}))
        # with every answer: the leader shows a witness that cannot write as one that
        # gives no vote, long before the failover that would need it
        return 200, dict(ans, write_failed=self.write_failed is not None)

    def precheck(self, method, path, headers):
        """Before the body of a call is read (the server): 'member' when its headers carry
        a good signature, inside the window and with a nonce not seen yet, over the body
        digest they name, from a voter the config held here names; 'open' when only the
        body can tell (a voter that the chain in it names, a clock that is off); '' for a
        call that is refused whatever its body says. Nothing is spent."""
        sender, digest = headers.get(ha_wire.PEER_HEADER), headers.get(ha_wire.PEER_BODY_HEADER)
        kind = ROUTES.get((path, method))
        if kind is None or not isinstance(sender, str) or not ha_wire.ID_RE.fullmatch(sender):
            return ''
        with self.lock:
            if not self.paired():
                return ''
            rec = self._node().view.records.get(sender)
            if rec is None:
                return 'open' if kind in ('vote', 'renew') else ''
            if not isinstance(digest, str) or not ha_wire.DIGEST_RE.fullmatch(digest):
                return ''
            verdict = ha_wire.signature_verdict(headers, method, path, None, sender, rec.get('public_key'),
                                                self.instance_id(), self.wall(), self.started, digest=digest)
            if verdict in ('window', 'early'):
                return 'open'
            if verdict != 'ok':
                return ''
            return '' if self._spend(sender, kind, headers, spend=False) == 'seen' else 'member'

    def _spend(self, sender, kind, headers, spend=True):
        """The nonce of a call with a good signature: 'ok' the first time, 'seen' or 'full'
        (ha_wire.take_nonce). A member numbers its calls (stream nonces, one stream for
        the votes and renewals and one for the rest, ha_wire.take_stream): a confirm round
        before every write of the leader never fills a share here. The lease calls and
        the others are kept apart, as random nonces are. With spend=False nothing is
        taken."""
        nonce = headers[ha_wire.PEER_NONCE_HEADER]
        ts = int(headers[ha_wire.PEER_TS_HEADER])
        lease = kind in ('vote', 'renew')
        if ha_wire.is_stream_nonce(nonce):
            key = sender if lease else (sender, 'calls')
            streams = self.streams.setdefault(key, {}) if spend else dict(self.streams.get(key) or {})
            return ha_wire.take_stream(streams, nonce, ts, self.wall(), spend)
        if not spend:
            return 'seen' if nonce in (self.nonces.get((sender, lease)) or {}) else 'ok'
        return ha_wire.take_nonce(self.nonces.setdefault((sender, lease), {}), nonce, ts, self.wall())

    def _key_of(self, sender, kind, data, node):
        """The public key `sender` signs with, as the voter config held here names it, or
        as a newer config names it that a vote or a renewal carries and that hangs off
        the one held here (a data voter that joined while this witness missed it)."""
        if not ha_wire.ID_RE.fullmatch(sender):
            return None
        rec = node.view.records.get(sender)
        if rec is not None:
            return rec.get('public_key')
        if kind in ('vote', 'renew') and data.get('chain'):
            try:
                chain = ha_vote.newer_chain(node.chain, data.get('chain'), ha_wire.cfg_signed)
            except Exception:
                # CfgRefused, or a chain that is no chain at all
                return None
            if chain:
                rec = ha_vote.CfgView(chain[-1]).records.get(sender)
                return rec.get('public_key') if rec else None
        return None

    def _note_skew(self, sender, headers, wall=None):
        """How far the caller's wall clock is from ours, from the time it signed (whole
        seconds, the trip included) or from the time a renewal carries."""
        now = self.wall()
        try:
            skew = float(wall) - now if type(wall) in (int, float) else int(headers[ha_wire.PEER_TS_HEADER]) - now
        except (KeyError, TypeError, ValueError):
            return
        self.skews[sender] = (round(skew, 3), self.clock())
        if abs(skew) > ha_vote.SKEW_LIMIT and ('skew', sender) not in self.said:
            self.said.add(('skew', sender))
            self.events.append(('skew', {'member': sender, 'skew': round(skew, 1)}))

    def status(self, sender=None):
        node = self._node()
        st = self.st
        view = node.view
        now = self.clock()
        holder = node.promise_to if node.promise_to and now < node.promise_until else None
        out = {'instance_id': self.instance_id(), 'role': ha_vote.ROLE_WITNESS,
               'kind': ha_vote.KIND_WITNESS, 'epoch': node.epoch, 'group': ha_wire.GROUP_MARK,
               'lease_mark': ha_wire.LEASE_MARK, 'wall': self.wall(), 'release': RELEASE, 'zone': '',
               'mode': view.mode, 'cfg_id': list(view.id), 'gen': st.get('gen') or 0,
               'voted_for': st.get('voted_for'), 'reach': {},
               'lease': {'holds': False, 'holder': holder, 'epoch': node.epoch},
               'floor_cv': list(node.st.get('floor_cv') or ha_vote.ZERO),
               # the config held, by digest, as a data member says it (ha.py _manual_known)
               'cfg_digest': ha_vote.cfg_digest(view.cfg)}
        if view.mode == ha_vote.MODE_PENDING and isinstance(view.cfg.get('by'), str):
            out['pending_by'] = view.cfg['by']
        if self.dir_sync is False:
            out['dir_sync'] = False
        if self.write_failed is not None:
            # no vote and no renewal that needs a write until it can (auto_findings)
            out['write_failed'] = True
        if sender in self.skews:
            out['skew'] = self.skews[sender][0]
        return out

    def _unpaired(self, sender, data):
        """The leader took this witness out of the group. Taken from a data voter of the
        voter config held here; from anybody else nothing happens."""
        rec = self._node().view.records.get(sender) or {}
        if data.get('removed') is not True or not rec.get('voter'):
            return {'success': True, 'left_group': False}
        self.let_go(f'removed by member {sender[:8]}', by=sender)
        return {'success': True, 'left_group': True}

    def let_go(self, why, by=None):
        """Out of the group: the vote, the config and the key go; the instance id stays."""
        keep = {'role': ha_vote.ROLE_WITNESS, 'instance_id': self.instance_id(),
                'own_url': self.st.get('own_url') or '',
                'left': {'at': _now_text(), 'why': why, 'by': by}}
        self.write(keep)
        self.node = None
        self.nonces.clear()
        self.streams.clear()
        self.said.add('left')
        self.events.append(('left', {'why': why}))

    def _say(self):
        events, self.events = self.events, []
        for name, info in events:
            if name == 'granted':
                logging.warning(f"[witness] vote for {str(info.get('candidate'))[:8]} at epoch "
                                f"{info.get('epoch')}")
            elif name == 'write_failed':
                logging.error(f"[witness] could not write the vote: {info.get('error')} - no vote "
                              "is given until it can")
            elif name == 'unwritable':
                logging.error(f"[witness] cannot write the state file {self.path}: {info.get('error')} - "
                              "no vote is given until it can, and the members are told so")
            elif name == 'writable':
                logging.warning(f"[witness] the state file {self.path} can be written again")
            elif name == 'cfg':
                logging.info(f"[witness] voter config {(info.get('cfg') or {}).get('id')}")
            elif name == 'skew':
                logging.warning(f"[witness] the clock of member {info['member'][:8]} is {info['skew']} s "
                                f"off this one - automatic failover needs {ha_vote.SKEW_LIMIT} s or "
                                "less (NTP)")
            elif name == 'refused':
                logging.warning(f"[witness] refused a call from {info.get('from') or 'unknown'} to "
                                f"{info.get('path')}")
            elif name == 'left':
                logging.warning(f"[witness] out of the group: {info.get('why')}")
            elif name == 'not_named':
                if 'not_named' not in self.said:
                    self.said.add('not_named')
                    logging.info("[witness] the voter config held here does not name this witness "
                                 "(yet): it votes once the leader's next config does")
            elif name == 'restart':
                logging.error(f"[witness] the protocol asked for a restart: {info.get('why')}")

    # --- pairing ---

    def join(self, code, own_url):
        """Pair with the leader behind `code` (made on its HA page). The witness sends its
        id, address, certificate pin and public key; the sealed answer carries the
        leader's key, the voter config and the epoch, no field key and no snapshot.
        Returns the leader's instance id."""
        if self.paired():
            raise WitnessError('This witness is paired already - leave its group first')
        try:
            info = ha_wire.decode_code(ha_wire.WITNESS_CODE_PREFIX, code, 'a PegaProx witness code')
        except ha_wire.WireError as e:
            raise WitnessError(str(e))
        url = ha_wire.valid_https_url(own_url or '')
        if not url:
            raise WitnessError('--url is the https:// address the members reach this witness at')
        _cert, _key, pin = tls_pair(self.dir)
        me = self.instance_id() or uuid.uuid4().hex
        if me == info['instance_id']:
            raise WitnessError('That code was made by this instance')
        signing_key = ha_wire.new_signing_key()
        public = ha_wire.public_of(ha_wire.private_key(signing_key))
        body = ha_wire.wire_body({'code': info['secret'], 'instance_id': me, 'url': url,
                                  'fingerprint': pin, 'public_key': public})
        status, data = self.call('POST', info['url'], info['fingerprint'], PAIR_PATH, body, {})
        if status != 200 or data is None:
            raise WitnessError((data or {}).get('error') or f'The leader refused the pairing (HTTP {status})')
        if data.get('instance_id') != info['instance_id']:
            raise WitnessError('The instance that answered is not the one that made the code')
        try:
            opened = ha_wire.unseal(info['secret'], data.get('sealed') or '', aad=me)
        except Exception:
            raise WitnessError('The answer from the leader could not be opened')
        chain, epoch, floor = self._paired_with(info, opened)
        st = {'role': ha_vote.ROLE_WITNESS, 'instance_id': me, 'signing_key': signing_key,
              'own_url': url, 'paired': {'instance_id': info['instance_id'], 'url': info['url'],
                                         'fingerprint': info['fingerprint'], 'at': _now_text()},
              'epoch': epoch, 'voted_for': None, 'gen': 0, 'cfg': chain[-1],
              'cfg_chain': chain[:-1][-ha_vote.CFG_KEEP:], 'floor_cv': list(floor), 'led': None,
              'released': None, 'campaign_after': None, 'promised': None}
        self.write(st)
        self.node = None
        self.nonces.clear()
        self.streams.clear()
        return info['instance_id']

    @staticmethod
    def _paired_with(info, opened):
        """(chain, epoch, floor) from the sealed answer, checked: a chain of voter configs
        that starts at one its own data voter signed, each one signed by a data voter of
        the one before, with the leader a data voter of the newest under the key it
        sent. The code is the trust: only the leader that made it could seal this."""
        if not isinstance(opened, dict):
            raise WitnessError('The answer from the leader is incomplete')
        leader_key = opened.get('public_key')
        chain = opened.get('chain')
        epoch = opened.get('epoch')
        if (not ha_wire.public_key(leader_key) or not isinstance(chain, list)
                or not 0 < len(chain) <= ha_vote.CFG_KEEP + 1 or type(epoch) is not int
                or not 0 <= epoch <= ha_vote.EPOCH_MAX):
            raise WitnessError('The answer from the leader is incomplete')
        if not all(isinstance(c, dict) and ha_vote.pair(c.get('id')) for c in chain):
            raise WitnessError('The voter config from the leader cannot be read')
        first = chain[0]
        if ha_vote.body_error(first.get('body')):
            raise WitnessError('The voter config from the leader cannot be read')
        signer = ha_vote.CfgView(first).records.get(first.get('by')) or {}
        if not signer.get('voter') or not ha_wire.cfg_signed(
                signer.get('public_key'), bytes.fromhex(ha_vote.cfg_digest(first)), first.get('sig')):
            raise WitnessError('The voter config from the leader is not signed by one of its voters')
        for prev, cfg in zip(chain, chain[1:]):
            if ha_vote.link_error(prev, cfg, ha_wire.cfg_signed):
                raise WitnessError('The voter configs from the leader do not chain')
        top = ha_vote.CfgView(chain[-1]).records.get(info['instance_id']) or {}
        if not top.get('voter') or top.get('public_key') != leader_key:
            raise WitnessError('The leader is no voter of the voter config it sent')
        floor = ha_vote.pair(opened.get('floor_cv')) or ha_vote.ZERO
        return chain, epoch, floor

    def leave(self, force=False):
        """Leave the group: the leader takes the witness out of its voter config first,
        where that leaves enough votes. With `force` the state goes here even when the
        leader cannot be told or refuses; the leader then shows the witness as down
        until an admin removes it there."""
        if not self.paired():
            raise WitnessError('This witness is not paired')
        paired = self.st.get('paired') or {}
        why = ''
        try:
            told = self._tell_leader(paired)
        except WitnessError as e:
            told, why = False, str(e)
        if not told and not force:
            raise WitnessError((why or 'The leader did not take the witness out') +
                               ' - leave with --force to drop it here all the same')
        self.let_go('left' if told else f'left by hand, the leader was not told ({why})')
        return told

    def _tell_leader(self, paired):
        signing_key = self.st.get('signing_key')
        if not signing_key or not paired.get('url'):
            raise WitnessError('No leader to tell')
        private = ha_wire.private_key(signing_key)
        target = dict(paired)
        # numbered like every signed call of a member (ha._signed_headers)
        stream = ha_wire.new_stream()
        for seq in (1, 2):
            body = ha_wire.wire_body({'epoch': self.st.get('epoch') or 0})
            headers = ha_wire.signed_headers(private, self.instance_id(), target['instance_id'],
                                             'POST', LEAVE_PATH, body, self.wall(),
                                             nonce=ha_wire.stream_nonce(stream, seq))
            status, data = self.call('POST', target['url'], target.get('fingerprint') or '',
                                     LEAVE_PATH, body, headers)
            if status == 200:
                return True
            hint = (data or {}).get('follow') if status == 409 else None
            rec = self._node().view.records.get((hint or {}).get('instance_id')) or {}
            url = ha_wire.valid_https_url((hint or {}).get('url') or '')
            if not hint or not rec.get('voter') or rec.get('public_key') != hint.get('public_key') or not url:
                raise WitnessError((data or {}).get('error') or f'The leader refused (HTTP {status})')
            # the member it paired with leads no more and names the one that does
            target = {'instance_id': hint['instance_id'], 'url': url,
                      'fingerprint': str(hint.get('fingerprint') or '').upper()}
        raise WitnessError('The leader could not be found')


# --- the server ------------------------------------------------------------------------
#
# MK Oct 2026 (#625) - a small HTTP/1.1 server of its own instead of pywsgi behind a
# greenlet pool: a pool that idle sockets fill takes the third vote offline without a
# single signed byte. Here every connection has a deadline from the moment it is
# accepted, not one that starts over with each byte. A source address holds a few
# connections that showed no signature yet and gives one up for the next; when all of
# them are taken, the source that holds the most gives way. Either way the one that
# moved on the longest ago goes (accepted, first byte, handshake, head): a flood can be
# silent or talk as a member does, but to push a member out it has to fill the place
# within one step of the member's own. A connection whose call carried a good
# signature moves to a reserve of its own, so a member's call is served however many
# sockets others hold open. Of a call that is refused whatever it says, nothing past
# the head is read. With an allow list, other addresses are closed at the accept.

def _ip(address):
    """The address of a peer (an IPv4 one inside IPv6 as IPv4), None when it is none."""
    host = address[0] if isinstance(address, tuple) and address else ''
    try:
        ip = ipaddress.ip_address(str(host).split('%')[0])
    except ValueError:
        return None
    return (ip.ipv4_mapped or ip) if ip.version == 6 else ip


def _source(address, v6_prefix=64):
    """Who a connection counts against: its IPv4 address, or the /64 of an IPv6 one (a
    host holds a /64 as easily as one address)."""
    ip = _ip(address)
    if ip is None:
        return str(address[0] if isinstance(address, tuple) and address else '')
    if ip.version == 6:
        return str(ipaddress.ip_network(f'{ip}/{v6_prefix}', strict=False))
    return str(ip)


def _group(address):
    """Who a connection counts against in the share of all: as _source, but an IPv6 one
    by its /48, what a site is handed (many /64s at once)."""
    return _source(address, _SHARE_V6)


# always let in, whatever the allow list says: `health` asks the port from this host
_LOOPBACK = (ipaddress.ip_network('127.0.0.1/32'), ipaddress.ip_network('::1/128'))


def allow_list(values):
    """The networks an allow list names (CIDR or a single address, given to --allow or
    comma-separated in PEGAPROX_WITNESS_ALLOW), and this host's loopback; None when it
    names none, and every address is let in. Raises WitnessError for an entry that is
    no network."""
    nets = []
    for value in values or ():
        for part in str(value).split(','):
            part = part.strip()
            if not part:
                continue
            try:
                nets.append(ipaddress.ip_network(part, strict=False))
            except ValueError:
                raise WitnessError(f'--allow takes networks like 192.0.2.0/24 or 2001:db8::/48, '
                                   f'not {part!r}')
    return tuple(nets) + _LOOPBACK if nets else None


def _allowed(address, nets):
    ip = _ip(address)
    return ip is not None and any(ip in net for net in nets)


class _Conn:
    """One accepted connection: where it comes from (its source, and the group it counts
    in for the share of all), the greenlet that serves it, its deadline, when it last
    moved on (_Gate.moved), and whether it is inside a call right now (never dropped
    then)."""

    __slots__ = ('source', 'group', 'greenlet', 'busy', 'member', 'last', 'timer')

    def __init__(self, source, greenlet=None, group=None):
        self.source, self.greenlet = source, greenlet
        self.group = source if group is None else group
        self.busy = self.member = False
        self.last = self.timer = None

    def deadline(self, seconds):
        from gevent import Timeout
        if self.timer is not None:
            self.timer.close()
        self.timer = Timeout(seconds)
        self.timer.start()


class _Gate:
    """The connections the server holds. One that showed no signature yet gives way to
    the next from its source once that source holds `per_source`. Once there are
    `anon_max` of them, the group (_group) that holds the most gives one up. Either way
    the one that moved on the longest ago goes: not one that sent nothing as such (a
    member's own is silent on its way in, and a flood that talks first would leave it
    the only one), but the one stuck the longest, silent or not. One whose call
    carried a good signature counts in a reserve of `member_max`, where the one idle
    the longest gives way."""

    def __init__(self, per_source=_PER_SOURCE, anon_max=_ANON_MAX, member_max=_MEMBER_MAX, now=time.monotonic):
        self.per_source, self.anon_max, self.member_max = per_source, anon_max, member_max
        self.now = now
        self.anon = OrderedDict()
        self.members = OrderedDict()

    def moved(self, conn):
        """`conn` got one step further: its first byte, its handshake, a head."""
        conn.last = self.now()

    def admit(self, conn):
        if conn.last is None:
            self.moved(conn)
        same = [c for c in self.anon if c.source == conn.source and not c.busy]
        if len(same) >= self.per_source:
            self._drop(self._victim(same), self.anon)
        if len(self.anon) >= self.anon_max:
            held = Counter(c.group for c in self.anon)
            idle = [c for c in self.anon if not c.busy]
            if idle:
                most = max(held[c.group] for c in idle)
                self._drop(self._victim([c for c in idle if held[c.group] == most]), self.anon)
        self.anon[conn] = True

    @staticmethod
    def _victim(conns):
        """Of `conns`, in the order they came: the one that moved on the longest ago."""
        return min(conns, key=lambda c: c.last)

    def promote(self, conn):
        self.anon.pop(conn, None)
        if not conn.member:
            conn.member = True
            idle = [c for c in self.members if not c.busy]
            if len(self.members) >= self.member_max and idle:
                self._drop(idle[0], self.members)
        self.members[conn] = True
        self.members.move_to_end(conn)

    def release(self, conn):
        self.anon.pop(conn, None)
        self.members.pop(conn, None)

    @staticmethod
    def _drop(conn, where):
        where.pop(conn, None)
        if conn.greenlet is not None:
            conn.greenlet.kill(block=False)


def _parse_head(head):
    """(method, target, version, {lower-case name: value}) of the head of a call, None
    for one this server does not take: no folded lines and no header twice."""
    lines = head.decode('latin-1').split('\r\n')
    parts = lines[0].split(' ')
    if (len(parts) != 3 or not re.fullmatch(r'[A-Z]{3,7}', parts[0])
            or not re.fullmatch(r'/[!-~]*', parts[1]) or parts[2] not in ('HTTP/1.1', 'HTTP/1.0')):
        return None
    fields = {}
    for line in lines[1:]:
        name, colon, value = line.partition(':')
        name = name.strip().lower()
        if (not colon or line[:1] in (' ', '\t') or '\r' in line or '\n' in line or not name
                or name in fields):
            return None
        fields[name] = value.strip()
    return parts[0], parts[1], parts[2], fields


def _read_head(sock, buf):
    """The head of the next call and what came after it; (None, b'') when the peer closed
    before it sent any. Raises ValueError for a head too large or cut off."""
    while b'\r\n\r\n' not in buf:
        if len(buf) > _HEAD_MAX:
            raise ValueError('head too large')
        data = sock.recv(4096)
        if not data:
            if buf:
                raise ValueError('closed inside the head')
            return None, b''
        buf += data
    head, _sep, rest = buf.partition(b'\r\n\r\n')
    if len(head) > _HEAD_MAX:
        raise ValueError('head too large')
    return head, rest


def _read_body(sock, buf, length):
    while len(buf) < length:
        data = sock.recv(min(65536, length - len(buf)))
        if not data:
            raise ValueError('closed inside the body')
        buf += data
    return buf[:length], buf[length:]


def _send(sock, status, payload, keep):
    data = json.dumps(payload, default=list).encode()
    head = (f'HTTP/1.1 {status} {_REASONS.get(status, "Error")}\r\n'
            'Content-Type: application/json\r\n'
            f'Content-Length: {len(data)}\r\n'
            'Cache-Control: no-store\r\n'
            f'Connection: {"keep-alive" if keep else "close"}\r\n\r\n')
    sock.sendall(head.encode() + data)


class _Server:
    """The peer calls of one witness over TLS: connection() serves one accepted socket,
    call by call, until it closes, its deadline runs out or the gate drops it. `allow`,
    the networks of an allow list (allow_list): any other address is closed at once."""

    def __init__(self, w, ctx, gate=None, allow=None):
        self.w, self.ctx = w, ctx
        self.gate = gate or _Gate()
        self.allow = allow

    def connection(self, raw, address):
        from gevent import Timeout, getcurrent
        if self.allow and not _allowed(address, self.allow):
            try:
                raw.close()
            except Exception:
                pass
            return
        conn = _Conn(_source(address), getcurrent(), _group(address))
        remote = address[0] if isinstance(address, tuple) and address else ''
        self.gate.admit(conn)
        tls = None
        try:
            conn.deadline(_ANON_S)
            # each step counts as moving on: the first byte (looked at before the
            # handshake reads it), the handshake, each head
            if not raw.recv(1, socket.MSG_PEEK):
                return
            self.gate.moved(conn)
            tls = self.ctx.wrap_socket(raw, server_side=True, do_handshake_on_connect=False)
            tls.do_handshake()
            self.gate.moved(conn)
            buf = b''
            while True:
                head, buf = _read_head(tls, buf)
                if head is None:
                    return
                self.gate.moved(conn)
                status, payload, keep, buf = self._call(conn, tls, head, buf, remote)
                _send(tls, status, payload, keep)
                if not keep:
                    return
                self.gate.promote(conn)
                conn.deadline(_IDLE_S)
        except Timeout:
            pass
        except Exception as e:
            logging.debug(f"[witness] connection from {remote}: {type(e).__name__}: {e}")
        finally:
            if conn.timer is not None:
                conn.timer.close()
            self.gate.release(conn)
            for sock in (tls, raw):
                if sock is not None:
                    try:
                        sock.close()
                    except Exception:
                        pass

    def _call(self, conn, tls, head, buf, remote):
        """(status, payload, keep the connection, what came after the call) for one call."""
        parsed = _parse_head(head)
        if parsed is None:
            return 400, {'error': 'Bad request'}, False, buf
        method, target, version, fields = parsed
        length = fields.get('content-length', '0')
        if 'transfer-encoding' in fields or not length.isdigit() or len(length) > 9:
            return 400, {'error': 'Bad request'}, False, buf
        if '?' in target:
            # the signature covers the path alone
            return 400, {'error': 'No query string here'}, False, buf
        if int(length) > MAX_BODY:
            return 413, {'error': 'Too large'}, False, buf
        headers = {name: fields[name.lower()] for name in _PEER_HEADERS if name.lower() in fields}
        check = self.w.precheck(method, target, headers)
        if check == 'member':
            self.gate.promote(conn)
            conn.deadline(_BODY_S)
        elif check != 'open':
            status, payload = self._handle(conn, method, target, headers, b'', remote)
            return status, payload, False, buf
        body, buf = _read_body(tls, buf, int(length))
        status, payload = self._handle(conn, method, target, headers, body, remote)
        keep = (status == 200 and version == 'HTTP/1.1'
                and fields.get('connection', '').lower() != 'close')
        return status, payload, keep, buf

    def _handle(self, conn, method, path, headers, body, remote):
        conn.busy = True
        try:
            return self.w.handle(method, path, headers, body, remote)
        except Exception as e:
            logging.error(f"[witness] {method} {path}: {type(e).__name__}: {e}")
            return 500, {'error': 'The witness could not answer'}
        finally:
            conn.busy = False


# --- the commands ----------------------------------------------------------------------

def _drop_root(path):
    """Run as the owner of the state directory when started as root, so nothing in it
    ends up owned by root where the service user cannot read it."""
    if not hasattr(os, 'geteuid') or os.geteuid() != 0:
        return
    if not os.path.isdir(path):
        raise WitnessError(f'{path} does not exist - create it for the user the witness runs as '
                           '(the service does that on its first start), or pass --dir')
    owner = os.stat(path)
    if owner.st_uid == 0:
        return
    os.setgid(owner.st_gid)
    os.setuid(owner.st_uid)


def serve(w, host, port, cert_file, key_file, allow=None):
    """Serve the peer calls until the process is stopped; `allow` as for _Server."""
    import signal
    import ssl
    from gevent import ssl as gevent_ssl
    from gevent.server import StreamServer

    ctx = gevent_ssl.SSLContext(gevent_ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.load_cert_chain(cert_file, key_file)
    logging.getLogger('gevent').setLevel(logging.CRITICAL)
    # a greenlet per connection, no pool to fill: the gate bounds what is held
    server = StreamServer((host, port), _Server(w, ctx, allow=allow).connection)

    def stop(_signum, _frame):
        from gevent import spawn
        spawn(lambda: server.stop(timeout=5))
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    server.serve_forever()


def _wait_for_pairing(path, sleep=time.sleep):
    told = False
    while True:
        try:
            if Witness(path).paired():
                return
        except WitnessError as e:
            raise e
        if not told:
            told = True
            print(f"This witness is not paired yet - run `pegaprox-witness join <code> --url "
                  f"https://<this host>:{DEFAULT_PORT}` on this host. Waiting for it.", flush=True)
        sleep(2)


def cmd_run(args):
    path = check_dir(args.dir)
    allow = allow_list(args.allow or [os.environ.get('PEGAPROX_WITNESS_ALLOW', '')])
    cert_file, key_file, pin = tls_pair(path)
    print(f"Witness state in {path}", flush=True)
    print(f"Certificate fingerprint: {pin or '(signed by a CA - members check its chain)'}", flush=True)
    if allow:
        named = ', '.join(str(net) for net in allow[:-len(_LOOPBACK)])
        print(f"Connections only from {named} (and this host)", flush=True)
    _wait_for_pairing(path)
    fd = lock_dir(path)
    try:
        w = Witness(path)
        w.start()
        print(f"Witness {w.instance_id()} of the group of {(w.st.get('paired') or {}).get('url')} "
              f"on https://{args.host}:{args.port}", flush=True)
        serve(w, args.host, args.port, cert_file, key_file, allow)
    finally:
        unlock_dir(fd)
    return 0


def cmd_join(args):
    path = check_dir(args.dir)
    fd = lock_dir(path)
    try:
        w = Witness(path)
        leader = w.join(args.code, args.url)
        _c, _k, pin = tls_pair(path)
    finally:
        unlock_dir(fd)
    print(f"Paired with {leader} as witness {w.instance_id()}.")
    print(f"Certificate fingerprint: {pin or '(signed by a CA)'}")
    print("Start the witness now: systemctl enable --now pegaprox-witness (or the container).")
    return 0


def cmd_leave(args):
    path = check_dir(args.dir, create=False)
    fd = lock_dir(path)
    try:
        told = Witness(path).leave(force=args.force)
    finally:
        unlock_dir(fd)
    print('Left the group.' if told else 'Left the group here; the leader was not told - remove '
          'the witness on its HA page.')
    return 0


def cmd_fingerprint(args):
    path = check_dir(args.dir)
    _c, _k, pin = tls_pair(path)
    print(pin or '(signed by a CA - nothing to pin)')
    return 0


def cmd_status(args):
    path = check_dir(args.dir, create=False)
    w = Witness(path)
    st = w.st
    out = {'dir': path, 'instance_id': w.instance_id(), 'paired': bool(w.paired()),
           'leader': (st.get('paired') or {}).get('url'), 'own_url': st.get('own_url'),
           'epoch': st.get('epoch'), 'voted_for': st.get('voted_for'),
           'cfg_id': (st.get('cfg') or {}).get('id'), 'mode': ((st.get('cfg') or {}).get('body') or {}).get('mode'),
           'floor_cv': st.get('floor_cv'), 'left': st.get('left'), 'release': RELEASE}
    # what a vote needs: a write into the state directory, file and directory synced
    why = write_check(path)
    out['writable'] = not why
    if why:
        out['write_error'] = why
    print(json.dumps(out, indent=2))
    return 0


def cmd_health(args):
    """0 when this witness is paired, can write its state directory as a vote does, and
    its port answers with its own certificate."""
    import socket
    import ssl
    path = os.path.abspath(args.dir)
    try:
        if not Witness(path).paired():
            return 1
        why = write_check(path)
        if why:
            # it acks renewals that need no write, and would first say no at the vote of
            # a failover: unhealthy from now on, not from then
            print(f'pegaprox-witness: cannot write the state directory {path} ({why}) - no vote '
                  'is given until it can', file=sys.stderr)
            return 1
        cert_file, _k, _pin = tls_pair(path, create=False)
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        with socket.create_connection(('127.0.0.1' if args.host in ('0.0.0.0', '::') else args.host,
                                       args.port), timeout=4) as raw:
            with ctx.wrap_socket(raw) as tls:
                served = tls.getpeercert(binary_form=True)
        with open(cert_file, 'rb') as fh:
            mine = ssl.PEM_cert_to_DER_cert(fh.read().decode())
        return 0 if served == mine else 1
    except Exception:
        return 1


def parser():
    p = argparse.ArgumentParser(prog='pegaprox-witness',
                                description='The witness of a PegaProx group: a third vote for '
                                            'automatic failover, and nothing else.',
                                epilog='Firewall the witness port so that only the networks of '
                                       'the members reach it. Where you cannot, name those '
                                       'networks with --allow (or PEGAPROX_WITNESS_ALLOW).')
    p.add_argument('--dir', default=default_dir(), help='the state directory of this witness')
    p.add_argument('--host', default=os.environ.get('PEGAPROX_WITNESS_HOST', '0.0.0.0'))
    p.add_argument('--port', type=int, default=int(os.environ.get('PEGAPROX_WITNESS_PORT') or DEFAULT_PORT))
    p.add_argument('--allow', action='append', metavar='CIDR',
                   help='take connections only from this network, repeatable; any other address '
                        'is closed at once (default: the comma-separated PEGAPROX_WITNESS_ALLOW, '
                        'else every address). This host is always let in, for health')
    sub = p.add_subparsers(dest='command')
    sub.add_parser('run', help='serve the votes (the default)')
    j = sub.add_parser('join', help='pair with the leader that made the code')
    j.add_argument('code')
    j.add_argument('--url', required=True, help='https://host:port the members reach this witness at')
    lv = sub.add_parser('leave', help='leave the group')
    lv.add_argument('--force', action='store_true', help='drop the pairing here even when the '
                                                          'leader cannot be told')
    sub.add_parser('fingerprint', help='the certificate fingerprint members pin')
    sub.add_parser('status', help='what the state file says')
    sub.add_parser('health', help='exit 0 when the witness can write its state and answers on its port')
    return p


COMMANDS = {'run': cmd_run, 'join': cmd_join, 'leave': cmd_leave, 'fingerprint': cmd_fingerprint,
            'status': cmd_status, 'health': cmd_health}


def main(argv=None):
    args = parser().parse_args(sys.argv[1:] if argv is None else argv)
    command = args.command or 'run'
    if command == 'run':
        try:
            from gevent import monkey
            if not monkey.is_module_patched('socket'):
                monkey.patch_all()
        except ImportError:
            print('gevent is needed to run the witness', file=sys.stderr)
            return 1
    logging.basicConfig(level=logging.INFO, format='%(message)s')
    try:
        _drop_root(os.path.abspath(args.dir))
        return COMMANDS[command](args)
    except WitnessError as e:
        print(f'pegaprox-witness: {e}', file=sys.stderr)
        return EXIT_CONFIG


if __name__ == '__main__':
    sys.exit(main())
