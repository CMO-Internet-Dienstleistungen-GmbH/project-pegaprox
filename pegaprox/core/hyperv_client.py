"""Transport to a Hyper-V host: PowerShell Remoting over WinRM, and nothing else.

PegaProx runs on Linux, so a Hyper-V host is reached the way any non-Windows client
reaches one — PSRP over WSMan/HTTPS, authenticated with NTLM. This module owns that
conversation and hands everything above it parsed data, so no layer above has to know
that pypsrp, requests or WSMan exist.

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

from pegaprox.core.hyperv_errors import HyperVError, KIND_MISSING_FEATURE, KIND_REFUSED

logger = logging.getLogger(__name__)

# The WinRM HTTPS listener. The plaintext listener on 5985 is never used: an NTLM exchange
# and everything after it would be readable on the wire.
DEFAULT_WINRM_HTTPS_PORT = 5986

# How long the host may spend on one cmdlet, and how long this side waits for the answer.
# The read timeout must exceed the operation timeout, or this side gives up while the host
# is still working and reports a timeout that the host never saw. _ConnectionSettings
# enforces that rather than trusting a caller to keep them in step.
DEFAULT_OPERATION_TIMEOUT_SECONDS = 180
_READ_TIMEOUT_HEADROOM_SECONDS = 30

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
    port: int = DEFAULT_WINRM_HTTPS_PORT
    verify_certificate: bool = True
    operation_timeout: int = DEFAULT_OPERATION_TIMEOUT_SECONDS

    def __repr__(self) -> str:
        # Explicit rather than relying on field(repr=False) alone, so the intent survives
        # somebody adding a second secret field later.
        return (f'HyperVConnection(host={self.host!r}, username={self.username!r}, '
                f'port={self.port}, verify_certificate={self.verify_certificate})')

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
    """PowerShell Remoting over WinRM HTTPS, via pypsrp.

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
                ssl=True,
                auth='ntlm',
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
