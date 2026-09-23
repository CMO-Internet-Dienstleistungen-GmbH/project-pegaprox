"""Transport to a Hyper-V host: PowerShell Remoting over WinRM, and nothing else.

PegaProx runs on Linux, so a Hyper-V host is reached the way any non-Windows client
reaches one — PSRP over WSMan, on whichever listener and with whichever authentication
provider the host is set up for. This module owns that conversation and hands everything
above it parsed data, so no layer above has to know that pypsrp, requests or WSMan exist.

Two rules shape the interface:

Every script sent from here is read-only unless the caller went through one of the
explicitly named action methods. The inventory path physically cannot change a host;
that is not a convention but the reason `run_json` refuses a script carrying a mutating
verb.

Credentials live in exactly one place. They are never logged, never interpolated into a
script, never passed as a command argument, and never returned in any structure that
leaves this module.
"""

from __future__ import annotations

import json
import logging
import re
import threading
from dataclasses import dataclass, field
from typing import Any

from pegaprox.core import hyperv_tasks
from pegaprox.core.hyperv_errors import (
    HyperVError, KIND_BUSY, KIND_MISSING_FEATURE, KIND_REFUSED,
)

logger = logging.getLogger(__name__)

# The two WinRM listeners. Which one a host offers is the host's configuration, and the
# product follows it: `winrm quickconfig` creates only the HTTP listener, so that is the
# default, and an HTTPS listener exists only where somebody set one up with a certificate.
DEFAULT_WINRM_HTTP_PORT = 5985
DEFAULT_WINRM_HTTPS_PORT = 5986

# The authentication providers pypsrp accepts for a username/password login. `negotiate`
# picks Kerberos where the client has a ticket and falls back to NTLM, which is what a
# standalone host without a domain join ends up with. Kerberos needs the optional gssapi
# libraries; absent, pypsrp reports that as a client dependency rather than an auth error.
SUPPORTED_AUTH_METHODS = ('negotiate', 'ntlm', 'basic', 'kerberos')
DEFAULT_AUTH_METHOD = 'negotiate'

# What pypsrp's `encryption` argument may be set to. Over TLS the transport carries the
# protection and `auto` adds nothing; over HTTP `auto` seals the SOAP body with the NTLM or
# Kerberos session key, and `never` sends it in clear. Basic authentication has no session
# key to seal with, so basic over HTTP is only possible with `never`.
_ENCRYPTION_AUTO = 'auto'
_ENCRYPTION_NEVER = 'never'

# How long the host may spend on one cmdlet, and how long this side waits for the answer.
# The read timeout must exceed the operation timeout, or this side gives up while the host
# is still working and reports a timeout that the host never saw. _ConnectionSettings
# enforces that rather than trusting a caller to keep them in step.
DEFAULT_OPERATION_TIMEOUT_SECONDS = 180
_READ_TIMEOUT_HEADROOM_SECONDS = 30

# How many sessions PegaProx keeps to one host at most. Each is its own WSMan shell on the
# host, so this is also the most shells a host ever sees from PegaProx. Four lets a wizard,
# an inventory refresh and a running migration proceed side by side; eight is well under
# WinRM's default MaxShellsPerUser of 30 and keeps the load on a customer's hypervisor
# bounded. See docs/adr/0006.
DEFAULT_MAX_SESSIONS = 4
MIN_SESSIONS = 1
MAX_SESSIONS = 8

# Verbs that change a host. A script carrying one of these does not go out over the
# inventory path, whatever the caller believed it was sending.
_MUTATING_VERB_PATTERN = re.compile(
    r'\b(?:New|Set|Remove|Add|Start|Stop|Restart|Suspend|Resume|Rename|Move|Import|Export|'
    r'Checkpoint|Convert|Optimize|Merge|Mount|Dismount|Enable|Disable|Repair|Reset|Save)-',
    re.IGNORECASE,
)


@dataclass(frozen=True)
class HyperVConnection:
    """Where a host is and how to authenticate to it.

    `password` is held here and nowhere else. The dataclass deliberately overrides its
    repr: a connection object reaching a log line or a traceback must not take the
    password with it, and dataclasses print every field by default.
    """

    host: str
    username: str
    password: str = field(repr=False)
    # None means "the default listener for the chosen transport"; resolved once below so
    # every reader sees a number and nobody repeats the transport-to-port rule.
    port: int | None = None
    use_ssl: bool = False
    auth: str = DEFAULT_AUTH_METHOD
    encrypt_messages: bool = True
    verify_certificate: bool = True
    operation_timeout: int = DEFAULT_OPERATION_TIMEOUT_SECONDS

    def __post_init__(self) -> None:
        if self.auth not in SUPPORTED_AUTH_METHODS:
            raise ValueError(
                f'Unsupported WinRM authentication method {self.auth!r}; '
                f'expected one of {", ".join(SUPPORTED_AUTH_METHODS)}')
        if self.port is None:
            # The dataclass is frozen, so the resolved default is written the one way a
            # frozen dataclass permits.
            object.__setattr__(self, 'port', default_winrm_port(self.use_ssl))

    def __repr__(self) -> str:
        # Explicit rather than relying on field(repr=False) alone, so the intent survives
        # somebody adding a second secret field later.
        return (f'HyperVConnection(host={self.host!r}, username={self.username!r}, '
                f'port={self.port}, use_ssl={self.use_ssl}, auth={self.auth!r}, '
                f'encrypt_messages={self.encrypt_messages}, '
                f'verify_certificate={self.verify_certificate})')

    @property
    def wsman_encryption(self) -> str:
        """The `encryption` value pypsrp needs for this connection."""
        return wsman_encryption_for(self.use_ssl, self.auth, self.encrypt_messages)

    @property
    def read_timeout(self) -> int:
        """Always longer than the operation timeout, by construction."""
        return self.operation_timeout + _READ_TIMEOUT_HEADROOM_SECONDS

    def redactions(self) -> list[str]:
        """Values that must not appear in any message this connection produces.

        The host and account names are included because the fork's repository is public
        and an error message is quoted into issues; the password because it is a secret.
        """
        return [v for v in (self.password, self.host, self.username) if v and len(v) > 1]


def default_winrm_port(use_ssl: bool) -> int:
    """The listener port a transport uses unless the operator says otherwise."""
    return DEFAULT_WINRM_HTTPS_PORT if use_ssl else DEFAULT_WINRM_HTTP_PORT


def wsman_encryption_for(use_ssl: bool, auth: str, encrypt_messages: bool) -> str:
    """Translate the operator's choice into pypsrp's `encryption` argument.

    Over TLS the answer is always `auto`: the transport protects the body and pypsrp
    would reject `always` for basic auth anyway. Over HTTP the operator decides, except
    that basic authentication cannot seal anything and therefore forces `never` — pypsrp
    raises otherwise, and a host that only offers basic over HTTP is a host somebody
    chose to run that way, not an error for this side to report.
    """
    if use_ssl:
        return _ENCRYPTION_AUTO
    if auth == 'basic' or not encrypt_messages:
        return _ENCRYPTION_NEVER
    return _ENCRYPTION_AUTO


def parse_max_sessions(value: Any) -> int:
    """The submitted number of sessions, or ValueError naming the accepted range.

    None and an empty string mean "not set" and give the default, so a host saved before
    the setting existed and a form that leaves the field blank both get four.
    """
    if value is None or value == '':
        return DEFAULT_MAX_SESSIONS
    not_whole = 'The number of parallel sessions must be a whole number.'
    # int() would quietly turn True into 1 and 4.5 into 4; neither is what was meant.
    if isinstance(value, bool) or (isinstance(value, float) and not value.is_integer()):
        raise ValueError(not_whole)
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(not_whole) from exc
    if not MIN_SESSIONS <= number <= MAX_SESSIONS:
        raise ValueError(f'The number of parallel sessions must be between {MIN_SESSIONS} '
                         f'and {MAX_SESSIONS}.')
    return number


def redact(text: str, secrets: list[str]) -> str:
    """Remove known secret and identifying values from a message."""
    for secret in secrets:
        text = text.replace(secret, '***')
    return text


class HyperVPowerShellClient:
    """The contract every transport implementation and every test double satisfies.

    Kept deliberately narrow: one way to run a read-only script, one way to run an
    explicitly requested action. Everything the migration needs is expressed as a script
    by the layer above, which is what makes that layer testable without a host.
    """

    def run_json(self, script: str, **parameters: Any) -> Any:
        """Run a read-only script and return its parsed JSON output.

        Values go to the script's param() block as parameters, never as text spliced into
        the script. A VM named `x'; Remove-VM -Name *` therefore arrives as a string of 23
        characters and not as two statements.
        """
        raise NotImplementedError

    def run_action(self, script: str, description: str, **parameters: Any) -> Any:
        """Run a script that changes the host. `description` is what the audit log records."""
        raise NotImplementedError

    def close(self) -> None:
        """Release whatever the transport holds. Safe to call more than once."""


def assert_read_only(script: str) -> None:
    """Refuse a script that would change the host.

    This guards the inventory path against a mutating cmdlet arriving by copy-paste or by
    a later edit to a script constant. It is a coarse check on purpose: a false positive
    costs a rename, a false negative costs somebody's VM.
    """
    match = _MUTATING_VERB_PATTERN.search(script)
    if match:
        raise HyperVError(
            f'Refusing to run a script containing the mutating cmdlet verb '
            f'"{match.group(0)}" on the read-only path.',
            kind=KIND_REFUSED,
        )


class PsrpHyperVClient(HyperVPowerShellClient):
    """PowerShell Remoting over WinRM, via pypsrp.

    HTTP or HTTPS, and which authentication provider, is the connection's business: the
    host's listener configuration is a given, and this client follows it rather than
    demanding one.

    One runspace pool is opened per client and reused, because opening one costs a full
    WSMan handshake and an inventory walk runs several scripts in a row. The pool is
    opened lazily so constructing a client for a host that is down costs nothing until
    somebody actually asks it something.

    That one pool is also why every call through this client is serialised. A client is
    held for the life of the process, so two requests arriving at once share the pool —
    and PSRP is one stateful conversation over one shell, not a request/response pair.
    Interleaved, neither caller fails outright: one of them waits for a reply the other
    has already taken, until the read timeout. A web UI that polls several endpoints at
    the same time hits that on every page load.
    """

    def __init__(self, connection: HyperVConnection):
        self._connection = connection
        self._wsman = None
        self._pool = None
        # Reentrant, because close() takes the same lock and a caller may reach it from
        # inside a with-block that is already holding it.
        self._lock = threading.RLock()

    def _ensure_pool(self):
        """Open the runspace pool if it is not open yet, classifying any failure."""
        if self._pool is not None:
            return self._pool
        try:
            from pypsrp.powershell import RunspacePool
            from pypsrp.wsman import WSMan
        except ImportError as exc:
            raise HyperVError.from_exception(exc, 'pypsrp is not installed') from exc

        conn = self._connection
        try:
            self._wsman = WSMan(
                conn.host,
                port=conn.port,
                username=conn.username,
                password=conn.password,
                ssl=conn.use_ssl,
                auth=conn.auth,
                encryption=conn.wsman_encryption,
                cert_validation=conn.verify_certificate,
                operation_timeout=conn.operation_timeout,
                read_timeout=conn.read_timeout,
            )
            self._pool = RunspacePool(self._wsman)
            self._pool.open()
        except Exception as exc:
            self._pool = None
            self._wsman = None
            raise self._classified(exc, 'opening a PowerShell session') from exc
        return self._pool

    def _classified(self, exc: Exception, context: str) -> HyperVError:
        """Turn a transport exception into a HyperVError with every secret removed."""
        err = HyperVError.from_exception(exc, context)
        secrets = self._connection.redactions()
        return HyperVError(redact(err.message, secrets), kind=err.kind,
                           detail=redact(err.detail, secrets))

    def _invoke(self, script: str, context: str, parameters: dict | None = None) -> Any:
        with self._lock:
            # The wait for the session ends here, and that is the moment the task bar has
            # to see: everything before it was queueing behind somebody else's call.
            hyperv_tasks.mark_running()
            # A shell the host has already closed is not a failed request, it is a
            # connection that has to be reopened — and the caller cannot tell the two
            # apart. WinRM ends an idle shell on its own schedule, so the first call after
            # that always finds a pipe with nothing on the other end. Measured as a broken
            # pipe on the VM list, which reached the browser as an empty table on a host
            # that was up and answering a second later.
            reused = self._pool is not None
            try:
                return self._invoke_locked(script, context, parameters)
            except HyperVError:
                # Only a transport failure drops the pool, and only a pool that already
                # existed can have gone stale. A freshly opened one that failed means the
                # host is genuinely not answering, and trying again would just double the
                # wait before saying so.
                if not reused or self._pool is not None:
                    raise
            return self._invoke_locked(script, context, parameters)

    def _invoke_locked(self, script: str, context: str, parameters: dict | None = None) -> Any:
        # The pool is opened first because _ensure_pool is where a missing pypsrp becomes a
        # classified error. Importing before it would let a bare ImportError escape this
        # layer, and the caller would learn nothing about what to fix.
        pool = self._ensure_pool()
        try:
            from pypsrp.powershell import PowerShell
            powershell = PowerShell(pool)
            powershell.add_script(script)
            # Bound, not spliced. The script's param() block receives these the way it
            # would from a caller on the host, which is why its [Parameter(Mandatory)]
            # declarations keep working: a value assigned after the block would arrive
            # too late, because PowerShell demands a mandatory parameter on entry.
            for name, value in (parameters or {}).items():
                powershell.add_parameter(name, value)
            output = powershell.invoke()
            if powershell.had_errors:
                message = '; '.join(str(err) for err in powershell.streams.error)
                raise HyperVError(redact(message, self._connection.redactions()),
                                  kind=_kind_for_script_error(message))
        except HyperVError:
            raise
        except Exception as exc:
            # The failure was in the transport, not in the script, so the shell's state is
            # no longer known: a half-finished exchange left on it makes every later call
            # wait for a reply that will not come. Drop it and let the next caller pay for
            # a fresh handshake.
            self._discard_pool()
            raise self._classified(exc, context) from exc

        if not output:
            return None
        # PSRP may split one long string across several output records, so a large
        # inventory must be joined before parsing or it fails as malformed JSON.
        raw = ''.join(str(part) for part in output)
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise HyperVError(
                f'The host answered with something that is not JSON ({exc}).',
                kind='unknown',
                detail=redact(raw[:500], self._connection.redactions()),
            ) from exc

    def run_json(self, script: str, **parameters: Any) -> Any:
        assert_read_only(script)
        return self._invoke(script, 'reading from the host', parameters)

    def run_action(self, script: str, description: str, **parameters: Any) -> Any:
        # No read-only assertion here by design: this is the path a caller takes when it
        # means to change something, and `description` is what gets audited.
        logger.info('Hyper-V action on %s: %s', self._connection.host, description)
        return self._invoke(script, description, parameters)

    def _discard_pool(self) -> None:
        """Throw the pool away without letting a failure to close it mask the real error."""
        self.close()

    def close(self) -> None:
        with self._lock:
            for resource in (self._pool, self._wsman):
                try:
                    if resource is not None:
                        resource.close()
                except Exception:  # noqa: BLE001 - closing must not mask the real failure
                    logger.debug('Ignoring error while closing a Hyper-V transport resource',
                                 exc_info=True)
            self._pool = None
            self._wsman = None

    def __enter__(self) -> 'PsrpHyperVClient':
        return self

    def __exit__(self, *_exc_info) -> None:
        self.close()


def _kind_for_script_error(message: str) -> str:
    """Classify an error the host raised while running a script, rather than a transport one."""
    from pegaprox.core.hyperv_errors import classify
    kind = classify('PowerShellScriptError', message)
    # A cmdlet that does not exist is the host missing the Hyper-V role, which the generic
    # classifier only recognises from the message.
    if kind == 'unknown' and 'not recognized' in message.lower():
        return KIND_MISSING_FEATURE
    return kind


class PooledHyperVClient(HyperVPowerShellClient):
    """Up to N independent sessions to one host, handed out one caller at a time each.

    A single `PsrpHyperVClient` serialises every call to its host, which put all users of a
    host into one queue: four preflights on four VMs took as long as the four in a row.
    This spreads calls over several sessions instead.

    Several sessions, not one session with several runspaces. A runspace pool with
    `max_runspaces > 1` would share one WSMan connection between callers, and pypsrp makes
    no promise that one connection can serve several greenlets at once — the failure
    `PsrpHyperVClient` describes, where a caller waits for a reply another caller took,
    is exactly what that sharing risks. Each session here keeps its own connection, its
    own lock and its own stale-shell retry, unchanged. docs/adr/0006 has the alternatives.

    Sessions are created on first need and kept for reuse, so a host that only ever sees
    one caller at a time only ever has one shell open. A transport failure on one session
    drops that session's shell and nothing else; the others carry on.
    """

    def __init__(self, connection: HyperVConnection, max_sessions: int = DEFAULT_MAX_SESSIONS,
                 wait_seconds: float | None = None, session_factory=None):
        self._connection = connection
        self._max_sessions = parse_max_sessions(max_sessions)
        # A caller waits at most as long as one call may take before its own read timeout
        # fires. Waiting longer than that means every session is held by something that is
        # itself overdue, and "the host is busy" is then the honest answer.
        self._wait_seconds = connection.read_timeout if wait_seconds is None else wait_seconds
        self._session_factory = session_factory or PsrpHyperVClient
        self._slots = threading.BoundedSemaphore(self._max_sessions)
        self._lock = threading.Lock()
        self._idle: list[HyperVPowerShellClient] = []
        self._sessions: list[HyperVPowerShellClient] = []

    @property
    def max_sessions(self) -> int:
        return self._max_sessions

    def _borrow(self) -> HyperVPowerShellClient:
        if not self._slots.acquire(timeout=self._wait_seconds):
            raise HyperVError(
                f'All {self._max_sessions} sessions to this host stayed in use for '
                f'{self._wait_seconds:.0f} s.',
                kind=KIND_BUSY)
        try:
            with self._lock:
                if self._idle:
                    return self._idle.pop()
                session = self._session_factory(self._connection)
                self._sessions.append(session)
                return session
        except BaseException:
            self._slots.release()
            raise

    def _give_back(self, session: HyperVPowerShellClient) -> None:
        with self._lock:
            self._idle.append(session)
        self._slots.release()

    def run_json(self, script: str, **parameters: Any) -> Any:
        # Refused before a session is taken: a script that may not go out should not
        # occupy one of N slots while it is being refused.
        assert_read_only(script)
        session = self._borrow()
        try:
            return session.run_json(script, **parameters)
        finally:
            self._give_back(session)

    def run_action(self, script: str, description: str, **parameters: Any) -> Any:
        session = self._borrow()
        try:
            return session.run_action(script, description, **parameters)
        finally:
            self._give_back(session)

    def close(self) -> None:
        """Close every session this pool opened. Each stays usable and reopens on demand,
        like a single client after `close()`."""
        with self._lock:
            sessions = list(self._sessions)
        for session in sessions:
            try:
                session.close()
            except Exception:  # noqa: BLE001 - one session failing to close must not keep the rest open
                logger.debug('Ignoring error while closing a pooled Hyper-V session',
                             exc_info=True)
